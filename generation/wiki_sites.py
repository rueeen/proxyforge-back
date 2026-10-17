from collections import defaultdict
from datetime import timedelta

from django.utils import timezone

from .models import WikiSite
from .wiki_images import _query, _is_fandom, normalize_wiki_reference, reference_page_url, WikiImageError


def normalize_domain(domain):
    return normalize_wiki_reference(domain, 'Main Page')[0]


def probe_wiki(domain: str) -> dict:
    try:
        domain = normalize_domain(domain)
    except ValueError as exc:
        return dict(domain=domain, ok=False, sitename='', lang='', fandom=False, main_page_url='', error=str(exc)[:200])
    fields = ('domain', 'ok', 'sitename', 'lang', 'fandom', 'main_page_url', 'error')
    cached = WikiSite.objects.filter(domain=domain, checked_at__gte=timezone.now() - timedelta(days=7)).first()
    if cached:
        return {field: getattr(cached, field) for field in fields}
    result = dict(domain=domain, ok=False, sitename='', lang='', fandom=_is_fandom(domain, ''), main_page_url='', error='')
    try:
        data = _query(domain, action='query', meta='siteinfo', siprop='general')
        general = data.get('query', {}).get('general')
        if not isinstance(general, dict) or not general:
            raise WikiImageError('La wiki no devolvió información del sitio.')
        result.update(ok=True, sitename=str(general.get('sitename', ''))[:300], lang=str(general.get('lang', ''))[:30],
                      main_page_url=reference_page_url(domain, general.get('mainpage') or 'Main Page'))
    except WikiImageError as exc:
        result['error'] = str(exc)[:200]
    WikiSite.objects.update_or_create(domain=domain, defaults={**{key: value for key, value in result.items() if key != 'domain'},
                                                             'checked_at': timezone.now()})
    return result


def _resolve_pages(domain, titles):
    """Resolve one batch, preserving the requested title through normalization and redirects."""
    data = _query(domain, action='query', titles='|'.join(titles), redirects=1)
    query = data.get('query', {})
    aliases = {item['from']: item['to'] for key in ('normalized', 'redirects', 'converted')
               for item in query.get(key, [])}
    pages = {item.get('title'): item for item in query.get('pages', [])}
    resolved = {}
    for title in titles:
        actual, seen = title, set()
        while actual in aliases and actual not in seen:
            seen.add(actual)
            actual = aliases[actual]
        entry = pages.get(actual) or pages.get(actual.replace('_', ' '))
        resolved[title] = entry['title'] if entry is not None and 'missing' not in entry and 'invalid' not in entry else None
    return resolved


def _search_page(domain, title):
    data = _query(domain, action='query', list='search', srsearch=title, srlimit=1, srnamespace=0)
    found = data.get('query', {}).get('search') or []
    return found[0]['title'] if found else None


def page_hits(domain: str, titles: list[str]) -> dict:
    domain = normalize_domain(domain)
    titles = list(dict.fromkeys(titles[:10]))
    if not titles:
        return dict(checked=0, found=0, resolved={})
    resolved = _resolve_pages(domain, titles)
    for title, actual in list(resolved.items()):
        if actual is None:
            resolved[title] = _search_page(domain, title)
    return dict(checked=len(titles), found=sum(value is not None for value in resolved.values()), resolved=resolved)


def verify_assignment_pages(deck, assignments):
    groups = defaultdict(list)
    for assignment in assignments:
        if assignment.reference_wiki and assignment.reference_page:
            groups[assignment.reference_wiki].append(assignment)
    for domain, items in groups.items():
        titles = list(dict.fromkeys(item.reference_page for item in items))
        resolved, statuses = {}, {}
        for offset in range(0, len(titles), 50):
            batch = titles[offset:offset + 50]
            try:
                found = _resolve_pages(domain, batch)
                for title, actual in found.items():
                    status = 'ok'
                    if actual is None:
                        actual = _search_page(domain, title)
                        status = 'search' if actual else 'missing'
                    resolved[title], statuses[title] = actual, status
            except WikiImageError:
                for title in batch:
                    resolved[title], statuses[title] = None, 'missing'
        for assignment in items:
            title = assignment.reference_page
            assignment.reference_page_status = statuses[title]
            if resolved[title]:
                assignment.reference_page = resolved[title]
            assignment.save(update_fields=['reference_page', 'reference_page_status'])
