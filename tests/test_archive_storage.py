"""Provenance, sample coverage and conservative clip-only storage policies."""
from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from core import config, provenance as prov, storage_policy as policy, vmaf, job_plan


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.mkv'
        self.source.write_bytes(b'fixture')
        self.binary = self.root / 'ffmpeg'
        self.binary.write_bytes(b'fake binary')
        self.model = self.root / 'model.json'
        self.model.write_bytes(b'model-v1')
        for key, value in {'FFMPEG':str(self.binary), 'VMAF_MODEL_DIR':self.root,
                           'VMAF_MODEL_1080P_NEG':self.model.name, 'VMAF_MODEL_1080P':self.model.name}.items():
            p = patch.object(config,key,value)
            p.start()
            self.addCleanup(p.stop)
        self.version = patch.object(prov,'_binary_version',return_value='ffmpeg test')
        self.version.start()
        self.addCleanup(self.version.stop)

    def archive(self):
        return {'source_path':str(self.source), 'analysis':{'results':[]},
                'provenance':prov.capture(source_path=str(self.source),model_name=self.model.name,
                                          model_path=self.model,source_info={'is_4k':False})}

    def test_unchanged_changed_missing_and_legacy_sources(self):
        data = self.archive()
        self.assertEqual(prov.archive_context(data)['source']['status'],'unchanged')
        self.source.write_bytes(b'changed source')
        self.assertEqual(prov.archive_context(data)['source']['status'],'changed')
        self.source.unlink()
        self.assertEqual(prov.archive_context(data)['source']['status'],'missing')
        self.source.write_bytes(b'new')
        self.assertEqual(prov.archive_context({'source_path':str(self.source)})['source']['status'],'unknown')

    def test_model_hash_not_filename_and_no_score_mutation(self):
        data = self.archive()
        data['analysis']['results'] = [{'vmaf':94,'recommended':True}]
        original = deepcopy(data)
        self.assertEqual(prov.archive_context(data)['model']['status'],'same')
        self.model.write_bytes(b'changed-model-with-same-name')
        result = prov.archive_context(data)
        self.assertEqual(result['model']['status'],'changed')
        self.assertFalse(result['scores_reinterpreted'])
        self.assertEqual(data,original)

    def test_runtime_binary_change_and_missing_are_detected_without_executing_it(self):
        data = self.archive()
        self.assertEqual(prov.archive_context(data)['runtime_status'],'same')
        self.binary.write_bytes(b'a different binary')
        with patch.object(prov.subprocess,'run',side_effect=AssertionError('must not execute archive binaries')):
            self.assertEqual(prov.archive_context(data)['runtime_status'],'changed')

    def test_actual_alternate_toolchain_is_checked(self):
        alternate = self.root / 'ffmpeg-jellyfin'
        alternate.write_bytes(b'alternate')
        with patch.object(config,'FFMPEG_JELLYFIN',str(alternate)):
            data = self.archive()
            prov.record_binary(data['provenance'],'reference',str(alternate))
            self.assertEqual(prov.archive_context(data)['runtime_status'],'same')
            alternate.write_bytes(b'changed alternate')
            self.assertEqual(prov.archive_context(data)['runtime_status'],'changed')

    def test_visual_comparison_has_no_vmaf_model(self):
        data = self.archive()
        data['provenance']['model'] = prov.model_identity('',None)
        self.assertEqual(prov.archive_context(data)['model']['status'],'not_applicable')

    def test_comparability_requires_actual_model_and_source_metadata(self):
        a = self.archive()
        b = deepcopy(a)
        self.assertTrue(prov.session_comparability(a,b)['comparable'])
        b['provenance']['model']['sha256'] = 'different'
        self.assertFalse(prov.session_comparability(a,b)['comparable'])
        self.assertIsNone(prov.session_comparability({'source_path':'x'},{'source_path':'x'})['comparable'])

    def test_union_of_overlaps_is_not_count_of_test_runs(self):
        scenes = [{'scene':0,'start':0,'scored_offset':1,'scored_seconds':10,'frame_count':240},
                  {'scene':1,'start':5,'scored_offset':1,'scored_seconds':10,'frame_count':240}]
        result = prov.analysis_coverage({'results':[{'scene_scores':scenes}]*4},100)
        self.assertEqual(len(result['results']),4)
        for row in result['results']:
            self.assertEqual(row['scored_seconds'],20)
            self.assertEqual(row['unique_scored_seconds'],15)
            self.assertEqual(row['film_fraction'],0.15)
            self.assertEqual(row['frame_count'],480)

    def test_measured_frame_count_survives_unknown_seconds_without_inventing_coverage(self):
        analysis = {'results':[{'scene_scores':[{'frame_count':240,'start':5}]}]}
        row = prov.analysis_coverage(analysis,100)['results'][0]
        self.assertEqual(row['frame_count'],240)
        self.assertIsNone(row['scored_seconds'])
        self.assertIsNone(row['film_fraction'])
        analysis['results'][0]['scene_scores'][0].update(scored_seconds=float('nan'),scored_offset=1)
        row = prov.analysis_coverage(analysis,100)['results'][0]
        self.assertIsNone(row['scored_seconds'])
        self.assertIsNone(row['film_fraction'])

    def test_old_missing_timing_and_visual_samples_are_not_invented(self):
        for visual in (False,True):
            result = prov.analysis_coverage({'visual_only':visual,'results':[{'scene_scores':[{'start':5,'length':15}]}]},100)
            self.assertIsNone(result['results'][0]['film_fraction'])
            self.assertIsNone(result['results'][0]['scored_seconds'])


class RetentionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.previews = self.root/'previews'
        self.archives = self.root/'vmaf'
        for key,value in {'PREVIEW_DIR':self.previews,'VMAF_SESSIONS_DIR':self.archives}.items():
            p = patch.object(config,key,value)
            p.start()
            self.addCleanup(p.stop)
        self.old = time.time()-60*86400
        self.session = 'movie_123'
        self.folder = self.previews/self.session
        self.clips = self.archives/self.session
        self.folder.mkdir(parents=True)
        self.clips.mkdir(parents=True)
        self.metadata = self.folder/'analysis.json'
        self.metadata.write_text(json.dumps({'created':self.old,'analysis':{'results':[]}}),encoding='utf-8')
        self.video = self.clips/'test_cpu_av1_28_s0.mkv'
        self.video.write_bytes(b'clip')
        self.keep = [self.metadata,self.folder/'scene0_ref.jpg',self.clips/'metrics.json',
                     self.clips/'reference_0.mkv',self.clips/'user_movie.mkv',
                     self.clips/'source_s0.mkv']
        for path in self.keep[1:]:
            path.write_bytes(b'keep')
        for path in [self.video,*self.keep]:
            os.utime(path,(self.old,self.old))

    def plan(self,**kwargs):
        return policy.dry_run_cleanup(min_age_days=30,**kwargs)

    def test_source_remux_plays_and_survives_cleanup(self):
        from core import vmaf as vmaf_mod
        src = self.clips / "source_s0.mkv"
        self.assertEqual(vmaf_mod.clip_path(self.session, "source_s0.mkv"), src.resolve())
        self.assertIsNone(vmaf_mod.clip_path(self.session, "../source_s0.mkv"))
        planned = self.plan()
        self.assertEqual(policy.apply_cleanup(planned)["deleted"], 1)
        self.assertTrue(src.exists())
        self.assertFalse(self.video.exists())

    def test_dry_run_is_non_destructive_apply_only_removes_owned_test_clip(self):
        planned = self.plan()
        self.assertEqual(planned['bytes'],4)
        self.assertTrue(self.video.exists())
        self.assertEqual(policy.apply_cleanup(planned)['deleted'],1)
        self.assertFalse(self.video.exists())
        self.assertTrue(all(p.exists() for p in self.keep))
        self.assertEqual(policy.apply_cleanup(planned)['deleted'],0)

    def test_replaced_or_modified_clip_is_not_deleted_by_stale_plan(self):
        plan = self.plan()
        self.video.write_bytes(b'new and different')
        os.utime(self.video,(self.old,self.old))
        self.assertEqual(policy.apply_cleanup(plan)['deleted'],0)
        self.assertTrue(self.video.exists())

    def test_activity_protection_and_unknown_providers_fail_closed(self):
        self.assertEqual(self.plan(active_sessions={self.session})['candidates'],[])
        def broken():
            raise RuntimeError('unknown')
        self.assertEqual(self.plan(active_sessions=broken)['candidates'],[])
        with policy.active_session(self.session):
            self.assertEqual(self.plan()['candidates'],[])
        self.assertFalse((self.folder/'.analysis-active').exists())
        planned = self.plan()
        self.assertEqual(policy.apply_cleanup(planned,active_sessions={self.session})['deleted'],0)

    def test_protected_status_is_persisted_and_revalidated(self):
        planned = self.plan()
        self.assertTrue(policy.set_session_protected(self.session,True)['protected'])
        self.assertEqual(self.plan()['candidates'],[])
        self.assertEqual(policy.apply_cleanup(planned)['deleted'],0)
        self.assertTrue(self.video.exists())

    def test_zero_negative_and_nonfinite_ages_are_never_deletion_permissions(self):
        for age in (0,-1,float('inf'),float('nan')):
            with self.assertRaises(ValueError):
                policy.dry_run_cleanup(min_age_days=age)
            with self.assertRaises(ValueError):
                policy.apply_cleanup({'min_age_days':age})

    def test_fresh_missing_or_invalid_metadata_is_not_permission_to_delete(self):
        self.metadata.write_text('{bad json',encoding='utf-8')
        self.assertEqual(self.plan()['candidates'],[])
        self.metadata.unlink()
        self.assertEqual(self.plan()['candidates'],[])

    def test_malicious_session_or_client_candidate_cannot_escape_archive(self):
        for name in ('../outside','a/b','a\\b','C:evil','.','..'):
            with self.assertRaises(ValueError):
                policy.session_directory(self.previews,name)
        plan = self.plan()
        outside = self.root/'important.mkv'
        outside.write_bytes(b'keep')
        plan['candidates'][0]['filename'] = '../../important.mkv'
        self.assertEqual(policy.apply_cleanup(plan)['deleted'],0)
        self.assertTrue(outside.exists())

    def test_hardlinks_are_never_cleanup_candidates(self):
        link = self.clips/'test_cpu_av1_24_s0.mkv'
        try:
            os.link(self.video,link)
        except OSError as e:
            self.skipTest(str(e))
        self.assertEqual(self.plan()['candidates'],[])

    def test_missing_protection_target_does_not_create_an_archive(self):
        self.assertIsNone(policy.set_session_protected('missing',True))
        self.assertFalse((self.previews/'missing').exists())

    def test_loading_real_archive_adds_context_without_rewriting_scores(self):
        before = self.metadata.read_bytes()
        data = vmaf.load_session(self.session)
        self.assertIn('archive_context',data)
        self.assertIn('coverage',data)
        self.assertFalse(data['source_available'])
        self.assertEqual(self.metadata.read_bytes(),before)


class StoragePlanningTests(unittest.TestCase):
    def filesystem(self,path):
        kind = 'work' if 'work' in str(path) else 'archive' if 'archive' in str(path) else 'output'
        device = {'work':1,'archive':2,'output':3}[kind]
        return {'key':kind,'device':device,'path':str(path),'free_bytes':10**12}

    def rows(self):
        return [{'source':'not-real.mkv','source_rel':'not-real.mkv','output':'output/movie.mkv','est_output_bytes':1000}]

    def estimate(self):
        return {'not-real.mkv':{'source_info':{'width':1920,'height':1080,'fps':24,'duration':600,'size_bytes':1000000}}}

    def test_separate_filesystems_get_temp_archive_and_output_reserves(self):
        with patch.object(policy,'_filesystem',side_effect=self.filesystem), patch.object(config,'WORK_DIR',Path('work')), patch.object(config,'VMAF_SESSIONS_DIR',Path('archive')):
            plan = policy.storage_plan(self.rows(),{'vmaf_check':True,'generate_screenshots':False},self.estimate())
        groups = {r['device']:r for r in plan['filesystems']}
        self.assertEqual(set(groups),{1,2,3})
        self.assertEqual(groups[3]['required_bytes'],1250)
        self.assertGreater(groups[1]['required_bytes'],groups[2]['required_bytes'])

    def test_same_filesystem_does_not_duplicate_archive_rename(self):
        fs = {'key':'same','device':1,'path':'same','free_bytes':10**12}
        with patch.object(policy,'_filesystem',return_value=fs):
            plan = policy.storage_plan(self.rows(),{'generate_screenshots':False},self.estimate())
        self.assertEqual(len(plan['filesystems']),1)
        kinds = [r['kind'] for r in plan['filesystems'][0]['allocations']]
        self.assertNotIn('vmaf_archive',kinds)
        self.assertIn('vmaf_temp',kinds)

    def test_unknown_dimensions_remain_open_and_insufficient_never_claims_ok(self):
        fs = {'key':'same','device':1,'path':'same','free_bytes':1}
        with patch.object(policy,'_filesystem',return_value=fs):
            plan = policy.storage_plan(self.rows(),{'generate_screenshots':False})
        group = plan['filesystems'][0]
        self.assertEqual(group['status'],'insufficient')
        self.assertIn('vmaf_temp',group['unknown_components'])
        self.assertTrue(group['required_is_lower_bound'])

    def test_compare_only_does_not_budget_nonexistent_full_movie_output(self):
        fs = {'key':'same','device':1,'path':'same','free_bytes':10**12}
        with patch.object(policy,'_filesystem',return_value=fs):
            plan = policy.storage_plan(self.rows(),{'workflow':'compare_only','generate_screenshots':False},self.estimate())
        self.assertNotIn('output',[r['kind'] for r in plan['filesystems'][0]['allocations']])

    def test_preview_batch_api_contract_includes_storage(self):
        with patch.object(job_plan,'plan_one',return_value={**self.rows()[0],'duplicate':False}), patch.object(policy,'storage_plan',return_value={'filesystems':[]}) as storage:
            result = job_plan.preview_batch(['not-real.mkv'],{'vmaf_check':False})
        self.assertIn('storage',result)
        self.assertFalse(storage.call_args.args[1]['vmaf_check'])


if __name__ == '__main__':
    unittest.main()
