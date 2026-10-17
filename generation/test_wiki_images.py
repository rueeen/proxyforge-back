from unittest.mock import Mock, patch

import requests
from django.test import SimpleTestCase

from . import wiki_images as wiki
from .references import MAX_REFERENCE_BYTES


def response(data=None, status=200, content=b'raw image', mime='image/png'):
    return Mock(status_code=status, json=Mock(return_value=data), content=content, headers={'Content-Type': mime})


def page(**info):
    return {'query': {'pages': [info]}}


@patch('generation.wiki_images.time.sleep')
@patch('generation.wiki_images.requests.get')
class WikiImageTests(SimpleTestCase):
    def setUp(self):
        wiki.API_PATHS.clear()
        wiki.LAST_RESULTS.clear()
        wiki._last_requests.clear()

    def test_original_thumbnail_and_cached_api_route(self, get, sleep):
        for prop in ('original', 'thumbnail'):
            with self.subTest(prop=prop):
                get.reset_mock()
                get.side_effect = [response(page(**{prop: {'source': 'https://cdn.example.com/image.png'}})), response()]
                self.assertEqual(wiki.find_wiki_image('warcraft.wiki.gg', "Vol'jin"), (b'raw image', 'https://cdn.example.com/image.png'))
                self.assertEqual(get.call_args_list[0].kwargs['timeout'], 15)
                self.assertEqual(get.call_args_list[1].kwargs['timeout'], 20)
                self.assertEqual(get.call_args_list[0].kwargs['headers']['Accept'], 'application/json')
                self.assertIn('image/jpeg', get.call_args_list[1].kwargs['headers']['Accept'])
                self.assertEqual(wiki.HEADERS['Accept'], 'application/json')
                params = get.call_args_list[0].kwargs['params']
                self.assertEqual((params['format'], params['formatversion']), ('json', 2))
                self.assertEqual(wiki.API_PATHS['warcraft.wiki.gg'], '/api.php')
                self.assertEqual(wiki.LAST_RESULTS[('warcraft.wiki.gg', "Vol'jin")]['step'], 1)

    def test_missing_page_uses_search(self, get, sleep):
        get.side_effect = [response(page(missing=True)), response({'query': {'search': [{'title': 'Vol’jin'}]}}),
                           response(page(original={'source': 'https://cdn.example.com/image.png'})), response()]
        wiki.find_wiki_image('warcraft.wiki.gg', "Vol'jin")
        self.assertEqual(get.call_args_list[1].kwargs['params']['list'], 'search')
        self.assertEqual(get.call_args_list[2].kwargs['params']['titles'], 'Vol’jin')
        self.assertEqual(wiki.LAST_RESULTS[('warcraft.wiki.gg', "Vol'jin")]['step'], 2)
        self.assertEqual(wiki.LAST_RESULTS[('warcraft.wiki.gg', "Vol'jin")]['matched_title'], 'Vol’jin')

    def test_parse_filters_and_imageinfo(self, get, sleep):
        get.side_effect = [response(page(title='Character')), response({'parse': {'images': [
            'Logo.png', 'Icon_x.png', 'Vector.svg', 'Animation.gif', 'Map.jpg', 'Portrait.png', 'Later.jpg']}}),
            response(page(imageinfo=[{'thumburl': 'https://cdn.example.com/thumb.png', 'url': 'https://cdn.example.com/full.png'}])), response()]
        self.assertEqual(wiki.find_wiki_image('example.wiki.gg', 'Character')[1], 'https://cdn.example.com/thumb.png')
        params = get.call_args_list[2].kwargs['params']
        self.assertEqual(params['titles'], 'File:Portrait.png')
        self.assertEqual(params['iiurlwidth'], 1536)
        self.assertEqual(wiki.LAST_RESULTS[('example.wiki.gg', 'Character')]['step'], 3)

    def test_w_api_fallback_after_404_or_html(self, get, sleep):
        for first in (response(status=404), response()):
            if first.status_code != 404:
                first.json.side_effect = ValueError('HTML')
            with self.subTest(status=first.status_code):
                wiki.API_PATHS.clear()
                get.reset_mock()
                get.side_effect = [first, response(page(original={'source': 'https://cdn.example.com/image.png'})), response(),
                                   response(page(original={'source': 'https://cdn.example.com/image.png'})), response()]
                wiki.find_wiki_image('example.fandom.com', 'Character')
                self.assertTrue(get.call_args_list[1].args[0].endswith('/w/api.php'))
                wiki.find_wiki_image('example.fandom.com', 'Other')
                self.assertTrue(get.call_args_list[3].args[0].endswith('/w/api.php'))

    def test_no_result_and_network_server_errors(self, get, sleep):
        get.side_effect = [response(page()), response({'parse': {'images': []}})]
        with self.assertRaisesRegex(wiki.WikiImageNotFound, 'La página "Character" no tiene imagen utilizable'):
            wiki.find_wiki_image('example.wiki.gg', 'Character')
        get.side_effect = [response(page(missing=True)), response({'query': {'search': []}})]
        with self.assertRaisesRegex(wiki.WikiImageNotFound, 'No existe la página "Character" en example.wiki.gg y la búsqueda no encontró resultados'):
            wiki.find_wiki_image('example.wiki.gg', 'Character')
        get.side_effect = None
        get.return_value = response(status=500)
        with self.assertRaises(wiki.WikiImageError) as caught:
            wiki.find_wiki_image('example.wiki.gg', 'Character')
        self.assertLessEqual(len(str(caught.exception)), 200)
        get.side_effect = requests.Timeout('x' * 300)
        with self.assertRaises(wiki.WikiImageError) as caught:
            wiki.find_wiki_image('example.wiki.gg', 'Character')
        self.assertLessEqual(len(str(caught.exception)), 200)
        get.side_effect = [response(status=404), response(status=404)]
        with self.assertRaisesRegex(wiki.WikiImageError, 'La wiki no expone api.php'):
            wiki.find_wiki_image('example.wiki.gg', 'Character')

    def test_download_limits_and_mime(self, get, sleep):
        for downloaded, error in ((response(mime='image/gif'), wiki.WikiImageNotFound),
                                  (response(content=b'x' * (MAX_REFERENCE_BYTES + 1)), wiki.WikiImageError)):
            get.side_effect = [response(page(original={'source': 'https://cdn.example.com/image'})), downloaded]
            with self.subTest(error=error), self.assertRaises(error):
                wiki.find_wiki_image('example.wiki.gg', 'Character')

    def test_throttle_between_same_host_requests(self, get, sleep):
        get.return_value = response()
        with patch('generation.wiki_images.time.monotonic', side_effect=[0.0, 0.0, 0.1, 0.3]):
            wiki._get('https://example.com/first', 15)
            wiki._get('https://example.com/second', 15)
        sleep.assert_called_once()
        self.assertAlmostEqual(sleep.call_args.args[0], 0.2)
