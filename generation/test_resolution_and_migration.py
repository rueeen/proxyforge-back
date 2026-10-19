import base64
import os
import tempfile
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from django.core.files.base import ContentFile
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TestCase, TransactionTestCase

from decks.models import Deck, DeckCard
from .models import GeneratedImage, GenerationJob, ThemeAssignment
from .providers.grok import GrokProvider
from .runner import render_card_image, run_job
from .test_references import png
from .test_references import api_response
from .tests import make_card, valid_prompt


@patch.dict(os.environ, {'IMAGE_PROVIDER': 'grok', 'IMAGE_FALLBACK_PROVIDER': '', 'XAI_API_KEY': 'test', 'XAI_IMAGE_RESOLUTION': '1k'})
class ResolutionTests(TestCase):
    def setUp(self):
        self.deck = Deck.objects.create(name='Deck', theme='theme', raw_list='', image_resolution='2k')
        self.card = make_card()
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Card', quantity=1, section='main')

    @patch('generation.providers.grok.OpenAI')
    @patch('generation.runner.build_visual_prompt', return_value=valid_prompt())
    def test_render_passes_resolution_and_edits_prioritize_it_over_env(self, prompt, sdk):
        sdk.return_value.images.generate.return_value = SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(png()).decode())])
        render_card_image(self.card, 'theme', GrokProvider(), resolution=self.deck.image_resolution)
        self.assertEqual(sdk.return_value.images.generate.call_args.kwargs['extra_body']['resolution'], '2k')
        with patch('generation.providers.grok.requests.post', return_value=api_response({'data': [{'b64_json': base64.b64encode(png()).decode()}]})) as post:
            GrokProvider().generate('scene', references=[b'jpeg'], resolution='2k')
            self.assertEqual(post.call_args.kwargs['json']['resolution'], '2k')

    @patch('generation.runner.connection.close')
    @patch('generation.runner.generate_card_image')
    @patch('generation.runner.get_provider')
    def test_image_job_passes_deck_resolution(self, provider, generate, close):
        ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id, element='Subject')
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1, card_oracle_ids=[str(self.card.oracle_id)])
        run_job(job.pk)
        self.assertEqual(generate.call_args.kwargs['resolution'], '2k')

    @patch('generation.views.render_card_image')
    @patch('generation.views.get_provider')
    def test_regenerate_passes_resolution(self, provider, render):
        provider.return_value.name = 'grok'
        render.side_effect = RuntimeError('Stop before saving')
        result = self.client.post(f'/api/cards/{self.entry.pk}/regenerate/', {}, content_type='application/json')
        self.assertEqual(result.status_code, 502)
        self.assertEqual(render.call_args.kwargs['resolution'], '2k')

    def test_patch_resolution_cache_unchanged_and_estimate(self):
        from .prompt_builder import make_cache_key
        key = make_cache_key(self.card.oracle_id, self.deck.theme, 'grok', layout=self.deck.card_layout)
        image = GeneratedImage.objects.create(card=self.card, cache_key=key, theme=self.deck.theme, visual_prompt='', prompt_json={}, image='generated/kept.jpg', provider='grok', prompt_version='v3')
        base = f'/api/decks/{self.deck.pk}/'
        for value in ('1k', '2k'):
            result = self.client.patch(base, {'image_resolution': value}, content_type='application/json')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['image_resolution'], value)
            self.assertEqual(result.json()['cards'][0]['generated_image']['id'], image.pk)
        self.assertEqual(self.client.patch(base, {'image_resolution': '4k'}, content_type='application/json').status_code, 400)
        image.delete()
        with patch.dict(os.environ, {'COST_PER_IMAGE_USD': '0.04', 'COST_2K_EXTRA_USD': '0.02'}):
            result = self.client.get(base + 'estimate/').json()
            self.assertEqual((result['estimated_cost_usd'], result['batch_cost_usd']), (0.06, 0.06))
            self.client.patch(base, {'image_resolution': '1k'}, content_type='application/json')
            self.assertEqual(self.client.get(base + 'estimate/').json()['estimated_cost_usd'], 0.04)

    def test_new_deck_uses_environment_default(self):
        with patch.dict(os.environ, {'XAI_IMAGE_RESOLUTION': '2k'}):
            self.assertEqual(Deck().image_resolution, '2k')


class FreeFramingMigrationTests(TransactionTestCase):
    def test_existing_framing_dimensions_missing_files_and_deck_resolution(self):
        before = [('decks', '0006_deck_auto_reference_count_deck_reference_wiki_and_more'),
                  ('generation', '0010_generationjob_kind_generationjob_options_and_more')]
        after = [('decks', '0008_deck_original_art_mode'), ('generation', '0011_generatedimage_face_center_x_and_more')]
        executor = MigrationExecutor(connection)
        executor.migrate(before)
        try:
            apps = executor.loader.project_state(before).apps
            Card = apps.get_model('decks', 'Card')
            from uuid import uuid4
            card = Card.objects.create(scryfall_id=uuid4(), oracle_id=uuid4(), name='Card', cmc=1, type_line='Creature', rarity='common', set_code='abc', collector_number='1', layout='normal', scryfall_payload={})
            deck = apps.get_model('decks', 'Deck').objects.create(name='Deck', theme='theme', raw_list='')
            OldImage = apps.get_model('generation', 'GeneratedImage')
            with tempfile.TemporaryDirectory() as directory, self.settings(MEDIA_ROOT=directory):
                images = []
                for key, offset, zoom in (('a', 22, 100), ('b', 10, 120), ('c', 50, 150)):
                    item = OldImage(card=card, cache_key=key, theme='theme', visual_prompt='', prompt_json={}, provider='grok', prompt_version='v3', framing_offset_y=offset, framing_zoom=zoom)
                    item.image.save(key + '.png', ContentFile(png(size=(864, 1152))))
                    images.append(item)
                absent = OldImage.objects.create(card=card, cache_key='missing', theme='theme', visual_prompt='', prompt_json={}, provider='grok', prompt_version='v3', image='absent.png')
                executor = MigrationExecutor(connection)
                with patch.dict(os.environ, {'XAI_IMAGE_RESOLUTION': '2k'}):
                    executor.migrate(after)
                apps = executor.loader.project_state(after).apps
                ImageModel = apps.get_model('generation', 'GeneratedImage')
                actual = [ImageModel.objects.get(pk=item.pk) for item in images]
                self.assertEqual([(item.framing_offset_x, item.framing_offset_y, item.framing_zoom) for item in actual], [(0, 22, 100), (0, 20, 120), (0, 60, 150)])
                self.assertTrue(all((item.width, item.height) == (864, 1152) for item in actual))
                missing = ImageModel.objects.get(pk=absent.pk)
                self.assertEqual((missing.width, missing.height), (None, None))
                self.assertEqual(apps.get_model('decks', 'Deck').objects.get(pk=deck.pk).image_resolution, '2k')
        finally:
            MigrationExecutor(connection).migrate(after)
