import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import Mock, patch

import requests
from django.test import TestCase


class SymbologyTests(TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = self.settings(MEDIA_ROOT=Path(directory.name))
        settings.enable()
        self.addCleanup(settings.disable)
        self.path = Path(directory.name) / 'cache' / 'symbology.json'
        self.data = [{'symbol': '{R}', 'svg_uri': 'https://svgs.scryfall.io/card-symbols/R.svg', 'english': 'one red mana'}]

    def cache(self, expired=False):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps(self.data))
        if expired:
            old = time.time() - 8 * 24 * 60 * 60
            os.utime(self.path, (old, old))

    @patch('scryfall.views.requests.get')
    def test_fresh_cache(self, get):
        self.cache()
        self.assertEqual(self.client.get('/api/symbology/').json(), self.data)
        get.assert_not_called()

    @patch('scryfall.views.requests.get')
    def test_refresh_expired(self, get):
        self.cache(expired=True)
        new = [{**self.data[0], 'symbol': '{G}'}]
        get.return_value = Mock()
        get.return_value.json.return_value = {'data': [{**new[0], 'other': 'ignored'}]}
        result = self.client.get('/api/symbology/')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), new)
        self.assertEqual(json.loads(self.path.read_text()), new)
        from .client import HEADERS
        get.assert_called_once_with('https://api.scryfall.com/symbology', headers=HEADERS, timeout=30)

    @patch('scryfall.views.requests.get', side_effect=requests.ConnectionError('offline'))
    def test_stale_cache_on_failure(self, get):
        self.cache(expired=True)
        result = self.client.get('/api/symbology/')
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json(), self.data)

    @patch('scryfall.views.requests.get', side_effect=requests.ConnectionError('offline'))
    def test_no_cache_failure(self, get):
        self.assertEqual(self.client.get('/api/symbology/').status_code, 502)
