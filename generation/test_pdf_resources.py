import importlib
import json
import os
import tempfile
import time
from io import BytesIO
from pathlib import Path
from unittest.mock import Mock, patch

from PIL import Image, PdfParser
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import clear_url_caches

from decks.models import Deck, DeckCard
from .models import GeneratedImage
from .pdf_export import build_pdf, CARD_SIZE, PAPER_SIZES, MAX_IMAGE_BYTES
from .test_references import png
from .tests import make_card
from .thumbnails import get_thumbnail


def read_pdf(raw):
    parser = PdfParser.PdfParser(buf=raw)
    pages = []
    for ref in parser.pages:
        page = parser.read_indirect(ref)
        image = parser.read_indirect(page[b'Resources'][b'XObject'][b'image'])
        with Image.open(BytesIO(image.buf)) as jpeg:
            jpeg.load()
            pages.append((page[b'MediaBox'], jpeg.copy()))
    parser.close()
    return pages


@patch.dict(os.environ, {'IMAGE_PROVIDER': 'gemini'})
class ResourceTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        setting = self.settings(MEDIA_ROOT=Path(directory.name), DEBUG=True)
        setting.enable()
        import config.urls
        importlib.reload(config.urls)
        clear_url_caches()
        def reset():
            setting.disable()
            importlib.reload(config.urls)
            clear_url_caches()
        self.addCleanup(reset)
        self.root = Path(directory.name)
        self.deck = Deck.objects.create(name='Deck', raw_list='', theme='theme')
        self.card = make_card()
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Card', section='main', quantity=1)
        from .prompt_builder import card_cache_key
        self.image = GeneratedImage(card=self.card, cache_key=card_cache_key(self.card, self.deck.theme, 'gemini', layout=self.deck.card_layout),
            theme='theme', visual_prompt='', prompt_json={}, provider='gemini', prompt_version='v3')
        self.image.image.save('art.png', ContentFile(png(size=(960, 1440))))

    def body(self, response):
        data = b''.join(response.streaming_content) if response.streaming else response.content
        response.close()
        return data

    def test_thumbnail_webp_width_cache_invalidation_endpoint_and_cleanup(self):
        path = get_thumbnail(self.image)
        with Image.open(path) as image:
            self.assertEqual((image.format, image.size), ('WEBP', (480, 720)))
        stamp = path.stat().st_mtime_ns
        with patch('generation.thumbnails.Image.open', side_effect=AssertionError('Cache should be reused')):
            self.assertEqual(get_thumbnail(self.image), path)
        self.assertEqual(path.stat().st_mtime_ns, stamp)
        newer = time.time() + 2
        os.utime(self.image.image.path, (newer, newer))
        with patch('generation.thumbnails.Image.open', wraps=Image.open) as opened:
            get_thumbnail(self.image)
        opened.assert_called_once()
        for width, expected in (('320', 320), ('800', 800), ('bad', 480), ('999', 480)):
            response = self.client.get(f'/api/images/{self.image.pk}/thumb/?w={width}')
            self.assertEqual(response['Content-Type'], 'image/webp')
            self.assertEqual(response['Cache-Control'], 'public, max-age=604800')
            with Image.open(BytesIO(self.body(response))) as thumb:
                self.assertEqual(thumb.width, expected)
        detail = self.client.get(f'/api/decks/{self.deck.pk}/').json()
        self.assertEqual(detail['cards'][0]['generated_image']['thumb_url'], f'http://testserver/api/images/{self.image.pk}/thumb/?w=480')
        self.assertEqual(self.client.get('/api/images/999999/thumb/').status_code, 404)
        pk = self.image.pk
        self.image.delete()
        self.assertFalse(list((self.root / 'cache' / 'thumbs').glob(f'{pk}-*.webp')))

    @patch('generation.original_art.requests.get')
    def test_original_art_cached_jpeg_and_deck_card_url(self, get):
        self.card.original_art_url = 'https://cards.scryfall.io/original.jpg'
        self.card.save()
        directory = self.root / 'cache' / 'original_art'
        directory.mkdir(parents=True)
        raw = BytesIO()
        Image.new('RGB', (30, 40), 'red').save(raw, 'JPEG')
        (directory / f'{self.card.scryfall_id}.jpg').write_bytes(raw.getvalue())
        url = f'/api/cards/{self.entry.pk}/original-art/'
        response = self.client.get(url)
        self.assertEqual((response.status_code, response['Content-Type'], response['Cache-Control']), (200, 'image/jpeg', 'public, max-age=604800'))
        self.assertEqual(response.content, raw.getvalue())
        get.assert_not_called()
        entry = self.client.get(f'/api/decks/{self.deck.pk}/').json()['cards'][0]
        self.assertEqual(entry['original_art_local_url'], 'http://testserver' + url)
        self.card.original_art_url = ''
        self.card.save()
        self.assertEqual(self.client.get(url).status_code, 404)
        self.assertIsNone(self.client.get(f'/api/decks/{self.deck.pk}/').json()['cards'][0]['original_art_local_url'])
        self.entry.card = None
        self.entry.save()
        self.assertEqual(self.client.get(url).status_code, 404)

    @patch('scryfall.views.requests.get')
    def test_symbols_cached_svg_and_unknown_symbol(self, get):
        cache = self.root / 'cache' / 'symbology.json'
        cache.parent.mkdir(parents=True)
        cache.write_text(json.dumps([{'symbol': '{W/P}', 'svg_uri': 'https://svgs.scryfall.io/card-symbols/WP.svg', 'english': 'white phyrexian mana'}]))
        get.return_value = Mock(content=b'<svg xmlns="http://www.w3.org/2000/svg"></svg>')
        for _ in range(2):
            response = self.client.get('/api/symbology/svg/W-P.svg')
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response['Content-Type'], 'image/svg+xml')
            self.assertEqual(response['Cache-Control'], 'public, max-age=2592000')
            self.assertTrue(self.body(response).startswith(b'<svg'))
        self.assertEqual(get.call_count, 1)
        self.assertEqual(get.call_args.args[0], 'https://svgs.scryfall.io/card-symbols/WP.svg')
        self.assertEqual(self.client.get('/api/symbology/svg/UNKNOWN.svg').status_code, 404)
        self.assertEqual(self.client.get('/api/symbology/').json()[0]['local_svg_uri'], 'http://testserver/api/symbology/svg/W-P.svg')

    def test_cors_media_and_new_api_endpoints_for_both_origins(self):
        for origin in ('http://localhost:5173', 'http://127.0.0.1:5173'):
            media = self.client.get(self.image.image.url, HTTP_ORIGIN=origin)
            self.assertEqual(media.status_code, 200)
            self.assertEqual(media['Access-Control-Allow-Origin'], origin)
            self.body(media)
            thumb = self.client.get(f'/api/images/{self.image.pk}/thumb/', HTTP_ORIGIN=origin)
            self.assertEqual(thumb['Access-Control-Allow-Origin'], origin)
            self.body(thumb)
            missing = self.client.get(f'/api/cards/{self.entry.pk}/original-art/', HTTP_ORIGIN=origin)
            self.assertEqual(missing['Access-Control-Allow-Origin'], origin)


class PdfExportTests(TestCase):
    def setUp(self):
        self.deck = Deck.objects.create(name='Mazo de prueba', raw_list='', theme='theme')
        self.entries = [DeckCard.objects.create(deck=self.deck, card=make_card(), raw_name='Card', quantity=5, section='main') for _ in range(2)]
        self.keys = [str(entry.pk) for entry in self.entries]
        self.url = f'/api/decks/{self.deck.pk}/export-pdf/'

    def export(self, layout=None, paper='a4', cut_marks='true', raw=None, **extra):
        files = [SimpleUploadedFile(f'{index}.png', raw if raw is not None else png(color, size=(100, 150)), content_type='image/png')
                 for index, color in enumerate(('red', 'blue'))]
        data = dict(paper=paper, cut_marks=cut_marks, bleed_mm='0', images=files, keys=json.dumps(self.keys),
                    layout=json.dumps(layout if layout is not None else self.keys * 5))
        data.update(extra)
        return self.client.post(self.url, data)

    def test_ten_keys_two_pages_pdf_images_and_a4_300dpi(self):
        with self.assertLogs('generation.pdf_export', level='INFO') as logs:
            response = self.export()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response['Content-Type'], 'application/pdf')
        self.assertEqual(response['Content-Disposition'], 'attachment; filename="mazo-de-prueba-proxies.pdf"')
        self.assertTrue(response.content.startswith(b'%PDF-'))
        pages = read_pdf(response.content)
        self.assertEqual(len(pages), 2)
        for box, image in pages:
            self.assertEqual(image.size, PAPER_SIZES['a4'])
            self.assertAlmostEqual(box[2] / 72 * 300, 2480)
            self.assertAlmostEqual(box[3] / 72 * 300, 3508)
        left, top = (2480 - 3 * 744) // 2, (3508 - 3 * 1039) // 2
        self.assertGreater(pages[0][1].getpixel((left + 50, top + 50))[0], 240)
        self.assertGreater(pages[0][1].getpixel((left + 744 + 50, top + 50))[2], 240)
        self.assertTrue(any('2 páginas' in line for line in logs.output))

    def test_letter_size_and_cut_marks_only_in_margins(self):
        marked = read_pdf(self.export(layout=self.keys, paper='letter').content)[0][1]
        plain = read_pdf(self.export(layout=self.keys, paper='letter', cut_marks='false').content)[0][1]
        self.assertEqual(plain.size, PAPER_SIZES['letter'])
        left, top = (2550 - 3 * 744) // 2, (3300 - 3 * 1039) // 2
        self.assertLess(max(marked.getpixel((left, top - 20))), 180)
        self.assertEqual(plain.getpixel((left, top - 20)), (255, 255, 255))
        self.assertGreater(marked.getpixel((left + 30, top + 30))[0], 240)

    def test_invalid_key_images_limits_alignment_and_options(self):
        for response in (self.export(layout=['unknown']), self.export(raw=b'not an image'),
                         self.export(layout=[self.keys[0]] * 401), self.export(raw=b'x' * (MAX_IMAGE_BYTES + 1)),
                         self.export(paper='legal'), self.export(cut_marks='yes'), self.export(bleed_mm='1'),
                         self.export(layout=[]), self.export(layout='not a list'), self.export(keys='["unknown"]')):
            self.assertEqual(response.status_code, 400)
            self.assertIn('detail', response.json())
        response = self.client.post(self.url, {'layout': '[]', 'images': [], 'keys': '[]'})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post('/api/decks/999999/export-pdf/', {}).status_code, 404)
