import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.core.files.base import ContentFile
from django.test import TestCase, SimpleTestCase

from decks.models import Card, Deck, DeckCard
from scryfall.client import HEADERS
from .card_text import themed_name_limit, shorten_themed_name
from .models import GeneratedImage, ReferenceImage, ThemeAssignment
from .original_art import get_original_art
from .prompt_builder import _clean_json, build_visual_prompt, make_cache_key, card_cache_key, PROMPT_VERSION
from .references import normalize_reference
from .runner import generate_card_image, render_card_image
from .test_references import png
from .test_wiki_sites import claude_response
from .tests import make_card, valid_prompt


class OriginalArtDownloadTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        override = self.settings(MEDIA_ROOT=self.directory.name)
        override.enable()
        self.addCleanup(override.disable)
        self.card = make_card(original_art_url='https://cards.scryfall.io/art_crop/front/a/b/card.jpg')

    @patch('generation.original_art.requests.get')
    def test_disk_cache_normalized_jpeg_and_headers(self, get):
        get.return_value = Mock(content=png())
        first = get_original_art(self.card)
        self.assertEqual(first, normalize_reference(png())[0])
        path = Path(self.directory.name) / 'cache' / 'original_art' / f'{self.card.scryfall_id}.jpg'
        self.assertEqual(path.read_bytes(), first)
        self.assertEqual(get_original_art(self.card), first)
        get.assert_called_once_with(self.card.original_art_url, timeout=20, headers=HEADERS)

    @patch('generation.original_art.requests.get')
    def test_download_and_storage_errors_do_not_escape(self, get):
        get.side_effect = requests.Timeout('Scryfall offline')
        with self.assertLogs('generation.original_art', level='WARNING'):
            self.assertIsNone(get_original_art(self.card))
        get.side_effect = None
        get.return_value = Mock(content=b'invalid image')
        with self.assertLogs('generation.original_art', level='WARNING'):
            self.assertIsNone(get_original_art(self.card))
        get.return_value = Mock(content=png())
        with patch('generation.original_art.Path.mkdir', side_effect=OSError('Disk unavailable')), self.assertLogs('generation.original_art', level='WARNING'):
            self.assertIsNone(get_original_art(self.card))
        self.card.original_art_url = ''
        get.reset_mock()
        self.assertIsNone(get_original_art(self.card))
        get.assert_not_called()


class ThemedNameTests(SimpleTestCase):
    def test_name_limits_first_face_hybrid_and_floor(self):
        for cost, expected in (('{W}{U}{B}{R}{G}', 18), ('{2}{W/U}', 24), ('', 28), ('{G}' * 20, 14)):
            self.assertEqual(themed_name_limit(Card(mana_cost=cost)), expected)
        self.assertEqual(themed_name_limit(Card(mana_cost='{G}', card_faces=[{'mana_cost': '{W}{U}{B}{R}{G}'}])), 18)

    def test_clean_names_comma_word_boundary_and_punctuation(self):
        data = valid_prompt()
        data['themed_name'] = 'The First Sliver, Yandere Queen'
        self.assertEqual(_clean_json(json.dumps(data), 18)['themed_name'], 'The First Sliver')
        self.assertEqual(shorten_themed_name('Yandere Hive Queen of Eggs', 18), 'Yandere Hive Queen')
        self.assertEqual(shorten_themed_name('Mother -   Extra long title', 9), 'Mother')

    def test_source_details_optional_validation_clipping_and_version(self):
        data = valid_prompt()
        data.pop('source_details')
        self.assertEqual(_clean_json(json.dumps(data))['source_details'], [])
        data['source_details'] = ['x' * 150] * 9
        self.assertEqual(_clean_json(json.dumps(data))['source_details'], ['x' * 100] * 6)
        for invalid in ('eggs', [None]):
            data['source_details'] = invalid
            with self.assertRaises(ValueError):
                _clean_json(json.dumps(data))
        self.assertEqual(PROMPT_VERSION, 'v3')

    @patch('generation.claude_client.Anthropic')
    def test_prompt_sends_name_limit_original_role_and_source_details(self, sdk):
        payload = valid_prompt()
        payload['themed_name'] = 'The First Sliver, Yandere Queen'
        payload['source_details'] = ['amber eggs', 'orange hive cavern', 'hatching slivers']
        sdk.return_value.messages.create.return_value = claude_response(payload)
        card = Card(name='The First Sliver', mana_cost='{W}{U}{B}{R}{G}', original_art_url='https://cards.scryfall.io/original.jpg')
        result = build_visual_prompt(card, 'anime yandere', reference_count=2, original_art_reference=True)
        call = sdk.return_value.messages.create.call_args.kwargs
        content = call['messages'][0]['content']
        self.assertEqual(content[0]['type'], 'image')
        data = json.loads(content[-1]['text'])
        self.assertEqual((data['max_name_chars'], data['reference_images'], data['original_art_reference']), (18, 2, True))
        self.assertEqual(result['themed_name'], 'The First Sliver')
        self.assertIn('Last reference image: the ORIGINAL card artwork', call['system'])
        self.assertIn('criaturas secundarias', call['system'])
        self.assertIn('max_name_chars', call['system'])
        self.assertIn('fallback_prompt sigue las mismas reglas', call['system'])


@patch.dict(os.environ, {'IMAGE_PROVIDER': 'gemini', 'IMAGE_FALLBACK_PROVIDER': '', 'AUTO_FRAMING': 'false'})
@patch('generation.runner.get_original_art', return_value=b'original-jpeg')
@patch('generation.claude_client.Anthropic')
class OriginalArtModeTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        override = self.settings(MEDIA_ROOT=directory.name)
        override.enable()
        self.addCleanup(override.disable)
        self.deck = Deck.objects.create(name='Deck', theme='Anime', raw_list='')
        self.card = make_card(original_art_url='https://cards.scryfall.io/original.jpg')
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Card', quantity=1, section='main')
        self.provider = Mock(name='provider')
        self.provider.name = 'gemini'
        self.provider.generate.return_value = png()

    def assignment(self):
        item = ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id, element='Character', category='', visual_brief='A scene')
        ref = ReferenceImage(assignment=item, sha256='a' * 64)
        ref.image.save('character.jpg', ContentFile(b'character-jpeg'))
        return item

    def test_auto_without_characters_sends_original_and_key_matches_detail(self, sdk, original):
        sdk.return_value.messages.create.return_value = claude_response(valid_prompt())
        legacy = make_cache_key(self.card.oracle_id, self.deck.theme, 'gemini', layout=self.deck.card_layout)
        expected = hashlib.sha256(f'{self.card.oracle_id}|anime|v3|gemini|layout:borderless|origart:{self.card.scryfall_id}'.encode()).hexdigest()
        image = generate_card_image(self.card, self.deck.theme, self.provider, layout=self.deck.card_layout)
        self.assertNotEqual(image.cache_key, legacy)
        self.assertEqual(image.cache_key, expected)
        self.assertEqual(self.provider.generate.call_args.kwargs['references'], [b'original-jpeg'])
        data = json.loads(sdk.return_value.messages.create.call_args.kwargs['messages'][0]['content'][-1]['text'])
        self.assertTrue(data['original_art_reference'])
        self.assertNotIn('reference_images', data)
        original.reset_mock()
        detail = self.client.get(f'/api/decks/{self.deck.pk}/').json()
        self.assertEqual(detail['cards'][0]['generated_image']['id'], image.pk)
        self.assertEqual(detail['cards'][0]['generated_image']['name_limit'], 28)
        self.assertEqual(self.client.get(f'/api/decks/{self.deck.pk}/estimate/').json()['cached'], 1)
        original.assert_not_called()
        with patch('generation.views.start_job') as start:
            self.assertEqual(self.client.post(f'/api/decks/{self.deck.pk}/generate/', {}, content_type='application/json').status_code, 400)
            start.assert_not_called()
        self.assertEqual(self.client.patch(f'/api/decks/{self.deck.pk}/', {'original_art_mode': 'off'}, content_type='application/json').status_code, 200)
        self.assertIsNone(self.client.get(f'/api/decks/{self.deck.pk}/').json()['cards'][0]['generated_image'])
        self.assertEqual(self.client.get(f'/api/decks/{self.deck.pk}/estimate/').json()['to_generate'], 1)

    def test_auto_with_characters_keeps_legacy_key_and_no_original(self, sdk, original):
        sdk.return_value.messages.create.return_value = claude_response(valid_prompt())
        assignment = self.assignment()
        image = generate_card_image(self.card, self.deck.theme, self.provider, assignment=assignment, layout=self.deck.card_layout)
        original.assert_not_called()
        self.assertEqual(self.provider.generate.call_args.kwargs['references'], [b'character-jpeg'])
        self.assertEqual(image.cache_key, make_cache_key(self.card.oracle_id, self.deck.theme, 'gemini', assignment,
                         layout=self.deck.card_layout, reference_hashes=['a' * 64]))
        data = json.loads(sdk.return_value.messages.create.call_args.kwargs['messages'][0]['content'][-1]['text'])
        self.assertEqual(data['reference_images'], 1)
        self.assertNotIn('original_art_reference', data)

    def test_always_appends_original_after_characters_and_off_keeps_original_for_claude(self, sdk, original):
        sdk.return_value.messages.create.return_value = claude_response(valid_prompt())
        assignment = self.assignment()
        render_card_image(self.card, self.deck.theme, self.provider, assignment, original_art_mode='always')
        self.assertEqual(self.provider.generate.call_args.kwargs['references'], [b'character-jpeg', b'original-jpeg'])
        data = json.loads(sdk.return_value.messages.create.call_args.kwargs['messages'][0]['content'][-1]['text'])
        self.assertEqual(data['reference_images'], 1)
        self.assertTrue(data['original_art_reference'])
        original.reset_mock()
        render_card_image(self.card, self.deck.theme, self.provider, original_art_mode='off')
        original.assert_not_called()
        self.assertEqual(self.provider.generate.call_args.kwargs['references'], [])
        content = sdk.return_value.messages.create.call_args.kwargs['messages'][0]['content']
        self.assertEqual(content[0]['source']['url'], self.card.original_art_url)
        self.assertEqual(card_cache_key(self.card, 'theme', 'gemini', original_art_mode='off'),
                         make_cache_key(self.card.oracle_id, 'theme', 'gemini'))

    def test_failed_original_uses_legacy_key_and_remains_visible_and_cached(self, sdk, original):
        sdk.return_value.messages.create.return_value = claude_response(valid_prompt())
        original.return_value = None
        image = generate_card_image(self.card, self.deck.theme, self.provider, layout=self.deck.card_layout)
        legacy = make_cache_key(self.card.oracle_id, self.deck.theme, 'gemini', layout=self.deck.card_layout)
        self.assertEqual(image.cache_key, legacy)
        self.assertFalse(image.prompt_json['original_art_reference'])
        self.assertEqual(self.client.get(f'/api/decks/{self.deck.pk}/').json()['cards'][0]['generated_image']['id'], image.pk)
        self.assertEqual(self.client.get(f'/api/decks/{self.deck.pk}/estimate/').json()['cached'], 1)
        original.reset_mock()
        self.assertEqual(generate_card_image(self.card, self.deck.theme, self.provider, layout=self.deck.card_layout).pk, image.pk)
        original.assert_not_called()
        # Regeneration can later replace the fallback with a successful original-art variant.
        original.return_value = b'original-jpeg'
        with patch('generation.views.get_provider', return_value=self.provider):
            result = self.client.post(f'/api/cards/{self.entry.pk}/regenerate/', {}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertNotEqual(result.json()['generated_image']['id'], image.pk)
        self.assertFalse(GeneratedImage.objects.filter(pk=image.pk).exists())

    def test_failed_original_download_does_not_fail_render_and_patch_validation(self, sdk, original):
        sdk.return_value.messages.create.return_value = claude_response(valid_prompt())
        original.return_value = None
        prompt, image = render_card_image(self.card, self.deck.theme, self.provider)
        self.assertFalse(prompt['original_art_reference'])
        self.assertEqual(self.provider.generate.call_args.kwargs['references'], [])
        self.assertEqual(image, png())
        for value in ('off', 'auto', 'always'):
            result = self.client.patch(f'/api/decks/{self.deck.pk}/', {'original_art_mode': value}, content_type='application/json')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['original_art_mode'], value)
        self.assertEqual(self.client.patch(f'/api/decks/{self.deck.pk}/', {'original_art_mode': 'invalid'}, content_type='application/json').status_code, 400)


@patch.dict(os.environ, {'IMAGE_PROVIDER': 'gemini'})
class ShortNameRewriteTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        setting = self.settings(MEDIA_ROOT=directory.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.deck = Deck.objects.create(name='Deck', theme='theme', raw_list='')
        self.long = make_card(mana_cost='{W}{U}{B}{R}{G}')
        self.short = make_card(mana_cost='')
        self.images = []
        for card, name in ((self.long, 'The First Sliver, Yandere Queen'), (self.short, 'Hive Mother')):
            DeckCard.objects.create(deck=self.deck, card=card, raw_name='Card', quantity=1, section='main')
            image = GeneratedImage(card=card, cache_key=card_cache_key(card, self.deck.theme, 'gemini', layout=self.deck.card_layout),
                theme=self.deck.theme, visual_prompt='Keep art', provider='gemini', prompt_version='v3',
                prompt_json={'themed_name': name, 'themed_flavor': 'Keep this flavor', 'full_prompt': 'Keep art'})
            image.image.save('art.png', ContentFile(png()))
            self.images.append(image)
        self.url = f'/api/decks/{self.deck.pk}/rewrite-texts/'

    @patch('generation.claude_client.Anthropic')
    def test_only_long_names_filters_input_and_keeps_flavor_and_art(self, sdk):
        sdk.return_value.messages.create.return_value = claude_response({'cards': [{'oracle_id': str(self.long.oracle_id),
            'themed_name': 'The First Sliver, Yandere Queen', 'themed_flavor': 'Do not store this'}]})
        snapshots = [(image.image.name, Path(image.image.path).read_bytes(), image.cache_key) for image in self.images]
        result = self.client.post(self.url, {'only_long_names': True}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['rewrite_summary'], {'updated': 1, 'skipped': 0, 'shortened': 1})
        cards = json.loads(sdk.return_value.messages.create.call_args.kwargs['messages'][0]['content'])['cards']
        self.assertEqual(len(cards), 1)
        self.assertEqual(cards[0]['max_name_chars'], 18)
        self.assertEqual(cards[0]['oracle_id'], str(self.long.oracle_id))
        for image, snapshot in zip(self.images, snapshots):
            image.refresh_from_db()
            self.assertEqual(image.prompt_json['themed_flavor'], 'Keep this flavor')
            self.assertEqual((image.image.name, Path(image.image.path).read_bytes(), image.cache_key), snapshot)
        self.assertEqual(self.images[0].prompt_json['themed_name'], 'The First Sliver')
        self.assertEqual(self.images[1].prompt_json['themed_name'], 'Hive Mother')
        sdk.return_value.messages.create.reset_mock()
        result = self.client.post(self.url, {'only_long_names': True}, content_type='application/json')
        self.assertEqual(result.json()['rewrite_summary']['shortened'], 0)
        sdk.return_value.messages.create.assert_not_called()

    @patch('generation.claude_client.Anthropic')
    def test_name_only_response_can_omit_flavor_and_flag_must_be_boolean(self, sdk):
        sdk.return_value.messages.create.return_value = claude_response({'cards': [{'oracle_id': str(self.long.oracle_id), 'themed_name': 'Sliver Mother'}]})
        self.assertEqual(self.client.post(self.url, {'only_long_names': True}, content_type='application/json').status_code, 200)
        self.images[0].refresh_from_db()
        self.assertEqual(self.images[0].prompt_json['themed_flavor'], 'Keep this flavor')
        self.assertEqual(self.client.post(self.url, {'only_long_names': 'true'}, content_type='application/json').status_code, 400)
