import json
import time
from pathlib import Path
from uuid import uuid4

import requests
from django.conf import settings
from django.http import FileResponse
from django.urls import reverse
from rest_framework.response import Response
from rest_framework.views import APIView

from generation.errors import short_error

from .client import HEADERS

CACHE_SECONDS = 7 * 24 * 60 * 60


def get_symbology():
    path = Path(settings.MEDIA_ROOT) / 'cache' / 'symbology.json'
    cached = None
    try:
        cached = json.loads(path.read_text())
        if time.time() - path.stat().st_mtime < CACHE_SECONDS:
            return cached
    except (OSError, ValueError):
        pass
    try:
        response = requests.get('https://api.scryfall.com/symbology', headers=HEADERS, timeout=30)
        response.raise_for_status()
        data = [{key: item[key] for key in ('symbol', 'svg_uri', 'english')}
                for item in response.json()['data']]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        if cached is not None:
            return cached
        raise
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f'.{uuid4().hex}.tmp')
    temporary.write_text(json.dumps(data))
    temporary.replace(path)
    return data


def _symbol_code(symbol):
    return symbol.strip('{}').replace('/', '-')


class SymbologyView(APIView):
    def get(self, request):
        try:
            data = get_symbology()
        except (requests.RequestException, OSError, ValueError, KeyError, TypeError) as exc:
            return Response({'detail': short_error(exc)}, status=502)
        return Response([{**item, 'local_svg_uri': request.build_absolute_uri(
            reverse('symbology-svg', args=[_symbol_code(item['symbol'])]))} for item in data])


class SymbolSvgView(APIView):
    def get(self, request, code):
        try:
            symbols = get_symbology()
            symbol = next((item for item in symbols if _symbol_code(item['symbol']) == code), None)
            if symbol is None:
                return Response({'detail': 'Símbolo no encontrado.'}, status=404)
            path = Path(settings.MEDIA_ROOT) / 'cache' / 'symbols' / f'{code}.svg'
            if not path.exists():
                response = requests.get(symbol['svg_uri'], headers=HEADERS, timeout=20)
                response.raise_for_status()
                path.parent.mkdir(parents=True, exist_ok=True)
                temporary = path.with_suffix(f'.{uuid4().hex}.tmp')
                temporary.write_bytes(response.content)
                temporary.replace(path)
            response = FileResponse(path.open('rb'), content_type='image/svg+xml')
            response['Cache-Control'] = 'public, max-age=2592000'
            return response
        except (requests.RequestException, OSError, ValueError, KeyError, TypeError) as exc:
            return Response({'detail': short_error(exc)}, status=502)
