import json
import os
from pathlib import Path
from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from . import test_references as fixtures
from .auto_references import VerificationResults
from .models import ReferenceImage, GenerationJob
from .references import MAX_REFERENCE_BYTES, normalize_reference
from .test_references import png
from .test_wiki_sites import claude_response


@patch.dict(os.environ, {'IMAGE_PROVIDER': 'gemini'})
class BrowserReferenceTests(TestCase):
    setUp = fixtures.ReferenceEndpointTests.setUp
    reference = fixtures.ReferenceEndpointTests.reference

    def upload(self, colors=('red',), mode='pick', urls=None):
        data = {'mode': mode, 'files': [SimpleUploadedFile(f'{index}.png', png(color), content_type='image/png')
                                      for index, color in enumerate(colors)]}
        if urls is not None:
            data['source_urls'] = urls
        return self.client.post(self.url + 'browser/', data)

    @patch('generation.views.verify_references')
    def test_pick_first_only_marks_edited_and_duplicate(self, verify):
        result = self.upload(('red', 'blue'), urls=json.dumps(['https://cdn.example/red.png', 'https://cdn.example/blue.png']))
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()['id'], self.entry.pk)
        ref = ReferenceImage.objects.get()
        self.assertEqual((ref.origin, ref.source_url), ('wiki', 'https://cdn.example/red.png'))
        self.assertEqual(ref.sha256, normalize_reference(png('red'))[1])
        self.assignment.refresh_from_db()
        self.assertTrue(self.assignment.edited)
        self.assertEqual((self.assignment.auto_reference_status, self.assignment.auto_reference_message), ('found', 'Elegida desde el navegador'))
        verify.assert_not_called()
        result = self.upload()
        self.assertEqual((result.status_code, result.json()['detail']), (400, 'Esa imagen ya está agregada.'))
        self.reference('green')
        self.reference('blue')
        self.assertEqual(self.upload(('purple',)).status_code, 400)
        self.assertEqual(self.assignment.references.count(), 3)

    @patch('generation.claude_client.Anthropic')
    def test_auto_one_verification_deduplicates_orders_and_replaces(self, sdk):
        manual = self.reference('yellow')
        old = self.reference('orange', position=1)
        old.origin = 'auto'
        old.save()
        old_path = Path(old.image.path)
        sdk.return_value.messages.create.return_value = claude_response({'results': [dict(index=index, ok=True, reason='Canónico') for index in (1, 2, 3)], 'best_order': [3, 1, 2]})
        result = self.upload(('red', 'blue', 'red', 'green', 'yellow'), mode='auto',
                             urls=['https://cdn.example/red', 'https://cdn.example/blue', 'https://cdn.example/duplicate', 'https://cdn.example/green', 'https://cdn.example/yellow'])
        self.assertEqual(result.status_code, 201)
        refs = list(self.assignment.references.filter(origin='auto'))
        self.assertEqual([ref.sha256 for ref in refs], [normalize_reference(png('green'))[1], normalize_reference(png('red'))[1]])
        self.assertEqual([ref.source_url for ref in refs], ['https://cdn.example/green', 'https://cdn.example/red'])
        self.assertEqual([ref.position for ref in refs], [1, 2])
        self.assertTrue(ReferenceImage.objects.filter(pk=manual.pk).exists())
        self.assertFalse(ReferenceImage.objects.filter(pk=old.pk).exists())
        self.assertFalse(old_path.exists())
        self.assertEqual(sdk.return_value.messages.create.call_count, 1)
        content = sdk.return_value.messages.create.call_args.kwargs['messages'][0]['content']
        self.assertEqual(len([block for block in content if block['type'] == 'image']), 3)
        self.assignment.refresh_from_db()
        self.assertFalse(self.assignment.edited)
        self.assertEqual(self.assignment.auto_reference_message, 'Se agregaron 2 referencias (descargadas por el navegador)')

    @patch('generation.views.verify_references')
    def test_auto_limits_slots_and_all_rejected(self, verify):
        first = self.reference('yellow')
        second = self.reference('orange', position=1)
        second.origin = 'wiki'
        second.save()
        verify.return_value = VerificationResults([(True, ''), (True, '')], [1, 0])
        result = self.upload(('red', 'blue'), mode='auto')
        self.assertEqual(result.status_code, 201)
        self.assertEqual(self.assignment.references.filter(origin='auto').count(), 1)
        self.assertEqual(self.assignment.references.get(origin='auto').sha256, normalize_reference(png('blue'))[1])
        verify.return_value = [(False, 'No es canónico'), (False, 'Logo')]
        result = self.upload(('red', 'blue'), mode='auto')
        self.assertEqual(result.status_code, 201)
        self.assertEqual(result.json()['theme_assignment']['auto_reference_status'], 'rejected')
        self.assertEqual(result.json()['theme_assignment']['auto_reference_message'], 'No es canónico')
        self.assertEqual(self.assignment.references.filter(origin='auto').count(), 0)
        self.assertEqual(set(self.assignment.references.values_list('pk', flat=True)), {first.pk, second.pk})
        self.reference('purple')
        verify.reset_mock()
        self.assertEqual(self.upload(('red',), mode='auto').status_code, 400)
        verify.assert_not_called()

    @patch('generation.views.verify_references')
    def test_validation_files_urls_mode_and_no_assignment_or_active_job(self, verify):
        for count in (0, 7):
            self.assertEqual(self.upload(('red',) * count).status_code, 400)
        self.assertEqual(self.upload(mode='other').status_code, 400)
        self.assertEqual(self.upload(urls=['not a url']).status_code, 400)
        self.assertEqual(self.upload(('red', 'blue'), urls=['https://cdn.example/red']).status_code, 400)
        for raw in (b'not an image', b'x' * (MAX_REFERENCE_BYTES + 1)):
            result = self.client.post(self.url + 'browser/', {'mode': 'auto', 'files': [SimpleUploadedFile('bad.png', raw)]})
            self.assertEqual(result.status_code, 400)
        # Even pick validates every supplied file before saving the first one.
        result = self.client.post(self.url + 'browser/', {'mode': 'pick', 'files': [SimpleUploadedFile('ok.png', png()), SimpleUploadedFile('bad.png', b'invalid')]})
        self.assertEqual(result.status_code, 400)
        self.assertFalse(ReferenceImage.objects.exists())
        job = GenerationJob.objects.create(deck=self.deck, kind='references', total_cards=1)
        self.assertEqual(self.upload().status_code, 409)
        job.delete()
        self.assignment.delete()
        self.assertEqual(self.upload().status_code, 400)
        verify.assert_not_called()

    @patch('generation.views.verify_references')
    def test_duplicate_protected_image_does_not_replace_prior_auto(self, verify):
        self.reference('red')
        previous = self.reference('blue')
        previous.origin = 'auto'
        previous.save()
        self.assertEqual(self.upload(mode='auto').status_code, 400)
        self.assertTrue(ReferenceImage.objects.filter(pk=previous.pk).exists())
        verify.assert_not_called()
