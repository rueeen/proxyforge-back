import json
import os
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import requests
from django.test import TestCase, SimpleTestCase
from django.utils import timezone

from decks.models import Deck, DeckCard
from . import wiki_images as wiki
from .claude_client import call_claude_json_with_tools
from .models import ThemeAssignment, WikiSite, GenerationJob
from .test_references import png
from .test_wiki_images import response
from .tests import make_card
from .wiki_sites import probe_wiki, page_hits, verify_assignment_pages
from .theme_planner import build_theme_plan


def siteinfo():
    return response({'query': {'general': {'sitename': 'Warcraft Wiki', 'lang': 'en', 'mainpage': 'Main Page'}}})


def claude_response(data, stop='end_turn', other=None):
    return SimpleNamespace(content=[*(other or []), SimpleNamespace(type='text', text=json.dumps(data))], stop_reason=stop,
                           usage=SimpleNamespace(server_tool_use=SimpleNamespace(web_search_requests=1)))


@patch('generation.wiki_images.time.sleep')
@patch('generation.wiki_images.requests.get')
class WikiSiteTests(TestCase):
    def setUp(self):
        wiki.API_PATHS.clear()
        wiki._last_requests.clear()

    def test_probe_normalizes_caches_and_expires(self, get, sleep):
        get.return_value = siteinfo()
        result = probe_wiki('https://www.Warcraft.wiki.gg/wiki/Arthas')
        self.assertEqual(result, dict(domain='warcraft.wiki.gg', ok=True, sitename='Warcraft Wiki', lang='en', fandom=False,
                                      main_page_url='https://warcraft.wiki.gg/wiki/Main_Page', error=''))
        self.assertEqual(get.call_args.kwargs['params']['meta'], 'siteinfo')
        self.assertEqual(probe_wiki('warcraft.wiki.gg'), result)
        self.assertEqual(get.call_count, 1)
        WikiSite.objects.update(checked_at=timezone.now() - timedelta(days=8))
        probe_wiki('warcraft.wiki.gg')
        self.assertEqual(get.call_count, 2)

    def test_probe_failures_and_negative_cache(self, get, sleep):
        get.side_effect = requests.ConnectionError('DNS unavailable')
        result = probe_wiki('bad.example.com')
        self.assertFalse(result['ok'])
        self.assertIn('DNS', result['error'])
        self.assertEqual(probe_wiki('bad.example.com'), result)
        self.assertEqual(get.call_count, 1)
        get.side_effect = None
        get.return_value = response({'query': {}})
        self.assertFalse(probe_wiki('invalid.example.com')['ok'])
        self.assertFalse(probe_wiki('not a domain')['ok'])

    def test_page_hits_direct_redirect_and_search(self, get, sleep):
        get.side_effect = [response({'query': {'normalized': [{'from': 'Old_name', 'to': 'Old name'}],
            'redirects': [{'from': 'Old name', 'to': 'Canonical'}],
            'pages': [{'title': 'Direct'}, {'title': 'Canonical'}, {'title': 'Typo', 'missing': True},
                      {'title': 'Absent', 'missing': True}]}}),
            response({'query': {'search': [{'title': 'Found by search'}]}}), response({'query': {'search': []}})]
        result = page_hits('warcraft.wiki.gg', ['Direct', 'Old_name', 'Typo', 'Absent'])
        self.assertEqual(result, dict(checked=4, found=3, resolved={'Direct': 'Direct', 'Old_name': 'Canonical',
                                                                  'Typo': 'Found by search', 'Absent': None}))
        self.assertEqual(get.call_count, 3)
        self.assertEqual(get.call_args_list[0].kwargs['params']['titles'], 'Direct|Old_name|Typo|Absent')

    def test_page_hits_limits_to_ten(self, get, sleep):
        get.side_effect = [response({'query': {'pages': []}}), *[response({'query': {'search': []}}) for _ in range(10)]]
        self.assertEqual(page_hits('warcraft.wiki.gg', [str(i) for i in range(15)])['checked'], 10)
        self.assertEqual(get.call_count, 11)

    def test_verify_pages_statuses_and_batches(self, get, sleep):
        deck = Deck.objects.create(name='Deck', theme='Warcraft', raw_list='')
        items = [ThemeAssignment.objects.create(deck=deck, oracle_id=make_card().oracle_id, element=str(i),
                    reference_wiki='warcraft.wiki.gg', reference_page=title) for i, title in enumerate(('Direct', 'Old', 'Typo', 'Absent'))]
        get.side_effect = [response({'query': {'redirects': [{'from': 'Old', 'to': 'Canonical'}],
            'pages': [{'title': 'Direct'}, {'title': 'Canonical'}, {'title': 'Typo', 'missing': True}, {'title': 'Absent', 'missing': True}]}}),
            response({'query': {'search': [{'title': 'Search match'}]}}), response({'query': {'search': []}})]
        verify_assignment_pages(deck, items)
        self.assertEqual([(i.reference_page, i.reference_page_status) for i in items],
                         [('Direct', 'ok'), ('Canonical', 'ok'), ('Search match', 'search'), ('Absent', 'missing')])
        self.assertEqual(list(deck.theme_assignments.order_by('id').values_list('reference_page_status', flat=True)), ['ok', 'ok', 'search', 'missing'])
        for i in range(51):
            items.append(ThemeAssignment.objects.create(deck=deck, oracle_id=make_card().oracle_id,
                         element=str(i), reference_wiki='other.wiki.gg', reference_page=f'Page {i}'))
        get.reset_mock()
        def answer(url, **kwargs):
            return response({'query': {'pages': [{'title': title} for title in kwargs['params']['titles'].split('|')]}})
        get.side_effect = answer
        verify_assignment_pages(deck, items[4:])
        self.assertEqual([len(call.kwargs['params']['titles'].split('|')) for call in get.call_args_list], [50, 1])


class ToolClaudeTests(SimpleTestCase):
    @patch('generation.claude_client.Anthropic')
    def test_text_only_parsing_pause_continuation_and_usage(self, sdk):
        tool = SimpleNamespace(type='server_tool_use', id='tool1', name='web_search')
        result = SimpleNamespace(type='web_search_tool_result')
        create = sdk.return_value.messages.create
        create.side_effect = [claude_response({}, 'pause_turn', [tool, result]), claude_response({'ok': True}, other=[tool, result])]
        messages = [{'role': 'user', 'content': 'Find wikis'}]
        tools = [{'type': 'web_search_20250305', 'name': 'web_search', 'max_uses': 3}]
        with self.assertLogs('generation.claude_client', level='INFO') as logs:
            data = call_claude_json_with_tools(system='System', messages=messages, max_tokens=800, validate=lambda d: d, tools=tools)
        self.assertEqual(data, {'ok': True})
        self.assertEqual(create.call_count, 2)
        self.assertEqual(create.call_args.kwargs['messages'][-1]['role'], 'assistant')
        self.assertEqual(create.call_args.kwargs['messages'][-1]['content'][0], tool)
        self.assertEqual(create.call_args.kwargs['tools'], tools)
        self.assertEqual(len(messages), 1)
        self.assertTrue(any('1' in message for message in logs.output))

    @patch('generation.claude_client.Anthropic')
    def test_invalid_json_retries_and_pause_only_once(self, sdk):
        create = sdk.return_value.messages.create
        create.side_effect = [claude_response([]), claude_response({'ok': True})]
        params = dict(system='System', messages=[{'role': 'user', 'content': 'Wikis'}], max_tokens=800, validate=lambda d: d, tools=[])
        self.assertEqual(call_claude_json_with_tools(**params), {'ok': True})
        create.side_effect = [claude_response({}, 'pause_turn'), claude_response({}, 'pause_turn')]
        with self.assertRaises(ValueError):
            call_claude_json_with_tools(**params)

    @patch('generation.claude_client.Anthropic')
    def test_tool_truncation_retries_with_larger_budget(self, sdk):
        create = sdk.return_value.messages.create
        create.side_effect = [claude_response({}, 'max_tokens'), claude_response({'ok': True})]
        result = call_claude_json_with_tools(system='System', messages=[{'role': 'user', 'content': 'Wikis'}],
                                           max_tokens=800, validate=lambda d: d, tools=[])
        self.assertEqual(result, {'ok': True})
        self.assertEqual([call.kwargs['max_tokens'] for call in create.call_args_list], [800, 1600])



@patch('generation.wiki_images.time.sleep')
@patch('generation.wiki_images.requests.get')
@patch('generation.claude_client.Anthropic')
class WikiWorkflowTests(TestCase):
    def setUp(self):
        wiki.API_PATHS.clear()
        self.deck = Deck.objects.create(name='Deck', theme='Warcraft', raw_list='')
        self.card = make_card()
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Card', quantity=1, section='main')
        self.assignment = ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id, element='Arthas',
            category='Personaje', visual_brief='El Rey Exánime', needs_reference=True, reference_page='Old name')
        self.url = f'/api/decks/{self.deck.pk}/'

    def api(self, url, **kwargs):
        params = kwargs['params']
        if params.get('meta') == 'siteinfo':
            if 'bad.example' in url:
                raise requests.ConnectionError('No DNS')
            return siteinfo()
        if params.get('list') == 'search':
            return response({'query': {'search': []}})
        return response({'query': {'pages': [{'title': title} for title in params['titles'].split('|')]}})

    def proposal(self, domains):
        return claude_response({'candidates': [{'domain': domain, 'why': 'Wiki comunitaria'} for domain in domains],
                                'sample_titles': ['Arthas', 'Thrall']})

    def test_candidates_sorted_recommended_and_never_confirmed(self, sdk, get, sleep):
        get.side_effect = self.api
        sdk.return_value.messages.create.return_value = self.proposal(['warcraft.fandom.com', 'warcraft.wiki.gg', 'bad.example.com'])
        result = self.client.post(self.url + 'wiki-candidates/')
        self.assertEqual(result.status_code, 200)
        candidates = result.json()['wiki_candidates']
        self.assertEqual([i['domain'] for i in candidates], ['warcraft.wiki.gg', 'warcraft.fandom.com', 'bad.example.com'])
        self.assertTrue(candidates[0]['recommended'])
        self.assertFalse(result.json()['reference_wiki_confirmed'])
        self.deck.refresh_from_db()
        self.assertEqual(self.deck.wiki_candidates, candidates)
        args = sdk.return_value.messages.create.call_args.kwargs
        self.assertEqual(args['max_tokens'], 800)
        self.assertEqual(json.loads(args['messages'][0]['content'])['elements'], ['Arthas'])
        self.assertNotIn('tools', args)

    def test_web_discovery_only_without_valid_hits(self, sdk, get, sleep):
        get.side_effect = self.api
        sdk.return_value.messages.create.side_effect = [self.proposal(['bad.example.com', 'other.bad.example.com']),
            self.proposal(['warcraft.wiki.gg', 'warcraft.fandom.com'])]
        result = self.client.post(self.url + 'wiki-candidates/')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['wiki_candidates'][0]['domain'], 'warcraft.wiki.gg')
        self.assertEqual(sdk.return_value.messages.create.call_count, 2)
        self.assertEqual(sdk.return_value.messages.create.call_args.kwargs['tools'][0]['max_uses'], 3)
        with patch.dict(os.environ, {'WIKI_DISCOVERY_WEB_SEARCH': 'false'}):
            sdk.return_value.messages.create.side_effect = None
            sdk.return_value.messages.create.return_value = self.proposal(['bad.example.com', 'other.bad.example.com'])
            sdk.return_value.messages.create.reset_mock()
            self.assertEqual(self.client.post(self.url + 'wiki-candidates/').status_code, 200)
            self.assertEqual(sdk.return_value.messages.create.call_count, 1)

    def test_more_hits_rank_before_non_fandom_and_recommendation_threshold(self, sdk, get, sleep):
        def answer(url, **kwargs):
            params = kwargs['params']
            if params.get('meta') == 'siteinfo':
                return siteinfo()
            if params.get('list') == 'search':
                return response({'query': {'search': []}})
            titles = params['titles'].split('|')
            count = 2 if 'fandom.com' in url else 1
            return response({'query': {'pages': [{'title': title, **({'missing': True} if i >= count else {})}
                                                   for i, title in enumerate(titles)]}})
        get.side_effect = answer
        proposal = {'candidates': [{'domain': domain, 'why': ''} for domain in ('warcraft.wiki.gg', 'warcraft.fandom.com')],
                    'sample_titles': ['Arthas', 'Thrall', 'Jaina', 'Sylvanas', 'Voljin', 'Tyrande']}
        sdk.return_value.messages.create.return_value = claude_response(proposal)
        candidates = self.client.post(self.url + 'wiki-candidates/').json()['wiki_candidates']
        self.assertEqual(candidates[0]['domain'], 'warcraft.fandom.com')
        self.assertNotIn('recommended', candidates[0])
        self.assertEqual(sdk.return_value.messages.create.call_count, 1)

    def test_no_hits_also_triggers_web_and_sort_prefers_more_hits(self, sdk, get, sleep):
        def answer(url, **kwargs):
            if kwargs['params'].get('meta') == 'siteinfo':
                return siteinfo()
            if kwargs['params'].get('list') == 'search':
                return response({'query': {'search': []}})
            titles = kwargs['params']['titles'].split('|')
            return response({'query': {'pages': [{'title': title, **({'missing': True} if 'empty' in url else {})} for title in titles]}})
        get.side_effect = answer
        sdk.return_value.messages.create.side_effect = [self.proposal(['empty.wiki.gg', 'empty.fandom.com']),
            self.proposal(['warcraft.fandom.com', 'warcraft.wiki.gg'])]
        self.assertEqual(self.client.post(self.url + 'wiki-candidates/').json()['wiki_candidates'][0]['domain'], 'warcraft.wiki.gg')
        self.assertEqual(sdk.return_value.messages.create.call_count, 2)

    def test_candidate_active_job_and_claude_failure(self, sdk, get, sleep):
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1)
        self.assertEqual(self.client.post(self.url + 'wiki-candidates/').status_code, 409)
        sdk.assert_not_called()
        job.delete()
        sdk.return_value.messages.create.side_effect = RuntimeError('Claude offline')
        result = self.client.post(self.url + 'wiki-candidates/')
        self.assertEqual((result.status_code, result.json()), (502, {'detail': 'Claude offline'}))

    def test_confirmation_propagates_respects_edited_and_verifies(self, sdk, get, sleep):
        get.side_effect = self.api
        edited = ThemeAssignment.objects.create(deck=self.deck, oracle_id=make_card().oracle_id, element='Manual',
            needs_reference=True, edited=True, reference_wiki='manual.wiki.gg', reference_page='Manual page')
        empty = ThemeAssignment.objects.create(deck=self.deck, oracle_id=make_card().oracle_id, element='Empty',
            needs_reference=True, edited=True, reference_wiki='', reference_page='Empty page')
        from django.core.files.base import ContentFile
        from .models import ReferenceImage
        import tempfile
        with tempfile.TemporaryDirectory() as directory, self.settings(MEDIA_ROOT=directory):
            for origin, color in [('auto', 'red'), ('manual', 'blue'), ('wiki', 'green')]:
                ref = ReferenceImage(assignment=self.assignment, origin=origin, sha256=color)
                ref.image.save(origin + '.png', ContentFile(png(color)))
            result = self.client.put(self.url + 'reference-wiki/', {'domain': 'https://www.warcraft.wiki.gg/', 'alt_domain': 'warcraft.fandom.com'}, content_type='application/json')
            self.assertEqual(result.status_code, 200)
            self.assertTrue(result.json()['reference_wiki_confirmed'])
            self.assertEqual(set(self.assignment.references.values_list('origin', flat=True)), {'manual', 'wiki'})
            self.assignment.refresh_from_db()
            empty.refresh_from_db()
            edited.refresh_from_db()
            self.assertEqual((self.assignment.reference_wiki, self.assignment.reference_wiki_alt, self.assignment.reference_page_status),
                             ('warcraft.wiki.gg', 'warcraft.fandom.com', 'ok'))
            self.assertEqual(empty.reference_wiki, 'warcraft.wiki.gg')
            self.assertEqual(edited.reference_wiki, 'manual.wiki.gg')
            self.assertEqual(self.client.delete(self.url + 'reference-wiki/').json()['reference_wiki_confirmed'], False)
            self.assignment.refresh_from_db()
            self.assertEqual(self.assignment.reference_wiki, 'warcraft.wiki.gg')

    def test_confirmation_bad_domain_and_conflict(self, sdk, get, sleep):
        get.side_effect = self.api
        result = self.client.put(self.url + 'reference-wiki/', {'domain': 'bad.example.com'}, content_type='application/json')
        self.assertEqual(result.status_code, 400)
        self.assertIn('No se pudo acceder a bad.example.com', result.json()['detail'])
        GenerationJob.objects.create(deck=self.deck, total_cards=1)
        self.assertEqual(self.client.put(self.url + 'reference-wiki/', {'domain': 'warcraft.wiki.gg'}, content_type='application/json').status_code, 409)
        self.assertEqual(self.client.delete(self.url + 'reference-wiki/').status_code, 409)

    def test_planner_confirmed_domain_enforced_and_invalid_domain_cleared(self, sdk, get, sleep):
        get.side_effect = self.api
        self.deck.reference_wiki = 'warcraft.wiki.gg'
        self.deck.reference_wiki_confirmed = True
        self.deck.save()
        create = sdk.return_value.messages.create
        create.return_value = claude_response({'assignments': [{'oracle_id': str(self.card.oracle_id), 'element': 'Thrall',
            'category': 'Personaje', 'visual_brief': 'El jefe de guerra', 'reference': {'wiki': 'bad.example.com', 'page': 'Thrall'}}]})
        result = build_theme_plan(self.deck)[0]
        self.assertEqual(result.reference_wiki, 'warcraft.wiki.gg')
        self.assertEqual(result.reference_page_status, 'ok')
        self.assertEqual(json.loads(create.call_args.kwargs['messages'][0]['content'])['reference_wiki'], 'warcraft.wiki.gg')
        self.deck.reference_wiki_confirmed = False
        self.deck.save()
        result = build_theme_plan(self.deck)[0]
        self.assertEqual((result.reference_wiki, result.auto_reference_status), ('', 'error'))
        self.assertIn('Confirma la wiki del mazo.', result.auto_reference_message)

    def test_patch_verifies_changed_page(self, sdk, get, sleep):
        get.return_value = response({'query': {'redirects': [{'from': 'Old', 'to': 'Canonical'}], 'pages': [{'title': 'Canonical'}]}})
        result = self.client.patch(f'/api/cards/{self.entry.pk}/theme-assignment/',
            {'element': 'Arthas', 'category': '', 'visual_brief': 'At war', 'reference_wiki': 'warcraft.wiki.gg', 'reference_page': 'Old'}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['theme_assignment']['reference_page'], 'Canonical')
        self.assertEqual(result.json()['theme_assignment']['reference_page_status'], 'ok')
