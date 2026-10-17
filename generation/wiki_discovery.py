import json
import logging
import os

from .claude_client import call_claude_json, call_claude_json_with_tools
from .theme_interview import deck_brief
from .wiki_images import WikiImageError
from .wiki_sites import normalize_domain, probe_wiki, page_hits

logger = logging.getLogger(__name__)
DISCOVERY_PROMPT = '''Propón entre 2 y 4 wikis comunitarias (MediaWiki) donde estén documentados los personajes, lugares y objetos de esta franquicia. Prioriza wikis activas fuera de Fandom (por ejemplo, wiki.gg) porque permiten descargar imágenes; incluye también la de Fandom si existe. Para cada una, da el dominio exacto, sin https ni rutas. Además, da hasta 6 títulos de página en inglés que deberían existir en esa wiki (personajes principales del tema). Responde solo JSON: {"candidates": [{"domain": "...", "why": "máx. 80 caracteres"}], "sample_titles": ["..."]}.'''


def _validate_candidates(data):
    candidates, titles = data.get('candidates'), data.get('sample_titles')
    if not isinstance(candidates, list) or not 2 <= len(candidates) <= 4:
        raise ValueError('Se esperaban entre 2 y 4 wikis candidatas.')
    if not isinstance(titles, list) or not 1 <= len(titles) <= 6 or any(not isinstance(t, str) or not t.strip() or len(t) > 300 or '|' in t for t in titles):
        raise ValueError('Títulos de muestra inválidos.')
    cleaned = []
    for item in candidates:
        if not isinstance(item, dict) or not isinstance(item.get('why'), str):
            raise ValueError('Candidata inválida.')
        cleaned.append(dict(domain=normalize_domain(item.get('domain')), why=item['why'][:80]))
    return dict(candidates=cleaned, sample_titles=[title.strip() for title in titles])


def discover_wikis(deck):
    elements = list(deck.theme_assignments.order_by('id').values_list('element', flat=True)[:10])
    if not elements:
        elements = list(deck.cards.filter(card__type_line__icontains='legendary').values_list('card__name', flat=True).distinct()[:10])
    messages = [{'role': 'user', 'content': json.dumps(dict(theme=deck.theme, brief=deck_brief(deck), elements=elements), ensure_ascii=False)}]
    params = dict(system=DISCOVERY_PROMPT, messages=messages, max_tokens=800, validate=_validate_candidates)
    proposal = call_claude_json(**params)
    candidates = {}

    def check(proposal):
        for item in proposal['candidates']:
            domain = item['domain']
            if domain in candidates:
                continue
            site = probe_wiki(domain)
            hits = dict(found=0, checked=0)
            error = site['error']
            if site['ok']:
                try:
                    hits = page_hits(domain, proposal['sample_titles'])
                except WikiImageError as exc:
                    error = str(exc)[:200]
            candidates[domain] = dict(domain=domain, why=item['why'], ok=site['ok'], sitename=site['sitename'],
                                      fandom=site['fandom'], found=hits['found'], checked=hits['checked'], error=error)

    check(proposal)
    if not any(item['ok'] and item['found'] >= 1 for item in candidates.values()) and os.getenv('WIKI_DISCOVERY_WEB_SEARCH', 'true').lower() in ('true', '1', 'yes'):
        proposal = call_claude_json_with_tools(**{**params, 'system': DISCOVERY_PROMPT + '\nBusca en la web wikis reales y devuelve el mismo JSON.'},
            tools=[{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 3}])
        check(proposal)
    result = sorted(candidates.values(), key=lambda item: (not item['ok'], -item['found'], item['fandom']))
    if result and result[0]['ok'] and result[0]['checked'] and result[0]['found'] / result[0]['checked'] >= 0.5:
        result[0]['recommended'] = True
    deck.wiki_candidates = result
    deck.save(update_fields=['wiki_candidates'])
    return result
