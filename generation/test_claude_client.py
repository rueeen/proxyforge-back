import json
import os
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from decks.models import Card, Deck, DeckCard
from .claude_client import CONCISION_MESSAGE, ClaudeTruncatedError, call_claude_json
from .models import GenerationJob, ThemeAssignment
from .prompt_builder import build_visual_prompt
from .runner import render_card_image, run_job
from .tests import make_card, valid_prompt
from .theme_planner import build_theme_plan


def response(raw, stop='end_turn'):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=raw)], stop_reason=stop)


@patch('generation.claude_client.Anthropic')
class ClaudeClientTests(SimpleTestCase):
    def call(self, messages, **kwargs):
        return call_claude_json(system='system', messages=messages, max_tokens=100,
                                validate=lambda data: data, **kwargs)

    def test_truncation_retries_original_messages_and_preserves_input(self, anthropic):
        for content in ('original', [{'type': 'image', 'source': {'type': 'url', 'url': 'https://example.com/image'}},
                                     {'type': 'text', 'text': 'original'}]):
            with self.subTest(content=content):
                messages = [{'role': 'user', 'content': content}]
                original = deepcopy(messages)
                create = anthropic.return_value.messages.create
                create.reset_mock()
                create.side_effect = [response('TRUNCATED PRIVATE CONTENT', 'max_tokens'), response('```json\n{"ok": true}\n```')]
                with self.assertLogs('generation.claude_client', level='WARNING'):
                    result = self.call(messages)
                self.assertEqual(result, {'ok': True})
                self.assertEqual(messages, original)
                self.assertEqual(create.call_args_list[0].kwargs['messages'], original)
                self.assertEqual(create.call_args_list[0].kwargs['max_tokens'], 100)
                second = create.call_args_list[1].kwargs
                self.assertEqual(second['max_tokens'], 200)
                self.assertNotIn('TRUNCATED PRIVATE CONTENT', str(second['messages']))
                self.assertEqual(len(second['messages']), 1)
                if isinstance(content, list):
                    self.assertEqual(second['messages'][0]['content'][:-1], content)
                    self.assertEqual(second['messages'][0]['content'][-1], {'type': 'text', 'text': CONCISION_MESSAGE})
                else:
                    self.assertEqual(second['messages'][0]['content'], content + '\n\n' + CONCISION_MESSAGE)

    def test_twice_truncated_has_short_error_and_capped_logs(self, anthropic):
        create = anthropic.return_value.messages.create
        create.return_value = response('PRIVATE' + 'x' * 3000, 'max_tokens')
        with self.assertLogs('generation.claude_client', level='WARNING') as logs:
            with self.assertRaises(ClaudeTruncatedError) as caught:
                self.call([{'role': 'user', 'content': 'original'}], retry_max_tokens=350)
        self.assertEqual(str(caught.exception), 'La respuesta de Claude se cortó por largo incluso tras reintentar.')
        self.assertNotIn('PRIVATE', str(caught.exception))
        self.assertEqual(create.call_count, 2)
        self.assertEqual(create.call_args.kwargs['max_tokens'], 350)
        self.assertEqual(len(logs.records), 2)
        self.assertTrue(all(len(record.args[0]) == 2000 for record in logs.records))

    def test_invalid_json_retry_uses_assistant_history(self, anthropic):
        create = anthropic.return_value.messages.create
        create.side_effect = [response('invalid response'), response('{"ok": true}')]
        original = [{'role': 'user', 'content': 'original'}]
        with self.assertLogs('generation.claude_client', level='WARNING'):
            self.assertEqual(self.call(original), {'ok': True})
        messages = create.call_args.kwargs['messages']
        self.assertEqual(messages[1], {'role': 'assistant', 'content': 'invalid response'})
        self.assertEqual(messages[2]['role'], 'user')
        self.assertEqual(create.call_args.kwargs['max_tokens'], 100)
        self.assertEqual(len(original), 1)

    def test_validation_errors_and_invalid_json_do_not_leak_raw(self, anthropic):
        create = anthropic.return_value.messages.create
        for raw, validate in (('PRIVATE invalid JSON', lambda data: data),
                              ('{"private": "response"}', Mock(side_effect=ValueError('PRIVATE validation error'))),
                              ('{}', Mock(side_effect=TypeError('PRIVATE validation error')))):
            with self.subTest(raw=raw):
                create.reset_mock()
                create.return_value = response(raw)
                with self.assertLogs('generation.claude_client', level='WARNING'):
                    with self.assertRaises(ValueError) as caught:
                        call_claude_json(system='system', messages=[{'role': 'user', 'content': 'original'}],
                                         max_tokens=100, validate=validate)
                self.assertEqual(str(caught.exception), 'Claude devolvió JSON inválido tras dos intentos.')
                self.assertNotIn('PRIVATE', str(caught.exception))
                self.assertEqual(create.call_count, 2)

    def test_validated_normalized_result_and_model(self, anthropic):
        anthropic.return_value.messages.create.return_value = response('{"name": "long"}')
        with patch.dict(os.environ, {'ANTHROPIC_MODEL': 'configured-model'}):
            result = call_claude_json(system='system', messages=[{'role': 'user', 'content': 'original'}],
                                      max_tokens=100, validate=lambda data: {'name': data['name'][:1]})
        self.assertEqual(result, {'name': 'l'})
        self.assertEqual(anthropic.return_value.messages.create.call_args.kwargs['model'], 'configured-model')

    def test_card_token_default_and_override(self, anthropic):
        create = anthropic.return_value.messages.create
        create.return_value = response(json.dumps(valid_prompt()))
        with patch.dict(os.environ):
            os.environ.pop('ANTHROPIC_CARD_MAX_TOKENS', None)
            build_visual_prompt(Card(name='Card'), 'theme')
            self.assertEqual(create.call_args.kwargs['max_tokens'], 4000)
            os.environ['ANTHROPIC_CARD_MAX_TOKENS'] = '5000'
            build_visual_prompt(Card(name='Card'), 'theme')
            self.assertEqual(create.call_args.kwargs['max_tokens'], 5000)
            create.side_effect = [response('cut', 'max_tokens'), response(json.dumps(valid_prompt()))]
            with self.assertLogs('generation.claude_client', level='WARNING'):
                build_visual_prompt(Card(name='Card'), 'theme')
            self.assertEqual(create.call_args.kwargs['max_tokens'], 10000)


class PlannerBatchTests(TestCase):
    @patch('generation.claude_client.Anthropic')
    def test_sixty_five_cards_in_three_batches(self, anthropic):
        deck = Deck.objects.create(name='Deck', raw_list='', theme='theme')
        for i in range(65):
            card = make_card(name=f'Card {i}')
            DeckCard.objects.create(deck=deck, card=card, raw_name=card.name, quantity=1, section='main')
        create = anthropic.return_value.messages.create
        def answer(**kwargs):
            data = json.loads(kwargs['messages'][0]['content'])
            assignments = [{'oracle_id': card['oracle_id'], 'element': card['name'], 'category': '',
                             'visual_brief': 'A short scene'} for card in data['cards']]
            return response(json.dumps({'assignments': assignments}))
        create.side_effect = answer
        result = build_theme_plan(deck)
        self.assertEqual(len(result), 65)
        self.assertEqual(create.call_count, 3)
        inputs = [json.loads(call.kwargs['messages'][0]['content']) for call in create.call_args_list]
        self.assertEqual([len(data['cards']) for data in inputs], [30, 30, 5])
        self.assertEqual([len(data['used_elements']) for data in inputs], [0, 30, 60])
        self.assertEqual(set(inputs[1]['used_elements']), {card['name'] for card in inputs[0]['cards']})
        self.assertEqual(set(inputs[2]['used_elements']), {card['name'] for data in inputs[:2] for card in data['cards']})
        self.assertTrue(all(call.kwargs['max_tokens'] == 8000 for call in create.call_args_list))


class ShortErrorTests(TestCase):
    def setUp(self):
        self.deck = Deck.objects.create(name='Deck', raw_list='', theme='theme')
        self.card = make_card()
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Card', quantity=1, section='main')
        ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id, element='Element', category='', visual_brief='Brief')

    @patch('generation.runner.connection.close')
    @patch('generation.runner.get_provider')
    @patch('generation.runner.generate_card_image', side_effect=RuntimeError('x' * 700))
    def test_job_card_error_is_capped(self, generate, provider, close):
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1, card_oracle_ids=[str(self.card.oracle_id)])
        with self.assertLogs('generation.runner', level='ERROR'):
            run_job(job.id)
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error_message, 'x' * 499 + '…')

    @patch('generation.runner.connection.close')
    @patch('generation.runner.get_provider', side_effect=RuntimeError('x' * 700))
    def test_job_startup_error_is_capped(self, provider, close):
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1, card_oracle_ids=[str(self.card.oracle_id)])
        with self.assertLogs('generation.runner', level='ERROR'):
            run_job(job.id)
        job.refresh_from_db()
        self.assertEqual(len(job.error_message), 500)
        self.assertTrue(job.error_message.endswith('…'))

    def test_502_messages_are_capped(self):
        paths = [('generation.views.render_card_image', f'/api/cards/{self.entry.id}/regenerate/'),
                 ('generation.views.build_theme_plan', f'/api/decks/{self.deck.id}/theme-plan/'),
                 ('generation.views.build_theme_questions', f'/api/decks/{self.deck.id}/theme-questions/')]
        for target, path in paths:
            with self.subTest(path=path), patch(target, side_effect=RuntimeError('x' * 700)):
                result = self.client.post(path, {}, content_type='application/json')
                self.assertEqual(result.status_code, 502)
                self.assertEqual(result.json()['detail'], 'x' * 499 + '…')

    @patch('generation.runner.build_visual_prompt', return_value=valid_prompt())
    @patch('generation.runner.get_fallback_provider')
    def test_composed_provider_errors_are_capped(self, fallback, build):
        primary, backup = Mock(), Mock()
        primary.name, backup.name = 'grok', 'gemini'
        primary.generate.side_effect = RuntimeError('x' * 700)
        backup.generate.side_effect = RuntimeError('y' * 700)
        fallback.return_value = backup
        with self.assertLogs('generation.runner', level='WARNING'):
            with self.assertRaises(RuntimeError) as caught:
                render_card_image(self.card, self.deck.theme, primary)
        self.assertEqual(len(str(caught.exception)), 500)
        self.assertTrue(str(caught.exception).endswith('…'))
        self.assertEqual(primary.generate.call_count, 2)
        backup.generate.assert_called_once()
