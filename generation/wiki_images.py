import re
import threading
import time
from urllib.parse import urlparse

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


def valid_wiki(wiki):
    return isinstance(wiki, str) and len(wiki) <= 200 and re.fullmatch(WIKI_PATTERN, wiki) is not None


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
    url = (info.get('original') or {}).get('source') or (info.get('thumbnail') or {}).get('source')
    return info, url


def _download(url):
    response = _get(url, 20, accept='image/jpeg,image/png,image/webp;q=0.9,*/*;q=0.5')
    if not 200 <= response.status_code < 300:
        raise WikiImageError(f'La descarga respondió HTTP {response.status_code}')
    mime = response.headers.get('Content-Type', '').split(';', 1)[0].strip().lower()
    if mime not in ('image/jpeg', 'image/png', 'image/webp'):
        raise WikiImageNotFound('El archivo no es una imagen JPEG, PNG o WEBP')
    try:
        length = int(response.headers.get('Content-Length', '0'))
    except ValueError:
        length = 0
    if length > MAX_REFERENCE_BYTES:
        raise WikiImageError('La imagen supera 10 MB')
    raw = response.content
    if len(raw) > MAX_REFERENCE_BYTES:
        raise WikiImageError('La imagen supera 10 MB')
    return raw


def find_wiki_image(wiki, page):
    if not valid_wiki(wiki) or not isinstance(page, str) or not page.strip() or len(page) > 300:
        raise WikiImageError('Dominio o página de wiki inválidos')
    info, url = _principal(wiki, page)
    step = 1
    title = info.get('title') or page
    if info.get('missing'):
        search = _query(wiki, action='query', list='search', srsearch=page, srlimit=1, srnamespace=0)
        results = search.get('query', {}).get('search') or []
        if not results:
            raise WikiImageNotFound('Página no encontrada')
        title = results[0]['title']
        info, url = _principal(wiki, title)
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
                url = images[0].get('thumburl') or images[0].get('url')
                step = 3
    if not url:
        raise WikiImageNotFound('No se encontró una imagen de la página')
    raw = _download(url)
    LAST_RESULTS[(wiki, page)] = {'api': API_PATHS[wiki], 'step': step, 'url': url}
    return raw, url
