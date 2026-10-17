import json
import os
import tempfile
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import SimpleTestCase, TestCase

from decks.models import Deck, DeckCard
from .auto_references import fetch_auto_reference, fetch_auto_references, verify_reference
from .models import ThemeAssignment, ReferenceImage, GenerationJob
from .runner import run_job
from .selection import assignment_map
from .test_references import png
from .tests import make_card
from .theme_planner import build_theme_plan
from .wiki_images import WikiImageError, WikiImageNotFound, LAST_RESULTS


def response(data):
    return SimpleNamespace(content=[SimpleNamespace(type='text', text=json.dumps(data))], stop_reason='end_turn')


class VerifyTests(SimpleTestCase):
    @patch('generation.claude_client.Anthropic')
    def test_accept_reject_and_configured_model(self, anthropic):
        item = SimpleNamespace(element="Vol'jin", category='Troll', visual_brief='A portrait')
        create = anthropic.return_value.messages.create
        for ok in (True, False):
            create.return_value = response({'ok': ok, 'reason': 'Motivo'})
            with patch.dict(os.environ, {'ANTHROPIC_VERIFY_MODEL': 'verify-model'}):
                self.assertEqual(verify_reference(b'jpeg', item), (ok, 'Motivo'))
            args = create.call_args.kwargs
            self.assertEqual(args['model'], 'verify-model')
            self.assertEqual(args['max_tokens'], 300)
            self.assertEqual(args['messages'][0]['content'][0]['source']['media_type'], 'image/jpeg')
            self.assertEqual(json.loads(args['messages'][0]['content'][1]['text'])['element'], "Vol'jin")

    @patch('generation.auto_references.call_claude_json', side_effect=RuntimeError('offline'))
    def test_technical_failure_is_accepted(self, call):
        with self.assertLogs('generation.auto_references', level='WARNING'):
            self.assertEqual(verify_reference(b'jpeg', SimpleNamespace(element='X', category='', visual_brief='')), (True, 'sin verificar: offline'))


@patch.dict(os.environ, {'AUTO_REFERENCE_VERIFY': 'false', 'AUTO_REFERENCES_IN_JOBS': 'true'})
class AutoReferenceTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        setting = self.settings(MEDIA_ROOT=directory.name)
        setting.enable()
        self.addCleanup(setting.disable)
        self.deck = Deck.objects.create(name='Deck', raw_list='', theme='Warcraft')
        self.card = make_card()
        self.entry = DeckCard.objects.create(deck=self.deck, card=self.card, raw_name='Card', quantity=1, section='main')
        self.assignment = ThemeAssignment.objects.create(deck=self.deck, oracle_id=self.card.oracle_id,
            element="Vol'jin", category='Troll', visual_brief='At war', needs_reference=True,
            reference_wiki='warcraft.wiki.gg', reference_page="Vol'jin")

    def reference(self, origin='manual'):
        item = ReferenceImage(assignment=self.assignment, sha256='a'*64, origin=origin)
        item.image.save('ref.jpg', ContentFile(png()))
        return item

    @patch('generation.auto_references.find_wiki_image', return_value=(png('blue'), 'https://cdn.example.com/portrait.png'))
    def test_manual_coexists_and_force_replaces_automatic(self, find):
        manual = self.reference()
        result = fetch_auto_reference(self.assignment)
        self.assertEqual(result['status'], 'found')
        auto = ReferenceImage.objects.get(origin='auto')
        old_path = Path(auto.image.path)
        self.assertEqual(auto.position, manual.position + 1)
        self.assertEqual(fetch_auto_reference(self.assignment)['skipped_reason'], 'already_has_auto_reference')
        self.assertEqual(fetch_auto_reference(self.assignment, force=True)['status'], 'found')
        self.assertFalse(old_path.exists())
        self.assertTrue(ReferenceImage.objects.filter(pk=manual.pk).exists())
        self.assertEqual(ReferenceImage.objects.count(), 2)
        self.assignment.references.filter(origin='auto').delete()
        self.assignment.auto_reference_status = 'not_found'
        self.assignment.save()
        self.assertEqual(fetch_auto_reference(self.assignment)['skipped_reason'], 'already_attempted')
        self.assertEqual(fetch_auto_reference(self.assignment, force=True)['status'], 'found')

    @patch('generation.auto_references.find_wiki_image')
    def test_not_found_error_and_rejected_statuses(self, find):
        for error, status in ((WikiImageNotFound(), 'not_found'), (WikiImageError('offline'), 'error')):
            with self.subTest(status=status):
                find.side_effect = error
                self.assertEqual(fetch_auto_reference(self.assignment, force=True)['status'], status)
                self.assignment.refresh_from_db()
                self.assertEqual(self.assignment.auto_reference_status, status)
        find.side_effect = None
        find.return_value = (png(), 'https://cdn.example.com/portrait.png')
        with patch.dict(os.environ, {'AUTO_REFERENCE_VERIFY': 'true'}), patch('generation.auto_references.verify_reference', return_value=(False, 'Es un logo')):
            self.assertEqual(fetch_auto_reference(self.assignment, force=True)['status'], 'rejected')
        self.assertEqual(self.assignment.auto_reference_message, 'Es un logo')
        self.assertFalse(ReferenceImage.objects.exists())

    @patch('generation.auto_references.find_wiki_image', return_value=(png(), 'https://cdn.example.com/image.png'))
    def test_prefetched_relation_is_invalidated_and_subset_summary(self, find):
        item = assignment_map(self.deck)[self.card.oracle_id]
        self.assertEqual(list(item.references.all()), [])
        self.assertEqual(fetch_auto_reference(item)['status'], 'found')
        self.assertEqual(len(list(item.references.all())), 1)
        self.assertEqual(fetch_auto_references(self.deck, deck_card_ids=[]), dict(found=0, not_found=0, rejected=0, error=0, blocked=0, skipped=0))
        summary = fetch_auto_references(self.deck, deck_card_ids=[self.entry.id])
        self.assertEqual(summary['skipped'], 1)

    @patch('generation.claude_client.Anthropic')
    def test_plan_saves_valid_metadata_ignores_bad_wiki_and_cleans_changed_element(self, anthropic):
        manual = self.reference('manual')
        auto = self.reference('auto')
        auto_path = Path(auto.image.path)
        self.assignment.auto_reference_status = 'found'
        self.assignment.save()
        create = anthropic.return_value.messages.create
        create.return_value = response({'assignments': [{'oracle_id': str(self.card.oracle_id), 'element': 'Sylvanas',
            'category': 'Personaje', 'visual_brief': 'At war', 'reference': {'wiki': 'warcraft.wiki.gg', 'page': 'Sylvanas Windrunner'}}]})
        item = build_theme_plan(self.deck)[0]
        self.assertEqual((item.needs_reference, item.reference_page), (True, 'Sylvanas Windrunner'))
        self.assertEqual(item.auto_reference_status, '')
        self.assertFalse(auto_path.exists())
        self.assertTrue(ReferenceImage.objects.filter(pk=manual.pk).exists())
        create.return_value = response({'assignments': [{'oracle_id': str(self.card.oracle_id), 'element': 'Sylvanas',
            'category': '', 'visual_brief': 'At war', 'reference': {'wiki': 'https://invalid/wiki', 'page': 'Sylvanas'}}]})
        item = build_theme_plan(self.deck)[0]
        self.assertFalse(item.needs_reference)
        self.assertEqual((item.reference_wiki, item.reference_page), ('', ''))

    @patch('generation.runner.connection.close')
    @patch('generation.runner.get_provider')
    @patch('generation.runner.generate_card_image')
    @patch('generation.runner.fetch_auto_reference')
    def test_job_fetches_before_generating_and_continues_after_failure(self, fetch, generate, provider, close):
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1, card_oracle_ids=[str(self.card.oracle_id)])
        order = Mock()
        order.attach_mock(fetch, 'fetch')
        order.attach_mock(generate, 'generate')
        run_job(job.pk)
        self.assertEqual([call[0] for call in order.mock_calls], ['fetch', 'generate'])
        fetch.side_effect = RuntimeError('wiki offline')
        generate.reset_mock()
        job = GenerationJob.objects.create(deck=self.deck, total_cards=1, card_oracle_ids=[str(self.card.oracle_id)])
        with self.assertLogs('generation.runner', level='WARNING'):
            run_job(job.pk)
        job.refresh_from_db()
        self.assertEqual((job.status, job.completed_cards), ('done', 1))
        generate.assert_called_once()
        fetch.reset_mock()
        with patch.dict(os.environ, {'AUTO_REFERENCES_IN_JOBS': 'false'}):
            job = GenerationJob.objects.create(deck=self.deck, total_cards=1, card_oracle_ids=[str(self.card.oracle_id)])
            run_job(job.pk)
        fetch.assert_not_called()

    @patch('generation.views.fetch_auto_references')
    @patch('generation.views.fetch_auto_reference')
    def test_endpoints_and_active_job_conflicts(self, fetch_one, fetch_deck):
        fetch_one.return_value = {'status': 'found', 'message': '', 'skipped_reason': ''}
        fetch_deck.return_value = dict(found=1, not_found=0, rejected=0, error=0, skipped=0)
        deck_url = f'/api/decks/{self.deck.id}/auto-references/'
        card_url = f'/api/cards/{self.entry.id}/theme-assignment/auto-reference/'
        result = self.client.post(deck_url, {'deck_card_ids': [self.entry.id]}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['auto_reference_summary']['found'], 1)
        fetch_deck.assert_called_once_with(self.deck, deck_card_ids=[self.entry.id], force=False)
        result = self.client.post(card_url, {}, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['id'], self.entry.id)
        self.assertTrue(fetch_one.call_args.kwargs['force'])
        GenerationJob.objects.create(deck=self.deck, total_cards=1)
        self.assertEqual(self.client.post(deck_url).status_code, 409)
        self.assertEqual(self.client.post(card_url).status_code, 409)
        self.deck.jobs.all().delete()
        self.assignment.reference_page = ''
        self.assignment.save()
        self.assertEqual(self.client.post(card_url).status_code, 400)
        self.assignment.delete()
        self.assertEqual(self.client.post(card_url).status_code, 400)

    def test_patch_resets_auto_reference_preserves_manual_and_validates_domain(self):
        manual = self.reference('manual')
        auto = self.reference('auto')
        path = Path(auto.image.path)
        self.assignment.auto_reference_status = 'found'
        self.assignment.auto_reference_message = 'Previous'
        self.assignment.save()
        url = f'/api/cards/{self.entry.id}/theme-assignment/'
        body = {'element': "Vol'jin", 'category': 'Troll', 'visual_brief': 'At war',
                'needs_reference': True, 'reference_wiki': 'wowpedia.fandom.com', 'reference_page': "Vol'jin"}
        result = self.client.patch(url, body, content_type='application/json')
        self.assertEqual(result.status_code, 200)
        data = result.json()['theme_assignment']
        self.assertEqual((data['auto_reference_status'], data['auto_reference_message']), ('', ''))
        self.assertEqual(data['references'][0]['origin'], 'manual')
        self.assertEqual(data['references'][0]['id'], manual.pk)
        self.assertFalse(path.exists())
        self.assertEqual(self.client.patch(url, {**body, 'reference_wiki': 'https://bad/wiki'}, content_type='application/json').status_code, 400)
        self.assertEqual(self.client.patch(url, {**body, 'needs_reference': 'true'}, content_type='application/json').status_code, 400)

    def test_counts_and_constant_queries(self):
        url = f'/api/decks/{self.deck.id}/'
        with self.assertNumQueries(8):
            result = self.client.get(url)
        self.assertEqual(result.json()['auto_reference_counts'], dict(needed=1, with_reference=0, pending=1))
        self.reference()
        for i in range(10):
            card = make_card()
            DeckCard.objects.create(deck=self.deck, card=card, raw_name='Card', quantity=1, section='main')
            ThemeAssignment.objects.create(deck=self.deck, oracle_id=card.oracle_id, element=f'Element {i}', category='',
                visual_brief='Brief', needs_reference=True, auto_reference_status='not_found' if i == 0 else '')
        with self.assertNumQueries(8):
            result = self.client.get(url)
        self.assertEqual(result.json()['auto_reference_counts'], dict(needed=11, with_reference=1, pending=9))

    @patch('generation.management.commands.find_reference.find_wiki_image', return_value=(png(), 'https://cdn.example.com/image.png'))
    @patch('generation.management.commands.find_reference.verify_reference', return_value=(True, 'Correcta'))
    def test_diagnostic_command(self, verify, find):
        LAST_RESULTS[('warcraft.wiki.gg', "Vol'jin")] = {'api': '/w/api.php', 'step': 1}
        output = StringIO()
        call_command('find_reference', "Vol'jin", wiki='warcraft.wiki.gg', verify=True, stdout=output)
        self.assertIn('/w/api.php', output.getvalue())
        self.assertIn('Paso: 1', output.getvalue())
        self.assertIn('Dimensiones: 10 x 10', output.getvalue())
        saved = Path(settings.MEDIA_ROOT) / 'cache' / 'find_reference' / 'voljin.jpg'
        self.assertTrue(saved.is_file())
        self.assertTrue(saved.read_bytes().startswith(b'\xff\xd8'))
        self.assertIn('Verificación: True', output.getvalue())
