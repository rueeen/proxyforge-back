import importlib
import json
import os
import tempfile
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch
from uuid import uuid4

import httpx
from anthropic import BadRequestError
from PIL import Image
from django.apps import apps
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from decks.models import Card, Deck, DeckCard
from scryfall.client import _save_card
from .jobs import expire_stale_jobs
from .models import GeneratedImage, GenerationJob
from .prompt_builder import SYSTEM_PROMPT, PROMPT_VERSION, _clean_json, build_visual_prompt, make_cache_key
from .runner import generate_card_image, render_card_image


def valid_prompt():
    return dict(subject="árbol", action="crece", setting="bosque", palette=["#FFFFFF"],
                style_notes="acuarela", composition="horizontal", full_prompt="Un árbol crece", fallback_prompt="Un árbol crece sin nombres",
                concept_source="crecimiento", source_details=[], themed_name="Árbol ancestral", themed_flavor="La vida retorna.",
                frame_style=dict(border="#112233", background="#334455", text_box="#FFFFFF",
                                 accent="#008800", texture="papel"))


def make_card(**extra):
    data = dict(scryfall_id=uuid4(), oracle_id=uuid4(), name="Tree", cmc=1,
                scryfall_payload={}, type_line="Creature", rarity="common", set_code="abc",
                collector_number="1", layout="normal")
    data.update(extra)
    return Card.objects.create(**data)


class PromptTests(SimpleTestCase):
    def test_validation_and_truncation(self):
        data = valid_prompt()
        self.assertEqual(_clean_json(json.dumps(data)), data)
        data['themed_name'] = 'a' * 40
        data['themed_flavor'] = 'b' * 150
        cleaned = _clean_json(json.dumps(data))
        self.assertEqual(len(cleaned['themed_name']), 30)
        self.assertEqual(len(cleaned['themed_flavor']), 140)
        for frame in ({}, {**data['frame_style'], 'accent': '#GGGGGG'}):
            with self.subTest(frame=frame), self.assertRaises(ValueError):
                _clean_json(json.dumps({**data, 'frame_style': frame}))

    def test_literal_theme_prompt_and_cache_version(self):
        self.assertNotIn("No nombres franquicias", SYSTEM_PROMPT)
        self.assertNotIn("No copies la composición", SYSTEM_PROMPT)
        self.assertIn("Nunca reescribas el coste de maná", SYSTEM_PROMPT)
        self.assertIn("nunca texto, letras, números, logos", SYSTEM_PROMPT)
        self.assertEqual(PROMPT_VERSION, "v3")

    def test_fallback_prompt_required_and_nonempty(self):
        data = valid_prompt()
        del data["fallback_prompt"]
        with self.assertRaisesRegex(ValueError, "fallback_prompt"):
            _clean_json(json.dumps(data))
        for value in (None, 123, "", "   "):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "fallback_prompt"):
                _clean_json(json.dumps({**valid_prompt(), "fallback_prompt": value}))

    @patch("generation.claude_client.Anthropic")
    def test_missing_fallback_retries_claude(self, anthropic):
        data = valid_prompt()
        del data["fallback_prompt"]
        create = anthropic.return_value.messages.create
        create.side_effect = [
            SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(data))]),
            SimpleNamespace(content=[SimpleNamespace(type="text", text=json.dumps(valid_prompt()))]),
        ]
        self.assertEqual(build_visual_prompt(Card(name="Tree"), "tema"), valid_prompt())
        self.assertEqual(create.call_count, 2)

    @patch('generation.claude_client.Anthropic')
    def test_image_and_download_fallback(self, anthropic):
        card = Card(name="Tree", original_art_url="https://example.com/art.jpg")
        response = SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(valid_prompt()))])
        create = anthropic.return_value.messages.create
        create.return_value = response
        build_visual_prompt(card, 'botánica')
        content = create.call_args.kwargs['messages'][0]['content']
        self.assertEqual(content[0], {'type': 'image', 'source': {'type': 'url', 'url': card.original_art_url}})
        self.assertIn('keywords', json.loads(content[1]['text']))
        error = BadRequestError('Could not download image URL', response=httpx.Response(
            400, request=httpx.Request('POST', 'https://api.anthropic.com')), body=None)
        create.reset_mock()
        create.side_effect = [error, response]
        self.assertEqual(build_visual_prompt(card, 'botánica'), valid_prompt())
        self.assertEqual(create.call_count, 2)
        self.assertEqual([b['type'] for b in create.call_args.kwargs['messages'][0]['content']], ['text'])

    @patch('generation.claude_client.Anthropic')
    def test_invalid_json_retries(self, anthropic):
        anthropic.return_value.messages.create.side_effect = [
            SimpleNamespace(content=[SimpleNamespace(type='text', text='{}')]),
            SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(valid_prompt()))]),
        ]
        self.assertEqual(build_visual_prompt(Card(name='Tree'), 'tema'), valid_prompt())


@patch.dict(os.environ, {"IMAGE_FALLBACK_PROVIDER": ""})
class RenderTests(SimpleTestCase):
    def setUp(self):
        self.card = Card(name="Tree")
        self.provider = Mock()
        self.provider.name = "gemini"
        stream = BytesIO()
        Image.new("RGB", (2, 2)).save(stream, format="PNG")
        self.image_bytes = stream.getvalue()

    @patch("generation.runner.build_visual_prompt", side_effect=lambda *args, **kwargs: valid_prompt())
    def test_full_prompt_success(self, build):
        self.provider.generate.return_value = self.image_bytes
        prompt, image = render_card_image(self.card, "tema", self.provider)
        self.assertEqual(prompt["used_prompt"], "full")
        self.assertEqual(image, self.image_bytes)
        self.provider.generate.assert_called_once_with(valid_prompt()["full_prompt"], aspect_ratio="16:9", references=[], resolution=None)
        build.assert_called_once_with(self.card, "tema", None, None, "classic", reference_count=0, original_art_reference=False)

    @patch("generation.runner.build_visual_prompt", side_effect=lambda *args, **kwargs: valid_prompt())
    def test_full_error_or_invalid_image_uses_fallback_once(self, build):
        for first_result in (RuntimeError("policy rejected"), b"not an image"):
            with self.subTest(first_result=first_result):
                self.provider.generate.reset_mock()
                self.provider.generate.side_effect = [first_result, self.image_bytes]
                with self.assertLogs("generation.runner", level="WARNING"):
                    prompt, image = render_card_image(self.card, "tema", self.provider)
                self.assertEqual(prompt["used_prompt"], "fallback")
                self.assertEqual(image, self.image_bytes)
                self.assertEqual(self.provider.generate.call_args_list, [
                    ((valid_prompt()["full_prompt"],), {"aspect_ratio": "16:9", "references": [], "resolution": None}),
                    ((valid_prompt()["fallback_prompt"],), {"aspect_ratio": "16:9", "references": [], "resolution": None}),
                ])

    @patch("generation.runner.build_visual_prompt", side_effect=lambda *args, **kwargs: valid_prompt())
    def test_both_failures_are_reported(self, build):
        second_error = RuntimeError("fallback unavailable")
        self.provider.generate.side_effect = [RuntimeError("full rejected"), second_error]
        with self.assertLogs("generation.runner", level="WARNING"):
            with self.assertRaisesRegex(RuntimeError, "full rejected.*fallback unavailable") as caught:
                render_card_image(self.card, "tema", self.provider)
        self.assertIs(caught.exception.__cause__, second_error)
        self.assertEqual(self.provider.generate.call_count, 2)

    @patch("generation.runner.build_visual_prompt", side_effect=lambda *args, **kwargs: valid_prompt())
    def test_invalid_fallback_is_not_returned(self, build):
        self.provider.generate.side_effect = [RuntimeError("full rejected"), b"invalid fallback"]
        with self.assertLogs("generation.runner", level="WARNING"):
            with self.assertRaisesRegex(RuntimeError, "full rejected.*gemini/fallback"):
                render_card_image(self.card, "tema", self.provider)
        self.assertEqual(self.provider.generate.call_count, 2)


@patch.dict(os.environ, {"IMAGE_FALLBACK_PROVIDER": ""})
@override_settings(IMAGE_PROVIDER='gemini')
class ContractTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        settings = override_settings(MEDIA_ROOT=self.directory.name)
        settings.enable()
        self.addCleanup(settings.disable)
        self.deck = Deck.objects.create(card_layout="classic", name='Deck', raw_list='Tree', theme='botánica')
        self.card = make_card()
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Tree', quantity=4, section='main')
        self.provider = Mock(name='provider')
        self.provider.name = 'gemini'
        stream = BytesIO()
        Image.new('RGB', (2, 2)).save(stream, format='JPEG')
        self.provider.generate.return_value = stream.getvalue()

    def generate(self):
        with patch('generation.runner.build_visual_prompt', return_value=valid_prompt()):
            return generate_card_image(self.card, self.deck.theme, self.provider)

    def test_detail_constant_queries_and_contract(self):
        image = self.generate()
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1)
        url = f'/api/decks/{self.deck.pk}/'
        with self.assertNumQueries(7):
            response = self.client.get(url)
        data = response.json()
        self.assertEqual(data['active_job']['id'], job.id)
        self.assertEqual(data['card_count'], 4)
        summary = data['cards'][0]['generated_image']
        self.assertEqual(set(summary), {'id', 'image_url', 'theme', 'concept_source', 'themed_name', 'themed_flavor', 'frame_style', 'created_at', 'framing_offset_y', 'framing_zoom', 'framing_source', 'face_center_y', 'framing_offset_x', 'width', 'height', 'face_center_x', 'name_limit'})
        self.assertEqual(summary['themed_name'], valid_prompt()['themed_name'])
        self.assertEqual(data['cards'][0]['card']['power'], '')
        for _ in range(10):
            DeckCard.objects.create(deck=self.deck, card=make_card(), raw_name='Other', quantity=1, section='main')
        with self.assertNumQueries(7):
            response = self.client.get(url)
        self.assertEqual(response.json()['card_count'], 14)
        self.assertIsNone(response.json()['cards'][-1]['generated_image'])
        image.prompt_json = {}
        image.save()
        summary = self.client.get(url).json()['cards'][0]['generated_image']
        self.assertEqual(summary['themed_name'], '')
        self.assertIsNone(summary['frame_style'])

    @patch('generation.views.get_provider')
    @patch('generation.runner.build_visual_prompt', return_value=valid_prompt())
    def test_regenerate_and_unique_real_format(self, prompt, provider):
        old = self.generate()
        old_path = old.image.path
        provider.return_value = self.provider
        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(f'/api/cards/{self.entry.pk}/regenerate/', data={}, content_type='application/json')
            from pathlib import Path
            self.assertTrue(Path(old_path).exists())
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data['id'], self.entry.id)
        self.assertFalse(GeneratedImage.objects.filter(pk=old.pk).exists())
        self.assertEqual(GeneratedImage.objects.count(), 1)
        new = GeneratedImage.objects.get()
        self.assertEqual(new.visual_prompt, valid_prompt()['full_prompt'])
        self.assertEqual(new.prompt_json['used_prompt'], 'full')
        self.assertIsInstance(data['card'], dict)
        self.assertNotEqual(data['generated_image']['image_url'], old.image.url)
        self.assertTrue(data['generated_image']['image_url'].endswith('.jpg'))
        from pathlib import Path
        self.assertFalse(Path(old_path).exists())
        self.assertEqual(data['generated_image']['theme'], self.deck.theme)

    def test_cache_hit_does_not_render(self):
        existing = self.generate()
        with patch("generation.runner.render_card_image") as render:
            cached = generate_card_image(self.card, self.deck.theme, self.provider)
        self.assertEqual(cached.pk, existing.pk)
        render.assert_not_called()

    @patch("generation.runner.build_visual_prompt", side_effect=lambda *args, **kwargs: valid_prompt())
    def test_saved_visual_prompt_matches_fallback(self, build):
        image_bytes = self.provider.generate.return_value
        self.provider.generate.side_effect = [RuntimeError("full rejected"), image_bytes]
        with self.assertLogs("generation.runner", level="WARNING"):
            generated = generate_card_image(self.card, self.deck.theme, self.provider)
        generated.refresh_from_db()
        self.assertEqual(generated.visual_prompt, valid_prompt()["fallback_prompt"])
        self.assertEqual(generated.prompt_json["used_prompt"], "fallback")

    @patch("generation.views.get_provider")
    @patch("generation.views.save_card_image", side_effect=RuntimeError("save failed"))
    @patch("generation.views.render_card_image")
    def test_failed_save_rolls_back_old_record_and_keeps_file(self, render, save, provider):
        old = self.generate()
        old_name = old.image.name
        provider.return_value = self.provider
        render.return_value = ({**valid_prompt(), "used_prompt": "full", "used_provider": "gemini"}, self.provider.generate.return_value)
        with self.captureOnCommitCallbacks(execute=True) as callbacks:
            response = self.client.post(f"/api/cards/{self.entry.pk}/regenerate/", data={}, content_type="application/json")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(callbacks, [])
        old.refresh_from_db()
        self.assertEqual(old.image.name, old_name)
        self.assertTrue(old.image.storage.exists(old_name))

    def test_regenerate_conflict(self):
        GenerationJob.objects.create(deck=self.deck, total_cards=1)
        with patch('generation.views.get_provider') as provider:
            response = self.client.post(f'/api/cards/{self.entry.pk}/regenerate/', data={}, content_type='application/json')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json(), {'detail': 'Hay una generación en curso para este mazo.'})
        provider.assert_not_called()

    @patch('generation.views.render_card_image', side_effect=RuntimeError('provider failed'))
    def test_regenerate_failure(self, generate):
        old = self.generate()
        old_name = old.image.name
        old_bytes = old.image.read()
        old.image.close()
        response = self.client.post(f'/api/cards/{self.entry.pk}/regenerate/', data={}, content_type='application/json')
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json(), {'detail': 'provider failed'})
        old.refresh_from_db()
        self.assertEqual(old.image.name, old_name)
        self.assertTrue(old.image.storage.exists(old_name))
        with old.image.open('rb') as image:
            self.assertEqual(image.read(), old_bytes)

    def test_expire_stale_jobs_and_detail(self):
        stale = GenerationJob.objects.create(deck=self.deck, total_cards=1, status='running')
        recent = GenerationJob.objects.create(deck=self.deck, total_cards=1)
        GenerationJob.objects.filter(pk=stale.pk).update(updated_at=timezone.now()-timedelta(minutes=11))
        self.assertEqual(expire_stale_jobs(self.deck), 1)
        stale.refresh_from_db()
        recent.refresh_from_db()
        self.assertEqual(stale.status, 'failed')
        self.assertEqual(stale.error_message, 'Job interrumpido (el servidor se reinició).')
        self.assertIsNotNone(stale.finished_at)
        self.assertEqual(recent.status, 'pending')

    def test_estimate_contract(self):
        self.generate()
        self.assertEqual(self.client.get(f'/api/decks/{self.deck.pk}/estimate/').json(), {
            'unique_cards': 1, 'cached': 1, 'to_generate': 0, 'estimated_cost_usd': 0.0, 'batch_limit': 5, 'batch_size': 0, 'batch_cost_usd': 0.0, 'reference_cost_usd': 0.0})

    def test_backfill_and_scryfall_faces(self):
        payload = {'power': '3', 'toughness': '4', 'keywords': ['Flying']}
        self.card.scryfall_payload = payload
        self.card.save()
        migration = importlib.import_module('decks.migrations.0003_backfill_card_data')
        migration.backfill_cards(apps, SimpleNamespace(connection=connection))
        self.card.refresh_from_db()
        self.assertEqual((self.card.power, self.card.toughness, self.card.keywords), ('3', '4', ['Flying']))
        payload.update(id=str(uuid4()), oracle_id=str(uuid4()), name='Faces', set='abc', collector_number='2',
                       card_faces=[{'name': 'Front', 'power': '2', 'toughness': '1'}])
        del payload['power']
        del payload['toughness']
        card = _save_card(payload)
        self.assertEqual((card.power, card.toughness), ('2', '1'))
        self.assertEqual(card.card_faces[0]['loyalty'], '')


from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase


class BackfillMigrationTests(TransactionTestCase):
    def test_existing_rows_are_backfilled_by_migration(self):
        executor = MigrationExecutor(connection)
        before = [('decks', '0001_initial'), ('generation', '0001_initial')]
        after = [('decks', '0008_deck_original_art_mode'), ('generation', '0011_generatedimage_face_center_x_and_more')]
        executor.migrate(before)
        try:
            old_apps = executor.loader.project_state(before).apps
            old_card = old_apps.get_model('decks', 'Card').objects.create(
                scryfall_id=uuid4(), oracle_id=uuid4(), name='Old creature', cmc=2,
                scryfall_payload={'power': '5', 'toughness': '6', 'keywords': ['Trample']},
                type_line='Creature', rarity='common', set_code='abc', collector_number='9', layout='normal')
            executor = MigrationExecutor(connection)
            executor.migrate(after)
            card = executor.loader.project_state(after).apps.get_model('decks', 'Card').objects.get(pk=old_card.pk)
            self.assertEqual((card.power, card.toughness, card.keywords, card.card_faces), ('5', '6', ['Trample'], []))
        finally:
            MigrationExecutor(connection).migrate(after)
