import json
import os
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from django.test import TestCase

from decks.models import Deck, DeckCard
from .models import GeneratedImage, GenerationJob, ThemeAssignment
from .prompt_builder import build_visual_prompt, make_cache_key
from .runner import run_job
from .theme_planner import build_theme_plan
from .tests import make_card, valid_prompt


def response(assignments):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps({'assignments': assignments}))])


def assignment(card, element='Storm Eagle'):
    return dict(oracle_id=str(card.oracle_id), element=element, category='Jefe', visual_brief='Vuela sobre la aeronave')


@patch.dict(os.environ, {'IMAGE_PROVIDER': 'gemini', 'MAX_CARDS_PER_JOB': '5', 'COST_PER_IMAGE_USD': '0.04'})
class BatchAndPlanTests(TestCase):
    def setUp(self):
        self.deck = Deck.objects.create(name='Deck', raw_list='', theme='Megaman X')
        self.entries = []
        for i, section in enumerate(['sideboard', 'main', 'commander', 'main', 'main', 'main', 'main', 'sideboard']):
            self.entries.append(DeckCard.objects.create(deck=self.deck, card=make_card(name=f'Card {i}'),
                                                       raw_name=f'Card {i}', quantity=1, section=section))
        DeckCard.objects.create(deck=self.deck, card=self.entries[1].card, raw_name='Duplicate', quantity=2, section='main')
        DeckCard.objects.create(deck=self.deck, raw_name='Unresolved', quantity=1, section='main')

    def cached(self, entry, assignment=None):
        return GeneratedImage.objects.create(card=entry.card, theme=self.deck.theme,
            cache_key=make_cache_key(entry.card.oracle_id, self.deck.theme, 'gemini', assignment),
            visual_prompt='', prompt_json={}, image='generated/mock.png', provider='gemini', prompt_version='v3')

    def generate(self, data=None):
        with patch('generation.views.start_job'):
            return self.client.post(f'/api/decks/{self.deck.pk}/generate/', data or {}, content_type='application/json')

    def test_default_order_pending_and_unique(self):
        self.cached(self.entries[1])
        response = self.generate()
        self.assertEqual(response.status_code, 202)
        job = GenerationJob.objects.get()
        self.assertEqual(job.total_cards, 5)
        self.assertEqual(job.card_oracle_ids, [str(self.entries[i].card.oracle_id) for i in (2, 3, 4, 5, 6)])

    def test_limit_validation_and_clamping(self):
        for value in (0, -1, True, 1.5, '3', None, [], {}):
            with self.subTest(value=value):
                result = self.generate({'limit': value})
                self.assertEqual(result.status_code, 400)
                self.assertEqual(result.json(), {'limit': ['Debe ser un entero mayor o igual a 1.']})
        for value, expected in ((2, 2), (99, 5)):
            result = self.generate({'limit': value})
            self.assertEqual(result.status_code, 202)
            self.assertEqual(GenerationJob.objects.latest('id').total_cards, expected)
            GenerationJob.objects.update(status='done')

    def test_no_pending_and_estimate(self):
        self.cached(self.entries[0])
        estimate = self.client.get(f'/api/decks/{self.deck.pk}/estimate/').json()
        self.assertEqual(estimate, dict(unique_cards=8, cached=1, to_generate=7, estimated_cost_usd=0.28,
                                       batch_limit=5, batch_size=5, batch_cost_usd=0.20))
        for entry in self.entries[1:]:
            self.cached(entry)
        result = self.generate()
        self.assertEqual(result.status_code, 400)
        self.assertEqual(result.json(), {'detail': 'No hay cartas pendientes de generar.'})

    @patch('generation.theme_planner.Anthropic')
    def test_plan_missing_retry_edited_cleanup(self, anthropic):
        edited = ThemeAssignment.objects.create(deck=self.deck, **assignment(self.entries[0].card, 'Zero'), edited=True)
        stale = ThemeAssignment.objects.create(deck=self.deck, oracle_id=uuid4(), element='Old', category='', visual_brief='Old')
        cards = [entry.card for entry in self.entries[1:]]
        create = anthropic.return_value.messages.create
        create.side_effect = [response([assignment(card, f'Element {i}') for i, card in enumerate(cards[:-1])]),
                              response([assignment(cards[-1], 'Last')])]
        planned = build_theme_plan(self.deck)
        self.assertEqual(len(planned), 8)
        self.assertEqual(create.call_count, 2)
        first = json.loads(create.call_args_list[0].kwargs['messages'][0]['content'])
        self.assertEqual(len(first['cards']), 7)
        self.assertNotIn(str(edited.oracle_id), [card['oracle_id'] for card in first['cards']])
        self.assertIn('Zero', first['used_elements'])
        second = json.loads(create.call_args_list[1].kwargs['messages'][0]['content'])
        self.assertEqual([card['oracle_id'] for card in second['cards']], [str(cards[-1].oracle_id)])
        self.assertEqual(create.call_args.kwargs['max_tokens'], 16000)
        edited.refresh_from_db()
        self.assertEqual(edited.element, 'Zero')
        self.assertTrue(edited.edited)
        self.assertFalse(ThemeAssignment.objects.filter(pk=stale.pk).exists())

    @patch('generation.theme_planner.Anthropic')
    def test_missing_ids_error_and_duplicate_validation(self, anthropic):
        create = anthropic.return_value.messages.create
        create.return_value = response([dict(oracle_id=str(uuid4()), element='Unknown')])
        with self.assertRaisesRegex(ValueError, 'Card 0'):
            build_theme_plan(self.deck)
        self.assertEqual(create.call_count, 2)
        self.assertFalse(self.deck.theme_assignments.exists())
        create.return_value = response([assignment(self.entries[0].card)] * 2)
        with self.assertRaisesRegex(ValueError, 'duplicado'):
            build_theme_plan(self.deck)

    @patch('generation.theme_planner.Anthropic')
    def test_overwrite_edited(self, anthropic):
        for i, entry in enumerate(self.entries):
            ThemeAssignment.objects.create(deck=self.deck, **assignment(entry.card, f'Old {i}'), edited=True)
        anthropic.return_value.messages.create.return_value = response([
            assignment(entry.card, f'New {i}') for i, entry in enumerate(self.entries)])
        planned = build_theme_plan(self.deck, overwrite_edited=True)
        self.assertTrue(all(not item.edited and item.element.startswith('New') for item in planned))

    def test_patch_invalidates_cache_and_detail_has_plan(self):
        entry = self.entries[0]
        self.cached(entry)
        data = dict(element='Storm Eagle', category='', visual_brief='Storm Eagle vuela')
        result = self.client.patch(f'/api/cards/{entry.pk}/theme-assignment/', data, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['theme_assignment'], {**data, 'edited': True})
        self.assertIsNone(result.json()['generated_image'])
        self.assertEqual(self.client.get(f'/api/decks/{self.deck.pk}/estimate/').json()['cached'], 0)
        planned = ThemeAssignment.objects.get()
        self.assertNotEqual(make_cache_key(entry.card.oracle_id, self.deck.theme, 'gemini'),
                            make_cache_key(entry.card.oracle_id, self.deck.theme, 'gemini', planned))
        self.assertEqual(make_cache_key(entry.card.oracle_id, self.deck.theme, 'gemini', planned),
                         make_cache_key(entry.card.oracle_id, self.deck.theme, 'gemini', SimpleNamespace(
                             element='  STORM   EAGLE ', visual_brief='STORM EAGLE VUELA')))
        self.assertFalse(self.client.get(f'/api/decks/{self.deck.pk}/').json()['has_theme_plan'])
        for other in self.entries[1:]:
            ThemeAssignment.objects.create(deck=self.deck, **assignment(other.card, other.raw_name))
        detail = self.client.get(f'/api/decks/{self.deck.pk}/').json()
        self.assertTrue(detail['has_theme_plan'])
        self.assertIsNone(detail['cards'][-1]['theme_assignment'])

    def test_patch_validation(self):
        url = f'/api/cards/{self.entries[0].pk}/theme-assignment/'
        for data in ({}, dict(element='', category='', visual_brief='x'),
                     dict(element='x', category='', visual_brief=' '),
                     dict(element=1, category='', visual_brief='x')):
            self.assertEqual(self.client.patch(url, data, content_type='application/json').status_code, 400)
        unresolved = self.deck.cards.filter(card=None).get()
        self.assertEqual(self.client.patch(f'/api/cards/{unresolved.pk}/theme-assignment/', {}, content_type='application/json').status_code, 400)

    @patch('generation.views.build_theme_plan')
    def test_plan_endpoint_and_conflict(self, build):
        url = f'/api/decks/{self.deck.pk}/theme-plan/'
        self.assertEqual(self.client.post(url, {}, content_type='application/json').status_code, 200)
        build.assert_called_once_with(self.deck, overwrite_edited=False)
        build.side_effect = RuntimeError('Claude unavailable')
        result = self.client.post(url, {}, content_type='application/json')
        self.assertEqual(result.status_code, 502)
        self.assertEqual(result.json(), {'detail': 'Claude unavailable'})
        GenerationJob.objects.create(deck=self.deck, total_cards=1)
        self.assertEqual(self.client.post(url, {}, content_type='application/json').status_code, 409)

    @patch('generation.prompt_builder.Anthropic')
    def test_visual_prompt_assignment_input(self, anthropic):
        anthropic.return_value.messages.create.return_value = SimpleNamespace(
            content=[SimpleNamespace(type='text', text=json.dumps(valid_prompt()))])
        planned = ThemeAssignment(**assignment(self.entries[0].card))
        build_visual_prompt(self.entries[0].card, self.deck.theme, planned)
        data = json.loads(anthropic.return_value.messages.create.call_args.kwargs['messages'][0]['content'][0]['text'])
        self.assertEqual(data['assignment']['element'], 'Storm Eagle')

    @patch('generation.runner.connection.close')
    @patch('generation.runner.generate_card_image')
    @patch('generation.runner.get_provider')
    @patch('generation.runner.build_theme_plan')
    def test_run_job_selected_only_and_plan_failure(self, build, provider, generate, close):
        selected = self.entries[2]
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1, card_oracle_ids=[str(selected.card.oracle_id)])
        def plan(deck):
            ThemeAssignment.objects.create(deck=deck, **assignment(selected.card))
        build.side_effect = plan
        run_job(job.id)
        build.assert_called_once_with(self.deck)
        self.assertEqual(generate.call_count, 1)
        self.assertEqual(generate.call_args.args[0], selected.card)
        self.assertEqual(generate.call_args.args[3].element, 'Storm Eagle')
        job.refresh_from_db()
        self.assertEqual((job.status, job.completed_cards), ('done', 1))
        build.reset_mock()
        generate.reset_mock()
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1, card_oracle_ids=[str(self.entries[0].card.oracle_id)])
        build.side_effect = ValueError('Plan failed')
        with self.assertLogs('generation.runner', level='ERROR'):
            run_job(job.id)
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_message), ('failed', 'Plan failed'))
        generate.assert_not_called()

    def test_detail_queries_constant_with_assignments(self):
        for i, entry in enumerate(self.entries):
            item = ThemeAssignment.objects.create(deck=self.deck, **assignment(entry.card, f'Element {i}'))
            self.cached(entry, item)
        url = f'/api/decks/{self.deck.pk}/'
        with self.assertNumQueries(7):
            result = self.client.get(url)
        self.assertTrue(result.json()['has_theme_plan'])
        self.assertIsNotNone(result.json()['cards'][0]['generated_image'])
        for i in range(12):
            card = make_card()
            DeckCard.objects.create(deck=self.deck, card=card, raw_name='Extra', quantity=1, section='main')
            ThemeAssignment.objects.create(deck=self.deck, **assignment(card, f'Extra {i}'))
        with self.assertNumQueries(7):
            self.client.get(url)

    @patch('generation.runner.connection.close')
    @patch('generation.runner.generate_card_image')
    @patch('generation.runner.get_provider')
    @patch('generation.runner.build_theme_plan')
    def test_existing_plan_is_not_rebuilt(self, build, provider, generate, close):
        entry = self.entries[0]
        ThemeAssignment.objects.create(deck=self.deck, **assignment(entry.card))
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1, card_oracle_ids=[str(entry.card.oracle_id)])
        run_job(job.id)
        build.assert_not_called()
        self.assertEqual(generate.call_count, 1)

    @patch('generation.views.get_provider')
    @patch('generation.views.render_card_image')
    @patch('generation.views.save_card_image')
    def test_regenerate_uses_assignment_without_planning(self, save, render, provider):
        entry = self.entries[0]
        planned = ThemeAssignment.objects.create(deck=self.deck, **assignment(entry.card))
        image = self.cached(entry, planned)
        provider.return_value.name = 'gemini'
        render.return_value = ({}, b'bytes')
        # Only rendering/persistence are mocked; the response uses the actual serializers.
        save.return_value = image
        with patch('generation.views.build_theme_plan') as build:
            result = self.client.post(f'/api/cards/{entry.pk}/regenerate/', {}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(render.call_args.args[-1], planned)
        self.assertEqual(save.call_args.args[-1], planned)
        self.assertEqual(result.json()['theme_assignment']['element'], 'Storm Eagle')
        build.assert_not_called()
