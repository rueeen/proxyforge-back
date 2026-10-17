import base64
import hashlib
import json
import os
import tempfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from decks.models import Deck, DeckCard
from .models import GeneratedImage, GenerationJob, ThemeAssignment
from .prompt_builder import CLASSIC_COMPOSITION, PROMPT_VERSION, SYSTEM_PROMPT, build_visual_prompt, make_cache_key
from .providers.gemini import GeminiProvider
from .providers.grok import GrokProvider
from .providers.openai import OpenAIProvider
from .runner import generate_card_image, render_card_image, run_job
from .selection import aspect_for_layout
from .tests import make_card, valid_prompt


class ProviderLayoutTests(SimpleTestCase):
    @patch.dict(os.environ, {'XAI_API_KEY': 'test-key'})
    @patch('generation.providers.grok.OpenAI')
    def test_grok_ratios(self, client):
        client.return_value.images.generate.return_value = SimpleNamespace(data=[SimpleNamespace(b64_json='aW1hZ2U=')])
        for ratio in ('16:9', '3:4'):
            with self.subTest(ratio=ratio):
                self.assertEqual(GrokProvider().generate('prompt', ratio), b'image')
                self.assertEqual(client.return_value.images.generate.call_args.kwargs['extra_body']['aspect_ratio'], ratio)

    @patch('generation.providers.gemini.genai.Client')
    def test_gemini_ratios(self, client):
        part = SimpleNamespace(inline_data=SimpleNamespace(data=b'image'))
        client.return_value.models.generate_content.return_value = SimpleNamespace(
            candidates=[SimpleNamespace(content=SimpleNamespace(parts=[part]))])
        for ratio in ('16:9', '3:4'):
            with self.subTest(ratio=ratio):
                self.assertEqual(GeminiProvider().generate('prompt', ratio), b'image')
                config = client.return_value.models.generate_content.call_args.kwargs['config']
                if hasattr(config, 'image_config'):
                    self.assertEqual(config.image_config.aspect_ratio, ratio)
                else:
                    self.assertEqual(config.http_options.extra_body['generationConfig']['imageConfig']['aspectRatio'], ratio)

    def test_pinned_gemini_sdk_serializes_aspect_ratio(self):
        import httpx
        from google import genai
        from google.genai import types

        captured = []
        def handle(request):
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={'candidates': [{'content': {'parts': [
                {'inlineData': {'mimeType': 'image/png', 'data': base64.b64encode(b'image').decode()}}
            ]}}]})
        client = genai.Client(api_key='test-key', http_options=types.HttpOptions(
            client_args={'transport': httpx.MockTransport(handle)}
        ))
        with patch('generation.providers.gemini.genai.Client', return_value=client):
            self.assertEqual(GeminiProvider().generate('prompt', '3:4'), b'image')
        self.assertEqual(captured[0]['generationConfig']['imageConfig']['aspectRatio'], '3:4')
        self.assertEqual(captured[0]['generationConfig']['responseModalities'], ['IMAGE'])

    @patch('generation.providers.openai.OpenAI')
    def test_openai_sizes(self, client):
        client.return_value.images.generate.return_value = SimpleNamespace(data=[SimpleNamespace(b64_json='aW1hZ2U=')])
        for ratio, size in (('16:9', '1536x1024'), ('3:4', '1024x1536')):
            with self.subTest(ratio=ratio):
                self.assertEqual(OpenAIProvider().generate('prompt', ratio), b'image')
                self.assertEqual(client.return_value.images.generate.call_args.kwargs['size'], size)

    def test_invalid_aspect_rejected_before_creating_clients(self):
        for provider in (GrokProvider(), GeminiProvider(), OpenAIProvider()):
            for ratio in ('1:1', '', None):
                with self.subTest(provider=provider.name, ratio=ratio), self.assertRaises(ValueError):
                    provider.generate('prompt', ratio)
        self.assertEqual(aspect_for_layout('classic'), '16:9')
        self.assertEqual(aspect_for_layout('borderless'), '3:4')
        with self.assertRaises(ValueError):
            aspect_for_layout('unknown')

    @patch('generation.claude_client.Anthropic')
    def test_prompt_composition_and_layout_input(self, anthropic):
        anthropic.return_value.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type='text', text=json.dumps(valid_prompt()))], stop_reason='end_turn')
        from decks.models import Card
        card = Card(name='Card')
        for layout in ('classic', 'borderless'):
            build_visual_prompt(card, 'Warcraft', layout=layout)
            args = anthropic.return_value.messages.create.call_args.kwargs
            self.assertEqual(json.loads(args['messages'][0]['content'][0]['text'])['layout'], layout)
            if layout == 'borderless':
                self.assertIn('VERTICAL 3:4', args['system'])
                self.assertIn('40% inferior', args['system'])
                self.assertNotIn(CLASSIC_COMPOSITION, args['system'])
            else:
                self.assertIn(CLASSIC_COMPOSITION, args['system'])
        self.assertIn('Troll de Warcraft (no orco)', SYSTEM_PROMPT)
        self.assertIn('Arthas príncipe vs. Rey Exánime', SYSTEM_PROMPT)
        self.assertEqual(PROMPT_VERSION, 'v3')

    @patch('generation.runner.get_fallback_provider')
    @patch('generation.runner.build_visual_prompt', return_value=valid_prompt())
    def test_all_attempts_use_same_vertical_ratio(self, build, fallback):
        stream = BytesIO()
        Image.new('RGB', (3, 4)).save(stream, format='PNG')
        primary, backup = Mock(), Mock()
        primary.name, backup.name = 'grok', 'gemini'
        primary.generate.side_effect = [RuntimeError('full failed'), RuntimeError('fallback failed')]
        backup.generate.return_value = stream.getvalue()
        fallback.return_value = backup
        card = SimpleNamespace(name='Card')
        with self.assertLogs('generation.runner', level='WARNING'):
            result, _ = render_card_image(card, 'theme', primary, layout='borderless')
        self.assertEqual(primary.generate.call_count, 2)
        self.assertTrue(all(call.kwargs['aspect_ratio'] == '3:4' for call in primary.generate.call_args_list))
        backup.generate.assert_called_once_with(valid_prompt()['fallback_prompt'], aspect_ratio='3:4', references=[])
        build.assert_called_once_with(card, 'theme', None, None, 'borderless', reference_count=0)
        self.assertEqual(result['used_provider'], 'gemini')


@patch.dict(os.environ, {'IMAGE_PROVIDER': 'gemini', 'IMAGE_FALLBACK_PROVIDER': ''})
class DeckLayoutTests(TestCase):
    def setUp(self):
        self.deck = Deck.objects.create(name='Deck', theme='theme', raw_list='Card', card_layout='classic')
        self.card = make_card()
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Card', quantity=1, section='main')
        self.url = f'/api/decks/{self.deck.pk}/'

    def test_cache_compatibility_and_switching_back(self):
        key = make_cache_key(self.card.oracle_id, self.deck.theme, 'gemini')
        self.assertEqual(key, hashlib.sha256(f'{self.card.oracle_id}|theme|v3|gemini'.encode()).hexdigest())
        self.assertEqual(key, make_cache_key(self.card.oracle_id, self.deck.theme, 'gemini', layout='classic'))
        self.assertNotEqual(key, make_cache_key(self.card.oracle_id, self.deck.theme, 'gemini', layout='borderless'))
        GeneratedImage.objects.create(cache_key=key, card=self.card, theme=self.deck.theme, visual_prompt='',
                                      prompt_json={}, image='generated/mock.png', provider='gemini', prompt_version='v3')
        result = self.client.patch(self.url, {'card_layout': 'borderless'}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['card_layout'], 'borderless')
        self.assertIsNone(result.json()['cards'][0]['generated_image'])
        self.assertEqual(self.client.get(self.url + 'estimate/').json()['to_generate'], 1)
        result = self.client.patch(self.url, {'card_layout': 'classic'}, content_type='application/json')
        self.assertIsNotNone(result.json()['cards'][0]['generated_image'])
        self.assertEqual(self.client.get(self.url + 'estimate/').json()['cached'], 1)
        self.assertEqual(self.client.get('/api/decks/').json()[0]['card_layout'], 'classic')

    def test_layout_choices_and_job_conflict(self):
        for body in ({'name': 'new'}, {'card_layout': 'borderless', 'theme': 'new'}):
            result = self.client.patch(self.url, body, content_type='application/json')
            self.assertEqual(result.status_code, 400)
            self.assertEqual(result.json(), {'detail': 'Solo se puede modificar card_layout.'})
        for body in ({'card_layout': 'unknown'}, {'card_layout': None}, {}):
            self.assertEqual(self.client.patch(self.url, body, content_type='application/json').status_code, 400)
        GenerationJob.objects.create(deck=self.deck, total_cards=1)
        self.assertEqual(self.client.patch(self.url, {'card_layout': 'borderless'}, content_type='application/json').status_code, 409)
        self.deck.refresh_from_db()
        self.assertEqual(self.deck.card_layout, 'classic')

    @patch('decks.views.resolve_cards')
    def test_create_accepts_optional_layout_and_defaults_borderless(self, resolve):
        resolve.return_value = {'resolved': [self.card]}
        for value in (None, 'classic', 'borderless'):
            body = {'name': 'Deck', 'raw_list': '1 Card', 'theme': 'theme'}
            if value:
                body['card_layout'] = value
            result = self.client.post('/api/decks/', body, content_type='application/json')
            self.assertEqual(result.status_code, 201)
            self.assertEqual(result.json()['card_layout'], value or 'borderless')
        result = self.client.post('/api/decks/', {'name': 'Deck', 'raw_list': '1 Card', 'theme': 'theme', 'card_layout': 'invalid'}, content_type='application/json')
        self.assertEqual(result.status_code, 400)

    @patch('generation.runner.connection.close')
    @patch('generation.runner.get_provider')
    @patch('generation.runner.generate_card_image')
    def test_job_passes_deck_layout(self, generate, provider, close):
        self.deck.card_layout = 'borderless'
        self.deck.save()
        ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id, element='Element', category='', visual_brief='Brief')
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1, card_oracle_ids=[str(self.card.oracle_id)])
        run_job(job.pk)
        self.assertEqual(generate.call_args.args[5], 'borderless')

    @patch('generation.views.get_provider')
    @patch('generation.views.render_card_image')
    @patch('generation.views.save_card_image')
    def test_regeneration_uses_layout_for_render_save_and_response(self, save, render, provider):
        self.deck.card_layout = 'borderless'
        self.deck.save()
        provider.return_value.name = 'gemini'
        render.return_value = ({}, b'bytes')
        save.return_value = GeneratedImage(card=self.card, theme='theme', image='generated/mock.png', prompt_json={})
        result = self.client.post(f'/api/cards/{self.entry.id}/regenerate/', {}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(render.call_args.args[5], 'borderless')
        self.assertEqual(save.call_args.args[7], 'borderless')
        self.assertIsNotNone(result.json()['generated_image'])

    @patch('generation.runner.build_visual_prompt', return_value=valid_prompt())
    def test_generated_borderless_image_is_cached_under_layout_key(self, build):
        stream = BytesIO()
        Image.new('RGB', (3, 4)).save(stream, format='PNG')
        provider = Mock()
        provider.name = 'gemini'
        provider.generate.return_value = stream.getvalue()
        with tempfile.TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            image = generate_card_image(self.card, 'theme', provider, layout='borderless')
            self.assertEqual(image.cache_key, make_cache_key(self.card.oracle_id, 'theme', 'gemini', layout='borderless'))
            provider.generate.assert_called_once_with(valid_prompt()['full_prompt'], aspect_ratio='3:4', references=[])
            self.deck.card_layout = 'borderless'
            self.deck.save()
            self.assertIsNotNone(self.client.get(self.url).json()['cards'][0]['generated_image'])
            self.assertEqual(self.client.get(self.url + 'estimate/').json()['cached'], 1)
            with patch('generation.views.start_job') as start:
                self.assertEqual(self.client.post(self.url + 'generate/', {}, content_type='application/json').status_code, 400)
                start.assert_not_called()


class LayoutMigrationTests(TransactionTestCase):
    def test_existing_decks_classic_and_new_decks_borderless(self):
        before = [('decks', '0004_deck_theme_answers_deck_theme_answers_updated_at_and_more'),
                  ('generation', '0003_generationjob_card_oracle_ids_themeassignment')]
        after = [('decks', '0005_deck_card_layout'), ('generation', '0005_referenceimage_origin_and_more')]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        try:
            old_deck = executor.loader.project_state(before).apps.get_model('decks', 'Deck').objects.create(
                name='Existing', theme='theme', raw_list='')
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            DeckModel = executor.loader.project_state(after).apps.get_model('decks', 'Deck')
            self.assertEqual(DeckModel.objects.get(pk=old_deck.pk).card_layout, 'classic')
            self.assertEqual(DeckModel.objects.create(name='New', theme='theme', raw_list='').card_layout, 'borderless')
        finally:
            MigrationExecutor(connection).migrate(after)
