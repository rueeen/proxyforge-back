import os
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase

from . import wiki_images as wiki
from .auto_references import fetch_auto_reference
from .models import GenerationJob, ReferenceImage
from . import test_auto_references, test_references
from .test_references import png
from .test_wiki_images import response, page


def file_info(name, width=900, height=1200, mime='image/jpeg'):
    return dict(title='File:' + name, imageinfo=[dict(url='https://cdn.example/' + name,
                thumburl='https://cdn.example/thumb/' + name, width=width, height=height, mime=mime)])


@patch('generation.wiki_images.time.sleep')
@patch('generation.wiki_images.requests.get')
class CandidateTests(SimpleTestCase):
    def setUp(self):
        wiki.API_PATHS.clear()
        wiki.IMAGE_LIST_CACHE.clear()
        wiki.LAST_RESULTS.clear()
        wiki._last_requests.clear()

    def responses(self):
        names = ['Lich_King_by_Raneman.jpg', 'Official.jpg', 'Small.jpg', 'Inv_sword_01.png',
                 'Achievement_x.png', 'Vector.svg', 'Other_fanart.jpg', 'Wide.jpg', 'Bad.webp']
        return [response(page(title='Arthas', pageimage=names[0])),
                response({'parse': {'images': names}}),
                response({'query': {'pages': [file_info(names[0]), file_info('Official.jpg'),
                          file_info('Small.jpg', 200), file_info('Other_fanart.jpg', 2000, 2000),
                          file_info('Wide.jpg', 2000, 1500), file_info('Bad.webp', mime='image/gif')]}})]

    def test_filters_order_flags_cache_and_limit(self, get, sleep):
        get.side_effect = self.responses()
        result = wiki.list_wiki_images('warcraft.wiki.gg', 'Arthas', limit=2)
        self.assertEqual([item['file'] for item in result['images']], ['Lich_King_by_Raneman.jpg', 'Wide.jpg'])
        self.assertTrue(result['images'][0]['maybe_fan_art'])
        self.assertTrue(result['images'][0]['is_page_image'])
        titles = get.call_args.kwargs['params']['titles']
        for name in ('Inv_sword', 'Achievement', '.svg'):
            self.assertNotIn(name, titles)
        result['images'][0]['file'] = 'mutated'
        again = wiki.list_wiki_images('warcraft.wiki.gg', 'Arthas')
        self.assertEqual(len(again['images']), 4)
        self.assertEqual(again['images'][0]['file'], 'Lich_King_by_Raneman.jpg')
        self.assertEqual(get.call_count, 3)
        self.assertEqual(again['images'][-1]['file'], 'Other_fanart.jpg')
        with patch('generation.wiki_images.time.monotonic', return_value=wiki.IMAGE_LIST_CACHE[('warcraft.wiki.gg', 'Arthas')][0] + 601):
            get.side_effect = self.responses()
            wiki.list_wiki_images('warcraft.wiki.gg', 'Arthas')
        self.assertEqual(get.call_count, 6)

    def test_resolves_missing_page_and_reports_absent_page(self, get, sleep):
        get.side_effect = [response(page(missing=True)), response({'query': {'search': [{'title': 'Arthas'}]}}), *self.responses()]
        self.assertEqual(wiki.list_wiki_images('warcraft.wiki.gg', 'Wrong title')['page'], 'Arthas')
        get.side_effect = [response(page(missing=True)), response({'query': {'search': []}})]
        with self.assertRaises(wiki.WikiImageNotFound):
            wiki.list_wiki_images('warcraft.wiki.gg', 'Absent')

    def test_page_image_matches_mediawiki_space_normalization(self, get, sleep):
        get.side_effect = [response(page(title='Arthas', pageimage='Official_portrait.jpg')),
                           response({'parse': {'images': ['Official_portrait.jpg']}}),
                           response({'query': {'pages': [file_info('Official portrait.jpg')]}})]
        image = wiki.list_wiki_images('warcraft.wiki.gg', 'Arthas')['images'][0]
        self.assertTrue(image['is_page_image'])

    def test_batches_at_most_fifty_files(self, get, sleep):
        names = [f'Portrait_{i}.jpg' for i in range(105)]
        get.side_effect = [response(page(title='Arthas')), response({'parse': {'images': names}}),
                           *[response({'query': {'pages': [file_info(name) for name in names[i:i+50]]}})
                             for i in range(0, 105, 50)]]
        self.assertEqual(len(wiki.list_wiki_images('warcraft.wiki.gg', 'Arthas', 200)['images']), 105)
        self.assertEqual([len(call.kwargs['params']['titles'].split('|')) for call in get.call_args_list[2:]], [50, 50, 5])

    def test_automatic_avoids_fanart_and_keeps_it_without_alternative(self, get, sleep):
        original = 'https://cdn.example/Lich_King_by_Raneman.jpg'
        get.side_effect = [response(page(title='Arthas', original={'source': original})), *self.responses(), response()]
        _, source = wiki.find_wiki_image('warcraft.wiki.gg', 'Arthas')
        self.assertEqual(source, 'https://cdn.example/Wide.jpg')
        self.assertIn('Se evitó la imagen principal', wiki.LAST_RESULTS[('warcraft.wiki.gg', 'Arthas')]['message'])
        with patch('generation.wiki_images.list_wiki_images', return_value={'images': []}):
            get.side_effect = [response(page(original={'source': original})), response()]
            self.assertEqual(wiki.find_wiki_image('warcraft.wiki.gg', 'Arthas')[1], original)


@patch.dict(os.environ, {'IMAGE_PROVIDER': 'gemini', 'IMAGE_FALLBACK_PROVIDER': ''})
@patch('generation.wiki_images.time.sleep')
@patch('generation.wiki_images.requests.get')
class SelectorEndpointTests(TestCase):
    def setUp(self):
        test_references.ReferenceEndpointTests.setUp(self)
        self.assignment.reference_wiki = 'warcraft.wiki.gg'
        self.assignment.reference_page = 'Arthas'
        self.assignment.save()
        self.list_url = self.url.replace('references/', 'wiki-images/')
        self.select_url = self.url + 'wiki/'
        wiki.IMAGE_LIST_CACHE.clear()
        wiki.API_PATHS.clear()

    def test_get_defaults_overrides_missing_and_errors(self, get, sleep):
        get.side_effect = [response(page(title='Arthas')), response({'parse': {'images': []}})]
        result = self.client.get(self.list_url)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['page_url'], 'https://warcraft.wiki.gg/wiki/Arthas')
        get.side_effect = [response(page(title='Lich King')), response({'parse': {'images': []}})]
        result = self.client.get(self.list_url, {'wiki': 'other.wiki.gg', 'page': 'Lich King'})
        self.assertEqual(result.json()['wiki'], 'other.wiki.gg')
        self.assertIn('other.wiki.gg', get.call_args.args[0])
        get.side_effect = [response(page(missing=True)), response({'query': {'search': []}})]
        self.assertEqual(self.client.get(self.list_url, {'page': 'Absent'}).status_code, 404)
        get.side_effect = [response(status=500)]
        self.assertEqual(self.client.get(self.list_url, {'page': 'Error'}).status_code, 502)
        self.assignment.reference_page = ''
        self.assignment.save()
        self.assertEqual(self.client.get(self.list_url).status_code, 400)
        self.assignment.delete()
        self.assertEqual(self.client.get(self.list_url, {'wiki': 'other.wiki.gg', 'page': 'X'}).status_code, 400)

    def select(self, **extra):
        return self.client.post(self.select_url, {'file': 'Arthas.jpg', **extra}, content_type='application/json')

    def prepare(self, get, color='red'):
        get.side_effect = [response({'query': {'pages': [file_info('Arthas.jpg')]}}), response(content=png(color))]

    def test_select_duplicate_maximum_and_active_job(self, get, sleep):
        self.prepare(get)
        result = self.select()
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()['id'], self.entry.pk)
        ref = ReferenceImage.objects.get()
        self.assertEqual((ref.origin, ref.source_url), ('wiki', 'https://warcraft.wiki.gg/wiki/File:Arthas.jpg'))
        self.assignment.refresh_from_db()
        self.assertEqual((self.assignment.edited, self.assignment.auto_reference_status), (True, 'found'))
        self.assertEqual(get.call_args_list[0].kwargs['params']['iiurlwidth'], 1536)
        self.assertIn('/thumb/', get.call_args.args[0])
        self.prepare(get)
        self.assertEqual(self.select().json()['detail'], 'Esa imagen ya está agregada.')
        for color in ('blue', 'green'):
            self.prepare(get, color)
            self.assertEqual(self.select().status_code, 201)
        get.reset_mock()
        self.assertEqual(self.select().json()['detail'], 'Máximo 3 imágenes de referencia por carta.')
        get.assert_not_called()
        GenerationJob.objects.create(deck=self.deck, status='pending', total_cards=1)
        self.assertEqual(self.select().status_code, 409)

    def test_fandom_block_includes_image_link(self, get, sleep):
        info = file_info('Arthas.jpg')
        info['imageinfo'][0]['thumburl'] = 'https://static.wikia.nocookie.net/image.jpg'
        get.side_effect = [response({'query': {'pages': [info]}}), response(status=403)]
        result = self.select(wiki='warcraft.fandom.com')
        self.assertEqual(result.status_code, 400)
        self.assertTrue(result.json()['blocked'])
        self.assertEqual(result.json()['full_url'], info['imageinfo'][0]['thumburl'])
        self.assertIn('Ctrl+V', result.json()['detail'])
        self.assertFalse(ReferenceImage.objects.exists())

    def test_patch_keeps_chosen_wiki_reference(self, get, sleep):
        self.prepare(get)
        self.assertEqual(self.select().status_code, 201)
        result = self.client.patch(self.url.replace('references/', ''), {'reference_page': 'Lich King', 'element': self.assignment.element, 'category': self.assignment.category, 'visual_brief': self.assignment.visual_brief}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertTrue(self.assignment.references.filter(origin='wiki').exists())


@patch.dict(os.environ, {'AUTO_REFERENCE_VERIFY': 'false'})
class ChosenReferencePreservationTests(TestCase):
    setUp = test_auto_references.AutoReferenceTests.setUp
    reference = test_auto_references.AutoReferenceTests.reference

    @patch('generation.auto_references.find_wiki_image', return_value=(png('blue'), 'https://cdn.example/official.png'))
    def test_force_preserves_wiki_references_and_full_limit(self, find):
        chosen = self.reference('wiki')
        wiki.LAST_RESULTS[(self.assignment.reference_wiki, self.assignment.reference_page)] = {'message': 'Se evitó la imagen principal (posible fan art) y se usó "Official.jpg"'}
        self.addCleanup(wiki.LAST_RESULTS.clear)
        result = fetch_auto_reference(self.assignment, force=True)
        self.assertEqual(result['status'], 'found')
        self.assertIn('Se evitó', result['message'])
        self.assertTrue(ReferenceImage.objects.filter(pk=chosen.pk).exists())
        self.assignment.references.filter(origin='auto').delete()
        self.reference('wiki')
        self.reference('manual')
        find.reset_mock()
        self.assertEqual(fetch_auto_reference(self.assignment, force=True)['status'], 'error')
        find.assert_not_called()
