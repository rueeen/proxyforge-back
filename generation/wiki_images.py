import re
import math
import threading
import time
from urllib.parse import urlparse, parse_qs, unquote, quote

import requests

from .references import MAX_REFERENCE_BYTES

WIKI_PATTERN = r'^[a-z0-9.-]+\.[a-z]{2,}$'
HEADERS = {'User-Agent': 'ProxyForge/1.0 (herramienta personal local)', 'Accept': 'application/json'}
API_PATHS = {}
LAST_RESULTS = {}
_last_requests = {}
_rate_lock = threading.Lock()


class WikiImageNotFound(Exception):
    pass


class WikiImageError(Exception):
    pass


class WikiDownloadBlocked(WikiImageError):
    pass


def reference_page_url(wiki, page):
    if not wiki or not page:
        return ''
    return f'https://{wiki}/wiki/' + quote(page.replace(' ', '_'), safe="()_,'!:")


def valid_wiki(wiki):
    return isinstance(wiki, str) and len(wiki) <= 200 and re.fullmatch(WIKI_PATTERN, wiki) is not None


def normalize_wiki_reference(wiki, page):
    if not isinstance(wiki, str) or not isinstance(page, str):
        raise ValueError('Dominio de wiki inválido. Ejemplo: warcraft.wiki.gg')
    domain = wiki.strip()
    title = page.strip()
    # A URL in the page field takes precedence over one in the domain field.
    for value in (wiki.strip(), page.strip()):
        if value.lower().startswith(('http://', 'https://', '//')):
            try:
                parsed = urlparse(value)
                domain = parsed.hostname or ''
            except ValueError as exc:
                raise ValueError('Dominio de wiki inválido. Ejemplo: warcraft.wiki.gg') from exc
            candidate = parse_qs(parsed.query).get('title', [''])[0]
            if not candidate and '/wiki/' in parsed.path:
                candidate = parsed.path.split('/wiki/', 1)[1].rstrip('/')
            if candidate:
                title = candidate
    domain = domain.lower().split('/', 1)[0]
    if domain.startswith('www.'):
        domain = domain[4:]
    if not valid_wiki(domain):
        raise ValueError('Dominio de wiki inválido. Ejemplo: warcraft.wiki.gg')
    title = ' '.join(unquote(title).split('#', 1)[0].replace('_', ' ').split())
    if not title or len(title) > 300:
        raise ValueError('Página de wiki inválida: usa un título de hasta 300 caracteres.')
    return domain, title


def _get(url, timeout, accept=None, **kwargs):
    host = urlparse(url).hostname
    with _rate_lock:
        wait = 0.3 - (time.monotonic() - _last_requests.get(host, float('-inf')))
        if wait > 0:
            time.sleep(wait)
        _last_requests[host] = time.monotonic()
    headers = {**HEADERS, 'Accept': accept} if accept else HEADERS
    try:
        response = requests.get(url, headers=headers, timeout=timeout, **kwargs)
    except requests.RequestException as exc:
        raise WikiImageError(f'Error de red: {exc}'[:200]) from exc
    if response.status_code >= 500:
        raise WikiImageError(f'La wiki respondió HTTP {response.status_code}')
    return response


def _query(wiki, **params):
    paths = list(dict.fromkeys([API_PATHS.get(wiki), '/api.php', '/w/api.php']))
    for path in paths:
        if not path:
            continue
        response = _get(f'https://{wiki}{path}', 15, params={**params, 'format': 'json', 'formatversion': 2})
        if response.status_code == 404:
            continue
        try:
            data = response.json()
        except ValueError:
            continue
        if not 200 <= response.status_code < 300:
            raise WikiImageError(f'La wiki respondió HTTP {response.status_code}')
        if not isinstance(data, dict):
            continue
        API_PATHS[wiki] = path
        if data.get('error'):
            raise WikiImageError(f'Error de MediaWiki: {data["error"]}'[:200])
        return data
    API_PATHS.pop(wiki, None)
    raise WikiImageError('La wiki no expone api.php')


def _principal(wiki, page):
    data = _query(wiki, action='query', prop='pageimages', piprop='original|thumbnail',
                  pithumbsize=1536, titles=page, redirects=1)
    pages = data.get('query', {}).get('pages') or []
    info = pages[0] if pages else {'missing': True}
    original = (info.get('original') or {}).get('source')
    thumbnail = (info.get('thumbnail') or {}).get('source')
    return info, original, thumbnail


def _download_message(message, url):
    suffix = ' — ' + url[:120]
    return message[:300 - len(suffix)] + suffix


def _is_fandom(wiki, url):
    wiki_host = wiki.lower()
    image_host = (urlparse(url).hostname or '').lower()
    return (wiki_host == 'fandom.com' or wiki_host.endswith('.fandom.com')
            or image_host == 'wikia.nocookie.net' or image_host.endswith('.wikia.nocookie.net'))


def _download_one(url, wiki):
    try:
        response = _get(url, 20, accept='image/jpeg,image/png,image/webp;q=0.9,*/*;q=0.5')
        if response.status_code == 429:
            try:
                delay = float(response.headers.get('Retry-After', ''))
            except (ValueError, TypeError):
                delay = None
            if delay is not None and math.isfinite(delay) and 0 <= delay <= 5:
                time.sleep(delay)
                response = _get(url, 20, accept='image/jpeg,image/png,image/webp;q=0.9,*/*;q=0.5')
    except WikiImageError as exc:
        raise WikiImageError(_download_message(str(exc), url)) from exc
    if response.status_code in (401, 403, 429):
        if _is_fandom(wiki, url):
            message = ('Fandom bloquea descargas automáticas. Abre la página en la wiki, '
                       'copia la imagen (clic derecho → Copiar imagen) y pégala aquí con Ctrl+V.')
        else:
            host = wiki or urlparse(url).hostname or 'La wiki'
            message = f'{host} bloqueó la descarga (HTTP {response.status_code}). Prueba otra wiki o agrega la imagen manualmente.'
        raise WikiDownloadBlocked(_download_message(message, url))
    if not 200 <= response.status_code < 300:
        raise WikiImageError(_download_message(f'La descarga respondió HTTP {response.status_code}', url))
    mime = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    if mime not in ('image/jpeg', 'image/png', 'image/webp'):
        raise WikiImageNotFound(_download_message('El archivo no es una imagen JPEG, PNG o WEBP', url))
    try:
        length = int(response.headers.get('Content-Length', '0'))
    except ValueError:
        length = 0
    if length > MAX_REFERENCE_BYTES:
        raise WikiImageError(_download_message('La imagen supera 10 MB', url))
    raw = response.content
    if len(raw) > MAX_REFERENCE_BYTES:
        raise WikiImageError(_download_message('La imagen supera 10 MB', url))
    return raw


def _download(url, wiki='', fallback_url=None):
    try:
        return _download_one(url, wiki), url
    except WikiDownloadBlocked:
        if _is_fandom(wiki, url) or not fallback_url or fallback_url == url:
            raise
        return _download_one(fallback_url, wiki), fallback_url


def find_wiki_image(wiki, page):
    try:
        wiki, page = normalize_wiki_reference(wiki, page)
    except ValueError as exc:
        raise WikiImageError(str(exc)) from exc
    LAST_RESULTS.pop((wiki, page), None)
    searched = False
    info, original, thumbnail = _principal(wiki, page)
    url = original or thumbnail
    fallback_url = thumbnail if original else None
    step = 1
    title = info.get('title') or page
    if info.get('missing'):
        search = _query(wiki, action='query', list='search', srsearch=page, srlimit=1, srnamespace=0)
        results = search.get('query', {}).get('search') or []
        if not results:
            raise WikiImageNotFound(f'No existe la página "{page}" en {wiki} y la búsqueda no encontró resultados')
        searched = True
        title = results[0]['title']
        info, original, thumbnail = _principal(wiki, title)
        url = original or thumbnail
        fallback_url = thumbnail if original else None
        title = info.get('title') or title
        step = 2
    if not url:
        data = _query(wiki, action='parse', page=title, prop='images', redirects=1)
        filenames = data.get('parse', {}).get('images') or []
        excluded = ('icon', 'logo', 'banner', 'map', 'mapa', 'symbol', 'sigil', 'crest', 'flag', 'stub', 'button')
        filename = next((name for name in filenames if isinstance(name, str)
                         and not name.lower().endswith(('.svg', '.gif'))
                         and not any(word in name.lower() for word in excluded)), None)
        if filename:
            data = _query(wiki, action='query', titles=f'File:{filename}', prop='imageinfo',
                          iiprop='url|size|mime', iiurlwidth=1536)
            pages = data.get('query', {}).get('pages') or []
            images = pages[0].get('imageinfo') or [] if pages else []
            if images:
                url = images[0].get('url') or images[0].get('thumburl')
                fallback_url = images[0].get('thumburl') if images[0].get('url') else None
                step = 3
    if not url:
        raise WikiImageNotFound(f'La página "{title}" no tiene imagen utilizable')
    raw, url = _download(url, wiki, fallback_url)
    LAST_RESULTS[(wiki, page)] = {'api': API_PATHS[wiki], 'step': step, 'url': url,
                                  'matched_title': title if searched and title != page else ''}
    return raw, url
