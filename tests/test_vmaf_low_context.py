"""Low-Kontext: echte Produktionsfunktionen, ohne FFmpeg oder Browser.

Backend-Funktionen werden aus dem AST isoliert, damit die Tests weder die
Anwendung initialisieren noch deren optionale Encoder-Abhängigkeiten benötigen.
Frontend: optionale Entwicklungsabhängigkeit quickjs-ng.
"""
from __future__ import annotations

import ast
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

try:
    import quickjs
except ImportError:
    quickjs = None

ROOT = Path(__file__).resolve().parents[1]


def backend_functions():
    source = ast.parse((ROOT / "core" / "vmaf.py").read_text(encoding="utf-8"))
    names = {
        "_edge_margin", "_scored_window", "_finite", "_low_scored_seconds",
        "_metrics_from_json", "_downsample_vmaf", "_pct", "_series_stats",
        "_dip_line", "_downsample_min", "scene_frame_logs",
    }
    nodes = [node for node in source.body if isinstance(node, ast.FunctionDef) and node.name in names]
    module = ast.Module(body=nodes, type_ignores=[])
    scope = {"_EDGE_SEC": 0.5, "json": json}
    # Annotationen nicht auswerten: Die Tests brauchen keine Projektimporte.
    code = compile(ast.fix_missing_locations(module), "core/vmaf.py", "exec", flags=__import__("__future__").annotations.compiler_flag)
    exec(code, scope)
    return scope


class LowContextBackendTests(unittest.TestCase):
    def setUp(self):
        self.api = backend_functions()

    def test_scored_window_varies_with_clip_length(self):
        fn = self.api["_low_scored_seconds"]
        for duration, expected in [(2, 2), (4, 3.36), (15, 14), (60, 59)]:
            with self.subTest(duration=duration):
                self.assertAlmostEqual(fn({"length": duration}, {}, {}), expected)

    def test_stored_actual_window_wins_over_nominal_length(self):
        self.assertEqual(self.api["_low_scored_seconds"]({"scored_seconds": 13.96, "length": 15}, {}, {}), 13.96)

    def test_archive_fallbacks_and_unknown_duration(self):
        fn = self.api["_low_scored_seconds"]
        self.assertEqual(fn({}, {"clip_seconds": 30}, {}), 29)
        self.assertEqual(fn({}, {}, {"clip_seconds": 15}), 14)
        self.assertEqual(fn({"length": "bad", "scored_seconds": float("nan")}, {}, {}), 0)

    def test_frame_basis_matches_unchanged_low_calculation(self):
        for count in [1, 40, 99, 100, 337, 1500]:
            with self.subTest(count=count):
                scores = [80 + i / count for i in range(count)]
                metrics = self.api["_metrics_from_json"]({
                    "frames": [{"metrics": {"vmaf": v}} for v in scores],
                    "scored_seconds": 14,
                })
                k = max(1, int(count * 0.01))
                self.assertEqual(metrics["frame_count"], count)
                self.assertEqual(metrics["p1_count"], k)
                self.assertAlmostEqual(metrics["p1"], sum(scores[:k]) / k)
                self.assertEqual(metrics["scored_seconds"], 14)

    def test_full_logs_keep_raw_precision_and_trimmed_timing(self):
        with tempfile.TemporaryDirectory() as temp:
            log = Path(temp) / "frames.json"
            # 500 Frames werden für den Graphen gekürzt, nicht für den Kontext.
            log.write_text(json.dumps({"frames": [
                {"frameNum": i, "metrics": {"vmaf": 89.9999 if i < 5 else 98.0}}
                for i in range(500)
            ]}), encoding="utf-8")
            raw = {"label": "A", "scene_scores": [{"scene": 0, "length": 15}]}
            self.api.update({
                "config": SimpleNamespace(VMAF_SESSIONS_DIR=Path(temp)),
                "load_session": lambda _: {"analysis": {"results": [raw]}, "params": {}},
                "_frame_log_path": lambda *_: log,
            })
            short = self.api["scene_frame_logs"]("session", 0)["series"][0]
            full = self.api["scene_frame_logs"]("session", 0, True)["series"][0]
            self.assertEqual(len(short["frames"]), 400)
            self.assertEqual(len(full["frames"]), 500)
            self.assertEqual(full["low_context"], {"count": 500, "p1_count": 5, "scored_seconds": 14})
            self.assertEqual(full["frames"][0]["vmaf"], 90)
            self.assertEqual(full["frames"][0]["vmaf_raw"], 89.9999)
            self.assertNotIn("vmaf_raw", short["frames"][0])
            self.assertAlmostEqual(full["frame_sec"], 14 / 500, places=5)

    def test_missing_log_does_not_invent_context(self):
        self.api.update({
            "config": SimpleNamespace(VMAF_SESSIONS_DIR=ROOT),
            "load_session": lambda _: {"analysis": {"results": [{"label": "A"}]}},
            "_frame_log_path": lambda *_: None,
        })
        self.assertEqual(self.api["scene_frame_logs"]("session", 0), {"scene": 0, "series": []})


@unittest.skipIf(quickjs is None, "Install quickjs-ng for frontend tests")
class LowContextFrontendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = (ROOT / "static" / "js" / "app.js").read_text(encoding="utf-8")
        start = cls.source.index("  function vmafTargetLo(")
        end = cls.source.index("  function vmafSpread(", start)
        cls.policy = cls.source[start:end]
        start = cls.source.index("  function vmafLowContextLines(")
        end = cls.source.index("  function vmafCell(", start)
        cls.context = cls.source[start:end]
        start = cls.source.index("  function vmafBrief(")
        end = cls.source.index("  function lineMatchesKey(", start)
        cls.table = cls.source[start:end]

    def setUp(self):
        self.js = quickjs.Context()
        self.js.eval(r'''
          const state = {vmafSession: "session"};
          let gap = 6, anchor = "both", calls = [], fail = false;
          const vmafTargetSetting = () => 94;
          const vmafP1GapValue = () => gap;
          const vmafP1Anchor = () => anchor;
          const escapeHtml = (s) => String(s).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
          function fetch(url) {
            calls.push(url);
            return fail ? Promise.reject(new Error("offline")) : Promise.resolve({ok: true, json: () => Promise.resolve({series: [
              {label: "A", count: 3, low_context: {count: 3, scored_seconds: 2}, frames: [{n: 0, vmaf: 98}, {n: 1, vmaf: 89}, {n: 2, vmaf: 98}]},
              {label: "B", count: 3, low_context: {count: 3, scored_seconds: 2}, frames: [{n: 0, vmaf: 98}, {n: 1, vmaf: 89}, {n: 2, vmaf: 98}]}
            ]})});
          }
          function details() {
            const body = {innerHTML: ""};
            return {open: true, dataset: {}, isConnected: true, body, querySelector: () => body};
          }
        ''' + self.policy + self.context)

    def evaluate(self, code):
        return json.loads(self.js.eval("JSON.stringify(" + code + ")"))

    def drain(self):
        for _ in range(1000):
            if not self.js.execute_pending_job():
                return
        self.fail("Promise jobs did not settle")

    def lines(self, count, values=None, floor=90, p1=88.5, seconds=14, idle=False):
        series = None if values is None else {
            "count": count, "low_context": {"count": count, "scored_seconds": seconds},
            "frames": [{"n": i, "vmaf": value} for i, value in enumerate(values)],
        }
        sc = {"frame_count": count, "p1": p1, "idle": idle}
        return self.evaluate(f"vmafLowContextLines({json.dumps(sc)}, {json.dumps(series)}, {json.dumps(floor)})")

    def test_entire_application_parses(self):
        self.js.eval("new Function(" + json.dumps(self.source) + ")")

    def test_small_and_large_low_bases(self):
        for n, k in [(20, 1), (99, 1), (100, 1), (337, 3), (1500, 15)]:
            with self.subTest(n=n):
                text = " ".join(self.lines(n))
                self.assertIn(f"{k} schlechtesten von {n}", text)
                self.assertNotIn("ca.", text)
        self.assertIn("einziges Bild", " ".join(self.lines(20)))
        self.assertIn("5.0 % der Stichprobe", " ".join(self.lines(20)))
        self.assertIn("nur 3 Extremframes", " ".join(self.lines(337)))
        self.assertIn("vielen Bildern", " ".join(self.lines(1500)))

    def test_isolated_and_contiguous_dips(self):
        isolated = " ".join(self.lines(5, [98, 89, 98, 89, 98], seconds=2))
        self.assertIn("2 von 5 Frames (40.0 %)", isolated)
        self.assertIn("längste Folge: 1 Frame (ca. 0.40 s)", isolated)
        self.assertIn("keine direkt aufeinanderfolgenden", isolated)
        contiguous = " ".join(self.lines(5, [98, 89, 89, 89, 98], seconds=2))
        self.assertIn("längste Folge: 3 Frames (ca. 1.20 s)", contiguous)
        self.assertIn("einem zusammenhängenden Abschnitt", contiguous)

    def test_several_dip_runs(self):
        self.assertIn("weitere schwache Frames", " ".join(self.lines(6, [89, 89, 98, 89, 98, 98])))

    def test_boundary_is_strict_and_uses_unrounded_values(self):
        text = " ".join(self.lines(3, [90, 90, 91], p1=90))
        self.assertIn("Kein bewerteter Frame", text)
        series = {"count": 2, "frames": [{"n": 0, "vmaf": 90, "vmaf_raw": 89.9999}, {"n": 1, "vmaf": 91}]}
        lines = self.evaluate(f"vmafLowContextLines({{p1: 89.9999}}, {json.dumps(series)}, 90)")
        self.assertIn("1 von 2 Frames", " ".join(lines))

    def test_passing_low_is_not_a_minimum(self):
        text = " ".join(self.lines(4, [89, 91, 98, 98], p1=90))
        self.assertIn("Low-Kriterium erfüllt", text)
        self.assertIn("kein Einzelbild-Minimum", text)

    def test_disabled_floor_and_idle_scenes(self):
        text = " ".join(self.lines(3, [80, 80, 80], floor=None))
        self.assertIn("Low-Floor ausgeschaltet", text)
        self.assertNotIn("unter dieser Grenze", text)
        text = " ".join(self.lines(3, [80, 80, 80], idle=True))
        self.assertIn("zählt nicht", text)
        self.assertNotIn("Low-Kriterium nicht erfüllt", text)

    def test_missing_or_downsampled_logs_never_supply_duration(self):
        text = " ".join(self.lines(337, [80, 81]))
        self.assertIn("vollständige Frame-Log", text)
        self.assertNotIn("längste Folge", text)
        text = " ".join(self.lines(0))
        self.assertIn("Framebasis nicht bestimmen", text)

    def test_missing_indices_break_a_run(self):
        series = {"count": 2, "frames": [{"n": 1, "vmaf": 80}, {"n": 3, "vmaf": 80}]}
        lines = self.evaluate(f"vmafLowContextLines({{p1:80}}, {json.dumps(series)}, 90)")
        self.assertIn("längste Folge: 1 Frame", " ".join(lines))

    def test_floor_anchors_unchanged_and_explained(self):
        for name, expected in [("target", 88), ("mean", 90.78), ("both", 90.78)]:
            with self.subTest(anchor=name):
                value = self.evaluate(f'vmafSceneFloor({{vmaf:96.78}}, 94, 6, "{name}")')
                self.assertAlmostEqual(value, expected)
        html = self.evaluate('vmafLowContextHtml({vmaf:96.78, recommended:true, scene_scores:[{scene:0, p1:88.54, frame_count:337}]}, {target_lo:94})')
        self.assertIn("90.78", html)
        self.assertIn("hebt das Gesamtmittel", html)
        self.assertIn("Als Kompromiss empfohlen", html)
        self.assertIn("nur 3 Extremframes", html)
        self.assertEqual(self.evaluate('vmafMissLabel({p1:88})'), "Low-Kriterium verfehlt")

    def test_context_only_explains_failed_active_scenes(self):
        self.js.eval('''
          const r = {vmaf:96, scene_scores:[
            {scene:0, p1:88, frame_count:337},
            {scene:1, p1:94, frame_count:337},
            {scene:2, p1:80, idle:true, frame_count:337},
            {scene:3, p1:89, frame_count:337},
            {scene:4, p1:90, frame_count:337},
            {scene:5, frame_count:337}
          ]};
        ''')
        self.assertEqual(self.evaluate('vmafLowContextScenes(r, {target_lo:94}).map(sc => sc.scene)'), [0, 3])
        html = self.evaluate('vmafLowContextHtml(r, {target_lo:94})')
        self.assertIn('<strong>Szene 1</strong>', html)
        self.assertIn('<strong>Szene 4</strong>', html)
        for scene in [2, 3, 5, 6]:
            self.assertNotIn(f'<strong>Szene {scene}</strong>', html)
        self.assertEqual(html.count('Verwendete Low-Grenze:'), 1)
        self.assertEqual(html.count('nur die getesteten Ausschnitte'), 1)

    def test_unaffected_results_and_disabled_floor_have_no_context(self):
        self.assertEqual(self.evaluate('vmafLowContextHtml({vmaf:96, scene_scores:[{scene:0,p1:94}]}, {target_lo:94})'), '')
        # A failed mean alone is not a failed Low criterion.
        self.assertEqual(self.evaluate('vmafLowContextHtml({vmaf:92, scene_scores:[{scene:0,p1:90}]}, {target_lo:94})'), '')
        self.assertEqual(self.evaluate('vmafLowContextHtml({vmaf:96, scene_scores:[{scene:0,p1:80,idle:true}]}, {target_lo:94})'), '')
        self.js.eval('gap = 0;')
        self.assertEqual(self.evaluate('vmafLowContextHtml({vmaf:96, scene_scores:[{scene:0,p1:80}]}, {target_lo:94})'), '')

    def test_lazy_fetch_only_loads_affected_scenes(self):
        self.js.eval('''
          const v = {session:"session", target_lo:94}; state.vmafShown = v;
          const r = {label:"A", vmaf:96, scene_scores:[
            {scene:0,p1:89}, {scene:1,p1:95}, {scene:2,p1:80,idle:true}
          ]};
          const d = details(); loadVmafLowContext(d, r, v);
        ''')
        self.drain()
        self.assertEqual(len(self.evaluate('calls')), 1)
        self.assertIn('scene=0&full=1', self.evaluate('calls[0]'))
        self.assertIn('<strong>Szene 1</strong>', self.evaluate('d.body.innerHTML'))
        self.assertNotIn('<strong>Szene 2</strong>', self.evaluate('d.body.innerHTML'))

    def test_unaffected_result_never_fetches_frame_logs(self):
        self.js.eval('''
          const v = {session:"session", target_lo:94}; state.vmafShown = v;
          const d = details();
          loadVmafLowContext(d, {vmaf:96, scene_scores:[{scene:0,p1:95}]}, v);
        ''')
        self.drain()
        self.assertEqual(self.evaluate('calls'), [])
        self.assertEqual(self.evaluate('d.body.innerHTML'), '')

    def test_full_width_context_rows_keep_result_mapping_and_badge_linebreak(self):
        self.js.eval(r'''
          function syncKeepSourceBanner() {}
          function nerdSharedLine() { return ""; }
          function tt(s) { return s; }
          function vmafResultKey(r) { return r.label; }
          function bitrateReport() { return ""; }
          function vmafCell() { return ""; }
          function vmafRowOff() { return false; }
          function vmafRowColor() { return "blue"; }
          function vmafSpread() { return null; }
          const highlights = [];
          function highlightVmafSeries(key) { highlights.push(key); }
          function tableWrap() {
            const body = {
              innerHTML:"", rows:[],
              querySelectorAll(selector) {
                if (selector !== "[data-vmaf-row]") return [];
                this.rows = Array.from(this.innerHTML.matchAll(/<tr[^>]*data-vmaf-row="(\d+)"/g), (match) => ({
                  dataset:{vmafRow:match[1]}, listeners:{},
                  addEventListener(type, fn) { this.listeners[type] = fn; }
                }));
                return this.rows;
              }
            };
            return {body, querySelector:s => s === "tbody" ? body : {},};
          }
          const wraps = [tableWrap(), tableWrap()];
          const document = {querySelectorAll: () => wraps};
        ''' + self.table)
        self.js.eval('''
          const v = {target_lo:94, results:[
            {label:"A", vmaf:96, scene_scores:[{scene:0,p1:89}], predicted_human:"1 GB", savings_percent:20},
            {label:"B", vmaf:96, scene_scores:[{scene:0,p1:95}], predicted_human:"2 GB", savings_percent:10},
            {label:"C", vmaf:96, scene_scores:[{scene:0,p1:88}], predicted_human:"3 GB", savings_percent:5}
          ]};
          state.vmafShown = v; fillVmafTable(v);
        ''')
        for table in range(2):
            html = self.evaluate(f'wraps[{table}].body.innerHTML')
            self.assertEqual(html.count('colspan="6"'), 2)
            self.assertEqual(html.count('<summary>1%-Low einordnen</summary>'), 2)
            self.assertIn('<tr class="vmaf-context-row" data-vmaf-row="0"><td colspan="6"><details', html)
            self.assertNotIn('data-low-row="1"', html)
            self.assertIn('96.00<br><span class="badge vmaf-miss"', html)
            self.assertEqual(self.evaluate(f'wraps[{table}].body.rows.map(row => row.dataset.vmafRow)'), ['0', '0', '1', '2', '2'])
            self.js.eval(f'wraps[{table}].body.rows[2].listeners.mouseenter();')
            self.assertEqual(self.evaluate('highlights[highlights.length - 1]'), 'B')
            self.js.eval(f'wraps[{table}].body.rows[4].listeners.mouseenter();')
            self.assertEqual(self.evaluate('highlights[highlights.length - 1]'), 'C')
        self.js.eval('v.visual_only = true; fillVmafTable(v);')
        self.assertNotIn('vmaf-context-row', self.evaluate('wraps[0].body.innerHTML'))

    def test_context_escapes_external_values(self):
        html = self.evaluate('vmafLowContextHtml({vmaf:96, scene_scores:[{scene:"<script>", p1:88}]}, {target_lo:94})')
        self.assertNotIn("<script>", html)

    def test_lazy_fetch_is_shared_between_rows(self):
        self.js.eval('''
          const v = {session:"session", target_lo:94}; state.vmafShown = v;
          const a = {label:"A", vmaf:96, scene_scores:[{scene:0, p1:89}]};
          const b = {label:"B", vmaf:96, scene_scores:[{scene:0, p1:89}]};
          const da = details(), db = details();
          loadVmafLowContext(da, a, v); loadVmafLowContext(db, b, v);
        ''')
        self.drain()
        self.assertEqual(len(self.evaluate("calls")), 1)
        self.assertIn("full=1", self.evaluate("calls[0]"))
        self.assertIn("1 von 3 Frames", self.evaluate("da.body.innerHTML"))
        self.assertIn("1 von 3 Frames", self.evaluate("db.body.innerHTML"))

    def test_no_fetch_while_closed_or_without_session(self):
        self.js.eval('''
          const v = {target_lo:94}; state.vmafShown = v;
          const a = {label:"A", vmaf:96, scene_scores:[{scene:0, p1:89}]};
          const da = details(); da.open = false;
          loadVmafLowContext(da, a, v);
        ''')
        self.drain()
        self.assertEqual(self.evaluate("calls"), [])
        self.js.eval('da.open = true; state.vmafSession = ""; loadVmafLowContext(da, a, v);')
        self.drain()
        self.assertEqual(self.evaluate("calls"), [])
        self.assertIn("gespeicherten Vergleich", self.evaluate("da.body.innerHTML"))

    def test_full_logs_without_timing_only_report_frames(self):
        text = " ".join(self.lines(3, [89, 89, 98], seconds=0))
        self.assertIn("längste Folge: 2 Frames.", text)
        self.assertNotIn("ca.", text)

    def test_failed_fetch_can_retry_and_stale_view_is_ignored(self):
        self.js.eval('''
          const v = {session:"session", target_lo:94}; state.vmafShown = v;
          const a = {label:"A", vmaf:96, scene_scores:[{scene:0, p1:89}]};
          const da = details(); fail = true; loadVmafLowContext(da, a, v);
        ''')
        self.drain()
        self.assertIn("Erneutes Öffnen", self.evaluate("da.body.innerHTML"))
        self.js.eval("fail = false; loadVmafLowContext(da, a, v); state.vmafShown = {};")
        before = self.evaluate("da.body.innerHTML")
        self.drain()
        self.assertEqual(len(self.evaluate("calls")), 2)
        self.assertEqual(self.evaluate("da.body.innerHTML"), before)


if __name__ == "__main__":
    unittest.main()
