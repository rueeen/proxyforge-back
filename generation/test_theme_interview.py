import hashlib
import json
import os
from copy import deepcopy
from datetime import timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PIL import Image
from django.test import SimpleTestCase, TestCase
from django.utils import timezone

from decks.models import Deck, DeckCard
from .models import GeneratedImage, GenerationJob, ThemeAssignment
from .prompt_builder import build_visual_prompt, make_cache_key
from .runner import run_job, generate_card_image
from .selection import card_category, is_basic_land
from .tests import make_card, valid_prompt
from .theme_interview import build_theme_questions, deck_brief
from .theme_planner import build_theme_plan


def question_payload():
    return {'questions': [{'question': '¿Quién representa al comandante?', 'help': 'Sacrificio negro-rojo',
                           'options': [{'label': 'Arthas', 'detail': 'No-muertos'}, {'label': "Gul’dan", 'detail': 'Magia vil'}]}]}


def stored_questions():
    return [{'id': 'q1', 'question': '¿Quién representa al comandante?', 'help': '', 'multiple': False,
             'allow_other': True, 'options': [{'id': 'a', 'label': 'Arthas', 'detail': ''},
                                              {'id': 'b', 'label': "Gul’dan", 'detail': ''}]},
            {'id': 'q2', 'question': '¿Qué estilo?', 'help': '', 'multiple': True,
             'allow_other': False, 'options': [{'id': 'a', 'label': 'Cinemáticas', 'detail': ''},
                                               {'id': 'b', 'label': 'Hearthstone', 'detail': ''}]}]


def response(data):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(data, ensure_ascii=False))])


class CategoryTests(SimpleTestCase):
    def test_english_and_spanish_categories(self):
        cases = {
            'creature': ('Creature — Human', 'Criatura — Humano'),
            'legendary_creature': ('Legendary Creature — Elf', 'Criatura legendaria — Elfo'),
            'planeswalker': ('Legendary Planeswalker — Jace', 'Planeswalker legendario — Jace'),
            'equipment': ('Artifact — Equipment', 'Artefacto — Equipo'),
            'vehicle': ('Artifact — Vehicle', 'Artefacto — Vehículo'),
            'artifact_other': ('Artifact', 'Artefacto'),
            'enchantment': ('Enchantment', 'Encantamiento'),
            'instant': ('Instant', 'Instantáneo'), 'sorcery': ('Sorcery', 'Conjuro'),
            'land': ('Land — Desert', 'Tierra — Desierto'),
            'basic_land': ('Basic Snow Land — Island', 'Tierra básica nevada — Isla'),
        }
        for expected, lines in cases.items():
            for line in lines:
                with self.subTest(line=line):
                    self.assertEqual(card_category(line), expected)
                    self.assertEqual(is_basic_land(line), expected == 'basic_land')


@patch.dict(os.environ, {'IMAGE_PROVIDER': 'gemini', 'IMAGE_FALLBACK_PROVIDER': ''})
class InterviewTests(TestCase):
    def setUp(self):
        self.deck = Deck.objects.create(card_layout="classic", name='Warcraft', theme='Warcraft', raw_list='', theme_questions=stored_questions())
        self.card = make_card(name='Commander', type_line='Legendary Creature — Vampire', color_identity=['B', 'R'])
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Commander', quantity=1, section='commander')

    def save_answers(self, answers):
        return self.client.put(f'/api/decks/{self.deck.id}/theme-answers/', {'answers': answers}, content_type='application/json')

    def add_card(self, **kwargs):
        card = make_card(**kwargs)
        DeckCard.objects.create(deck=self.deck, card=card, raw_name=card.name, quantity=4, section='main')
        return card

    @patch('generation.claude_client.Anthropic')
    def test_questions_use_actual_deck_and_assign_ids(self, anthropic):
        land = self.add_card(name='Island', type_line='Basic Land — Island', color_identity=['U'], cmc=99)
        legendary = self.add_card(name='Legendary ally', type_line='Criatura legendaria — Elfo', cmc=1)
        for i in range(15):
            self.add_card(name=f'Creature {i}', type_line='Creature', cmc=i+2, oracle_text='x' * 250)
        DeckCard.objects.create(deck=self.deck, card=land, raw_name='Duplicate Island', quantity=8, section='main')
        DeckCard.objects.create(deck=self.deck, raw_name='Unresolved', quantity=1, section='commander')
        create = anthropic.return_value.messages.create
        create.return_value = response(question_payload())
        questions = build_theme_questions(self.deck)
        self.assertEqual(questions[0]['id'], 'q1')
        self.assertEqual([option['id'] for option in questions[0]['options']], ['a', 'b'])
        self.assertFalse(questions[0]['multiple'])
        self.assertTrue(questions[0]['allow_other'])
        data = json.loads(create.call_args.kwargs['messages'][0]['content'])
        self.assertEqual(data['commanders'][0]['name'], self.card.name)
        self.assertEqual(data['color_identity'], ['B', 'R', 'U'])
        self.assertEqual(data['type_counts']['basic_land'], 1)
        self.assertEqual(data['type_counts']['legendary_creature'], 2)
        self.assertEqual(data['type_counts']['creature'], 15)
        self.assertEqual(len(data['notable_cards']), 12)
        self.assertEqual(data['notable_cards'][0]['name'], legendary.name)
        self.assertEqual(data['notable_cards'][1]['name'], 'Creature 14')
        self.assertTrue(all(item['name'] not in ('Commander', 'Island') and len(item['oracle_text']) <= 200
                            for item in data['notable_cards']))
        self.assertEqual(create.call_args.kwargs['max_tokens'], 3000)

    @patch('generation.claude_client.Anthropic')
    def test_truncation_defaults_and_generated_ids(self, anthropic):
        data = question_payload()
        data['questions'] *= 5
        data = deepcopy(data)
        for question in data['questions']:
            question['id'] = 'untrusted'
            question['help'] = 'h' * 170
            for option in question['options']:
                option.update(id='untrusted', label='a' * 90, detail='d' * 150)
        anthropic.return_value.messages.create.return_value = response(data)
        questions = build_theme_questions(self.deck)
        self.assertEqual([q['id'] for q in questions], ['q1', 'q2', 'q3', 'q4', 'q5'])
        self.assertEqual(len(questions[0]['help']), 160)
        self.assertEqual(len(questions[0]['options'][0]['label']), 80)
        self.assertEqual(len(questions[0]['options'][0]['detail']), 140)

    @patch('generation.claude_client.Anthropic')
    def test_invalid_question_retries_once(self, anthropic):
        data = question_payload()
        data['questions'][0]['options'] = [{'label': 'Only one'}]
        create = anthropic.return_value.messages.create
        create.return_value = response(data)
        with self.assertRaisesRegex(ValueError, 'dos intentos'):
            build_theme_questions(self.deck)
        self.assertEqual(create.call_count, 2)
        self.assertEqual(create.call_args.kwargs['messages'][-1]['role'], 'user')
        create.reset_mock()
        create.side_effect = [response(data), response(question_payload())]
        self.assertEqual(build_theme_questions(self.deck)[0]['id'], 'q1')
        self.assertEqual(create.call_count, 2)

    @patch('generation.claude_client.Anthropic')
    def test_zero_questions_and_invalid_boolean(self, anthropic):
        create = anthropic.return_value.messages.create
        create.return_value = response({'questions': []})
        self.assertEqual(build_theme_questions(self.deck), [])
        for field in ('multiple', 'allow_other'):
            data = question_payload()
            data['questions'][0][field] = 'false'
            create.return_value = response(data)
            with self.subTest(field=field), self.assertRaises(ValueError):
                build_theme_questions(self.deck)

    @patch('generation.views.build_theme_questions')
    def test_questions_endpoint_replaces_and_resets(self, build):
        self.deck.theme_answers = {'q1': {'option_ids': ['a'], 'other': ''}}
        self.deck.theme_answers_updated_at = timezone.now()
        self.deck.save()
        build.return_value = []
        result = self.client.post(f'/api/decks/{self.deck.id}/theme-questions/')
        self.assertEqual(result.status_code, 200)
        self.deck.refresh_from_db()
        self.assertEqual(self.deck.theme_questions, [])
        self.assertEqual(self.deck.theme_answers, {})
        self.assertIsNone(self.deck.theme_answers_updated_at)
        self.assertEqual(result.json()['theme_brief'], [])
        self.assertIn('theme_plan_stale', result.json())

    @patch('generation.views.build_theme_questions', side_effect=RuntimeError('Claude unavailable'))
    def test_questions_failure_and_active_job(self, build):
        url = f'/api/decks/{self.deck.id}/theme-questions/'
        result = self.client.post(url)
        self.assertEqual((result.status_code, result.json()), (502, {'detail': 'Claude unavailable'}))
        self.deck.refresh_from_db()
        self.assertEqual(self.deck.theme_questions, stored_questions())
        GenerationJob.objects.create(deck=self.deck, total_cards=1)
        build.reset_mock()
        self.assertEqual(self.client.post(url).status_code, 409)
        build.assert_not_called()
        self.assertEqual(self.save_answers({}).status_code, 409)

    def test_valid_answers_replace_and_brief(self):
        result = self.save_answers({'q1': {'option_ids': ['a'], 'other': 'Otro personaje'},
                                    'q2': {'option_ids': ['a', 'b'], 'other': ''}})
        self.assertEqual(result.status_code, 200)
        self.deck.refresh_from_db()
        self.assertIsNotNone(self.deck.theme_answers_updated_at)
        expected = [{'question': stored_questions()[0]['question'], 'answer': 'Arthas, Otra: Otro personaje'},
                    {'question': '¿Qué estilo?', 'answer': 'Cinemáticas, Hearthstone'}]
        self.assertEqual(deck_brief(self.deck), expected)
        self.assertEqual(result.json()['theme_brief'], expected)
        result = self.save_answers({'q1': {'option_ids': [], 'other': 'Solo otra'}})
        self.assertEqual(result.json()['theme_answers'], {'q1': {'option_ids': [], 'other': 'Solo otra'}})
        self.assertEqual(result.json()['theme_brief'][0]['answer'], 'Otra: Solo otra')
        self.assertEqual(self.save_answers({}).json()['theme_brief'], [])

    def test_invalid_answers(self):
        cases = [None, [], {'unknown': {'option_ids': ['a']}}, {'q1': {'option_ids': ['unknown']}},
                 {'q1': {'option_ids': ['a', 'b']}}, {'q2': {'option_ids': [], 'other': 'no'}},
                 {'q1': {'option_ids': [], 'other': ''}}, {'q1': {'option_ids': 'a'}},
                 {'q1': {'option_ids': [False]}}, {'q1': {'option_ids': ['a'], 'other': 'x' * 201}},
                 {'q1': {'option_ids': ['a'], 'other': 123}}, {'q1': {'option_ids': ['a', 'a']}}]
        for answers in cases:
            with self.subTest(answers=answers):
                result = self.save_answers(answers)
                self.assertEqual(result.status_code, 400)
                self.assertEqual(list(result.json()), ['answers'])
                self.assertIsInstance(result.json()['answers'], list)
        self.deck.refresh_from_db()
        self.assertEqual(self.deck.theme_answers, {})

    @patch('generation.claude_client.Anthropic')
    def test_planner_and_card_prompt_use_brief(self, planner):
        visual = planner
        self.save_answers({'q1': {'option_ids': ['a']}})
        self.deck.refresh_from_db()
        expected = deck_brief(self.deck)
        planner.return_value.messages.create.return_value = response({'assignments': [
            {'oracle_id': str(self.card.oracle_id), 'element': 'Arthas', 'category': 'Comandante', 'visual_brief': 'Arthas al mando'}]})
        planned = build_theme_plan(self.deck)[0]
        data = json.loads(planner.return_value.messages.create.call_args.kwargs['messages'][0]['content'])
        self.assertEqual(data['brief'], expected)
        visual.return_value.messages.create.return_value = response(valid_prompt())
        build_visual_prompt(self.card, self.deck.theme, planned, expected)
        data = json.loads(visual.return_value.messages.create.call_args.kwargs['messages'][0]['content'][0]['text'])
        self.assertEqual(data['brief'], expected)
        build_visual_prompt(self.card, self.deck.theme, planned, [])
        data = json.loads(visual.return_value.messages.create.call_args.kwargs['messages'][0]['content'][0]['text'])
        self.assertNotIn('brief', data)

    def test_legacy_cache_key_and_invalidating_answers(self):
        planned = ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id,
                                                 element='Arthas', category='', visual_brief='Una escena')
        legacy = hashlib.sha256(f'{self.card.oracle_id}|warcraft|v3|gemini|arthas|una escena'.encode()).hexdigest()
        self.assertEqual(make_cache_key(self.card.oracle_id, 'Warcraft', 'gemini', planned, []), legacy)
        self.assertEqual(make_cache_key(self.card.oracle_id, 'Warcraft', 'gemini', planned), legacy)
        self.assertEqual(make_cache_key(self.card.oracle_id, 'Warcraft', 'gemini', brief=[]),
                         hashlib.sha256(f'{self.card.oracle_id}|warcraft|v3|gemini'.encode()).hexdigest())
        self.save_answers({'q1': {'option_ids': ['a']}})
        self.deck.refresh_from_db()
        brief = deck_brief(self.deck)
        key = make_cache_key(self.card.oracle_id, self.deck.theme, 'gemini', planned, brief)
        canonical = json.dumps(brief, sort_keys=True, ensure_ascii=False).lower()
        self.assertEqual(key, hashlib.sha256((f'{self.card.oracle_id}|warcraft|v3|gemini|arthas|una escena|brief:' + canonical).encode()).hexdigest())
        self.assertNotEqual(key, legacy)
        GeneratedImage.objects.create(card=self.card, theme=self.deck.theme, cache_key=key, visual_prompt='',
                                      prompt_json={}, image='generated/mock.png', provider='gemini', prompt_version='v3')
        self.assertEqual(self.client.get(f'/api/decks/{self.deck.id}/estimate/').json()['cached'], 1)
        detail = self.client.get(f'/api/decks/{self.deck.id}/').json()
        self.assertIsNotNone(detail['cards'][0]['generated_image'])
        self.save_answers({'q1': {'option_ids': ['b']}})
        self.assertEqual(self.client.get(f'/api/decks/{self.deck.id}/estimate/').json()['to_generate'], 1)
        self.assertIsNone(self.client.get(f'/api/decks/{self.deck.id}/').json()['cards'][0]['generated_image'])

    @patch('generation.runner.connection.close')
    @patch('generation.views.start_plan_job')
    @patch('generation.claude_client.Anthropic')
    def test_plan_stale_then_rebuilt(self, anthropic, start, close):
        from .runner import run_plan_job
        start.side_effect = lambda job: run_plan_job(job.pk)
        planned = ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id,
                                                 element='Arthas', category='', visual_brief='Una escena')
        ThemeAssignment.objects.filter(pk=planned.pk).update(updated_at=timezone.now()-timedelta(minutes=5))
        result = self.save_answers({'q1': {'option_ids': ['a']}})
        self.assertTrue(result.json()['theme_plan_stale'])
        anthropic.return_value.messages.create.return_value = response({'assignments': [
            {'oracle_id': str(self.card.oracle_id), 'element': 'Arthas', 'category': '', 'visual_brief': 'Una escena'}]})
        result = self.client.post(f'/api/decks/{self.deck.id}/theme-plan/', {}, content_type='application/json')
        self.assertEqual(result.status_code, 202)
        result = self.client.get(f'/api/decks/{self.deck.id}/')
        self.assertFalse(result.json()['theme_plan_stale'])
        ThemeAssignment.objects.update(edited=True)
        self.assertFalse(self.save_answers({'q1': {'option_ids': ['b']}}).json()['theme_plan_stale'])
        self.add_card(name='Unassigned')
        self.assertFalse(self.client.get(f'/api/decks/{self.deck.id}/').json()['theme_plan_stale'])

    def test_detail_query_count_with_brief(self):
        self.save_answers({'q1': {'option_ids': ['a']}})
        ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id, element='Element', category='', visual_brief='Brief')
        url = f'/api/decks/{self.deck.id}/'
        with self.assertNumQueries(8):
            self.client.get(url)
        for _ in range(15):
            card = self.add_card()
            ThemeAssignment.objects.create(deck=self.deck, oracle_id=card.oracle_id, element='Element', category='', visual_brief='Brief')
        with self.assertNumQueries(8):
            self.client.get(url)

    @patch('generation.runner.connection.close')
    @patch('generation.runner.get_provider')
    @patch('generation.runner.generate_card_image')
    def test_job_passes_brief(self, generate, provider, close):
        self.save_answers({'q1': {'option_ids': ['a']}})
        self.deck.refresh_from_db()
        ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id, element='Arthas', category='', visual_brief='Brief')
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1, card_oracle_ids=[str(self.card.oracle_id)])
        run_job(job.id)
        self.assertEqual(generate.call_args.args[4], deck_brief(self.deck))

    @patch('generation.views.render_card_image')
    @patch('generation.views.save_card_image')
    @patch('generation.views.get_provider')
    def test_regeneration_passes_brief_and_cache_key(self, provider, save, render):
        self.save_answers({'q1': {'option_ids': ['a']}})
        self.deck.refresh_from_db()
        brief = deck_brief(self.deck)
        provider.return_value.name = 'gemini'
        render.return_value = ({}, b'bytes')
        image = GeneratedImage(pk=999, card=self.card, theme=self.deck.theme, image='generated/mock.png', prompt_json={})
        save.return_value = image
        result = self.client.post(f'/api/cards/{self.entry.id}/regenerate/', {}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(render.call_args.args[4], brief)
        self.assertEqual(save.call_args.args[6], brief)
        self.assertIsNotNone(result.json()['generated_image'])

    @patch('generation.runner.build_visual_prompt', side_effect=lambda *args, **kwargs: valid_prompt())
    def test_saved_image_uses_brief_and_cache_hit(self, build):
        import tempfile
        with tempfile.TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            brief = [{'question': 'Estilo', 'answer': 'Cinemáticas'}]
            provider = Mock()
            provider.name = 'gemini'
            stream = BytesIO()
            Image.new('RGB', (2, 2)).save(stream, format='PNG')
            provider.generate.return_value = stream.getvalue()
            image = generate_card_image(self.card, self.deck.theme, provider, brief=brief)
            self.assertEqual(image.cache_key, make_cache_key(self.card.oracle_id, self.deck.theme, 'gemini', brief=brief))
            build.assert_called_once_with(self.card, self.deck.theme, None, brief, 'classic', reference_count=0, original_art_reference=False)
            with patch('generation.runner.render_card_image') as render:
                self.assertEqual(generate_card_image(self.card, self.deck.theme, provider, brief=brief).pk, image.pk)
            render.assert_not_called()
