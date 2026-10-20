import base64
import hashlib
import json
import os
import tempfile
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from PIL import Image
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, TestCase

from decks.models import Deck, DeckCard
from .models import ReferenceImage, ThemeAssignment, GeneratedImage, GenerationJob
from .prompt_builder import build_visual_prompt, make_cache_key, SYSTEM_PROMPT
from .providers.grok import GrokProvider
from .providers.gemini import GeminiProvider
from .providers.openai import OpenAIProvider
from .references import MAX_REFERENCE_BYTES, normalize_reference, reference_hashes
from .runner import render_card_image, generate_card_image
from .tests import make_card, valid_prompt


def png(color='red', size=(10, 10), mode='RGBA'):
    stream = BytesIO()
    Image.new(mode, size, color).save(stream, format='PNG')
    return stream.getvalue()


def api_response(data=None, status=200, text=''):
    return Mock(status_code=status, text=text, json=Mock(return_value=data))


class NormalizationTests(SimpleTestCase):
    def test_transparent_png_becomes_white_rgb_jpeg(self):
        output, digest = normalize_reference(png(color=(0, 0, 0, 0)))
        with Image.open(BytesIO(output)) as image:
            self.assertEqual((image.format, image.mode), ('JPEG', 'RGB'))
            self.assertEqual(image.getpixel((0, 0)), (255, 255, 255))
        self.assertEqual(digest, hashlib.sha256(output).hexdigest())

    def test_resize_and_invalid_bytes(self):
        output, _ = normalize_reference(png(size=(3000, 1500)))
        with Image.open(BytesIO(output)) as image:
            self.assertEqual(image.size, (1536, 768))
        with self.assertRaisesRegex(ValueError, '^Imagen de referencia inválida$'):
            normalize_reference(b'not an image')
        with self.assertRaisesRegex(ValueError, 'La imagen supera 10 MB'):
            normalize_reference(b'x' * (MAX_REFERENCE_BYTES + 1))
        gif = BytesIO()
        Image.new('RGB', (2, 2)).save(gif, format='GIF')
        with self.assertRaisesRegex(ValueError, 'Imagen de referencia inválida'):
            normalize_reference(gif.getvalue())


@patch.dict(os.environ, {'XAI_API_KEY': 'test-key', 'XAI_EDIT_MODEL': 'grok-imagine-image-2.0', 'XAI_IMAGE_QUALITY': 'medium'})
class ReferenceProviderTests(SimpleTestCase):
    @patch('generation.providers.grok.requests.post')
    @patch('generation.providers.grok.OpenAI')
    def test_grok_json_edits_b64(self, sdk, post):
        post.return_value = api_response({'data': [{'b64_json': base64.b64encode(b'image').decode()}]})
        self.assertEqual(GrokProvider().generate('scene', '3:4', [b'ref1', b'ref2']), b'image')
        sdk.assert_not_called()
        args = post.call_args
        self.assertEqual(args.args[0], 'https://api.x.ai/v1/images/edits')
        self.assertEqual(args.kwargs['timeout'], 180)
        self.assertEqual(args.kwargs['headers'], {'Authorization': 'Bearer test-key'})
        body = args.kwargs['json']
        self.assertEqual(body['model'], 'grok-imagine-image-2.0')
        self.assertEqual(body['aspect_ratio'], '3:4')
        self.assertEqual(body['quality'], 'medium')
        self.assertEqual(body['response_format'], 'b64_json')
        self.assertEqual(body['images'], [{'type': 'image_url', 'url': 'data:image/jpeg;base64,' + base64.b64encode(ref).decode()} for ref in (b'ref1', b'ref2')])

    @patch('generation.providers.grok.requests.get')
    @patch('generation.providers.grok.requests.post')
    def test_grok_downloads_image_url(self, post, get):
        post.return_value = api_response({'data': [{'url': 'https://example.com/output.jpg'}]})
        get.return_value = Mock(content=b'image')
        self.assertEqual(GrokProvider().generate('scene', references=[b'ref']), b'image')
        get.assert_called_once_with('https://example.com/output.jpg', timeout=60)
        get.return_value.raise_for_status.assert_called_once()

    @patch('generation.providers.grok.requests.post')
    def test_compatibility_retries_remove_format_and_use_single_image(self, post):
        post.side_effect = [api_response(status=400, text='Unsupported response_format'),
                            api_response(status=422, text='images must use image for a single image'),
                            api_response({'data': [{'b64_json': 'aW1hZ2U='}]})]
        self.assertEqual(GrokProvider().generate('scene', references=[b'ref']), b'image')
        self.assertEqual(post.call_count, 3)
        first, second, third = [call.kwargs['json'] for call in post.call_args_list]
        self.assertIn('response_format', first)
        self.assertNotIn('response_format', second)
        self.assertIn('images', second)
        self.assertNotIn('images', third)
        self.assertEqual(third['image'], first['images'][0])

    @patch('generation.providers.grok.requests.post')
    def test_bad_status_and_no_image(self, post):
        post.return_value = api_response(status=403, text='blocked' + 'x' * 1000)
        with self.assertRaisesRegex(RuntimeError, '403') as caught:
            GrokProvider().generate('scene', references=[b'ref'])
        self.assertLess(len(str(caught.exception)), 350)
        self.assertEqual(post.call_count, 1)
        for data in ({'data': []}, {'data': [{'respect_moderation': False, 'b64_json': 'aW1hZ2U='}]}):
            post.return_value = api_response(data)
            with self.subTest(data=data), self.assertRaisesRegex(RuntimeError, 'Grok no devolvió imagen'):
                GrokProvider().generate('scene', references=[b'ref'])

    @patch('generation.providers.gemini.genai.Client')
    def test_gemini_includes_image_parts(self, client):
        client.return_value.models.generate_content.return_value = SimpleNamespace(candidates=[
            SimpleNamespace(content=SimpleNamespace(parts=[SimpleNamespace(inline_data=SimpleNamespace(data=b'image'))]))])
        self.assertEqual(GeminiProvider().generate('scene', '3:4', [b'ref1', b'ref2']), b'image')
        contents = client.return_value.models.generate_content.call_args.kwargs['contents']
        self.assertEqual(contents[0], 'scene')
        self.assertEqual([part.inline_data.data for part in contents[1:]], [b'ref1', b'ref2'])
        self.assertTrue(all(part.inline_data.mime_type == 'image/jpeg' for part in contents[1:]))

    @patch('generation.providers.openai.OpenAI')
    def test_openai_logs_and_ignores_references(self, sdk):
        sdk.return_value.images.generate.return_value = SimpleNamespace(data=[SimpleNamespace(b64_json='aW1hZ2U=')])
        with self.assertLogs('generation.providers.openai', level='INFO'):
            self.assertEqual(OpenAIProvider().generate('scene', '3:4', [b'ref']), b'image')
        self.assertEqual(sdk.return_value.images.generate.call_args.kwargs['size'], '1024x1536')
        self.assertNotIn('images', sdk.return_value.images.generate.call_args.kwargs)


@patch.dict(os.environ, {'IMAGE_PROVIDER': 'gemini', 'IMAGE_FALLBACK_PROVIDER': ''})
class ReferenceEndpointTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        setting = self.settings(MEDIA_ROOT=directory.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.deck = Deck.objects.create(name='Deck', theme='theme', raw_list='Card', card_layout='classic')
        self.card = make_card()
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Card', quantity=1, section='main')
        self.assignment = ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id, element="Vol'jin", category='Troll', visual_brief='Vol’jin at war')
        self.url = f'/api/cards/{self.entry.id}/theme-assignment/references/'

    def upload(self, raw=None):
        return self.client.post(self.url, {'file': SimpleUploadedFile('input.png', raw if raw is not None else png(), content_type='image/png')})

    def reference(self, color='red', position=0, assignment=None):
        image, digest = normalize_reference(png(color))
        ref = ReferenceImage(assignment=assignment or self.assignment, sha256=digest, position=position)
        ref.image.save('reference.jpg', ContentFile(image))
        return ref

    def test_upload_response_and_duplicate(self):
        result = self.upload()
        self.assertEqual(result.status_code, 201)
        self.assignment.refresh_from_db()
        self.assertTrue(self.assignment.edited)
        refs = result.json()['theme_assignment']['references']
        self.assertEqual(len(refs), 1)
        self.assertTrue(refs[0]['image_url'].startswith('http://testserver/media/references/'))
        self.assertEqual(refs[0]['source_url'], '')
        ref = ReferenceImage.objects.get()
        with Image.open(ref.image.path) as image:
            self.assertEqual(image.format, 'JPEG')
        result = self.upload()
        self.assertEqual((result.status_code, result.json()), (400, {'detail': 'Esa imagen ya está agregada.'}))

    @patch('generation.views.requests.get')
    def test_url_upload_and_invalid_downloads(self, get):
        get.return_value = Mock(headers={'Content-Type': 'image/png'}, content=png())
        result = self.client.post(self.url, {'url': 'https://example.com/reference.png'}, content_type='application/json')
        self.assertEqual(result.status_code, 201)
        get.assert_called_once_with('https://example.com/reference.png', timeout=20, headers={'User-Agent': 'ProxyForge/1.0'})
        self.assertEqual(result.json()['theme_assignment']['references'][0]['source_url'], 'https://example.com/reference.png')
        get.return_value.headers = {'Content-Type': 'text/html'}
        self.assertEqual(self.client.post(self.url, {'url': 'https://example.com/page'}, content_type='application/json').status_code, 400)
        get.side_effect = requests.ConnectionError('offline')
        self.assertEqual(self.client.post(self.url, {'url': 'https://example.com/offline'}, content_type='application/json').status_code, 400)

    def test_missing_assignment_limit_active_and_invalid_image(self):
        self.assertEqual(self.upload(b'broken').status_code, 400)
        self.reference('red'); self.reference('green'); self.reference('blue')
        result = self.upload(png('yellow'))
        self.assertEqual((result.status_code, result.json()), (400, {'detail': 'Máximo 3 imágenes de referencia por carta.'}))
        GenerationJob.objects.create(deck=self.deck, total_cards=1)
        self.assertEqual(self.upload().status_code, 409)
        self.deck.jobs.all().delete()
        self.assignment.delete()
        result = self.upload()
        self.assertEqual((result.status_code, result.json()), (400, {'detail': 'Primero define o genera el plan temático de esta carta.'}))

    def test_delete_ownership_and_file_cleanup(self):
        ref = self.reference()
        path = Path(ref.image.path)
        other_card = make_card()
        other = ThemeAssignment.objects.create(deck=self.deck, oracle_id=other_card.oracle_id, element='Other', category='', visual_brief='Brief')
        other_ref = self.reference('blue', assignment=other)
        self.assertEqual(self.client.delete(self.url + f'{other_ref.id}/').status_code, 404)
        result = self.client.delete(self.url + f'{ref.id}/')
        self.assertEqual(result.status_code, 200)
        self.assertFalse(path.exists())
        self.assertFalse(ReferenceImage.objects.filter(pk=ref.id).exists())
        self.assertEqual(result.json()['theme_assignment']['references'], [])
        other_path = Path(other_ref.image.path)
        other.delete()
        self.assertFalse(other_path.exists())

    def test_cache_compatibility_and_add_remove_invalidation(self):
        legacy_value = f'{self.card.oracle_id}|theme|v3|gemini|vol\'jin|vol’jin at war'
        legacy = hashlib.sha256(legacy_value.encode()).hexdigest()
        self.assertEqual(make_cache_key(self.card.oracle_id, 'theme', 'gemini', self.assignment), legacy)
        self.assertEqual(make_cache_key(self.card.oracle_id, 'theme', 'gemini', self.assignment, reference_hashes=[]), legacy)
        GeneratedImage.objects.create(cache_key=legacy, card=self.card, theme='theme', visual_prompt='', prompt_json={}, image='generated/mock.png', provider='gemini', prompt_version='v3')
        estimate = f'/api/decks/{self.deck.id}/estimate/'
        self.assertEqual(self.client.get(estimate).json()['cached'], 1)
        result = self.upload()
        self.assertEqual(result.status_code, 201)
        self.assertIsNone(result.json()['generated_image'])
        self.assertEqual(self.client.get(estimate).json()['to_generate'], 1)
        ref = ReferenceImage.objects.get()
        key = make_cache_key(self.card.oracle_id, 'theme', 'gemini', self.assignment, reference_hashes=[ref.sha256])
        self.assertNotEqual(key, legacy)
        self.assertEqual(make_cache_key(self.card.oracle_id, 'theme', 'gemini', reference_hashes=['b', 'a']),
                         make_cache_key(self.card.oracle_id, 'theme', 'gemini', reference_hashes=['a', 'b']))
        self.assertIsNotNone(self.client.delete(self.url + f'{ref.id}/').json()['generated_image'])
        self.assertEqual(self.client.get(estimate).json()['cached'], 1)

    @patch('generation.claude_client.Anthropic')
    def test_prompt_reference_count(self, anthropic):
        anthropic.return_value.messages.create.return_value = SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(valid_prompt()))])
        build_visual_prompt(self.card, 'theme', reference_count=2)
        kwargs = anthropic.return_value.messages.create.call_args.kwargs
        data = json.loads(kwargs['messages'][0]['content'][0]['text'])
        self.assertEqual(data['reference_images'], 2)
        self.assertIn('Use the character shown in the reference image(s)', SYSTEM_PROMPT)

    @patch('generation.runner.build_visual_prompt', return_value=valid_prompt())
    @patch('generation.runner.get_fallback_provider')
    def test_runner_passes_ordered_bytes_to_all_attempts(self, fallback, build):
        later = self.reference('blue', position=1)
        first = self.reference('red', position=0)
        primary, backup = Mock(), Mock()
        primary.name, backup.name = 'grok', 'gemini'
        primary.generate.side_effect = RuntimeError('rejected')
        backup.generate.return_value = png()
        fallback.return_value = backup
        expected = [Path(first.image.path).read_bytes(), Path(later.image.path).read_bytes()]
        with self.assertLogs('generation.runner', level='WARNING'):
            render_card_image(self.card, 'theme', primary, self.assignment, layout='borderless')
        self.assertTrue(all(call.kwargs['references'] == expected for call in primary.generate.call_args_list))
        self.assertEqual(backup.generate.call_args.kwargs['references'], expected)
        self.assertEqual(backup.generate.call_args.kwargs['aspect_ratio'], '2:3')
        self.assertEqual(build.call_args.kwargs['reference_count'], 2)

    @patch('generation.runner.build_visual_prompt', return_value=valid_prompt())
    def test_generated_image_uses_reference_cache_key(self, build):
        ref = self.reference()
        provider = Mock()
        provider.name = 'gemini'
        provider.generate.return_value = png()
        image = generate_card_image(self.card, 'theme', provider, self.assignment)
        expected = make_cache_key(self.card.oracle_id, 'theme', 'gemini', self.assignment, reference_hashes=[ref.sha256])
        self.assertEqual(image.cache_key, expected)
        detail = self.client.get(f'/api/decks/{self.deck.id}/').json()
        self.assertEqual(detail['cards'][0]['generated_image']['id'], image.id)
        self.assertEqual(self.client.get(f'/api/decks/{self.deck.id}/estimate/').json()['cached'], 1)

    def test_detail_queries_constant_with_references(self):
        self.reference()
        url = f'/api/decks/{self.deck.id}/'
        with self.assertNumQueries(8):
            self.client.get(url)
        for i in range(12):
            card = make_card()
            DeckCard.objects.create(deck=self.deck, card=card, raw_name='Card', quantity=1, section='main')
            assignment = ThemeAssignment.objects.create(deck=self.deck, oracle_id=card.oracle_id, element=f'Element {i}', category='', visual_brief='Brief')
            self.reference(assignment=assignment)
        with self.assertNumQueries(8):
            self.client.get(url)
