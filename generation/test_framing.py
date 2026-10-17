import base64
import json
import os
import tempfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from django.db import DatabaseError
from django.test import SimpleTestCase, TestCase

from decks.models import Deck, DeckCard
from .framing import auto_framing, detect_face_center, apply_auto_framing
from .models import GeneratedImage, GenerationJob
from .prompt_builder import build_visual_prompt, make_cache_key, PROMPT_VERSION
from .runner import save_card_image, generate_card_image
from .tests import make_card, valid_prompt


def png():
    stream = BytesIO()
    Image.new('RGB', (864, 1152), 'green').save(stream, format='PNG')
    return stream.getvalue()


def response(data):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(data))], stop_reason='end_turn')


class FramingCalculationTests(SimpleTestCase):
    def test_face_positions_and_limits(self):
        self.assertEqual(auto_framing(0.5, 0.1, 864, 1152, 'borderless'), (0, 22, 100))
        self.assertEqual(auto_framing(0.5, 0.3, 864, 1152, 'borderless'), (0, 2, 100))
        self.assertEqual(auto_framing(0.5, 0.4, 864, 1152, 'borderless'), (0, 0, 100))
        self.assertEqual(auto_framing(0.5, 0, 864, 1152, 'borderless'), (0, 32, 100))
        self.assertEqual(auto_framing(0.5, 0.1, 864, 1152, 'classic'), (0, 0, 100))
        self.assertEqual(auto_framing(0.5, 0.1234, 864, 1152, 'borderless'), (0, 19.7, 100))
        x, y, zoom = auto_framing(0.2, 0.1, 864, 1152, 'borderless')
        self.assertGreater(x, 0)
        self.assertLessEqual(x, round((864 / 1152 / (63 / 88) * 100 - 100) / 2, 1))

    @patch('generation.claude_client.Anthropic')
    def test_detect_face_resizes_image_and_uses_vision_model(self, anthropic):
        create = anthropic.return_value.messages.create
        create.return_value = response({'found': True, 'face_center_x': 0.5, 'face_center_y': 0.1, 'face_height': 0.15})
        with patch.dict(os.environ, {'ANTHROPIC_VERIFY_MODEL': 'vision-model'}):
            self.assertEqual(detect_face_center(png()), (0.5, 0.1))
        args = create.call_args.kwargs
        self.assertEqual(args['model'], 'vision-model')
        self.assertEqual(args['max_tokens'], 200)
        raw = base64.b64decode(args['messages'][0]['content'][0]['source']['data'])
        with Image.open(BytesIO(raw)) as image:
            self.assertEqual((max(image.size), image.format, image.mode), (768, 'JPEG', 'RGB'))

    @patch('generation.claude_client.Anthropic')
    def test_no_face_invalid_json_invalid_numbers_and_api_error(self, anthropic):
        create = anthropic.return_value.messages.create
        create.return_value = response({'found': False})
        self.assertIsNone(detect_face_center(png()))
        create.return_value = SimpleNamespace(content=[SimpleNamespace(type='text', text='invalid JSON')], stop_reason='end_turn')
        with self.assertLogs('generation.framing', level='WARNING'):
            self.assertIsNone(detect_face_center(png()))
        for value in (-0.1, 1.1, True, float('nan')):
            create.return_value = response({'found': True, 'face_center_x': 0.5, 'face_center_y': value, 'face_height': 0.1})
            with self.subTest(value=value), self.assertLogs('generation.framing', level='WARNING'):
                self.assertIsNone(detect_face_center(png()))
        create.side_effect = RuntimeError('Claude unavailable')
        with self.assertLogs('generation.framing', level='WARNING'):
            self.assertIsNone(detect_face_center(png()))

    @patch('generation.claude_client.Anthropic')
    def test_borderless_prompt_requests_safe_head_position_and_english_suffix(self, anthropic):
        anthropic.return_value.messages.create.return_value = response(valid_prompt())
        from decks.models import Card
        build_visual_prompt(Card(name='Card'), 'theme', layout='borderless')
        system = anthropic.return_value.messages.create.call_args.kwargs['system']
        self.assertIn('entre el 20% y el 45%', system)
        self.assertIn('NUNCA en el 15% superior', system)
        self.assertIn('Vertical 3:4 composition, head positioned in the upper-middle', system)
        self.assertIn('termina full_prompt y fallback_prompt', system)
        self.assertEqual(PROMPT_VERSION, 'v3')


@patch.dict(os.environ, {'AUTO_FRAMING': 'true', 'IMAGE_PROVIDER': 'gemini', 'IMAGE_FALLBACK_PROVIDER': ''})
class FramingTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        setting = self.settings(MEDIA_ROOT=directory.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.deck = Deck.objects.create(name='Deck', theme='theme', raw_list='Card', card_layout='borderless')
        self.card = make_card()
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Card', quantity=1, section='main')
        self.provider = Mock()
        self.provider.name = 'gemini'
        self.provider.generate.return_value = png()
        self.url = f'/api/cards/{self.entry.id}/framing/'
        self.prompt = {**valid_prompt(), 'used_prompt': 'full', 'used_provider': 'gemini'}

    def save(self, card=None, layout='borderless'):
        return save_card_image(card or self.card, 'theme', self.provider, self.prompt, png(), layout=layout)

    @patch('generation.framing.detect_face_center', return_value=(0.5, 0.1))
    def test_persistence_and_serializer_fields(self, detect):
        image = self.save()
        image.refresh_from_db()
        self.assertEqual((image.face_center_y, image.framing_offset_y, image.framing_zoom, image.framing_source), (0.1, 22, 100, 'auto'))
        data = self.client.get(f'/api/decks/{self.deck.id}/').json()['cards'][0]['generated_image']
        self.assertEqual((data['framing_offset_x'], data['width'], data['height'], data['face_center_x']), (0, 864, 1152, 0.5))
        self.assertEqual((data['face_center_y'], data['framing_source'], data['framing_offset_y'], data['framing_zoom']), (0.1, 'auto', 22, 100))
        detect.reset_mock()
        classic = self.save(layout='classic')
        detect.assert_not_called()
        self.assertEqual((classic.framing_source, classic.framing_offset_y, classic.framing_zoom), ('default', 0, 100))

    @patch('generation.framing.detect_face_center', return_value=(0.2, 0.1))
    def test_auto_reads_missing_dimensions_and_centers_horizontally(self, detect):
        image = self.save()
        GeneratedImage.objects.filter(pk=image.pk).update(width=None, height=None)
        image.refresh_from_db()
        apply_auto_framing(image, 'borderless')
        image.refresh_from_db()
        self.assertEqual((image.width, image.height, image.face_center_x), (864, 1152, 0.2))
        self.assertGreater(image.framing_offset_x, 0)
        GeneratedImage.objects.filter(pk=image.pk).update(width=None, height=None)
        image.refresh_from_db()
        detect.reset_mock()
        apply_auto_framing(image, 'classic')
        self.assertEqual((image.width, image.height), (864, 1152))
        self.assertEqual((image.framing_offset_x, image.framing_offset_y, image.framing_zoom), (0, 0, 100))
        detect.assert_not_called()

    @patch('generation.framing.detect_face_center')
    def test_flag_disabled_and_no_face_defaults(self, detect):
        with patch.dict(os.environ, {'AUTO_FRAMING': 'false'}):
            image = self.save()
        detect.assert_not_called()
        image.delete()
        detect.return_value = None
        image = self.save()
        self.assertEqual((image.framing_source, image.face_center_y), ('default', None))

    @patch('generation.runner.apply_auto_framing', side_effect=RuntimeError('framing failed'))
    def test_unexpected_framing_error_keeps_saved_image(self, apply):
        with self.assertLogs('generation.runner', level='WARNING'):
            image = self.save()
        self.assertTrue(GeneratedImage.objects.filter(pk=image.pk).exists())
        self.assertTrue(image.image.storage.exists(image.image.name))

    @patch('generation.framing.detect_face_center', return_value=(0.5, 0.1))
    @patch('django.db.models.query.QuerySet.update', side_effect=DatabaseError('framing write failed'))
    def test_failed_framing_update_does_not_discard_image(self, update, detect):
        with self.assertLogs('generation.framing', level='WARNING'):
            image = self.save()
        image.refresh_from_db()
        self.assertEqual(image.framing_source, 'default')
        self.assertTrue(image.image.storage.exists(image.image.name))

    @patch('generation.framing.detect_face_center', return_value=(0.5, 0.1))
    def test_manual_framing_clamps_and_preserves_unspecified_fields(self, detect):
        image = self.save()
        result = self.client.patch(self.url, {'offset_x': 99, 'offset_y': 100, 'zoom': 10}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        data = result.json()['generated_image']
        self.assertEqual(data['framing_offset_x'], 60)
        self.assertEqual((data['framing_offset_y'], data['framing_zoom'], data['framing_source']), (60, 50, 'manual'))
        result = self.client.patch(self.url, {'offset_x': -999, 'offset_y': -99, 'zoom': 999}, content_type='application/json')
        self.assertEqual((result.json()['generated_image']['framing_offset_y'], result.json()['generated_image']['framing_zoom']), (-60, 300))
        self.assertEqual(result.json()['generated_image']['framing_offset_x'], -60)
        self.client.patch(self.url, {'offset_y': 12.5}, content_type='application/json')
        image.refresh_from_db()
        self.assertEqual((image.framing_offset_x, image.framing_offset_y, image.framing_zoom), (-60, 12.5, 300))

    def test_missing_art_and_invalid_numbers(self):
        result = self.client.patch(self.url, {'offset_y': 1}, content_type='application/json')
        self.assertEqual((result.status_code, result.json()), (404, {'detail': 'La carta no tiene arte generado.'}))
        self.assertEqual(self.client.post(self.url + 'auto/').status_code, 404)
        with patch('generation.framing.detect_face_center', return_value=None):
            self.save()
        for body in ({'zoom': 'bad'}, {'offset_y': None}, {'offset_x': True}, {'offset_x': '12'}, {'zoom': True}, {'offset_y': '12'}):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(self.url, body, content_type='application/json').status_code, 400)

    @patch('generation.framing.detect_face_center', return_value=(0.5, 0.1))
    def test_auto_endpoint_overrides_manual_and_resets_on_no_face(self, detect):
        image = self.save()
        self.client.patch(self.url, {'offset_y': -10}, content_type='application/json')
        result = self.client.post(self.url + 'auto/')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['generated_image']['framing_offset_y'], 22)
        self.assertEqual(result.json()['generated_image']['framing_source'], 'auto')
        detect.return_value = None
        result = self.client.post(self.url + 'auto/')
        data = result.json()['generated_image']
        self.assertEqual((data['framing_offset_y'], data['framing_zoom'], data['framing_source'], data['face_center_y']), (0, 100, 'default', None))

    @patch('generation.framing.detect_face_center', return_value=None)
    def test_deck_auto_skips_manual_deduplicates_and_rejects_active_job(self, detect):
        images = [self.save()]
        for _ in range(3):
            card = make_card()
            DeckCard.objects.create(deck=self.deck, card=card, raw_name='Extra', quantity=1, section='main')
            images.append(self.save(card=card))
        GeneratedImage.objects.filter(pk=images[0].pk).update(framing_source='manual', framing_offset_y=12)
        DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Duplicate', quantity=2, section='main')
        detect.reset_mock()
        detect.side_effect = [(0.5, 0.1), (0.5, 0.4), None]
        url = f'/api/decks/{self.deck.id}/framing/auto/'
        result = self.client.post(url)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['framing_summary'], {'adjusted': 1, 'unchanged': 1, 'no_face': 1})
        self.assertEqual(detect.call_count, 3)
        images[0].refresh_from_db()
        self.assertEqual((images[0].framing_source, images[0].framing_offset_y), ('manual', 12))
        GenerationJob.objects.create(deck=self.deck, total_cards=1)
        self.assertEqual(self.client.post(url).status_code, 409)

    @patch('generation.framing.detect_face_center', return_value=(0.5, 0.4))
    @patch('generation.runner.build_visual_prompt', return_value=valid_prompt())
    @patch('generation.views.get_provider')
    def test_regeneration_does_not_inherit_manual_framing(self, provider, prompt, detect):
        provider.return_value = self.provider
        old = generate_card_image(self.card, 'theme', self.provider, layout='borderless')
        self.client.patch(self.url, {'offset_y': 35, 'zoom': 150}, content_type='application/json')
        result = self.client.post(f'/api/cards/{self.entry.id}/regenerate/', {}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        data = result.json()['generated_image']
        self.assertNotEqual(data['id'], old.id)
        self.assertEqual((data['framing_source'], data['framing_offset_y'], data['framing_zoom']), ('auto', 0, 100))
        self.assertEqual(GeneratedImage.objects.get().cache_key, make_cache_key(self.card.oracle_id, 'theme', 'gemini', layout='borderless'))
