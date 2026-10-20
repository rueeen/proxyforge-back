import base64
import os
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from . import test_framing as fixtures
from .framing import auto_framing, apply_auto_framing
from .providers.base import validate_aspect_ratio
from .providers.grok import GrokProvider
from .selection import aspect_for_layout
from .test_references import api_response


class TallFramingTests(SimpleTestCase):
    def test_high_face_prioritizes_no_gap_and_normal_face_reaches_target(self):
        ratio = (63 / 88) / (832 / 1248) * 100
        x, y, zoom = auto_framing(0.5, 0.1, 832, 1248, 'borderless')
        self.assertLessEqual(50 + y - ratio / 2, 0)
        self.assertAlmostEqual(50 + y - ratio / 2 + 0.1 * ratio, 10.7, delta=0.2)
        x, y, zoom = auto_framing(0.5, 0.3, 832, 1248, 'borderless')
        self.assertLessEqual(50 + y - ratio / 2, 0)
        self.assertAlmostEqual(50 + y - ratio / 2 + 0.3 * ratio, 32, delta=0.1)
        self.assertEqual(auto_framing(0.5, 0.1, 864, 1152, 'borderless'), (0, 22, 100))
        self.assertEqual(aspect_for_layout('borderless'), '2:3')
        for ratio in ('2:3', '3:4', '16:9'):
            validate_aspect_ratio(ratio)

    @patch.dict(os.environ, {'XAI_API_KEY': 'test'})
    @patch('generation.providers.grok.OpenAI')
    @patch('generation.providers.grok.requests.post')
    def test_grok_generation_and_edit_forward_two_three(self, post, sdk):
        sdk.return_value.images.generate.return_value = SimpleNamespace(data=[SimpleNamespace(b64_json=base64.b64encode(b'image').decode())])
        GrokProvider().generate('scene', '2:3')
        self.assertEqual(sdk.return_value.images.generate.call_args.kwargs['extra_body']['aspect_ratio'], '2:3')
        post.return_value = api_response({'data': [{'b64_json': base64.b64encode(b'image').decode()}]})
        GrokProvider().generate('scene', '2:3', references=[b'reference'])
        self.assertEqual(post.call_args.kwargs['json']['aspect_ratio'], '2:3')


@patch.dict(os.environ, {'AUTO_FRAMING': 'true', 'IMAGE_PROVIDER': 'gemini', 'IMAGE_FALLBACK_PROVIDER': ''})
class StretchTests(TestCase):
    setUp = fixtures.FramingTests.setUp
    save = fixtures.FramingTests.save

    @patch('generation.framing.detect_face_center', return_value=(0.5, 0.1))
    def test_manual_stretch_clamps_and_auto_preserves_it_and_cache(self, detect):
        image = self.save()
        key = image.cache_key
        for value, expected in ((50, 100), (150, 112), (107.5, 107.5)):
            result = self.client.patch(self.url, {'stretch_y': value}, content_type='application/json')
            self.assertEqual(result.status_code, 200)
            self.assertEqual(result.json()['generated_image']['framing_stretch_y'], expected)
        result = self.client.post(self.url + 'auto/')
        self.assertEqual(result.json()['generated_image']['framing_stretch_y'], 107.5)
        image.refresh_from_db()
        self.assertEqual(image.cache_key, key)
        detect.return_value = None
        apply_auto_framing(image, 'classic')
        image.refresh_from_db()
        self.assertEqual(image.framing_stretch_y, 107.5)
        self.assertEqual(self.client.patch(self.url, {'stretch_y': True}, content_type='application/json').status_code, 400)
