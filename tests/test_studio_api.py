"""API integration without real encoders, hardware, media or deletion."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

AVAILABLE = all(importlib.util.find_spec(m) for m in ('fastapi', 'httpx', 'jinja2', 'multipart', 'psutil'))


@unittest.skipUnless(AVAILABLE, 'optional API test dependencies unavailable')
class StudioApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import app
        from fastapi.testclient import TestClient
        cls.module = app
        cls.client = TestClient(app.app)

    def test_runtime_contract_has_no_duplicate_streams(self):
        from core import player_hls
        with patch.object(player_hls, 'list_runtime_sessions', return_value=[{'id':'stream-x'}]), patch.object(self.module.queue, 'state', return_value={'items':[], 'active_ids':[]}):
            data = self.client.get('/api/runtime').json()
        self.assertEqual(data, {'sessions':[{'id':'stream-x'}]})

    def test_websocket_sends_runtime_and_compact_queue_without_mutating_audit(self):
        from types import SimpleNamespace
        from core import player_hls
        details = {'commands':[{'argv':['ffmpeg','source']}], 'command':{'encoder':'copy'}}
        report = {'summary':{'status':'ok'}, 'checks':[{'key':'video.codec','status':'ok'}]}
        with patch.object(player_hls,'list_runtime_sessions',return_value=[]), patch.object(self.module.monitor,'snapshot',return_value=SimpleNamespace(to_dict=lambda:{})), patch.object(self.module.queue,'state',return_value={'items':[{'id':'x','details':details,'output_report':report}], 'active_ids':[]}):
            with self.client.websocket_connect('/ws') as ws:
                message = ws.receive_json()
                self.assertIn('runtime',message)
                entry = message['queue']['items'][0]
                self.assertNotIn('commands',entry['details'])
                self.assertNotIn('checks',entry['output_report'])
        self.assertIn('commands',details)
        self.assertIn('checks',report)

    def test_runtime_job_has_phase_effective_parameters_and_unknown_metrics(self):
        from core import player_hls
        state = {'items':[{'id':'x','title':'Test','phase':{'key':'source.probe'},'details':{'command':{'encoder':'copy'}}}], 'active_ids':['x']}
        with patch.object(player_hls,'list_runtime_sessions',return_value=[]), patch.object(self.module.queue,'state',return_value=state):
            row = self.client.get('/api/runtime').json()['sessions'][0]
        self.assertEqual(row['kind'], 'job')
        self.assertEqual(row['phase']['key'], 'source.probe')
        self.assertIsNone(row['pid'])
        self.assertEqual(row['effective']['command']['encoder'], 'copy')

    def test_retention_plan_is_server_owned_and_one_use(self):
        from core import storage_policy
        plan = {'min_age_days':30,'candidates':[{'session':'s','filename':'test_x_s0.mkv','size_bytes':12}], 'bytes':12}
        self.module._retention_plans.clear()
        with patch.object(storage_policy,'dry_run_cleanup',return_value=plan), patch.object(storage_policy,'apply_cleanup',return_value={'deleted':1}) as apply:
            preview = self.client.post('/api/data/retention/preview', json={'days':30}).json()
            response = self.client.post('/api/data/retention/apply', json={'days':30,'token':preview['token'],'candidates':[{'filename':'arbitrary'}]})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(apply.call_args.args[0], plan)
            repeat = self.client.post('/api/data/retention/apply', json={'days':30,'token':preview['token']})
            self.assertEqual(repeat.status_code, 409)

    def test_invalid_retention_age_cannot_delete(self):
        from core import storage_policy
        with patch.object(storage_policy,'apply_cleanup') as apply:
            for age in (-1, 0, 3651):
                self.assertEqual(self.client.post('/api/data/retention/preview', json={'days':age}).status_code,422)
            self.assertEqual(self.client.post('/api/data/retention/apply',json={'days':30,'token':'unknown'}).status_code,409)
            apply.assert_not_called()

    def test_extra_scene_rejects_missing_changed_and_outside_source(self):
        from core import vmaf, config
        with patch.object(vmaf,'load_session',return_value=None), patch.object(self.module.queue,'add_file') as add:
            self.assertEqual(self.client.post('/api/vmaf/extra-scene',json={'session':'s','start':0,'seconds':15}).status_code,409)
            add.assert_not_called()
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'source.mkv'
            path.write_bytes(b'fixture')
            saved = {'source_available':True, 'source_path':str(path),'archive_context':{'source':{'status':'changed'}}}
            with patch.object(vmaf,'load_session',return_value=saved), patch.object(config,'resolve_input',return_value=path), patch.object(self.module.queue,'add_file') as add:
                self.assertEqual(self.client.post('/api/vmaf/extra-scene',json={'session':'s','start':0,'seconds':15}).status_code,409)
                add.assert_not_called()
            with patch.object(vmaf,'load_session',return_value=saved), patch.object(config,'resolve_input',return_value=None):
                self.assertEqual(self.client.post('/api/vmaf/extra-scene',json={'session':'s','start':0,'seconds':15}).status_code,403)

    def test_extra_scene_queues_exact_range_without_mutating_archive(self):
        from copy import deepcopy
        from types import SimpleNamespace
        from core import vmaf, config
        with tempfile.TemporaryDirectory() as root:
            path = Path(root)/'source.mkv'
            path.write_bytes(b'fixture')
            saved = {'source_available':True, 'source_path':str(path),
                     'archive_context':{'source':{'status':'unchanged'}},
                     'params':{'test_values':[20,24,28,32],'sample_mode':'bitrate','post_processing':'inplace'},
                     'analysis':{'results':[{'value':28,'score':95}]}}
            original = deepcopy(saved)
            with patch.object(vmaf,'load_session',return_value=saved), patch.object(config,'resolve_input',return_value=path), patch.object(self.module.ff,'ffprobe',return_value=SimpleNamespace(duration=300)), patch.object(self.module.queue,'add_file',return_value=SimpleNamespace(id='new')) as add:
                response = self.client.post('/api/vmaf/extra-scene',json={'session':'s','start':42.5,'seconds':15.5})
                self.assertEqual(response.status_code,200)
                settings = add.call_args.args[1]
                self.assertEqual(settings.sample_ranges,[[42.5,15.5]])
                self.assertEqual(settings.workflow,'compare_only')
                self.assertEqual(settings.post_processing,'keep')
                self.assertEqual(settings.sample_mode,'even')
                self.assertTrue(response.json()['separate_analysis'])
            self.assertEqual(saved,original)

    def test_extra_scene_rejects_invalid_times(self):
        for start, seconds in ((-1,15),(0,0),(0,121)):
            response = self.client.post('/api/vmaf/extra-scene',json={'session':'s','start':start,'seconds':seconds})
            self.assertEqual(response.status_code,422)

    def test_history_audit_json_is_optional_and_parsed_safely(self):
        self.assertIsNone(self.module._history_json(None))
        self.assertIsNone(self.module._history_json('broken'))
        self.assertIsNone(self.module._history_json('[]'))
        self.assertEqual(self.module._history_json('{"schema_version":1}'),{'schema_version':1})


if __name__ == '__main__':
    unittest.main()
