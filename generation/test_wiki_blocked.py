from io import StringIO
from unittest.mock import patch

import requests
from django.core.management import call_command, CommandError
from django.test import SimpleTestCase, TestCase

from . import test_auto_references as auto_tests
from . import wiki_images as wiki
from .auto_references import fetch_auto_reference, fetch_auto_references
from .test_wiki_images import response, page


@patch('generation.wiki_images.time.sleep')
@patch('generation.wiki_images.requests.get')
class WikiDownloadBlockedTests(SimpleTestCase):
    def setUp(self):
        wiki.API_PATHS.clear()
        wiki.LAST_RESULTS.clear()
        wiki._last_requests.clear()

    def principal(self, original, thumbnail):
        return response(page(original={'source': original}, thumbnail={'source': thumbnail}))

    def test_fandom_block_does_not_try_thumbnail(self, get, sleep):
        for host, original in (('megaman.fandom.com', 'https://cdn.example.com/original.png'),
                               ('other.example.com', 'https://static.wikia.nocookie.net/megaman/original.png')):
            with self.subTest(host=host):
                get.reset_mock()
                get.side_effect = [self.principal(original, 'https://cdn.example.com/thumbnail.png'), response(status=403)]
                with self.assertRaises(wiki.WikiDownloadBlocked) as caught:
                    wiki.find_wiki_image(host, 'Mega Man X (character)')
                message = str(caught.exception)
                self.assertIn('Fandom bloquea descargas automáticas.', message)
                self.assertIn('Ctrl+V', message)
                self.assertTrue(message.endswith(' — ' + original[:120]))
                self.assertLessEqual(len(message), 300)
                self.assertEqual(get.call_count, 2)

    def test_non_fandom_original_block_uses_thumbnail(self, get, sleep):
        original, thumbnail = 'https://cdn.example.com/original.png', 'https://cdn.example.com/thumb.png'
        get.side_effect = [self.principal(original, thumbnail), response(status=403), response(content=b'thumbnail')]
        self.assertEqual(wiki.find_wiki_image('warcraft.wiki.gg', 'Character'), (b'thumbnail', thumbnail))
        self.assertEqual([call.args[0] for call in get.call_args_list[1:]], [original, thumbnail])
        self.assertEqual(wiki.LAST_RESULTS[('warcraft.wiki.gg', 'Character')]['url'], thumbnail)

    def test_imageinfo_original_block_uses_thumbnail(self, get, sleep):
        original, thumbnail = 'https://cdn.example.com/full.png', 'https://cdn.example.com/thumb.png'
        get.side_effect = [response(page(title='Character')), response({'parse': {'images': ['Portrait.png']}}),
                           response(page(imageinfo=[{'url': original, 'thumburl': thumbnail}])),
                           response(status=403), response(content=b'thumbnail')]
        self.assertEqual(wiki.find_wiki_image('warcraft.wiki.gg', 'Character'), (b'thumbnail', thumbnail))

    def test_both_urls_blocked_show_last_url_and_generic_message(self, get, sleep):
        original, thumbnail = 'https://cdn.example.com/full.png', 'https://cdn.example.com/thumb.png'
        get.side_effect = [self.principal(original, thumbnail), response(status=403), response(status=403)]
        with self.assertRaises(wiki.WikiDownloadBlocked) as caught:
            wiki.find_wiki_image('warcraft.wiki.gg', 'Character')
        self.assertIn('warcraft.wiki.gg bloqueó la descarga (HTTP 403).', str(caught.exception))
        self.assertTrue(str(caught.exception).endswith(' — ' + thumbnail))
        self.assertEqual(get.call_count, 3)

    def test_retry_after_one_second_then_success(self, get, sleep):
        throttled = response(status=429)
        throttled.headers['Retry-After'] = '1'
        get.side_effect = [throttled, response(content=b'image')]
        with patch('generation.wiki_images.time.monotonic', side_effect=[0, 0, 1, 1]):
            raw, source = wiki._download('https://cdn.example.com/image.png', 'warcraft.wiki.gg')
        self.assertEqual(raw, b'image')
        sleep.assert_called_once_with(1)
        self.assertEqual(get.call_count, 2)

    def test_retry_once_only_and_no_long_wait(self, get, sleep):
        throttled = response(status=429)
        throttled.headers['Retry-After'] = '1'
        get.side_effect = [throttled, throttled]
        with patch('generation.wiki_images.time.monotonic', side_effect=[0, 0, 1, 1]):
            with self.assertRaises(wiki.WikiDownloadBlocked):
                wiki._download('https://cdn.example.com/image.png', 'warcraft.wiki.gg')
        self.assertEqual(get.call_count, 2)
        wiki._last_requests.clear()
        get.reset_mock()
        get.side_effect = None
        throttled.headers['Retry-After'] = '6'
        get.return_value = throttled
        with self.assertRaises(wiki.WikiDownloadBlocked):
            wiki._download('https://cdn.example.com/image.png', 'warcraft.wiki.gg')
        self.assertEqual(get.call_count, 1)

    def test_other_errors_preserve_type_and_always_append_truncated_url(self, get, sleep):
        url = 'https://cdn.example.com/' + 'x' * 200
        for status in (404, 500):
            wiki._last_requests.clear()
            get.reset_mock()
            get.return_value = response(status=status)
            with self.subTest(status=status), self.assertRaises(wiki.WikiImageError) as caught:
                wiki._download(url, 'warcraft.wiki.gg', 'https://cdn.example.com/thumb.png')
            self.assertNotIsInstance(caught.exception, wiki.WikiDownloadBlocked)
            self.assertTrue(str(caught.exception).endswith(' — ' + url[:120]))
            self.assertLessEqual(len(str(caught.exception)), 300)
            self.assertEqual(get.call_count, 1)
        get.side_effect = requests.Timeout('x' * 500)
        with self.assertRaises(wiki.WikiImageError) as caught:
            wiki._download(url, 'warcraft.wiki.gg')
        self.assertTrue(str(caught.exception).endswith(' — ' + url[:120]))
        self.assertLessEqual(len(str(caught.exception)), 300)

    @patch('generation.management.commands.find_reference.find_wiki_image')
    def test_command_prints_blocked_status_and_retains_full_message(self, find, get, sleep):
        message = 'Fandom bloquea descargas automáticas. Pega la imagen con Ctrl+V. — https://static.wikia.nocookie.net/' + 'x' * 100
        find.side_effect = wiki.WikiDownloadBlocked(message)
        output = StringIO()
        with self.assertRaises(CommandError) as caught:
            call_command('find_reference', 'X', wiki='megaman.fandom.com', stdout=output)
        self.assertIn('Status: blocked', output.getvalue())
        self.assertEqual(str(caught.exception), message)


class PageUrlTests(SimpleTestCase):
    def test_page_url_quotes_only_required_characters(self):
        self.assertEqual(wiki.reference_page_url('megaman.fandom.com', 'Mega Man X (character)'),
                         'https://megaman.fandom.com/wiki/Mega_Man_X_(character)')
        self.assertEqual(wiki.reference_page_url('warcraft.wiki.gg', "Vol'jin"), "https://warcraft.wiki.gg/wiki/Vol'jin")
        self.assertEqual(wiki.reference_page_url('example.com', 'A B/Ñ#Lore'), 'https://example.com/wiki/A_B%2F%C3%91%23Lore')
        self.assertEqual(wiki.reference_page_url('', 'Page'), '')
        self.assertEqual(wiki.reference_page_url('example.com', ''), '')


class BlockedAutoReferenceTests(TestCase):
    setUp = auto_tests.AutoReferenceTests.setUp

    @patch('generation.auto_references.find_wiki_image', side_effect=wiki.WikiDownloadBlocked('Fandom bloquea descargas automáticas. — https://static.wikia.nocookie.net/image.png'))
    def test_status_and_summary_count_blocked(self, find):
        result = fetch_auto_reference(self.assignment)
        self.assertEqual(result['status'], 'blocked')
        self.assignment.refresh_from_db()
        self.assertEqual(self.assignment.auto_reference_status, 'blocked')
        summary = fetch_auto_references(self.deck, force=True)
        self.assertEqual(summary, dict(found=0, not_found=0, rejected=0, error=0, skipped=0, blocked=1))
        detail = self.client.get(f'/api/decks/{self.deck.id}/').json()
        self.assertEqual(detail['cards'][0]['theme_assignment']['reference_page_url'], "https://warcraft.wiki.gg/wiki/Vol'jin")
        self.assertEqual(detail['cards'][0]['theme_assignment']['auto_reference_status'], 'blocked')
