"""A/B transport regressions against the actual frontend code.

Development-only dependency: quickjs-ng. No browser, FFmpeg or server needed.
Run with: python -m unittest discover -s tests -p test_ab_player_sync.py
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

try:
    import quickjs
except ImportError:
    quickjs = None


APP_JS = Path(__file__).resolve().parents[1] / "static" / "js" / "app.js"

MOCK_DOM = r"""
const events = [];
const timers = new Map();
let nextTimer = 0;
function setTimeout(fn) { const id = ++nextTimer; timers.set(id, fn); return id; }
function clearTimeout(id) { timers.delete(id); }
function flushEvent() { if (!events.length) return false; events.shift()(); return true; }
function fireTimers() {
  const pending = Array.from(timers.entries());
  pending.forEach(([id, fn]) => { if (timers.delete(id)) fn(); });
}
class Element {
  constructor() {
    this.listeners = new Map(); this.value = ""; this.checked = false;
    this.style = { setProperty() {} };
    const classes = new Set();
    this.classList = {
      contains: (name) => classes.has(name),
      toggle: (name, on) => on ? classes.add(name) : classes.delete(name),
      add: (name) => classes.add(name), remove: (name) => classes.delete(name),
    };
  }
  addEventListener(type, fn, opts) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push({ fn, once: !!(opts && opts.once) });
  }
  removeEventListener(type, fn) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter((h) => h.fn !== fn));
  }
  emit(type) {
    for (const handler of [...(this.listeners.get(type) || [])]) {
      if (handler.once) this.removeEventListener(type, handler.fn);
      handler.fn({ target: this, preventDefault() {}, stopPropagation() {} });
    }
  }
  click() { this.emit("click"); }
  querySelectorAll() { return []; }
}
class Video extends Element {
  constructor() {
    super(); this.paused = true; this.readyState = 4; this.seeking = false;
    this.ended = false; this.error = null; this._time = 0; this.duration = 600;
    this._rate = 1; this.defaultPlaybackRate = 1; this.rateWrites = 0;
    this.playCalls = 0; this.seekWrites = 0; this.loadCalls = 0;
    this.autoSeek = true; this.autoLoad = true; this.src = "";
    this.buffered = { length: 1, start: () => 0, end: () => 600 };
  }
  get playbackRate() { return this._rate; }
  set playbackRate(value) {
    this.rateWrites++;
    if (this._rate !== value) {
      this._rate = value;
      events.push(() => this.emit("ratechange"));
    }
  }
  get currentTime() { return this._time; }
  set currentTime(value) {
    this._time = value; this.seekWrites++; this.seeking = true; this.readyState = 2;
    events.push(() => this.emit("seeking"));
    if (this.autoSeek) events.push(() => this.finishSeek());
  }
  finishSeek() {
    this.seeking = false; this.readyState = 4;
    events.push(() => this.emit("seeked"));
    events.push(() => this.emit("canplay"));
    events.push(() => this.emit("timeupdate"));
  }
  play() {
    this.playCalls++;
    if (this.paused) {
      this.paused = false;
      events.push(() => this.emit("play"));
    }
    return Promise.resolve();
  }
  pause() {
    if (!this.paused) {
      this.paused = true;
      events.push(() => this.emit("pause"));
    }
  }
  removeAttribute(name) { if (name === "src") this.src = ""; }
  load() {
    this.loadCalls++; this._time = 0; this.seeking = false; this.readyState = 0;
    this.playbackRate = this.defaultPlaybackRate;
    if (this.src && this.autoLoad) events.push(() => {
      this.readyState = 4;
      this.emit("loadedmetadata"); this.emit("loadeddata"); this.emit("canplay");
    });
  }
  advance(seconds) { if (!this.paused && this.readyState >= 3) this._time += seconds * this.playbackRate; }
}
const elements = {};
for (const id of ["btn-ab-load", "btn-ab-unload", "ab-play", "ab-seek", "ab-sync",
                 "ab-offset", "ab-offset-frames", "ab-offset-reset",
                 "ab-nudge-b-back", "ab-nudge-b-fwd", "ab-nudge-a-back", "ab-nudge-a-fwd",
                 "ab-card", "ab-badge", "ab-status", "ab-speed",
                 "ab-step-back", "ab-step-fwd", "ab-path-a", "ab-path-b",
                 "ab-root-a", "ab-root-b", "ab-same-source", "ab-mode", "ab-zoom",
                 "ab-playback", "ab-weak"]) {
  elements[id] = new Element();
}
elements["ab-video-a"] = new Video();
elements["ab-video-b"] = new Video();
elements["ab-sync"].checked = true;
elements["ab-offset"].value = "0";
elements["ab-speed"].value = "1";
elements["ab-mode"].value = "side";
elements["ab-zoom"].value = "fit";
elements["ab-playback"].value = "auto";
elements["ab-root-a"].value = elements["ab-root-b"].value = "media";
elements["ab-card"].classList.add("is-nerd");
const $ = (id) => elements[id] || null;
const window = { addEventListener() {}, devicePixelRatio: 1 };
const localStorage = { getItem: () => null, setItem() {} };
const state = { currentPage: "vmafnerd", abJob: "", vmafSession: "test", vmafSource: { path: "source.mkv" } };
const tt = (text) => text;
function libDvProfileName() { return ""; }
const fmtClock = (value) => String(value);
function navTo() {}
function idsForPicks() { return []; }
function syncNerdAbToPicks() {}
const fetchCalls = [];
const response = (body) => ({ json: () => Promise.resolve(body) });
let fetchImpl = () => Promise.resolve(response({ ok: true }));
function fetch(url, options) {
  fetchCalls.push({ url, options });
  return fetchImpl(url, options);
}
"""


MOCK_HLS = r"""
// Die Validierungsbedingungen aus hls.js 1.5.17/src/config.ts (mergeConfig).
// Der frühere Dummy akzeptierte das ungültige Paar und übersah die Regression.
function validateHlsConfig(config) {
  if ((config.liveSyncDurationCount || config.liveMaxLatencyDurationCount)
      && (config.liveSyncDuration || config.liveMaxLatencyDuration)) {
    throw new Error("Illegal hls.js config: don't mix count and duration");
  }
  if (config.liveMaxLatencyDurationCount !== undefined
      && (config.liveSyncDurationCount === undefined
          || config.liveMaxLatencyDurationCount <= config.liveSyncDurationCount)) {
    throw new Error('Illegal hls.js config: "liveMaxLatencyDurationCount" must be greater than "liveSyncDurationCount"');
  }
  if (config.liveMaxLatencyDuration !== undefined
      && (config.liveSyncDuration === undefined || config.liveMaxLatencyDuration <= config.liveSyncDuration)) {
    throw new Error('Illegal hls.js config: "liveMaxLatencyDuration" must be greater than "liveSyncDuration"');
  }
}
class MockHls {
  constructor(config = {}) {
    validateHlsConfig(config);
    this.config = config; this.listeners = new Map(); this.destroyed = false;
    MockHls.instances.push(this);
  }
  on(type, fn) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(fn);
  }
  off(type, fn) {
    this.listeners.set(type, (this.listeners.get(type) || []).filter((h) => h !== fn));
  }
  emit(type, data) {
    for (const fn of [...(this.listeners.get(type) || [])]) fn(type, data);
  }
  loadSource(url) { this.url = url; }
  attachMedia(video) {
    video.src = this.url;
    events.push(() => {
      if (this.destroyed) return;
      if (MockHls.autoPts) this.emit(MockHls.Events.INIT_PTS_FOUND, {
        id: 'main', initPTS: MockHls.initPTS, timescale: MockHls.timescale,
      });
      video.readyState = 4;
      video.emit('loadedmetadata'); video.emit('loadeddata'); video.emit('canplay');
    });
  }
  destroy() {
    this.emit(MockHls.Events.DESTROYING);
    this.destroyed = true;
  }
}
MockHls.Events = { INIT_PTS_FOUND: 'pts', DESTROYING: 'destroying', ERROR: 'error' };
MockHls.isSupported = () => true;
MockHls.instances = [];
MockHls.autoPts = true;
MockHls.initPTS = 61;
MockHls.timescale = 1;
window.Hls = MockHls;
const hlsStarts = [];
fetchImpl = (url, options) => {
  if (url.startsWith('/api/probe')) return Promise.resolve(response({fps: 24, duration: 600}));
  if (url === '/api/player/session') {
    const body = JSON.parse(options.body);
    hlsStarts.push(body);
    return Promise.resolve(response({
      session: {id: 'hls-' + hlsStarts.length, mode: 'hls', start: body.start, duration: 600},
      info: {fps: 24, duration: 600},
    }));
  }
  return Promise.resolve(response({session: {ready: true}, ok: true}));
};
"""


@unittest.skipIf(quickjs is None, "Install quickjs-ng to run frontend transport regressions")
class AbPlayerSyncTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = APP_JS.read_text(encoding="utf-8")
        start = cls.source.index("  function picksFromClipIds(")
        end = cls.source.index("  /* ------------------------------------------------------------- DIAGNOSE */", start)
        cls.transport = cls.source[start:end]
        start = cls.source.index("  function navTo(")
        end = cls.source.index("  function settleVmafCharts(", start)
        cls.navigation = cls.source[start:end]
        start = cls.source.index("  function syncAbChrome(")
        end = cls.source.index("  function screenshotAbPicks(", start)
        cls.navigation += cls.source[start:end]

    def setUp(self):
        self.js = quickjs.Context()
        self.js.eval(MOCK_DOM + self.transport + "\ninitAbCompare();")
        self.js.eval("""
          const a = ab.sides.a, b = ab.sides.b;
          for (const side of [a, b]) {
            side.path = side.key; side.mode = "direct"; side.ready = true;
            side.info = { fps: 24 }; side.video.src = side.key;
          }
        """)

    def drain(self):
        for _ in range(2000):
            if self.js.execute_pending_job():
                continue
            if self.js.eval("flushEvent()"):
                continue
            return
        self.fail("Unbounded A/B event or promise loop")

    def evaluate(self, source):
        self.js.eval(source)
        self.drain()

    def value(self, expression):
        return json.loads(self.js.eval(f"JSON.stringify({expression})"))

    def assert_playing_together(self):
        self.assertEqual(self.value("[a.video.paused, b.video.paused]"), [False, False])
        self.assertAlmostEqual(self.value("abTime(a) + abOffsetB()"), self.value("abTime(b)"))
        self.assertEqual(self.value("[a.video.playbackRate, b.video.playbackRate]"), [1, 1])

    def test_speed_select_applies_to_both_without_starting_playback(self):
        for rate in [0.5, 0.25, 1]:
            with self.subTest(rate=rate):
                self.evaluate(f"$('ab-speed').value = '{rate}'; $('ab-speed').emit('change');")
                self.assertEqual(self.value("[a.video.playbackRate, b.video.playbackRate]"), [rate, rate])
                self.assertEqual(self.value("[a.video.defaultPlaybackRate, b.video.defaultPlaybackRate]"), [rate, rate])
                self.assertEqual(self.value("[a.video.paused, b.video.paused]"), [True, True])
                self.assertEqual(self.value("fetchCalls.length"), 0)

    def test_speed_change_during_playback_does_not_reload_seek_or_pause(self):
        self.evaluate("abPlayTogether();")
        self.evaluate("$('ab-speed').value = '0.25'; $('ab-speed').emit('change');")
        self.assertEqual(self.value("[a.video.paused, b.video.paused]"), [False, False])
        self.assertEqual(self.value("[a.video.seekWrites, b.video.seekWrites, a.video.loadCalls, b.video.loadCalls]"), [0, 0, 0, 0])
        self.evaluate("a.video.advance(4); b.video.advance(4);")
        self.assertEqual(self.value("[abTime(a), abTime(b)]"), [1, 1])
        self.assertEqual(self.value("fetchCalls.length"), 0)

    def test_speed_survives_seek_and_buffer_recovery(self):
        self.evaluate("$('ab-speed').value = '0.5'; $('ab-speed').emit('change'); abPlayTogether();")
        self.evaluate("abSeekBoth(42);")
        self.evaluate("b.video.readyState = 2; b.video.emit('waiting');")
        self.assertEqual(self.value("[a.video.paused, b.video.paused]"), [True, True])
        self.evaluate("b.video.readyState = 3; b.video.emit('canplay');")
        self.assertEqual(self.value("[a.video.paused, b.video.paused]"), [False, False])
        self.assertEqual(self.value("[a.video.playbackRate, b.video.playbackRate]"), [0.5, 0.5])
        self.assertEqual(self.value("[abTime(a), abTime(b)]"), [42, 42])

    def test_speed_survives_nerd_hls_reload_and_unload(self):
        self.js.eval(MOCK_HLS)
        self.evaluate("$('ab-speed').value = '0.25'; $('ab-speed').emit('change');")
        self.evaluate("""
          openClipAb([{kind: 'ref', start: 64, len: 8},
                      {kind: 'enc', start: 64, len: 8, clip: 'encoded.mp4'}], {stay: true});
        """)
        self.evaluate("abPlayTogether();")
        self.assertEqual(self.value("[a.video.playbackRate, b.video.playbackRate]"), [0.25, 0.25])
        self.evaluate("abUnload(true);")
        self.assertEqual(self.value("[a.video.playbackRate, b.video.playbackRate]"), [0.25, 0.25])
        self.assertEqual(self.value("$('ab-speed').value"), "0.25")

    def test_rate_reset_on_either_video_is_repaired_without_event_loop(self):
        self.evaluate("$('ab-speed').value = '0.5'; $('ab-speed').emit('change');")
        for key in ["a", "b"]:
            with self.subTest(side=key):
                self.evaluate(f"{key}.video.playbackRate = 1; {key}.video.emit('loadedmetadata');")
                self.assertEqual(self.value("[a.video.playbackRate, b.video.playbackRate]"), [0.5, 0.5])
        self.assertEqual(self.value("events.length"), 0)
        self.assertLessEqual(self.value("a.video.rateWrites + b.video.rateWrites"), 10)

    def test_invalid_speed_falls_back_to_normal(self):
        for value in ["", "0", "-1", "NaN", "Infinity", "2"]:
            with self.subTest(value=value):
                self.evaluate(f"$('ab-speed').value = {json.dumps(value)}; $('ab-speed').emit('change');")
                self.assertEqual(self.value("[a.video.playbackRate, b.video.playbackRate]"), [1, 1])
                self.assertEqual(self.value("$('ab-speed').value"), "1")

    def test_frame_step_stays_one_source_frame_at_quarter_speed(self):
        self.evaluate("$('ab-speed').value = '0.25'; $('ab-speed').emit('change');")
        self.evaluate("$('ab-step-fwd').click();")
        self.assertAlmostEqual(self.value("abTime(a)"), 1 / 24)
        self.assertAlmostEqual(self.value("abTime(b)"), 1 / 24)
        self.assertEqual(self.value("[a.video.paused, b.video.paused]"), [True, True])
        self.assertEqual(self.value("[a.video.playbackRate, b.video.playbackRate]"), [0.25, 0.25])

    def enable_real_navigation(self):
        self.js.eval('''
          function applyPageVisibility() {}
          function refreshNerdFrames() {}
          function rememberScreenshotPicks() {}
          function picksFromClipIds() { return []; }
          function settleVmafCharts() {}
        ''' + self.navigation)

    def test_hls_config_reproduces_old_error_and_accepts_current_pair(self):
        self.js.eval(MOCK_HLS)
        with self.assertRaisesRegex(Exception, "liveMaxLatencyDurationCount"):
            self.js.eval("new MockHls({liveMaxLatencyDurationCount: Infinity});")
        self.evaluate("abStartSide(a, 0);")
        self.assertTrue(self.value("a.ready"))
        self.assertEqual(self.value("a.hls.config.liveSyncDurationCount"), 3)
        self.assertTrue(self.value("a.hls.config.liveMaxLatencyDurationCount === Infinity"))
        self.assertEqual(self.value("a.hls.config.maxLiveSyncPlaybackRate"), 1)

    def test_both_standalone_sides_start_with_valid_hls_config(self):
        self.js.eval(MOCK_HLS)
        self.evaluate("ab.context = 'standalone'; abStartSide(a, 0); abStartSide(b, 0);")
        self.assertEqual(self.value("[a.ready, b.ready]"), [True, True])
        self.assertEqual(self.value("MockHls.instances.length"), 2)

    def test_nerd_start_does_not_overwrite_standalone_files(self):
        self.evaluate("""
          $('ab-path-a').value = 'normal-source.mkv';
          $('ab-path-b').value = 'normal-encode.mkv';
          openClipAb([{kind:'enc', clip:'first.mp4', len:15},
                      {kind:'enc', clip:'second.mp4', len:15}], {stay:true});
        """)
        self.assertEqual(self.value("[$('ab-path-a').value, $('ab-path-b').value]"),
                         ['normal-source.mkv', 'normal-encode.mkv'])
        self.assertIn("first.mp4", self.value("a.clipUrl"))
        self.assertIn("second.mp4", self.value("b.clipUrl"))
        self.js.eval(MOCK_HLS)
        self.evaluate("""
          openClipAb([{kind:'ref', start:64, len:15},
                      {kind:'enc', clip:'second.mp4', start:64, len:15}], {stay:true});
        """)
        self.assertEqual(self.value("a.path"), 'source.mkv')
        self.assertEqual(self.value("[$('ab-path-a').value, $('ab-path-b').value]"),
                         ['normal-source.mkv', 'normal-encode.mkv'])

    def test_source_remux_starts_with_the_encode_and_old_runs_use_the_file(self):
        self.evaluate("""
          openClipAb([{kind:'ref', start:64, len:15, clip:'source_s0.mkv', lead:2.5},
                      {kind:'enc', clip:'second.mp4', start:64, len:15}], {stay:true});
        """)
        self.assertIn("source_s0.mkv", self.value("a.clipUrl"))
        self.assertIn("source_s0.mkv", self.value("a.path"))
        self.assertAlmostEqual(self.value("a.video.currentTime"), 2.5)
        self.assertAlmostEqual(self.value("b.video.currentTime"), 0)
        self.assertAlmostEqual(self.value("abTime(a)"), 64)
        self.assertAlmostEqual(self.value("abTime(b)"), 64)
        self.assertAlmostEqual(self.value("a.offset"), 61.5)
        self.assertAlmostEqual(self.value("b.offset"), 64)
        self.assertIn("second.mp4", self.value("b.clipUrl"))
        self.evaluate("""
          openClipAb([{kind:'ref', start:64, len:15},
                      {kind:'enc', clip:'second.mp4', start:64, len:15}], {stay:true});
        """)
        self.assertEqual(self.value("a.path"), "source.mkv")
        self.assertEqual(self.value("a.clipUrl"), "")
        self.assertNotIn("source_s0", self.value("a.clipUrl"))

    def test_nerd_choice_uses_the_source_remux(self):
        self.evaluate("""
          function shotsOf(r) { return r.screenshots || []; }
          function vmafSceneList() { return [0]; }
          state.chartScene = 0;
          state.vmafShown = { results: [{ label: "ABR 6000", scene_scores: [{ scene: 0, start: 4700.889, length: 15 }],
            screenshots: [{ scene: 0, clip: "test_s0.mkv", source_clip: "source_s0.mkv", source_lead: 0.819 }] }] };
          state.clipPickIds = ["ref", "r0"];
          const picks = picksFromClipIds();
          openClipAb(picks, { stay: true });
        """)
        self.assertIn("source_s0.mkv", self.value("a.clipUrl"))
        self.assertAlmostEqual(self.value("a.offset"), 4700.889 - 0.819)
        self.assertIn("test_s0.mkv", self.value("b.clipUrl"))

    def test_testexport_uses_source_time_and_clip_time_without_start_offset(self):
        self.evaluate("""
          state.currentPage = 'abcompare';
          abSwitchContext('standalone');
          $('ab-path-a').value = 'original.mkv';
          $('ab-path-b').value = 'test-export:fixture/test.mkv';
          state.abTestSpan = {start:42, seconds:15, url:'/api/media?path=test'};
          $('btn-ab-load').click();
        """)
        self.assertEqual(self.value("ab.window"), {"start": 42, "end": 57})
        self.assertAlmostEqual(self.value("abTime(a)"), 42)
        self.assertAlmostEqual(self.value("abTime(b)"), 42)
        self.assertAlmostEqual(self.value("b.video.currentTime"), 0)

    def test_hidden_standard_load_cannot_replace_nerd_clips(self):
        self.evaluate("""
          $('ab-path-a').value = 'normal-source.mkv';
          $('ab-path-b').value = 'normal-encode.mkv';
          openClipAb([{kind:'enc', clip:'first.mp4', len:15},
                      {kind:'enc', clip:'second.mp4', len:15}], {stay:true});
          $('btn-ab-load').click();
        """)
        self.assertIn('first.mp4', self.value('a.video.src'))
        self.assertIn('second.mp4', self.value('b.video.src'))
        self.assertTrue(self.value('[a.ready, b.ready].every(Boolean)'))

    def test_unload_only_clears_files_in_standalone_context(self):
        self.evaluate("""
          $('ab-path-a').value = 'normal-source.mkv';
          $('ab-path-b').value = 'normal-encode.mkv';
          abUnload(true);
        """)
        self.assertEqual(self.value("[$('ab-path-a').value, $('ab-path-b').value]"),
                         ['normal-source.mkv', 'normal-encode.mkv'])
        self.evaluate("abSwitchContext('abcompare'); abUnload(true);")
        self.assertEqual(self.value("[$('ab-path-a').value, $('ab-path-b').value]"), ['', ''])

    def test_nav_nerd_to_standard_destroys_streams_and_preserves_file_selection(self):
        self.enable_real_navigation()
        self.js.eval(MOCK_HLS)
        self.evaluate("""
          $('ab-path-a').value = 'normal-source.mkv';
          $('ab-path-b').value = 'normal-encode.mkv';
          openClipAb([{kind:'ref', start:64, len:8},
                      {kind:'enc', start:64, len:8, clip:'encoded.mp4'}], {stay:true});
          abPlayTogether();
        """)
        old_sid = self.value("a.sid")
        self.evaluate("navTo('abcompare');")
        self.assertEqual(self.value("[ab.context, state.currentPage]"), ['standalone', 'abcompare'])
        self.assertTrue(self.value("abIsStandalone()"))
        self.assertEqual(self.value("[a.path, b.path, a.clipUrl, b.clipUrl, a.video.src, b.video.src]"), [''] * 6)
        self.assertEqual(self.value("[a.video.paused, b.video.paused]"), [True, True])
        self.assertTrue(self.value("MockHls.instances[0].destroyed"))
        self.assertIn(f"/api/player/session/{old_sid}", self.value("fetchCalls.filter(c => c.options && c.options.method === 'DELETE').map(c => c.url)"))
        self.assertEqual(self.value("[$('ab-path-a').value, $('ab-path-b').value]"),
                         ['normal-source.mkv', 'normal-encode.mkv'])
        self.assertIsNone(self.value("ab.window"))
        self.assertFalse(self.value("$('ab-card').classList.contains('is-nerd')"))
        self.evaluate("$('btn-ab-load').click();")
        self.assertEqual(self.value("hlsStarts.slice(-2).map(r => r.path)"),
                         ['normal-source.mkv', 'normal-encode.mkv'])
        self.assertEqual(self.value("[a.ready, b.ready]"), [True, True])

    def test_nav_standard_to_nerd_unloads_and_restores_each_context_controls(self):
        self.enable_real_navigation()
        self.evaluate("navTo('abcompare');")
        self.evaluate("""
          $('ab-speed').value = '0.5'; $('ab-speed').emit('change');
          $('ab-offset').value = '0.3'; $('ab-sync').checked = false;
          state.abJob = 'normal-job';
          a.path = 'normal-source.mkv'; a.video.src = a.path;
          b.path = 'normal-encode.mkv'; b.video.src = b.path;
          navTo('vmafnerd', {keepAb:true});
        """)
        self.assertEqual(self.value("[a.path, b.path, a.video.src, b.video.src]"), [''] * 4)
        self.assertEqual(self.value("[$('ab-speed').value, $('ab-offset').value, $('ab-sync').checked, state.abJob]"), ['1', '0', True, ''])
        self.evaluate("$('ab-speed').value = '0.25'; $('ab-speed').emit('change'); navTo('abcompare');")
        self.assertEqual(self.value("[$('ab-speed').value, $('ab-offset').value, $('ab-sync').checked, state.abJob]"), ['0.5', '0.3', False, 'normal-job'])
        self.assertEqual(self.value("[a.video.playbackRate, b.video.playbackRate]"), [0.5, 0.5])
        self.evaluate("navTo('vmafnerd', {keepAb:true});")
        self.assertEqual(self.value("$('ab-speed').value"), '0.25')

    def test_direct_open_nerd_switches_before_start_not_after(self):
        self.enable_real_navigation()
        self.js.eval(MOCK_HLS)
        self.evaluate("navTo('abcompare');")
        self.evaluate("""
          openClipAb([{kind:'ref', start:64, len:8},
                      {kind:'enc', start:64, len:8, clip:'encoded.mp4'}]);
        """)
        self.assertEqual(self.value("[ab.context, state.currentPage]"), ['nerd', 'vmafnerd'])
        self.assertEqual(self.value("$('ab-badge').textContent"), 'Geladen')
        self.assertEqual(self.value("hlsStarts.map(r => r.start)"), [63])
        self.assertEqual(self.value("[abTime(a), abTime(b)]"), [64, 64])
        self.assertEqual(self.value("[a.ready, b.ready]"), [True, True])

    def test_switching_same_context_does_not_unload(self):
        self.enable_real_navigation()
        self.evaluate("abPlayTogether(); navTo('vmafnerd', {keepAb:true});")
        self.assertEqual(self.value("[a.path, b.path]"), ['a', 'b'])
        self.assert_playing_together()

    def test_late_nerd_session_after_nav_cannot_repopulate_standard_player(self):
        self.enable_real_navigation()
        self.evaluate("""
          let finishSession;
          fetchImpl = (url) => {
            if (url.startsWith('/api/probe')) return Promise.resolve(response({fps:24}));
            if (url === '/api/player/session') return new Promise(r => {finishSession = r;});
            return Promise.resolve(response({ok:true}));
          };
          openClipAb([{kind:'ref', start:64, len:8},
                      {kind:'enc', start:64, len:8, clip:'encoded.mp4'}], {stay:true});
        """)
        self.evaluate("navTo('abcompare'); finishSession(response({session:{id:'late-nerd', mode:'hls'}}));")
        self.assertEqual(self.value("[a.path, b.path, a.video.src, b.video.src, a.sid]"), [''] * 5)
        self.assertIn('/api/player/session/late-nerd', self.value("fetchCalls.filter(c => c.options && c.options.method === 'DELETE').map(c => c.url)"))

    def test_weak_spot_response_from_other_context_is_ignored(self):
        self.enable_real_navigation()
        self.evaluate("navTo('abcompare');")
        self.evaluate("""
          let finishSpots;
          state.abJob = 'normal-job';
          fetchImpl = () => new Promise(r => {finishSpots = r;});
          abLoadWeakSpots();
          navTo('vmafnerd', {keepAb:true});
          finishSpots(response({spots:[{time:100, scene:0, vmaf:80}]}));
        """)
        self.assertIsNone(self.value("ab.weak"))
        self.assertEqual(self.value("$('ab-weak').innerHTML"), '')

    def test_complete_script_parses(self):
        self.js.eval("new Function(" + json.dumps(self.source) + ")")

    def test_loadeddata_and_timeout_are_not_seek_completion(self):
        self.evaluate("""
          b.video.readyState = 2; b.video.seeking = true;
          let result = null;
          abWaitMediaReady(b.video, 20).then((ok) => { result = ok; });
          b.video.emit('loadeddata');
        """)
        self.assertIsNone(self.value("result"))
        self.evaluate("b.video.readyState = 3; b.video.emit('canplay');")
        self.assertIsNone(self.value("result"))
        self.evaluate("fireTimers();")
        self.assertFalse(self.value("result"))

    def test_native_play_holds_master_until_both_are_ready(self):
        self.evaluate("b.video.readyState = 1; a.video.play();")
        self.assertEqual(self.value("[a.video.paused, b.video.paused]"), [True, True])
        self.evaluate("a.video.advance(2); b.video.advance(2);")
        self.assertEqual(self.value("[abTime(a), abTime(b)]"), [0, 0])
        self.evaluate("b.video.readyState = 3; b.video.emit('canplay');")
        self.assert_playing_together()

    def test_both_seek_completions_are_required_before_restart(self):
        self.evaluate("abPlayTogether();")
        self.evaluate("a.video.autoSeek = false; b.video.autoSeek = false; abSeekBoth(42);")
        self.assertEqual(self.value("[a.video.paused, b.video.paused]"), [True, True])
        self.evaluate("a.video.finishSeek();")
        self.assertTrue(self.value("a.video.paused"))
        self.evaluate("b.video.finishSeek();")
        self.assert_playing_together()
        self.assertEqual(self.value("[a.video.seekWrites, b.video.seekWrites]"), [1, 1])

    def test_a_buffer_gap_holds_b_and_recovers_together(self):
        self.evaluate("abPlayTogether();")
        self.evaluate("a.video.readyState = 2; a.video.emit('waiting');")
        self.assertEqual(self.value("[a.video.paused, b.video.paused, ab.wantPlay]"), [True, True, True])
        self.evaluate("b.video.advance(3);")
        self.assertEqual(self.value("abTime(b)"), 0)
        self.evaluate("a.video.readyState = 3; a.video.emit('canplay');")
        self.assert_playing_together()

    def test_b_buffer_gap_holds_a_and_recovers_together(self):
        self.evaluate("abPlayTogether();")
        self.evaluate("b.video.readyState = 2; b.video.emit('waiting');")
        self.assertEqual(self.value("[a.video.paused, b.video.paused, ab.wantPlay]"), [True, True, True])
        self.evaluate("a.video.advance(3);")
        self.assertEqual(self.value("abTime(a)"), 0)
        self.evaluate("b.video.readyState = 3; b.video.emit('canplay');")
        self.assert_playing_together()

    def test_pause_cancels_a_pending_start(self):
        self.evaluate("b.video.readyState = 1; abPlayTogether();")
        self.evaluate("$('ab-play').click(); b.video.readyState = 3; b.video.emit('canplay');")
        self.assertEqual(self.value("[a.video.paused, b.video.paused, ab.wantPlay]"), [True, True, False])

    def test_unload_cancels_buffer_recovery(self):
        self.evaluate("abPlayTogether();")
        self.evaluate("b.video.readyState = 2; b.video.emit('waiting');")
        self.evaluate("abUnload(true); b.video.readyState = 4; b.video.emit('canplay'); fireTimers();")
        self.assertEqual(self.value("[a.video.paused, b.video.paused, ab.wantPlay]"), [True, True, False])
        self.assertEqual(self.value("[a.path, b.path]"), ["", ""])

    def test_late_abort_only_deletes_its_own_session(self):
        self.evaluate("""
          a.token = 'new-token'; a.sid = 'new-session'; a.video.src = 'new-video';
          abAbortStart(a, 'old-token', 'old-session');
        """)
        self.assertEqual(self.value("[a.sid, a.token, a.video.src]"), ["new-session", "new-token", "new-video"])
        self.assertEqual(self.value("fetchCalls.filter(c => c.options.method === 'DELETE').map(c => c.url)"),
                         ["/api/player/session/old-session"])

    def test_late_session_response_cannot_detach_the_next_clip(self):
        self.evaluate("""
          let completeStart;
          a.info = { fps: 24 };
          fetchImpl = (url) => url === '/api/player/session'
            ? new Promise((resolve) => { completeStart = resolve; })
            : Promise.resolve(response({ok: true}));
          abStartSide(a, 0);
        """)
        self.evaluate("""
          a.epoch++; a.token = 'new-token'; a.sid = 'new-session'; a.video.src = 'new-video';
          completeStart(response({ session: {id: 'old-session', mode: 'hls'} }));
        """)
        self.assertEqual(self.value("[a.sid, a.video.src]"), ["new-session", "new-video"])

    def test_small_drift_has_no_seeks_or_rate_nudges(self):
        self.evaluate("abPlayTogether();")
        self.evaluate("""
          for (let i = 0; i < 120; i++) {
            a.video._time += 1 / 24; b.video._time = a.video._time - 0.04;
            a.video.emit('timeupdate');
          }
        """)
        self.assertEqual(self.value("[a.video.seekWrites, b.video.seekWrites]"), [0, 0])
        self.assertEqual(self.value("[a.video.playbackRate, b.video.playbackRate]"), [1, 1])

    def test_large_drift_is_corrected_once_without_seek_feedback(self):
        self.evaluate("abPlayTogether();")
        self.evaluate("a.video._time = 2; b.video._time = 1; a.video.emit('timeupdate');")
        self.assert_playing_together()
        self.assertEqual(self.value("[a.video.seekWrites, b.video.seekWrites]"), [0, 1])

    def test_nerd_original_and_clip_use_the_same_film_time(self):
        self.evaluate("""
          fetchImpl = (url, options) => {
            if (url.startsWith('/api/probe')) return Promise.resolve(response({fps: 24, duration: 600}));
            if (url === '/api/player/session') return Promise.resolve(response({
              session: {id: 'original', mode: 'direct', media_url: '/source', duration: 600},
              info: {fps: 24, duration: 600}
            }));
            return Promise.resolve(response({ok: true}));
          };
          openClipAb([{kind: 'ref', start: 64, len: 8},
                      {kind: 'enc', start: 64, len: 8, clip: 'encoded.mp4'}], {stay: true});
        """)
        self.assertEqual(self.value("[a.video.currentTime, b.video.currentTime, b.offset]"), [64, 0, 64])
        self.assertEqual(self.value("$('ab-badge').textContent"), "Geladen")
        self.evaluate("abPlayTogether();")
        self.assert_playing_together()

    def test_hls_pts_anchor_replaces_requested_start_and_honors_timescale(self):
        self.js.eval(MOCK_HLS)
        self.evaluate("""
          const hls = new MockHls();
          a.offset = 64;
          let anchored = null;
          abWaitHlsTimeline(a, hls).then((ok) => { anchored = ok; });
          hls.emit(MockHls.Events.INIT_PTS_FOUND, {id: 'main', initPTS: 61 * 90000, timescale: 90000});
          abSetTime(a, 64);
        """)
        self.assertTrue(self.value("anchored"))
        self.assertEqual(self.value("[a.offset, a.video.currentTime, abTime(a)]"), [61, 3, 64])
        self.assertEqual(self.value("timers.size"), 0)

    def test_hls_metadata_does_not_complete_start_without_pts(self):
        self.js.eval(MOCK_HLS)
        self.evaluate("""
          MockHls.autoPts = false;
          let started = null;
          abStartSide(a, 64).then((ok) => { started = ok; });
        """)
        self.assertIsNone(self.value("started"))
        self.assertTrue(self.value("a.starting"))
        self.assertFalse(self.value("a.ready"))
        self.evaluate("""
          a.hls.emit(MockHls.Events.INIT_PTS_FOUND, {id: 'main', initPTS: 61, timescale: 1});
        """)
        self.assertTrue(self.value("started"))
        self.assertEqual(self.value("a.offset"), 61)

    def test_nerd_hls_original_and_clip_use_measured_film_time(self):
        self.js.eval(MOCK_HLS)
        self.evaluate("""
          openClipAb([{kind: 'ref', start: 64, len: 8},
                      {kind: 'enc', start: 64, len: 8, clip: 'encoded.mp4'}], {stay: true});
        """)
        self.assertEqual(self.value("hlsStarts.map((request) => request.start)"), [63])
        self.assertEqual(self.value("[a.offset, a.video.currentTime, b.video.currentTime, b.offset]"),
                         [61, 3, 0, 64])
        self.assertEqual(self.value("$('ab-badge').textContent"), "Geladen")
        self.evaluate("abPlayTogether();")
        self.assert_playing_together()

    def test_nerd_hls_preroll_never_requests_negative_start(self):
        self.js.eval(MOCK_HLS)
        self.evaluate("""
          MockHls.initPTS = 0;
          openClipAb([{kind: 'ref', start: 0.3, len: 8},
                      {kind: 'enc', start: 0.3, len: 8, clip: 'encoded.mp4'}], {stay: true});
        """)
        self.assertEqual(self.value("hlsStarts.map((request) => request.start)"), [0])
        self.assertEqual(self.value("[abTime(a), abTime(b)]"), [0.3, 0.3])

    def test_hls_missing_anchor_fails_instead_of_guessing(self):
        self.js.eval(MOCK_HLS)
        self.evaluate("""
          MockHls.autoPts = false;
          let started = null;
          abStartSide(a, 64).then((ok) => { started = ok; });
        """)
        self.evaluate("fireTimers();")
        self.assertFalse(self.value("started"))
        self.assertFalse(self.value("a.ready"))
        self.assertEqual(self.value("a.sid"), "")

    def test_stale_hls_anchor_does_not_recalibrate_a_new_session(self):
        self.js.eval(MOCK_HLS)
        self.evaluate("""
          const hls = new MockHls(), epoch = a.epoch;
          let anchored = null;
          abWaitHlsTimeline(a, hls, () => a.epoch === epoch).then((ok) => { anchored = ok; });
          a.epoch++; a.offset = 100;
          hls.emit(MockHls.Events.INIT_PTS_FOUND, {id: 'main', initPTS: 61, timescale: 1});
        """)
        self.assertFalse(self.value("anchored"))
        self.assertEqual(self.value("a.offset"), 100)

    def test_hls_destroy_releases_pending_anchor_wait(self):
        self.js.eval(MOCK_HLS)
        self.evaluate("""
          const hls = new MockHls();
          let anchored = null;
          abWaitHlsTimeline(a, hls).then((ok) => { anchored = ok; });
          hls.destroy();
        """)
        self.assertFalse(self.value("anchored"))
        self.assertEqual(self.value("timers.size"), 0)

    def test_hls_ignores_invalid_and_alternate_audio_anchors(self):
        self.js.eval(MOCK_HLS)
        self.evaluate("""
          const hls = new MockHls();
          a.offset = 64;
          let anchored = null;
          abWaitHlsTimeline(a, hls).then((ok) => { anchored = ok; });
          for (const data of [null, {id: 'audio', initPTS: 4, timescale: 1},
                             {id: 'main', initPTS: null, timescale: 1},
                             {id: 'main', initPTS: 4, timescale: 0},
                             {id: 'main', initPTS: Infinity, timescale: 1}]) {
            hls.emit(MockHls.Events.INIT_PTS_FOUND, data);
          }
        """)
        self.assertIsNone(self.value("anchored"))
        self.assertEqual(self.value("a.offset"), 64)
        self.evaluate("hls.emit(MockHls.Events.INIT_PTS_FOUND, {id: 'main', initPTS: 61, timescale: 1});")
        self.assertTrue(self.value("anchored"))

    def test_standalone_hls_does_not_add_nerd_preroll(self):
        self.js.eval(MOCK_HLS)
        self.evaluate("""
          ab.context = 'standalone';
          $('ab-card').classList.contains = () => false;
          ab.window = {start: 64, end: 72};
          abStartSide(a, 64);
        """)
        self.assertEqual(self.value("hlsStarts.map((request) => request.start)"), [64])
        self.assertEqual(self.value("a.offset"), 61)

    def test_pause_and_resume_encode_requests_are_serialized(self):
        self.evaluate("""
          let finishPause;
          a.sid = 'encoder'; a.mode = 'hls';
          fetchImpl = (url) => url.endsWith('/pause')
            ? new Promise((resolve) => { finishPause = resolve; })
            : Promise.resolve(response({ok: true, encode_paused: false}));
          abSetEncodePaused(a, true);
          abSetEncodePaused(a, false);
        """)
        self.assertEqual(self.value("fetchCalls.map(c => c.url)"), ["/api/player/session/encoder/pause"])
        self.evaluate("finishPause(response({ok: true, encode_paused: true}));")
        self.assertEqual(self.value("fetchCalls.map(c => c.url)"),
                         ["/api/player/session/encoder/pause", "/api/player/session/encoder/resume"])
        self.assertFalse(self.value("a.encodePaused"))

    def test_nudge_single_side_shifts_offset_and_persists_across_joint_steps(self):
        self.evaluate("""
          a.info = { fps: 25 }; b.info = { fps: 25 };
          a.video._time = 10.0; b.video._time = 10.0;
          $('ab-nudge-b-fwd').click();
        """)
        self.assertAlmostEqual(self.value("Number($('ab-offset').value)"), 0.04, places=4)
        self.assertAlmostEqual(self.value("a.video.currentTime"), 10.0, places=3)
        self.assertAlmostEqual(self.value("b.video.currentTime"), 10.04, places=3)
        self.assertEqual(self.value("$('ab-offset-frames').textContent"), "+1 Bild")

        # Jetzt gemeinsames +1 Bild: beide rücken +0.04s vor, Versatz bleibt +1 Bild erhalten!
        self.evaluate("$('ab-step-fwd').click();")
        self.assertAlmostEqual(self.value("a.video.currentTime"), 10.04, places=3)
        self.assertAlmostEqual(self.value("b.video.currentTime"), 10.08, places=3)

        # Nur Video A um +1 Bild vor -> A auf 10.08, B bleibt auf 10.08 -> Versatz wieder 0!
        self.evaluate("$('ab-nudge-a-fwd').click();")
        self.assertAlmostEqual(self.value("Number($('ab-offset').value)"), 0.0, places=4)
        self.assertAlmostEqual(self.value("a.video.currentTime"), 10.08, places=3)
        self.assertAlmostEqual(self.value("b.video.currentTime"), 10.08, places=3)
        self.assertEqual(self.value("$('ab-offset-frames').textContent"), "±0 Bilder")


if __name__ == "__main__":
    unittest.main()
