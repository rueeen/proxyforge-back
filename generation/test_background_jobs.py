import json
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from unittest.mock import Mock, patch

from django.db.backends.utils import CursorWrapper
from django.test import TestCase, SimpleTestCase
from django.utils import timezone

from decks.models import Deck, DeckCard
from .jobs import expire_stale_jobs
from .models import GenerationJob, ThemeAssignment
from .runner import run_plan_job, run_references_job, run_job, start_plan_job, start_references_job
from .serializers import GenerationJobSerializer
from .test_wiki_images import response
from .test_wiki_sites import claude_response
from .tests import make_card
from .theme_planner import build_theme_plan
from .wiki_sites import verify_assignment_pages


@patch.dict(os.environ, {'PLAN_BATCH_SIZE': '20', 'PLAN_PARALLEL_BATCHES': '3'})
class ParallelPlannerTests(TestCase):
    def setUp(self):
        self.deck = Deck.objects.create(name='Deck', raw_list='', theme='Theme')

    def entries(self, count):
        return [DeckCard.objects.create(deck=self.deck, card=make_card(name=f'Card{i}'), raw_name=f'Card{i}',
                                       quantity=1, section='main') for i in range(count)]

    def answer(self, payload, duplicate=False):
        return claude_response({'assignments': [dict(oracle_id=card['oracle_id'], element=(
            'Shared' if duplicate and card['name'] in ('Card0', 'Card20') and not payload['used_elements'] else card['name']),
            category='', visual_brief='Una escena', reference=None) for card in payload['cards']]})

    @patch('generation.claude_client.Anthropic')
    def test_parallel_batches_priority_model_and_no_worker_database_access(self, sdk):
        entries = self.entries(45)
        entries[-1].section = 'commander'
        entries[-1].save()
        entries[-2].card.type_line = 'Legendary Creature'
        entries[-2].card.save()
        main = threading.get_ident()
        worker_ids = set()
        barrier = threading.Barrier(3)
        def answer(**kwargs):
            self.assertNotEqual(threading.get_ident(), main)
            worker_ids.add(threading.get_ident())
            barrier.wait(timeout=5)
            return self.answer(json.loads(kwargs['messages'][0]['content']))
        sdk.return_value.messages.create.side_effect = answer
        execute = CursorWrapper.execute
        writes = []
        def guarded(cursor, sql, params=None):
            self.assertEqual(threading.get_ident(), main, 'Un worker accedió a SQLite')
            if sql.lstrip().upper().startswith(('INSERT', 'UPDATE', 'DELETE')):
                writes.append(sql)
            return execute(cursor, sql, params)
        progress = Mock()
        with patch('generation.theme_planner.ThreadPoolExecutor', wraps=ThreadPoolExecutor) as executor, \
                patch.object(CursorWrapper, 'execute', guarded), \
                patch.dict(os.environ, {'ANTHROPIC_PLAN_MODEL': 'fast-plan-model'}), \
                self.assertLogs('generation.theme_planner', level='INFO') as logs:
            result = build_theme_plan(self.deck, progress=progress)
        executor.assert_called_once_with(max_workers=3)
        self.assertEqual(len(result), 45)
        self.assertEqual(len(worker_ids), 3)
        self.assertTrue(writes)
        calls = sdk.return_value.messages.create.call_args_list
        self.assertEqual(len(calls), 3)
        batches = [json.loads(call.kwargs['messages'][0]['content']) for call in calls]
        first = next(batch for batch in batches if any(card['section'] == 'commander' for card in batch['cards']))
        self.assertEqual([card['name'] for card in first['cards'][:2]], ['Card44', 'Card43'])
        self.assertEqual(sorted(len(batch['cards']) for batch in batches), [5, 20, 20])
        self.assertTrue(all(call.kwargs['max_tokens'] == 5000 and call.kwargs['model'] == 'fast-plan-model' for call in calls))
        self.assertTrue(any('Verificación de páginas' in line for line in logs.output))
        self.assertEqual(progress.call_args.args[1:], (3, 3))

    @patch('generation.claude_client.Anthropic')
    def test_duplicates_merge_in_batch_order_then_one_sequential_pass(self, sdk):
        entries = self.entries(21)
        main = threading.get_ident()
        threads = []
        payloads = []
        def answer(**kwargs):
            payload = json.loads(kwargs['messages'][0]['content'])
            threads.append(threading.get_ident())
            payloads.append(payload)
            return self.answer(payload, duplicate=True)
        sdk.return_value.messages.create.side_effect = answer
        # Merge must retain the first batch's element even when completion is reversed.
        with patch('generation.theme_planner.as_completed', side_effect=lambda futures: reversed(list(futures))):
            progress = Mock()
            result = build_theme_plan(self.deck, progress=progress)
        self.assertEqual(len(result), 21)
        self.assertEqual(len(payloads), 3)
        self.assertEqual(threads[-1], main)
        self.assertTrue(all(identity != main for identity in threads[:2]))
        self.assertEqual([card['name'] for card in payloads[-1]['cards']], ['Card20'])
        self.assertEqual(len(payloads[-1]['used_elements']), 20)
        self.assertEqual(ThemeAssignment.objects.get(oracle_id=entries[0].card.oracle_id).element, 'Shared')
        self.assertEqual(ThemeAssignment.objects.get(oracle_id=entries[20].card.oracle_id).element, 'Card20')
        self.assertTrue(any(call.args[0] == 'Completando cartas faltantes' for call in progress.call_args_list))
        self.assertEqual(progress.call_args.args[1:], (3, 3))


class BackgroundEndpointTests(TestCase):
    def setUp(self):
        self.deck = Deck.objects.create(name='Deck', theme='Theme', raw_list='')
        self.card = make_card()
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Card', quantity=1, section='main')
        self.assignment = ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id, element='Subject')
        self.base = f'/api/decks/{self.deck.pk}/'

    @patch('generation.views.start_plan_job')
    def test_plan_enqueues_options_and_conflicts_with_every_kind(self, start):
        for kind in ('images', 'plan', 'references'):
            job = GenerationJob.objects.create(deck=self.deck, kind=kind, total_cards=1)
            self.assertEqual(self.client.post(self.base + 'theme-plan/').status_code, 409)
            self.assertEqual(self.client.post(self.base + 'auto-references/').status_code, 409)
            job.delete()
        for invalid in ('yes', 1, None):
            self.assertEqual(self.client.post(self.base + 'theme-plan/', {'overwrite_edited': invalid}, content_type='application/json').status_code, 400)
        self.assertEqual(GenerationJob.objects.count(), 0)
        start.assert_not_called()
        self.assignment.edited = True
        self.assignment.save()
        result = self.client.post(self.base + 'theme-plan/', {'overwrite_edited': True}, content_type='application/json')
        self.assertEqual(result.status_code, 202)
        data = result.json()
        self.assertEqual((data['kind'], data['stage'], data['total_cards'], data['summary']), ('plan', 'En cola', 1, None))
        self.assertEqual(GenerationJob.objects.get(pk=data['id']).options, {'overwrite_edited': True})
        start.assert_called_once()
        detail = self.client.get(self.base).json()
        self.assertEqual(detail['active_job']['kind'], 'plan')
        self.assertEqual(self.client.get(f'/api/jobs/{data["id"]}/').json()['stage'], 'En cola')

    @patch('generation.views.start_references_job')
    def test_references_enqueues_subset_validates_body_and_empty_subset(self, start):
        for body in ({'force': 'true'}, {'deck_card_ids': [0]}, {'deck_card_ids': 'all'}):
            self.assertEqual(self.client.post(self.base + 'auto-references/', body, content_type='application/json').status_code, 400)
        result = self.client.post(self.base + 'auto-references/', {'deck_card_ids': [self.entry.pk], 'force': True}, content_type='application/json')
        self.assertEqual(result.status_code, 202)
        self.assertEqual((result.json()['kind'], result.json()['total_cards']), ('references', 1))
        job = GenerationJob.objects.get(pk=result.json()['id'])
        self.assertEqual(job.options, {'deck_card_ids': [self.entry.pk], 'force': True})
        self.assertIsNone(result.json()['summary'])
        start.assert_called_once()
        job.delete()
        self.assertEqual(self.client.post(self.base + 'auto-references/', {'deck_card_ids': []}, content_type='application/json').json()['total_cards'], 0)

    def test_plan_stale_expiration(self):
        job = GenerationJob.objects.create(deck=self.deck, kind='plan', total_cards=3, status='running')
        GenerationJob.objects.filter(pk=job.pk).update(updated_at=timezone.now() - timedelta(minutes=20))
        self.assertEqual(expire_stale_jobs(self.deck), 1)
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertIsNotNone(job.finished_at)


@patch('generation.runner.connection.close')
class BackgroundRunnerTests(TestCase):
    def setUp(self):
        self.deck = Deck.objects.create(name='Deck', theme='Theme', raw_list='')
        self.card = make_card()
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Card', quantity=1, section='main')

    @patch('generation.claude_client.Anthropic')
    def test_run_plan_completes_reports_stages_and_refreshes_heartbeat(self, sdk, close):
        sdk.return_value.messages.create.return_value = claude_response({'assignments': [dict(
            oracle_id=str(self.card.oracle_id), element='Subject', category='', visual_brief='Una escena', reference=None)]})
        job = GenerationJob.objects.create(deck=self.deck, kind='plan', total_cards=1, options={'overwrite_edited': True})
        stages = []
        real_save = GenerationJob.save
        def observe(obj, *args, **kwargs):
            self.assertIn('updated_at', kwargs.get('update_fields', []))
            stages.append(obj.stage)
            return real_save(obj, *args, **kwargs)
        old = job.updated_at
        with patch.object(GenerationJob, 'save', observe):
            run_plan_job(job.pk)
        job.refresh_from_db()
        self.assertEqual((job.status, job.stage, job.completed_cards), ('done', 'Plan listo', 1))
        self.assertGreater(job.updated_at, old)
        self.assertTrue(ThemeAssignment.objects.filter(deck=self.deck).exists())
        self.assertTrue(any(stage.startswith('Planificando lote') for stage in stages))
        self.assertIn('Verificando wiki', stages)
        self.assertTrue(any(stage.startswith('Verificando páginas de la wiki') for stage in stages))
        self.assertIsNotNone(job.started_at)
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(close.call_count, 2)

    @patch('generation.claude_client.Anthropic')
    def test_run_plan_failure_capped(self, sdk, close):
        sdk.return_value.messages.create.side_effect = RuntimeError('x' * 700)
        job = GenerationJob.objects.create(deck=self.deck, kind='plan', total_cards=1)
        with self.assertLogs('generation.runner', level='ERROR'):
            run_plan_job(job.pk)
        job.refresh_from_db()
        self.assertEqual(job.status, 'failed')
        self.assertEqual(job.error_message, 'x' * 499 + '…')
        self.assertIsNotNone(job.finished_at)
        self.assertEqual(close.call_count, 2)

    @patch('generation.runner.fetch_auto_reference')
    def test_run_references_sequential_counts_summary_and_progress(self, fetch, close):
        statuses = ('found', 'not_found', 'rejected', 'error', 'blocked', 'skipped')
        for index in range(6):
            ThemeAssignment.objects.create(deck=self.deck, oracle_id=make_card().oracle_id, element='Subject ' + str(index))
        job = GenerationJob.objects.create(deck=self.deck, kind='references', total_cards=6, options={'force': True})
        main, stages = threading.get_ident(), []
        def result(assignment, force):
            self.assertEqual(threading.get_ident(), main)
            self.assertTrue(force)
            current = GenerationJob.objects.get(pk=job.pk)
            self.assertEqual(current.completed_cards, fetch.call_count - 1)
            stages.append(current.stage)
            return {'status': statuses[fetch.call_count - 1]}
        fetch.side_effect = result
        real_save = GenerationJob.save
        def observe(obj, *args, **kwargs):
            self.assertIn('updated_at', kwargs.get('update_fields', []))
            return real_save(obj, *args, **kwargs)
        with patch.object(GenerationJob, 'save', observe):
            run_references_job(job.pk)
        job.refresh_from_db()
        self.assertEqual((job.status, job.completed_cards, job.failed_cards), ('done', 6, 3))
        expected = dict.fromkeys(statuses, 1)
        self.assertEqual(job.options['summary'], expected)
        self.assertEqual(GenerationJobSerializer(job).data['summary'], expected)
        self.assertTrue(all(stage.startswith('Buscando referencias: Subject') for stage in stages))
        self.assertEqual(close.call_count, 2)

    @patch('generation.runner.fetch_auto_reference', side_effect=RuntimeError('Reference error'))
    def test_references_fatal_error_and_empty_subset(self, fetch, close):
        ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id, element='Subject')
        job = GenerationJob.objects.create(deck=self.deck, kind='references', total_cards=1)
        with self.assertLogs('generation.runner', level='ERROR'):
            run_references_job(job.pk)
        job.refresh_from_db()
        self.assertEqual((job.status, job.error_message), ('failed', 'Reference error'))
        empty = GenerationJob.objects.create(deck=self.deck, kind='references', total_cards=0, options={'deck_card_ids': []})
        fetch.reset_mock()
        run_references_job(empty.pk)
        empty.refresh_from_db()
        self.assertEqual(empty.status, 'done')
        self.assertEqual(sum(empty.options['summary'].values()), 0)
        fetch.assert_not_called()

    @patch('generation.runner.generate_card_image')
    @patch('generation.runner.get_provider')
    @patch('generation.runner.build_theme_plan')
    def test_image_job_plan_progress_does_not_change_image_totals(self, build, provider, generate, close):
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1, card_oracle_ids=[str(self.card.oracle_id)])
        def plan(deck, brief, progress):
            progress('Planificando lote 2 de 3', 2, 3)
            current = GenerationJob.objects.get(pk=job.pk)
            self.assertEqual((current.stage, current.total_cards, current.completed_cards), ('Planificando lote 2 de 3', 1, 0))
        build.side_effect = plan
        def generated(*args):
            self.assertEqual(GenerationJob.objects.get(pk=job.pk).stage, 'Generando Tree (1 de 1)')
        generate.side_effect = generated
        run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual((job.status, job.completed_cards, job.total_cards), ('done', 1, 1))


class DaemonStarterTests(SimpleTestCase):
    @patch('generation.runner.threading.Thread')
    def test_daemon_threads_for_both_new_runners(self, thread):
        for start, run in ((start_plan_job, run_plan_job), (start_references_job, run_references_job)):
            job = GenerationJob(pk=42)
            start(job)
            thread.assert_called_with(target=run, args=(42,), daemon=True)
            thread.return_value.start.assert_called()


@patch('generation.wiki_images.time.sleep')
@patch('generation.wiki_images.requests.get')
class LimitedPageSearchTests(TestCase):
    @patch.dict(os.environ, {'PAGE_SEARCH_LIMIT': '2'})
    def test_search_limit_shared_across_wikis_and_unchecked_pages_remain_blank(self, get, sleep):
        deck = Deck.objects.create(name='Deck', raw_list='', theme='Theme')
        items = [ThemeAssignment.objects.create(deck=deck, oracle_id=make_card().oracle_id, element=str(index),
                 reference_wiki='first.wiki.gg' if index < 3 else 'second.wiki.gg', reference_page=f'Page{index}') for index in range(5)]
        def answer(url, **kwargs):
            params = kwargs['params']
            if params.get('list') == 'search':
                return response({'query': {'search': [{'title': 'Found'}] if params['srsearch'] == 'Page0' else []}})
            return response({'query': {'pages': [{'title': title, 'missing': True} for title in params['titles'].split('|')]}})
        get.side_effect = answer
        progress = Mock()
        verify_assignment_pages(deck, items, callback=progress)
        self.assertEqual([item.reference_page_status for item in items], ['search', 'missing', '', '', ''])
        self.assertEqual(sum(call.kwargs['params'].get('list') == 'search' for call in get.call_args_list), 2)
        self.assertEqual(progress.call_args.args[1:], (5, 5))
