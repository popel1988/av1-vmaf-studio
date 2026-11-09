"""Pure frontend project and explicit source-context regression checks."""
import json
from pathlib import Path
import unittest

try:
    import quickjs
except ImportError:
    quickjs = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipIf(quickjs is None, "Install quickjs-ng for frontend tests")
class ProjectFormatTests(unittest.TestCase):
    def setUp(self):
        source = (ROOT / "static/js/editor.js").read_text(encoding="utf-8")
        start = source.index("  const PROJECT_FIELDS =")
        end = source.index("  function applyProjectSettings(", start)
        self.js = quickjs.Context()
        self.js.eval("""
          const controls = {'ed-cq':{value:'19'}, 'ed-bgm-path':{value:'music.flac'},
            'ed-bgm-duck':{type:'checkbox',checked:false}};
          const $ = id => controls[id];
          const ed = {mediaManifest:[], src:{path:'movie.mkv', name:'Movie'},
            segments:[{id:'c1',kind:'media',path:'movie.mkv',start:0,end:4}],
            overlays:[], trackSel:{'movie.mkv':[0]}, subSel:{},
            bgmPath:'music.flac',inSec:0,outSec:4,playhead:2,tlZoom:2};
        """ + source[start:end])

    def evaluate(self, expression):
        return json.loads(self.js.eval("JSON.stringify(" + expression + ")"))

    def test_v2_remains_loadable(self):
        self.assertEqual(self.evaluate("validateProject({v:2,segments:[{kind:'black',start:0,end:2}]})")["v"], 2)

    def test_complete_project_preserves_settings_music_and_tracks(self):
        data = self.evaluate("projectData()")
        self.assertEqual(data["v"], 3)
        self.assertEqual(data["settings"]["ed-cq"], "19")
        self.assertFalse(data["bgm"]["ducking"])
        self.assertEqual(data["track_selection"]["movie.mkv"], [0])
        self.assertEqual({m["path"] for m in data["media_manifest"]}, {"movie.mkv", "music.flac"})
        self.assertEqual(self.evaluate("validateProject(projectData())")["view"]["seek"], 2)

    def test_bad_projects_cannot_modify_live_state(self):
        for value in (
            {"v": 4, "segments": []},
            {"v": 3, "segments": [{"kind": "black", "start": 3, "end": 2}]},
            {"v": 3, "segments": [{"kind": "media", "start": 0, "end": 2}]},
            {"v": 3, "segments": [], "settings": {"unknown": "value"}},
            {"v": 3, "segments": [], "src": {"path": "bad\npath"}},
            {"v": 3, "segments": [{"id": "x", "kind": "black", "start": 0, "end": 2},
                                   {"id": "x", "kind": "black", "start": 0, "end": 2}]},
        ):
            with self.subTest(value=value):
                with self.assertRaises(Exception):
                    self.js.eval("validateProject(" + json.dumps(value) + ")")
        self.assertEqual(self.evaluate("ed.segments")[0]["path"], "movie.mkv")


@unittest.skipIf(quickjs is None, "Install quickjs-ng for frontend tests")
class SourceContextTests(unittest.TestCase):
    def test_explicit_context_preserves_per_source_analysis(self):
        source = (ROOT / "static/js/app.js").read_text(encoding="utf-8")
        start = source.index("  const sourceEntries =")
        end = source.index("  function renderSourceContext(", start)
        js = quickjs.Context()
        js.eval("const window={}; let renders=0; function renderSourceContext(){renders++;}" + source[start:end])
        js.eval("""
          window.StudioSourceContext.publish({path:'first.mkv'},'encode',{duration:90});
          window.StudioSourceContext.publish({path:'second.mkv'},'remux',{duration:120});
          window.StudioSourceContext.publish({path:'first.mkv'},'editor');
        """)
        self.assertEqual(js.eval("window.StudioSourceContext.get().analyses.encode.duration"), 90)
        self.assertEqual(js.eval("sourceEntries.size"), 2)
        js.eval("window.StudioSourceContext.publish({path:'upload:temporary.mkv'},'editor')")
        self.assertEqual(js.eval("window.StudioSourceContext.get().path"), "first.mkv")


if __name__ == "__main__":
    unittest.main()
