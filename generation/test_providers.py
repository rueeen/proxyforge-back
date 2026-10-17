import base64
import os
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from PIL import Image
from django.test import SimpleTestCase, TestCase

from .models import GeneratedImage
from .prompt_builder import make_cache_key
from .providers import get_fallback_provider, get_provider
from .providers.grok import GrokProvider
from .runner import generate_card_image, render_card_image, save_card_image
from .tests import make_card, valid_prompt


@patch.dict(os.environ, {
    "XAI_API_KEY": "test-key",
    "XAI_IMAGE_MODEL": "grok-imagine-image-2.0",
    "XAI_IMAGE_RESOLUTION": "1k",
    "XAI_IMAGE_QUALITY": "medium",
})
class GrokProviderTests(SimpleTestCase):
    @patch("generation.providers.grok.OpenAI")
    def test_parameters_and_decoding(self, client):
        client.return_value.images.generate.return_value = SimpleNamespace(
            data=[SimpleNamespace(b64_json=base64.b64encode(b"image bytes").decode())]
        )
        self.assertEqual(GrokProvider().generate("scene"), b"image bytes")
        client.assert_called_once_with(api_key="test-key", base_url="https://api.x.ai/v1")
        client.return_value.images.generate.assert_called_once_with(
            model="grok-imagine-image-2.0", prompt="scene", n=1, response_format="b64_json",
            extra_body={"aspect_ratio": "16:9", "resolution": "1k", "quality": "medium"},
        )
        self.assertIsInstance(get_provider("grok"), GrokProvider)

    @patch("generation.providers.grok.OpenAI")
    def test_quality_only_for_version_two_models(self, client):
        client.return_value.images.generate.return_value = SimpleNamespace(
            data=[SimpleNamespace(b64_json="aW1hZ2U=")]
        )
        for model in ("grok-imagine-image", "grok-imagine-image-2.0", "grok-imagine-image-2-test"):
            with self.subTest(model=model), patch.dict(os.environ, {
                "XAI_IMAGE_MODEL": model, "XAI_IMAGE_RESOLUTION": "2k", "XAI_IMAGE_QUALITY": "low",
            }):
                GrokProvider().generate("scene")
                body = client.return_value.images.generate.call_args.kwargs["extra_body"]
                self.assertEqual(body["resolution"], "2k")
                self.assertEqual("quality" in body, model.startswith("grok-imagine-image-2"))
                if "quality" in body:
                    self.assertEqual(body["quality"], "low")

    @patch("generation.providers.grok.OpenAI")
    def test_missing_or_moderated_image(self, client):
        for data in (
            None, [], [SimpleNamespace()], [SimpleNamespace(b64_json=None)],
            [SimpleNamespace(b64_json="aW1hZ2U=", respect_moderation=False)],
        ):
            with self.subTest(data=data):
                client.return_value.images.generate.return_value = SimpleNamespace(data=data)
                with self.assertRaisesRegex(RuntimeError, "Grok no devolvió imagen"):
                    GrokProvider().generate("scene")

    @patch("generation.providers.grok.OpenAI")
    def test_missing_key_does_not_create_client(self, client):
        for value in (None, "", "   "):
            with self.subTest(value=value), patch.dict(os.environ):
                if value is None:
                    os.environ.pop("XAI_API_KEY", None)
                else:
                    os.environ["XAI_API_KEY"] = value
                with self.assertRaisesRegex(RuntimeError, "Falta XAI_API_KEY"):
                    GrokProvider().generate("scene")
        client.assert_not_called()


class FallbackProviderTests(SimpleTestCase):
    def test_no_fallback_or_same_provider(self):
        with patch.dict(os.environ, {"IMAGE_PROVIDER": "grok"}):
            os.environ.pop("IMAGE_FALLBACK_PROVIDER", None)
            self.assertIsNone(get_fallback_provider())
            for value in ("", "   ", "grok", "GROK"):
                os.environ["IMAGE_FALLBACK_PROVIDER"] = value
                self.assertIsNone(get_fallback_provider())

    @patch.dict(os.environ, {"IMAGE_PROVIDER": "grok", "IMAGE_FALLBACK_PROVIDER": "gemini"})
    def test_known_fallback(self):
        self.assertEqual(get_fallback_provider().name, "gemini")

    @patch.dict(os.environ, {"IMAGE_PROVIDER": "grok", "IMAGE_FALLBACK_PROVIDER": "unknown"})
    def test_unknown_fallback(self):
        with self.assertRaisesRegex(ValueError, "Proveedor de imágenes desconocido: unknown"):
            get_fallback_provider()


@patch("generation.runner.build_visual_prompt", side_effect=lambda *args, **kwargs: valid_prompt())
class ProviderAttemptTests(SimpleTestCase):
    def setUp(self):
        self.card = SimpleNamespace(name="Tree")
        self.primary = Mock()
        self.primary.name = "grok"
        self.backup = Mock()
        self.backup.name = "gemini"
        image = BytesIO()
        Image.new("RGB", (2, 2)).save(image, format="JPEG")
        self.image_bytes = image.getvalue()

    @patch("generation.runner.get_fallback_provider")
    def test_backup_receives_only_fallback_after_two_failures(self, fallback, build):
        fallback.return_value = self.backup
        self.primary.generate.side_effect = [RuntimeError("policy"), b"not an image"]
        self.backup.generate.return_value = self.image_bytes
        order = Mock()
        order.attach_mock(self.primary.generate, "primary")
        order.attach_mock(self.backup.generate, "backup")
        with self.assertLogs("generation.runner", level="WARNING") as logs:
            prompt, image = render_card_image(self.card, "theme", self.primary)
        self.assertEqual(image, self.image_bytes)
        self.assertEqual(prompt["used_provider"], "gemini")
        self.assertEqual(prompt["used_prompt"], "fallback")
        self.assertEqual(order.mock_calls, [
            call.primary(valid_prompt()["full_prompt"], aspect_ratio="16:9", references=[], resolution=None),
            call.primary(valid_prompt()["fallback_prompt"], aspect_ratio="16:9", references=[], resolution=None),
            call.backup(valid_prompt()["fallback_prompt"], aspect_ratio="16:9", references=[], resolution=None),
        ])
        self.assertEqual(len(logs.output), 2)
        self.assertIn("grok/full: policy", logs.output[0])
        self.assertIn("grok/fallback:", logs.output[1])

    @patch("generation.runner.get_fallback_provider")
    def test_all_errors_include_attempts(self, fallback, build):
        fallback.return_value = self.backup
        self.primary.generate.side_effect = [RuntimeError("first"), RuntimeError("second")]
        self.backup.generate.side_effect = RuntimeError("third")
        with self.assertLogs("generation.runner", level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "grok/full: first; grok/fallback: second; gemini/fallback: third"):
                render_card_image(self.card, "theme", self.primary)
        self.assertEqual(len(logs.output), 3)

    @patch("generation.runner.get_fallback_provider")
    def test_no_backup_means_two_attempts(self, fallback, build):
        fallback.return_value = None
        self.primary.generate.side_effect = RuntimeError("unavailable")
        with self.assertLogs("generation.runner", level="WARNING"):
            with self.assertRaisesRegex(RuntimeError, "grok/full: unavailable; grok/fallback: unavailable"):
                render_card_image(self.card, "theme", self.primary)
        self.assertEqual(self.primary.generate.call_count, 2)

    @patch("generation.runner.get_fallback_provider")
    def test_success_stops_attempts(self, fallback, build):
        fallback.return_value = self.backup
        for used_prompt in ("full", "fallback"):
            with self.subTest(used_prompt=used_prompt):
                self.primary.generate.reset_mock()
                self.primary.generate.side_effect = (
                    [self.image_bytes] if used_prompt == "full"
                    else [RuntimeError("policy"), self.image_bytes]
                )
                if used_prompt == "fallback":
                    with self.assertLogs("generation.runner", level="WARNING"):
                        prompt, _ = render_card_image(self.card, "theme", self.primary)
                else:
                    prompt, _ = render_card_image(self.card, "theme", self.primary)
                self.assertEqual(prompt["used_provider"], "grok")
                self.assertEqual(prompt["used_prompt"], used_prompt)
                self.assertEqual(self.primary.generate.call_count, 1 if used_prompt == "full" else 2)
                self.backup.generate.assert_not_called()


class ProviderCacheTests(TestCase):
    def test_cache_uses_primary_and_record_uses_effective_provider(self):
        import tempfile
        with tempfile.TemporaryDirectory() as media, self.settings(MEDIA_ROOT=media):
            card = make_card()
            provider = Mock()
            provider.name = "grok"
            prompt = {**valid_prompt(), "used_prompt": "fallback", "used_provider": "gemini"}
            stream = BytesIO()
            Image.new("RGB", (2, 2)).save(stream, format="JPEG")
            generated = save_card_image(card, "theme", provider, prompt, stream.getvalue())
            generated.refresh_from_db()
            self.assertEqual(generated.cache_key, make_cache_key(card.oracle_id, "theme", "grok"))
            self.assertEqual(generated.provider, "gemini")
            self.assertEqual(generated.visual_prompt, prompt["fallback_prompt"])
            self.assertTrue(generated.image.name.endswith(".jpg"))
            with patch("generation.runner.render_card_image") as render:
                cached = generate_card_image(card, "theme", provider)
            self.assertEqual(cached.pk, generated.pk)
            self.assertEqual(GeneratedImage.objects.count(), 1)
            render.assert_not_called()
