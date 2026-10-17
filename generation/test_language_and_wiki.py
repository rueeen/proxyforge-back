import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import SimpleTestCase, TestCase

from .auto_references import fetch_auto_reference, MANUAL_LIMIT_MESSAGE
from .card_text import card_text_language
from .models import GeneratedImage, ReferenceImage, GenerationJob
from .prompt_builder import build_visual_prompt, make_cache_key
from . import test_auto_references as auto_tests
from .test_references import png
from .tests import make_card, valid_prompt
from .wiki_images import normalize_wiki_reference, LAST_RESULTS
from decks.models import Card, DeckCard


def response(data):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(data))], stop_reason='end_turn')


class LanguageAndNormalizationTests(SimpleTestCase):
    @patch('generation.claude_client.Anthropic')
    def test_card_text_language_default_english_spanish_and_invalid_value(self, anthropic):
        create = anthropic.return_value.messages.create
        create.return_value = response(valid_prompt())
        with patch.dict(os.environ):
            os.environ.pop('CARD_TEXT_LANGUAGE', None)
            self.assertEqual(card_text_language(), 'en')
            for value, expected in (('en', 'INGLÉS'), ('es', 'ESPAÑOL'), ('unknown', 'INGLÉS')):
                os.environ['CARD_TEXT_LANGUAGE'] = value
                build_visual_prompt(Card(name='Card'), 'Warcraft')
                system = create.call_args.kwargs['system']
                self.assertIn('themed_name y themed_flavor se escriben en ' + expected, system)
                self.assertIn('Darkspear, no Lanza Negra', system)
                self.assertNotIn('themed_name y themed_flavor siguen en español', system)

    def test_normalize_urls_domains_and_titles(self):
        cases = [
            ('https://warcraft.wiki.gg/wiki/Vol%27jin', '', ('warcraft.wiki.gg', "Vol'jin")),
            ('', 'https://wowpedia.fandom.com/wiki/Sylvanas_Windrunner#Lore', ('wowpedia.fandom.com', 'Sylvanas Windrunner')),
            ('https://www.Warcraft.wiki.gg/', "Vol%27jin", ('warcraft.wiki.gg', "Vol'jin")),
            ('', 'https://www.warcraft.wiki.gg/index.php?title=Vol%27jin#Lore', ('warcraft.wiki.gg', "Vol'jin")),
            ('WWW.warcraft.wiki.gg/', '  Sylvanas__Windrunner  #Lore', ('warcraft.wiki.gg', 'Sylvanas Windrunner')),
        ]
        for wiki, page, expected in cases:
            with self.subTest(wiki=wiki, page=page):
                self.assertEqual(normalize_wiki_reference(wiki, page), expected)
        with self.assertRaisesRegex(ValueError, 'Dominio de wiki inválido. Ejemplo: warcraft.wiki.gg'):
            normalize_wiki_reference('not a domain', 'Page')


# Reuse only the fixture setup; tests from the original class remain independently covered.
@patch.dict(os.environ, {'AUTO_REFERENCE_VERIFY': 'false', 'IMAGE_PROVIDER': 'gemini'})
class ReliableWikiAndRewriteTests(TestCase):
    setUp = auto_tests.AutoReferenceTests.setUp
    reference = auto_tests.AutoReferenceTests.reference
    @patch('generation.auto_references.find_wiki_image', return_value=(png('blue'), 'https://cdn.example.com/new.png'))
    def test_force_replaces_auto_even_when_not_marked_needed(self, find):
        auto = self.reference('auto')
        path = Path(auto.image.path)
        self.assignment.needs_reference = False
        self.assignment.save()
        result = fetch_auto_reference(self.assignment, force=True)
        self.assertEqual(result['status'], 'found')
        self.assertFalse(path.exists())
        self.assertFalse(ReferenceImage.objects.filter(pk=auto.pk).exists())
        self.assertEqual(ReferenceImage.objects.get().origin, 'auto')

    @patch('generation.auto_references.find_wiki_image')
    def test_three_manual_references_and_skip_reasons(self, find):
        for _ in range(3):
            self.reference('manual')
        result = fetch_auto_reference(self.assignment)
        self.assertEqual(result, {'status': 'error', 'message': MANUAL_LIMIT_MESSAGE, 'skipped_reason': ''})
        find.assert_not_called()
        self.assignment.references.all().delete()
        self.assignment.needs_reference = False
        result = fetch_auto_reference(self.assignment)
        self.assertEqual(result['skipped_reason'], 'no_reference_needed')
        self.assignment.reference_wiki = self.assignment.reference_page = ''
        result = fetch_auto_reference(self.assignment, force=True)
        self.assertEqual(result['skipped_reason'], 'missing_wiki_or_page')
        self.assignment.reference_page = 'A plain title'
        self.assertEqual(fetch_auto_reference(self.assignment, force=True)['skipped_reason'], 'missing_wiki_or_page')

    @patch('generation.auto_references.find_wiki_image', return_value=(png(), 'https://cdn.example.com/portrait.png'))
    def test_success_message_includes_matched_title(self, find):
        with patch.dict(LAST_RESULTS, {(self.assignment.reference_wiki, self.assignment.reference_page): {'matched_title': 'Vol’jin (Darkspear)'}}, clear=True):
            result = fetch_auto_reference(self.assignment)
        self.assertEqual(result['message'], 'Encontrada como "Vol’jin (Darkspear)"')
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.auto_reference_message, result['message'])

    @patch('generation.views.fetch_auto_reference', return_value={'status': 'skipped', 'message': 'Already attempted', 'skipped_reason': 'already_attempted'})
    def test_card_endpoint_result_and_url_normalization(self, fetch):
        self.assignment.reference_wiki = ''
        self.assignment.reference_page = 'https://warcraft.wiki.gg/wiki/Vol%27jin'
        self.assignment.save()
        url = f'/api/cards/{self.entry.id}/theme-assignment/auto-reference/'
        result = self.client.post(url, {}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['auto_reference_result']['skipped_reason'], 'already_attempted')
        self.assignment.refresh_from_db()
        self.assertEqual((self.assignment.reference_wiki, self.assignment.reference_page), ('warcraft.wiki.gg', "Vol'jin"))
        self.assertEqual(fetch.call_args.args[0].reference_page, "Vol'jin")

    def test_patch_accepts_url_in_page_with_empty_wiki(self):
        auto = self.reference('auto')
        manual = self.reference('manual')
        url = f'/api/cards/{self.entry.id}/theme-assignment/'
        body = dict(element="Vol'jin", category='Troll', visual_brief='At war', reference_wiki='',
                    reference_page='https://wowpedia.fandom.com/wiki/Sylvanas_Windrunner#Lore')
        result = self.client.patch(url, body, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        data = result.json()['theme_assignment']
        self.assertEqual((data['reference_wiki'], data['reference_page']), ('wowpedia.fandom.com', 'Sylvanas Windrunner'))
        self.assertFalse(ReferenceImage.objects.filter(pk=auto.pk).exists())
        self.assertTrue(ReferenceImage.objects.filter(pk=manual.pk).exists())
        result = self.client.patch(url, {**body, 'reference_page': 'https://warcraft.wiki.gg/wiki/Vol%27jin'}, content_type='application/json')
        self.assertEqual(result.status_code, 200)

    @patch('generation.claude_client.Anthropic')
    def test_plan_normalizes_full_reference_url(self, anthropic):
        from .theme_planner import build_theme_plan
        anthropic.return_value.messages.create.return_value = response({'assignments': [{
            'oracle_id': str(self.card.oracle_id), 'element': "Vol'jin", 'category': 'Personaje',
            'visual_brief': 'Voljin al mando de sus tropas',
            'reference': {'wiki': '', 'page': 'https://warcraft.wiki.gg/wiki/Vol%27jin#Lore'},
        }]})
        item = build_theme_plan(self.deck)[0]
        self.assertTrue(item.needs_reference)
        self.assertEqual((item.reference_wiki, item.reference_page), ('warcraft.wiki.gg', "Vol'jin"))

    def make_image(self, card=None):
        card = card or self.card
        assignment = self.assignment if card.pk == self.card.pk else None
        key = make_cache_key(card.oracle_id, self.deck.theme, 'gemini', assignment, layout=self.deck.card_layout)
        image = GeneratedImage(cache_key=key, card=card, theme=self.deck.theme, visual_prompt='keep prompt',
                               prompt_json={'themed_name': 'Viejo', 'themed_flavor': 'Flavor viejo', 'full_prompt': 'Keep artwork'}, provider='gemini', prompt_version='v3')
        image.image.save('art.png', ContentFile(png()))
        return image

    @patch('generation.claude_client.Anthropic')
    def test_rewrite_twenty_five_in_two_calls_preserves_art_cache_and_elements(self, anthropic):
        self.assignment.element = "Vol'jin, líder de los Lanza Negra"
        self.assignment.save()
        images = [self.make_image()]
        for _ in range(24):
            card = make_card()
            DeckCard.objects.create(deck=self.deck, card=card, raw_name='Card', quantity=1, section='main')
            images.append(self.make_image(card))
        snapshot = {image.pk: (image.cache_key, image.image.name, Path(image.image.path).read_bytes()) for image in images}
        element = self.assignment.element
        omitted = str(images[-1].card.oracle_id)
        create = anthropic.return_value.messages.create
        def answer(**kwargs):
            cards = json.loads(kwargs['messages'][0]['content'])['cards']
            return response({'cards': [{'oracle_id': card['oracle_id'], 'themed_name': 'N'*35, 'themed_flavor': 'F'*150}
                                       for card in cards if card['oracle_id'] != omitted]})
        create.side_effect = answer
        with patch.dict(os.environ, {'CARD_TEXT_LANGUAGE': 'en'}):
            result = self.client.post(f'/api/decks/{self.deck.id}/rewrite-texts/')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['rewrite_summary'], {'updated': 24, 'skipped': 1})
        self.assertEqual(create.call_count, 2)
        self.assertEqual([len(json.loads(call.kwargs['messages'][0]['content'])['cards']) for call in create.call_args_list], [20, 5])
        for call in create.call_args_list:
            self.assertEqual(call.kwargs['max_tokens'], 4000)
            self.assertIn('themed_name y themed_flavor se escriben en INGLÉS', call.kwargs['system'])
            self.assertIsInstance(call.kwargs['messages'][0]['content'], str)
        for image in images:
            image.refresh_from_db()
            self.assertEqual((image.cache_key, image.image.name, Path(image.image.path).read_bytes()), snapshot[image.pk])
            self.assertEqual(image.prompt_json['full_prompt'], 'Keep artwork')
            if str(image.card.oracle_id) == omitted:
                self.assertEqual(image.prompt_json['themed_name'], 'Viejo')
            else:
                self.assertEqual((len(image.prompt_json['themed_name']), len(image.prompt_json['themed_flavor'])), (30, 140))
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.element, element)

    @patch('generation.claude_client.Anthropic')
    def test_rewrite_active_job_and_failure(self, anthropic):
        image = self.make_image()
        url = f'/api/decks/{self.deck.id}/rewrite-texts/'
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1)
        self.assertEqual(self.client.post(url).status_code, 409)
        anthropic.assert_not_called()
        job.delete()
        anthropic.return_value.messages.create.side_effect = RuntimeError('Claude unavailable')
        result = self.client.post(url)
        self.assertEqual((result.status_code, result.json()), (502, {'detail': 'Claude unavailable'}))
        image.refresh_from_db()
        self.assertEqual(image.prompt_json['themed_name'], 'Viejo')
