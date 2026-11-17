"""Lokale Bibliotheksidentitäten: Vorschläge sind kein Nachweis für Fassungen."""
import json
import tempfile
import threading
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest import mock

from core import library


def movie(title, year=2020, provider="tmdb", uid="42"):
    return (f'<movie><title>{title}</title><year>{year}</year>'
            f'<uniqueid type="{provider}">{uid}</uniqueid>'
            '<plot>Eine Beschreibung.</plot><genre>Dokumentation</genre></movie>')


def episode(number, uid="", season=1):
    return (f'<episodedetails><showtitle>Reise</showtitle><title>Ankunft</title>'
            f'<season>{season}</season><episode>{number}</episode>'
            + (f'<uniqueid type="tvdb">{uid}</uniqueid>' if uid else '') + '</episodedetails>')


class MediaRecognitionTests(unittest.TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        self.root = Path(stack.enter_context(tempfile.TemporaryDirectory()))
        self.cache = self.root / 'scan.json'
        for key, value in [('_CACHE_PATH', self.cache), ('_state', library._empty_snapshot()),
                           ('_by_root', {}), ('_stop', threading.Event())]:
            stack.enter_context(mock.patch.object(library, key, value))
        stack.enter_context(mock.patch.object(library.config, 'iter_input_files',
            side_effect=lambda *_: sorted(self.root.rglob('*.mkv'))))
        stack.enter_context(mock.patch.object(library.config, 'rel_input',
            side_effect=lambda p: p.relative_to(self.root).as_posix()))
        stack.enter_context(mock.patch.object(library.config, 'resolve_input',
            side_effect=lambda rel: self.root / rel))
        info = library.ff.VideoInfo('', 1920, 1080, 150, 'hevc', '', '', '', '', 1000, 1024)
        stack.enter_context(mock.patch.object(library.ff, 'probe_with_error', return_value=(info, '')))
        stack.enter_context(mock.patch('core.history.is_processed', return_value=False))

    def put(self, rel, data=''):
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data if isinstance(data, bytes) else data.encode('utf-8'))
        return path

    def scan(self):
        library._run('', {})
        state = library.get_cached()
        self.assertEqual(library.get_state()['error'], '')
        return {row['path']: row['media'] for row in state['matched']}

    def test_scanned_file_sidecars_produce_compatible_namespaced_identity(self):
        for stem, year, title, provider in [('Kopie A', 2020, 'Insel', 'tmdb'),
                                            ('Kopie B', 2020, 'Insel', 'tmdb'),
                                            ('Remake', 1990, 'Insel', 'tmdb'),
                                            ('Anderer Titel', 2020, 'Wald', 'tmdb'),
                                            ('Andere Quelle', 2020, 'Insel', 'tvdb')]:
            self.put(f'{stem}.mkv')
            self.put(f'{stem}.nfo', movie(title, year, provider))
        rows = self.scan()
        first = rows['Kopie A.mkv']
        self.assertTrue(first['identity'])
        self.assertIn('tmdb', first['identity'])
        self.assertEqual(first, { 'title': 'Insel', 'year': 2020, 'kind': 'doku',
            'description': 'Eine Beschreibung.', 'show': '', 'season': None, 'episode': None,
            'identity': rows['Kopie B.mkv']['identity'], 'cover': False, 'source': 'nfo'})
        for name in ['Remake', 'Anderer Titel', 'Andere Quelle']:
            self.assertNotEqual(first['identity'], rows[f'{name}.mkv']['identity'])

    def test_movie_nfo_in_dedicated_matching_single_movie_folders(self):
        for edition in ['HD', 'UHD']:
            self.put(f'{edition}/Insel (2020).mkv')
            self.put(f'{edition}/movie.nfo', movie('Insel'))
        rows = self.scan()
        ids = {m['identity'] for m in rows.values()}
        self.assertEqual(len(ids), 1)
        self.assertNotIn('', ids)
        self.put('HD/Heimvideo.mkv')
        rows = self.scan()
        self.assertFalse(rows['HD/Insel (2020).mkv']['identity'])
        self.assertFalse(rows['HD/Heimvideo.mkv']['identity'])

    def test_shared_folder_extras_and_wrong_sidecars_are_not_film_evidence(self):
        folder = 'Archiv (2001)'
        self.put(f'{folder}/movie.nfo', movie('Übernommener Titel'))
        self.put(f'{folder}/Fremde Aufnahme.nfo', movie('Falscher Film'))
        for name in ['Berg', 'Meer (2021)', 'Insel - Trailer', 'Extras/Interview']:
            self.put(f'{folder}/{name}.mkv')
        for name in ['Insel - Trailer', 'Extras/Interview']:
            self.put(f'{folder}/{name}.nfo', movie('Insel'))
        rows = self.scan()
        self.assertTrue(all(not m['identity'] for m in rows.values()))
        loose = rows[f'{folder}/Berg.mkv']
        self.assertEqual((loose['title'], loose['kind'], loose['source']), ('Berg', 'video', 'suggestion'))
        film = rows[f'{folder}/Meer (2021).mkv']
        self.assertEqual((film['title'], film['year'], film['kind']), ('Meer', 2021, 'film'))

    def test_only_episode_id_can_identify_one_episode_not_its_show_or_neighbor(self):
        self.put('Reise/tvshow.nfo', '<tvshow><title>Reise</title><uniqueid type="tvdb">77</uniqueid></tvshow>')
        for part, xml in [('S00E01', episode(1, '85', 0)), ('S01E02', episode(2)),
                          ('S01E03', episode(3, '77')), ('S01E04', episode(5, '85')),
                          ('S01E06', episode(6, '85'))]:
            self.put(f'Reise/Staffel 01/Reise.{part}.mkv')
            self.put(f'Reise/Staffel 01/Reise.{part}.nfo', xml)
        rows = self.scan()
        first = rows['Reise/Staffel 01/Reise.S00E01.mkv']
        self.assertTrue(first['identity'])
        self.assertEqual((first['kind'], first['show'], first['season'], first['episode'], first['title']),
                         ('serie', 'Reise', 0, 1, 'Ankunft'))
        for part in ['S01E02', 'S01E03', 'S01E04']:
            self.assertFalse(rows[f'Reise/Staffel 01/Reise.{part}.mkv']['identity'], part)
        self.assertNotEqual(first['identity'], rows['Reise/Staffel 01/Reise.S01E06.mkv']['identity'])

    def test_legacy_cache_is_enriched_without_retrusting_merged_nfo(self):
        video = self.put('Archiv/Fund.mkv')
        self.put('Archiv/Fund.nfo', movie('Echte Zuordnung'))
        row = {'path': 'Archiv/Fund.mkv', 'name': video.name, 'folder': 'Archiv', 'x_custom': 3,
               'nfo': {'kind': 'movie', 'title': 'Geerbt', 'uniqueid': 'tmdb:42', 'file': 'Fund.nfo'}}
        self.cache.write_text(json.dumps({'root': '', 'matched': [row]}), encoding='utf-8')
        cached = library.load_last('')['matched'][0]
        self.assertEqual((cached['nfo'], cached['x_custom']), (row['nfo'], 3))
        self.assertFalse(cached['media']['identity'])
        self.assertEqual(cached['media']['title'], 'Fund')
        fresh = self.scan()['Archiv/Fund.mkv']
        self.assertTrue(fresh['identity'])
        library._by_root.clear()
        library._state['matched'] = []
        with mock.patch.object(library, 'media_cover_path', side_effect=AssertionError('unerwarteter Dateizugriff')):
            self.assertEqual(library.load_last('')['matched'][0]['media'], fresh)
            self.assertEqual(library.get_cached()['matched'][0]['media'], fresh)

    def test_readable_local_cover_is_preferred_and_remote_art_ignored(self):
        video = self.put('Archiv/A.mkv')
        self.put('Archiv/A.nfo', '<movie><title>A</title><thumb>https://example.invalid/a.jpg</thumb></movie>')
        self.assertIsNone(library.media_cover_path(video))
        self.put('Archiv/poster.jpg', b'\xff\xd8\xff\xe0folder')
        cover = self.put('Archiv/A-poster.jpg', b'\xff\xd8\xff\xe0file')
        self.assertEqual(library.media_cover_path(video), cover)
        self.assertTrue(self.scan()['Archiv/A.mkv']['cover'])

    def test_technical_nfo_in_mixed_folder_belongs_to_the_selected_file(self):
        first = self.put('Urlaub/Meer.mkv')
        self.put('Urlaub/Geburtstag.mkv')
        self.put('Urlaub/Geburtstag.nfo', movie('Geburtstag'))
        self.assertIsNone(library.sidecar_details(first)['nfo'])
        self.put('Urlaub/Meer.nfo', movie('Meer'))
        self.assertEqual(library.file_details(first, probe=False)['nfo']['title'], 'Meer')

    def test_shared_poster_in_mixed_folder_is_not_a_cover_for_every_recording(self):
        first = self.put('Urlaub/Meer.mkv')
        self.put('Urlaub/Geburtstag.mkv')
        self.put('Urlaub/poster.jpg', b'\xff\xd8\xff\xe0folder')
        self.assertIsNone(library.media_cover_path(first))

    def test_show_seasons_do_not_split_by_episode_air_year(self):
        self.put('Reise/tvshow.nfo', '<tvshow><title>Reise</title><year>2019</year></tvshow>')
        for s, y in [(1, 2020), (2, 2024)]:
            base = f'Reise/Staffel {s}/Reise.S0{s}E01'
            self.put(base + '.mkv')
            self.put(base + '.nfo', episode(1, str(s), s).replace('</title>', f'</title><year>{y}</year>'))
        self.assertEqual({m['show_year'] for m in self.scan().values()}, {2019})

    def test_numeric_title_prefers_the_explicit_release_year(self):
        self.put('Blade Runner 2049 (2017).mkv')
        media = next(iter(self.scan().values()))
        self.assertEqual((media['title'], media['year']), ('Blade Runner 2049', 2017))

    def test_contradictory_second_provider_id_keeps_versions_separate(self):
        for name, other in [('A', '101'), ('B', '202')]:
            self.put(name + '.mkv')
            self.put(name + '.nfo', movie('Insel').replace('</movie>', f'<uniqueid type="tvdb">{other}</uniqueid></movie>'))
        rows = self.scan()
        self.assertNotEqual(rows['A.mkv']['identity'], rows['B.mkv']['identity'])

    def test_cover_symlink_cannot_point_outside_video_directory(self):
        video = self.put('Archiv/A.mkv')
        outside = self.put('outside.jpg', b'\xff\xd8\xff\xe0poster')
        try:
            (video.parent / 'poster.jpg').symlink_to(outside)
        except OSError as err:
            self.skipTest(f'Symlinks nicht verfügbar: {err}')
        self.assertIsNone(library.media_cover_path(video))
