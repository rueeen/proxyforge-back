import base64
import json
import os
from unittest.mock import patch

from django.core.files.base import ContentFile
from django.test import TestCase, SimpleTestCase

from decks.models import Deck
from . import test_auto_references as fixtures
from . import wiki_images as wiki
from .auto_references import fetch_auto_reference, verify_references
from .models import ReferenceImage, ThemeAssignment, GenerationJob
from .references import normalize_reference
from .test_references import png
from .test_wiki_images import response, page
from .test_wiki_sites import claude_response
from .tests import make_card


@patch.dict(os.environ, {'AUTO_REFERENCE_VERIFY': 'true', 'IMAGE_PROVIDER': 'gemini', 'MAX_CARDS_PER_JOB': '1'})
@patch('generation.wiki_images.time.sleep')
@patch('generation.wiki_images.requests.get')
@patch('generation.claude_client.Anthropic')
class MultipleReferenceTests(TestCase):
    setUp = fixtures.AutoReferenceTests.setUp

    def setUp(self):
        fixtures.AutoReferenceTests.setUp(self)
        wiki.IMAGE_LIST_CACHE.clear()
        wiki.API_PATHS.clear()
        self.files = ['Fan_by_Artist.jpg', 'Official.jpg', 'Duplicate.jpg', 'Portrait.jpg', 'Another.jpg', 'Extra.jpg']
        self.colors = dict(zip(self.files, ['yellow', 'blue', 'blue', 'red', 'green', 'orange']))

    def api(self, url, **kwargs):
        params = kwargs.get('params', {})
        if params.get('titles') and not params.get('prop'):
            return response({'query': {'pages': [{'title': title} for title in params['titles'].split('|')]}})
        if params.get('prop') == 'pageimages':
            return response(page(title='Character', pageimage=self.files[0]))
        if params.get('action') == 'parse':
            return response({'parse': {'images': self.files}})
        if params.get('prop') == 'imageinfo':
            return response({'query': {'pages': [dict(title='File:' + name, imageinfo=[dict(url='https://cdn.example/' + name,
                thumburl='https://cdn.example/thumb/' + name, width=1200 - index, height=900, mime='image/jpeg')])
                for index, name in enumerate(self.files)]}})
        return response(content=png(self.colors[url.rsplit('/', 1)[-1]]))

    def verification(self, sdk, count, order=None, ok=True):
        sdk.return_value.messages.create.return_value = claude_response({'results': [dict(index=i, ok=ok, reason='Canónico' if ok else 'Diseño incorrecto') for i in range(1, count + 1)],
            'best_order': order or list(range(1, count + 1))})

    def test_multiple_order_duplicates_one_verification_and_download_limit(self, sdk, get, sleep):
        get.side_effect = self.api
        self.verification(sdk, 4, [3, 1, 2, 4])
        result = fetch_auto_reference(self.assignment)
        self.assertEqual(result['status'], 'found')
        refs = list(self.assignment.references.order_by('position'))
        self.assertEqual(len(refs), 2)
        self.assertEqual([item.sha256 for item in refs], [normalize_reference(png('green'))[1], normalize_reference(png('blue'))[1]])
        self.assertEqual([item.position for item in refs], [0, 1])
        self.assertTrue(all(item.origin == 'auto' for item in refs))
        create = sdk.return_value.messages.create
        self.assertEqual(create.call_count, 1)
        images = [block for block in create.call_args.kwargs['messages'][0]['content'] if block['type'] == 'image']
        self.assertEqual(len(images), 4)
        self.assertEqual(len([call for call in get.call_args_list if not call.kwargs.get('params')]), 5)
        self.assertEqual(result['message'], 'Se agregaron 2 referencias; se evitó la principal (posible fan art)')
        old = {item.pk for item in refs}
        get.reset_mock()
        self.verification(sdk, 4)
        self.assertEqual(fetch_auto_reference(self.assignment, force=True)['status'], 'found')
        self.assertFalse(ReferenceImage.objects.filter(pk__in=old).exists())
        self.assertEqual(self.assignment.references.count(), 2)

    def add_reference(self, color, origin='manual'):
        image, digest = normalize_reference(png(color))
        reference = ReferenceImage(assignment=self.assignment, sha256=digest, origin=origin)
        reference.image.save(color + '.jpg', ContentFile(image))
        return reference

    def test_two_manual_references_only_one_auto_and_full_skips(self, sdk, get, sleep):
        manual = self.add_reference('blue')
        chosen = self.add_reference('purple', 'wiki')
        get.side_effect = self.api
        # Four download attempts: duplicate blue twice, then red and green.
        self.verification(sdk, 2, [2, 1])
        result = fetch_auto_reference(self.assignment)
        self.assertEqual(result['status'], 'found')
        auto = self.assignment.references.get(origin='auto')
        self.assertEqual(auto.sha256, normalize_reference(png('green'))[1])
        self.assertEqual(self.assignment.references.count(), 3)
        self.assignment.references.filter(origin='auto').delete()
        self.add_reference('orange')
        get.reset_mock()
        result = fetch_auto_reference(self.assignment, force=True)
        self.assertEqual((result['status'], result['skipped_reason']), ('skipped', 'references_full'))
        get.assert_not_called()
        self.assertTrue(ReferenceImage.objects.filter(pk=manual.pk).exists())
        self.assertTrue(ReferenceImage.objects.filter(pk=chosen.pk).exists())

    def test_technical_verification_failure_accepts_order(self, sdk, get, sleep):
        get.side_effect = self.api
        sdk.return_value.messages.create.side_effect = RuntimeError('Claude offline')
        with self.assertLogs('generation.auto_references', level='WARNING'):
            result = fetch_auto_reference(self.assignment)
        self.assertEqual(result['status'], 'found')
        self.assertEqual(list(self.assignment.references.order_by('position').values_list('sha256', flat=True)),
                         [normalize_reference(png('blue'))[1], normalize_reference(png('red'))[1]])

    def test_all_rejected_preserve_reason(self, sdk, get, sleep):
        get.side_effect = self.api
        self.verification(sdk, 4, ok=False)
        self.assertEqual(fetch_auto_reference(self.assignment)['status'], 'rejected')
        self.assertEqual(self.assignment.auto_reference_message, 'Diseño incorrecto')
        self.assertEqual(self.assignment.references.count(), 0)

    def test_alternative_after_block_and_missing(self, sdk, get, sleep):
        self.assignment.reference_wiki = 'warcraft.fandom.com'
        self.assignment.reference_wiki_alt = 'warcraft.wiki.gg'
        self.assignment.save()
        def answer(url, **kwargs):
            if url.startswith('https://warcraft.fandom.com') and kwargs.get('params', {}).get('prop') == 'imageinfo':
                result = self.api(url, **kwargs)
                for entry in result.json()['query']['pages']:
                    entry['imageinfo'][0]['url'] = 'https://static.wikia.nocookie.net/' + entry['title'][5:]
                return result
            if 'static.wikia.nocookie.net' in url:
                return response(status=403)
            return self.api(url, **kwargs)
        get.side_effect = answer
        self.verification(sdk, 4)
        self.assertEqual(fetch_auto_reference(self.assignment)['status'], 'found')
        self.assertEqual(len([call for call in get.call_args_list if 'static.wikia.nocookie.net' in call.args[0]]), 1)
        self.assignment.references.all().delete()
        self.assignment.reference_wiki_alt = ''
        self.assignment.save()
        self.deck.reference_wiki_alt = 'warcraft.wiki.gg'
        self.deck.save()
        self.assignment.deck = self.deck
        wiki.IMAGE_LIST_CACHE.clear()
        def missing(url, **kwargs):
            if url.startswith('https://warcraft.fandom.com'):
                if kwargs.get('params', {}).get('list') == 'search':
                    return response({'query': {'search': []}})
                return response(page(missing=True))
            return self.api(url, **kwargs)
        get.side_effect = missing
        self.assertEqual(fetch_auto_reference(self.assignment, force=True)['status'], 'found')

    def test_costs_existing_planned_and_batch_and_patch_count(self, sdk, get, sleep):
        self.deck.auto_reference_count = 2
        self.deck.save()
        self.add_reference('red')
        card = make_card()
        from decks.models import DeckCard
        DeckCard.objects.create(deck=self.deck, card=card, raw_name='Second', quantity=1, section='main')
        ThemeAssignment.objects.create(deck=self.deck, oracle_id=card.oracle_id, element='Second', needs_reference=True)
        with patch.dict(os.environ, {'COST_PER_IMAGE_USD': '0.04', 'COST_PER_REFERENCE_USD': '0.01'}):
            result = self.client.get(f'/api/decks/{self.deck.pk}/estimate/').json()
        self.assertEqual((result['reference_cost_usd'], result['estimated_cost_usd'], result['batch_cost_usd']), (0.03, 0.11, 0.05))
        for count in (1, 2, 3):
            result = self.client.patch(f'/api/decks/{self.deck.pk}/', {'auto_reference_count': count}, content_type='application/json')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['auto_reference_count'], count)
        for count in (0, 4):
            self.assertEqual(self.client.patch(f'/api/decks/{self.deck.pk}/', {'auto_reference_count': count}, content_type='application/json').status_code, 400)
        GenerationJob.objects.create(deck=self.deck, total_cards=1)
        self.assertEqual(self.client.patch(f'/api/decks/{self.deck.pk}/', {'auto_reference_count': 2}, content_type='application/json').status_code, 409)


class BatchVerificationTests(SimpleTestCase):
    @patch('generation.claude_client.Anthropic')
    def test_verification_result_is_indexed_with_best_order(self, sdk):
        sdk.return_value.messages.create.return_value = claude_response({'results': [{'index': 2, 'ok': True, 'reason': 'Mejor'}, {'index': 1, 'ok': False, 'reason': 'No canónico'}], 'best_order': [2, 1]})
        from types import SimpleNamespace
        result = verify_references([b'first', b'second'], SimpleNamespace(element='Arthas', category='', visual_brief=''))
        self.assertEqual(result, [(False, 'No canónico'), (True, 'Mejor')])
        self.assertEqual(result.best_order, [1, 0])
        content = sdk.return_value.messages.create.call_args.kwargs['messages'][0]['content']
        self.assertEqual([block['text'] for block in content if block['type'] == 'text'][:2], ['Imagen 1', 'Imagen 2'])
        self.assertEqual([base64.b64decode(block['source']['data']) for block in content if block['type'] == 'image'], [b'first', b'second'])

    @patch.dict(os.environ, {'AUTO_REFERENCE_COUNT': '3'})
    def test_count_default_from_environment(self):
        self.assertEqual(Deck().auto_reference_count, 3)
