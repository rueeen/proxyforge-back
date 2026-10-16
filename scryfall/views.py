import json
import time
from pathlib import Path
from uuid import uuid4

import requests
from django.conf import settings
from rest_framework.response import Response
from rest_framework.views import APIView

from .client import HEADERS

CACHE_SECONDS = 7 * 24 * 60 * 60


class SymbologyView(APIView):
    def get(self, request):
        path = Path(settings.MEDIA_ROOT) / 'cache' / 'symbology.json'
        cached = None
        try:
            cached = json.loads(path.read_text())
            if time.time() - path.stat().st_mtime < CACHE_SECONDS:
                return Response(cached)
        except (OSError, ValueError):
            pass
        try:
            response = requests.get('https://api.scryfall.com/symbology', headers=HEADERS, timeout=30)
            response.raise_for_status()
            data = [{key: item[key] for key in ('symbol', 'svg_uri', 'english')}
                    for item in response.json()['data']]
        except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
            return Response(cached) if cached is not None else Response({'detail': str(exc)}, status=502)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(f'.{uuid4().hex}.tmp')
        temporary.write_text(json.dumps(data))
        temporary.replace(path)
        return Response(data)
