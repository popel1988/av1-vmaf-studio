"""Contract tests for local library decisions, covers and explicit title lookup."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from core import works


API_AVAILABLE = all(importlib.util.find_spec(name) for name in (
    'fastapi', 'httpx', 'jinja2', 'multipart', 'psutil',
))


@unittest.skipUnless(API_AVAILABLE, 'optional API test dependencies unavailable')
class MediaCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from app import app
        from fastapi.testclient import TestClient
        cls.client = TestClient(app)
        cls.addClassCleanup(cls.client.close)

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.catalog = Path(temp.name) / 'catalog.json'
        self.enterContext(patch.object(works, '_CATALOG_PATH', self.catalog))
        self.url = '/api/library/catalog'
        self.a, self.b, self.c = 'Kino/A.mkv', 'Kino/B.mkv', 'Kino/C.mkv'

    def decide(self, paths, action, title=''):
        return self.client.post(self.url + '/decision', json={
            'paths': paths, 'action': action, 'title': title,
        })

    def test_decisions_survive_legacy_collection_operations_and_reload(self):
        legacy = {'collections': [{'id': 'old', 'name': 'Archiv', 'kind': 'film', 'created_at': 1}],
                  'assignments': {self.a: {'collection_id': 'old'}}}
        self.catalog.write_text(json.dumps(legacy), encoding='utf-8')
        self.assertEqual(self.client.get(self.url).json(), {**legacy, 'decisions': {}})
        response = self.decide([self.a, self.b], 'confirm', '  Mein Film  ')
        self.assertEqual(response.status_code, 200)
        catalog = response.json()
        self.assertEqual(catalog['assignments'], legacy['assignments'])
        self.assertEqual(catalog['collections'], legacy['collections'])
        expected = {p: {'title': 'Mein Film', 'group': '', 'status': 'confirm'} for p in (self.a, self.b)}
        self.assertEqual(catalog['decisions'], expected)
        self.assertEqual(json.loads(self.catalog.read_text(encoding='utf-8'))['decisions'], expected)
        collection = self.client.post(self.url, json={'name': 'Neu'}).json()['collection']
        self.client.patch(self.url + '/' + collection['id'], json={'name': 'Anders'})
        self.client.post(self.url + '/assign', json={'paths': [self.b], 'collection_id': collection['id']})
        self.client.delete(self.url + '/' + collection['id'])
        self.assertEqual(self.client.get(self.url).json(), {**legacy, 'decisions': expected})
        reset = self.decide([self.a], 'reset').json()
        self.assertEqual(reset['decisions'], {self.b: expected[self.b]})
        self.assertEqual(reset['assignments'], legacy['assignments'])

    def test_versions_share_stable_group_and_title_without_linking_other_files(self):
        def versions(paths, title):
            response = self.decide(paths, 'version', title)
            self.assertEqual(response.status_code, 200)
            return response.json()['decisions']

        first = versions([self.a, self.b], 'Erstfassung')
        group = first[self.a]['group']
        self.assertTrue(group)
        self.assertEqual(first[self.a], first[self.b])
        again = versions([self.b, self.a], 'Neuer Titel')
        self.assertEqual(again[self.a], {'group': group, 'status': 'version', 'title': 'Neuer Titel'})
        self.assertEqual(again[self.a], again[self.b])
        other = versions([self.b, self.c], 'Neuer Titel')
        self.assertNotEqual(other[self.c]['group'], group)
        item = self.decide([self.a], 'standalone', 'Solo').json()['decisions'][self.a]
        self.assertEqual(item, {'group': '', 'status': 'standalone', 'title': 'Solo'})

    def test_invalid_batch_never_partially_updates_catalog(self):
        initial = self.decide([self.a], 'confirm', 'Vorher')
        self.assertEqual(initial.status_code, 200)
        before = self.catalog.read_bytes()
        cases = [([], 'reset', ''), ([self.a, self.a], 'version', 'T'),
                 ([self.a, self.a.replace('/', '\\')], 'version', 'T'),
                 ([self.a], 'unknown', 'T'), ([self.a], 'confirm', ' '),
                 ([self.a], 'version', 'Test')]
        for path in ('../f.mkv', '/absolute.mkv', 'C:\\f.mkv', 'upload:f.mkv', 'private.txt', '', 'a\0.mkv'):
            cases.append(([self.a, path], 'standalone', 'Nachher'))
        for paths, action, title in cases:
            with self.subTest(paths=paths, action=action):
                self.assertIn(self.decide(paths, action, title).status_code, (400, 422))
                self.assertEqual(self.catalog.read_bytes(), before)

    def test_cover_only_returns_raster_artwork_for_resolved_video(self):
        from core import config, library
        media = self.catalog.parent / 'media'
        media.mkdir()
        video = media / 'Film.mkv'
        video.touch()
        poster = media / 'poster.png'
        png = b'\x89PNG\r\n\x1a\n' + b'cover'
        poster.write_bytes(png)
        external = media.parent / 'private.png'
        external.write_bytes(png)
        url = '/api/library/media-cover'
        with patch.object(config, 'all_media_roots', return_value=[('movies', media)]), \
                patch.object(library, 'media_cover_path', return_value=poster, create=True) as lookup:
            response = self.client.get(url, params={'path': video.name})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, png)
            self.assertEqual(response.headers['content-type'], 'image/png')
            self.assertEqual(response.headers['x-content-type-options'], 'nosniff')
            lookup.assert_called_once_with(video.resolve())
            for path in (poster.name, '../private.mkv', 'missing.mkv', str(external)):
                with self.subTest(path=path):
                    response = self.client.get(url, params={'path': path})
                    self.assertEqual(response.status_code, 404)
                    self.assertNotIn(str(media), response.text)
            lookup.assert_called_once()
            for candidate in (external, video, None):
                with self.subTest(candidate=candidate):
                    lookup.return_value = candidate
                    self.assertEqual(self.client.get(url, params={'path': video.name}).status_code, 404)
            lookup.return_value = poster
            poster.write_bytes(b'<svg></svg>')
            self.assertEqual(self.client.get(url, params={'path': video.name}).status_code, 404)

    def test_lookup_only_sends_the_explicit_title_and_never_updates_catalog(self):
        from io import BytesIO
        from urllib.parse import urlsplit, parse_qs
        with patch('urllib.request.urlopen') as send:
            send.return_value = BytesIO(json.dumps({'search': [{'id': 'Q42', 'label': 'Beispiel',
                'description': 'Film', 'url': 'unused'}]}).encode())
            url = '/api/library/title-search'
            self.assertEqual(self.client.get(url, params={'q': 'private/video.mkv'}).status_code, 400)
            send.assert_not_called()
            reply = self.client.get(url, params={'q': 'Mein Titel', 'path': 'private/video.mkv'})
            self.assertEqual(reply.json(), {'results': [{'id':'Q42', 'title':'Beispiel', 'description':'Film'}]})
            dest = urlsplit(send.call_args.args[0].full_url)
            self.assertEqual(dest.hostname, 'www.wikidata.org')
            self.assertEqual(parse_qs(dest.query)['search'], ['Mein Titel'])
            self.assertNotIn('private', dest.query)
            send.side_effect = TimeoutError()
            self.assertEqual(self.client.get(url, params={'q':'Mein Titel'}).status_code, 503)
        self.assertFalse(self.catalog.exists())

    def test_failed_atomic_replace_leaves_original_intact(self):
        self.assertEqual(self.decide([self.a], 'confirm', 'Vorher').status_code, 200)
        before = self.catalog.read_bytes()
        with patch('os.replace', side_effect=OSError('storage unavailable')):
            with self.assertRaises(OSError):
                works.create_collection('Neu')
        self.assertEqual(self.catalog.read_bytes(), before)
