"""Registered process lifecycle and cancellation; no FFmpeg/GPU dependency."""
from __future__ import annotations

import asyncio
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from core import runtime, media_stream, player_hls


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.registry = patch.object(runtime,'_STREAMS',{})
        self.registry.start()
        self.addCleanup(self.registry.stop)

    def test_unknown_cpu_first_sample_then_real_measurement_and_dead_process(self):
        proc = SimpleNamespace(pid=123,poll=lambda:None)
        handle = Mock()
        handle.is_running.return_value = True
        handle.cpu_percent.return_value = 132.5
        handle.memory_info.return_value = SimpleNamespace(rss=1048576)
        psutil = SimpleNamespace(Process=Mock(return_value=handle),Error=RuntimeError)
        with patch.object(runtime,'psutil',psutil):
            meter = runtime.ProcessMetrics()
            self.assertIsNone(meter.sample(proc)['cpu_percent'])
            row = meter.sample(proc)
            self.assertEqual(row['cpu_percent'],132.5)
            self.assertEqual(row['rss_bytes'],1048576)
            proc.poll = lambda:0
            self.assertIsNone(meter.sample(proc)['cpu_percent'])
            psutil.Process.assert_called_once_with(123)

    def test_stop_only_own_registered_process_and_expected_owner(self):
        proc = Mock(pid=123,returncode=None)
        proc.poll.return_value = None
        def stop():
            proc.poll.return_value = -9
            proc.returncode = -9
        proc.kill.side_effect = stop
        with patch.object(runtime,'psutil',None), patch.object(runtime.os,'killpg',create=True) as killpg:
            sid = runtime.register_process(proc,owner='editor',source='x')
            self.assertFalse(runtime.stop_session(sid,owner='ab_nerd'))
            proc.kill.assert_not_called()
            self.assertFalse(runtime.stop_session('123'))
            self.assertTrue(runtime.stop_session(sid,owner='editor'))
            proc.kill.assert_called_once()
        row = runtime.list_runtime_sessions()[0]
        self.assertEqual(row['owner'],'editor')
        self.assertEqual(row['state'],'finished')
        self.assertFalse(row['can_stop'])

    def test_unkillable_process_stays_visible_stopping(self):
        proc = Mock(pid=123,returncode=None)
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired('fake',5)
        with patch.object(runtime,'psutil',None), patch.object(runtime.os,'killpg',create=True):
            sid = runtime.register_process(proc)
            runtime.stop_session(sid)
        row = runtime.list_runtime_sessions(include_finished=False)[0]
        self.assertEqual(row['state'],'stopping')
        self.assertEqual(runtime.cleanup_idle(0),0)

    def test_hls_snapshot_is_not_a_keepalive_and_stop_owner_is_scoped(self):
        with tempfile.TemporaryDirectory() as root:
            sess = player_hls.PlayerSession(id='s',path=Path('in.mkv'),rel='in.mkv',audio_index=-1,
                subtitle_index=-1,start_sec=0,duration=10,title='Test',profile='copy',platform='cpu',
                codec='',encoder='copy',burn_subs=False,audio_codec='',mode='direct',height=0,
                v_bitrate=0,lookahead_sec=60,work_dir=Path(root),owner='ab_nerd')
            last_access = sess.last_access
            with patch.object(player_hls,'_SESSIONS',{'s':sess}), patch.object(player_hls,'_kill') as kill:
                rows = player_hls.list_runtime_sessions(include_finished=False)
                self.assertEqual(sess.last_access,last_access)
                self.assertEqual(rows[0]['owner'],'ab_nerd')
                self.assertFalse(player_hls.stop_session('s',owner='ab_standard'))
                kill.assert_not_called()
                self.assertTrue(player_hls.stop_session('s',owner='ab_nerd'))
                self.assertTrue(sess.cancel)

    def test_effective_tonemap_and_filters_follow_real_command_not_requested_look(self):
        sess = SimpleNamespace(requested={'look':{'tonemap':True,'engine':'cuda','algo':'bt2390','denoise':'light'}},
            source={'hdr':True},mode='hls',encoder='libx264',subtitle_index=-1,burn_subs=False,
            platform='cpu',codec='h264',profile='custom',fallback_reasons=[],
            audio_play_mode=lambda:'none',lock=runtime.threading.RLock())
        cmd = ['ffmpeg','-c:v','libx264','-vf','tonemap=tonemap=hable:desat=0,hqdn3d=1:1:2:2','out.m3u8']
        player_hls._record_effective(sess,cmd)
        self.assertEqual(sess.effective['tonemap']['engine'],'cpu')
        self.assertEqual(sess.effective['tonemap']['algo'],'hable')
        self.assertEqual(sess.effective['picture']['denoise'],'light')
        self.assertTrue(sess.effective['fallback'])


class AsyncMediaStreamTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.registry = patch.object(runtime,'_STREAMS',{})
        self.registry.start()
        self.addCleanup(self.registry.stop)

    async def asyncTearDown(self):
        await asyncio.to_thread(runtime.stop_all)

    async def test_client_close_reaps_process_and_preserves_owner(self):
        cmd = [sys.executable,'-c','import sys,threading; sys.stdout.buffer.write(b"abc"); sys.stdout.flush(); threading.Event().wait(30)']
        stream = media_stream.async_stream_bytes(cmd,chunk=3,owner='editor',label='Test',source='fixture')
        self.assertEqual(await asyncio.wait_for(anext(stream),5),b'abc')
        active = runtime.list_runtime_sessions(include_finished=False)
        self.assertEqual(active[0]['owner'],'editor')
        self.assertIsNotNone(active[0]['pid'])
        await asyncio.wait_for(stream.aclose(),5)
        self.assertEqual(runtime.list_runtime_sessions(include_finished=False),[])

    async def test_cancel_during_blocked_read_kills_process_instead_of_waiting_for_stdout(self):
        cmd = [sys.executable,'-c','import threading; threading.Event().wait(30)']
        stream = media_stream.async_stream_bytes(cmd,owner='preview')
        reader = asyncio.create_task(anext(stream))
        # Yield until the registered Popen exists, no blocking sleep or encoder.
        for _ in range(100):
            if runtime.list_runtime_sessions(include_finished=False):
                break
            await asyncio.sleep(0)
        self.assertTrue(runtime.list_runtime_sessions(include_finished=False))
        reader.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(reader,5)
        # Cleanup is shielded and can finish just after request cancellation.
        await asyncio.to_thread(runtime.stop_all)
        self.assertEqual(runtime.list_runtime_sessions(include_finished=False),[])

    async def test_verbose_stderr_cannot_fill_a_pipe_and_deadlock_media(self):
        cmd = [sys.executable,'-c','import sys; sys.stderr.write("x"*1000000); sys.stderr.flush(); sys.stdout.buffer.write(b"ok"); sys.stdout.flush()']
        stream = media_stream.async_stream_bytes(cmd,chunk=2,owner='preview')
        self.assertEqual(await asyncio.wait_for(anext(stream),5),b'ok')
        await stream.aclose()
        self.assertEqual(runtime.list_runtime_sessions(include_finished=False),[])


if __name__ == '__main__':
    unittest.main()
