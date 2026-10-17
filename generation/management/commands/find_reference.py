from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from PIL import Image
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.utils.text import slugify

from generation.auto_references import verify_reference
from generation.references import normalize_reference
from generation.wiki_images import WikiDownloadBlocked, LAST_RESULTS, WikiImageError, WikiImageNotFound, find_wiki_image


class Command(BaseCommand):
    help = 'Busca y guarda una referencia de una wiki para diagnóstico local.'

    def add_arguments(self, parser):
        parser.add_argument('page')
        parser.add_argument('--wiki', required=True)
        parser.add_argument('--verify', action='store_true')

    def handle(self, *args, **options):
        wiki, page = options['wiki'], options['page']
        try:
            raw, url = find_wiki_image(wiki, page)
            normalized, _ = normalize_reference(raw)
        except WikiDownloadBlocked as exc:
            self.stdout.write('Status: blocked')
            raise CommandError(str(exc)) from exc
        except (WikiImageError, WikiImageNotFound, ValueError) as exc:
            self.stdout.write('Status: not_found' if isinstance(exc, WikiImageNotFound) else 'Status: error')
            raise CommandError(str(exc)[:300]) from exc
        self.stdout.write('Status: found')
        details = LAST_RESULTS.get((wiki, page), {})
        self.stdout.write(f"API: https://{wiki}{details.get('api', '/api.php')}")
        self.stdout.write(f"Paso: {details.get('step', '?')}; origen: {url}")
        with Image.open(BytesIO(raw)) as image:
            self.stdout.write(f'Dimensiones: {image.width} x {image.height}')
        destination = Path(settings.MEDIA_ROOT) / 'cache' / 'find_reference' / f'{slugify(page) or "reference"}.jpg'
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(normalized)
        self.stdout.write(f'Guardada: {destination}')
        if options['verify']:
            ok, reason = verify_reference(normalized, SimpleNamespace(element=page, category='', visual_brief=''))
            self.stdout.write(f'Verificación: {ok}; {reason}')
