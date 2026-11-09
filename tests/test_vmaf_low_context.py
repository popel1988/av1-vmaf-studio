"""Low-Kontext: echte Produktionsfunktionen, ohne FFmpeg oder Browser.

Backend-Funktionen werden aus dem AST isoliert, damit die Tests weder die
Anwendung initialisieren noch deren optionale Encoder-Abhängigkeiten benötigen.
Frontend: optionale Entwicklungsabhängigkeit quickjs-ng.
"""
from __future__ import annotations

import ast
import json
import re
import tempfile
from html.parser import HTMLParser
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
        start = cls.source.index("  function vmafLowContextData(")
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

    def i18n_dom(self, language, html=""):
        # Echte Textknoten/Attribute statt kompletter HTML-Zeichenketten prüfen:
        # Die Laufzeit-i18n übersetzt auch Fragmente zwischen <strong>-Tags.
        class TextNodes(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.nodes = []

            def handle_data(self, data):
                if data.strip():
                    self.nodes.append({"text": data})

            def handle_starttag(self, tag, attrs):
                attrs = {k: v for k, v in attrs if k in ("title", "data-tip", "placeholder")}
                if attrs:
                    self.nodes.append({"attrs": attrs})

        parser = TextNodes()
        parser.feed(html)
        js = quickjs.Context()
        js.eval('''
          const window = {};
          let mutationCallback, observation;
          const localStorage = {getItem: () => LANGUAGE};
          function element(attrs = {}) {
            return {nodeType:1,tagName:'DIV',firstChild:null,nextSibling:null,attrs,
              hasAttribute(key) { return Object.prototype.hasOwnProperty.call(this.attrs,key); },
              getAttribute(key) { return this.attrs[key]; },
              setAttribute(key,value) { this.attrs[key] = value; }};
          }
          function nodesFrom(items) {
            const nodes = items.map(item => item.text !== undefined
              ? {nodeType:3,nodeValue:item.text,nextSibling:null} : element(item.attrs));
            nodes.forEach((node,i) => { node.nextSibling = nodes[i+1] || null; });
            return nodes;
          }
          const nodes = nodesFrom(ITEMS);
          const root = element(); root.firstChild = nodes[0] || null;
          const document = {documentElement:element(), body:root,getElementById:() => null};
          class MutationObserver {
            constructor(callback) { mutationCallback = callback; }
            observe(node, config) { observation = config; }
          }
          function values(items) { return items.map(node => node.nodeType === 3 ? node.nodeValue : node.attrs); }
        '''.replace('LANGUAGE', json.dumps(language)).replace('ITEMS', json.dumps(parser.nodes)))
        js.eval((ROOT / 'static' / 'js' / 'i18n.js').read_text(encoding='utf-8'))
        return js, parser.nodes

    def assert_dom_translated(self, language, html):
        js, originals = self.i18n_dom(language, html)
        translated = json.loads(js.eval('JSON.stringify(values(nodes))'))
        for original, result in zip(originals, translated):
            if 'text' in original:
                source = original['text'].strip()
                if re.fullmatch(r'[\d.,\s—%]+', source):
                    continue  # Zahlen ohne Beschriftung sind sprachneutral.
                self.assertNotEqual(source, result.strip(), (language, source))
                # Eine Mutation durch das Übersetzen darf keine weitere auslösen.
                actual = js.eval('window.I18N.t(' + json.dumps(result.strip()) + ')')
                self.assertEqual(actual, result.strip(), (language, result))
            else:
                for key, source in original['attrs'].items():
                    self.assertNotEqual(source, result[key], (language, key, source))
        return js

    def test_low_context_all_text_nodes_and_tooltips_translate_in_all_languages(self):
        self.js.eval('''
          const r = {label:'A',vmaf:96,recommended:true,scene_scores:[
            {scene:0,p1:80,frame_count:337},{scene:1,p1:85,frame_count:337}
          ]};
          const frames = Array.from({length:337}, (_,n) => ({n,vmaf:n < 5 ? 80 : 98}));
          const packs = new Map([0,1].map(scene => [scene,{series:[
            {label:'A',count:337,frames,low_context:{scored_seconds:14}}
          ]}]));
        ''')
        html = self.evaluate('vmafLowContextHtml(r,{target_lo:94},packs)')
        for language in ['en', 'es', 'fr']:
            with self.subTest(language=language):
                self.assert_dom_translated(language, html)
        js, originals = self.i18n_dom('de', html)
        self.assertEqual(json.loads(js.eval('JSON.stringify(values(nodes))')),
                         [item.get('text', item.get('attrs')) for item in originals])

    def test_every_duration_sentence_and_missing_log_message_translates(self):
        # Sämtliche echten Satzvarianten aus der Produktionsfunktion, auch
        # seltene Fälle ohne Zeitangabe und mit abgeschalteter Low-Grenze.
        start = self.source.index('  function vmafLowContextLines(')
        end = self.source.index('  function vmafLowContextScenes(', start)
        sentences = re.findall(r'"([^"\n]+\.)"', self.source[start:end])
        extras = [
            '1%-Low einordnen', 'Was bedeuten die Werte?', 'Frameanzahl unbekannt',
            'Low-Kriterium verfehlt', 'Mittelwertziel verfehlt', 'Zielkriterien verfehlt',
            'Frame-Log nicht verfügbar. Erneutes Öffnen versucht den Abruf nochmals.',
            'Vollständige Frame-Logs sind erst für einen gespeicherten Vergleich verfügbar.',
            'Vollständige Frame-Logs werden gelesen …',
            'Ein verfehltes Low-Kriterium ist kein Nachweis sichtbar schlechter Qualität. '
            '„1%-Low einordnen“ unter der betroffenen Ergebniszeile erklärt Framebasis, Häufigkeit und Dauer.',
        ]
        html = ''.join('<p>' + sentence + '</p>' for sentence in sentences + extras)
        html += self.evaluate('vmafLowContextHtml({vmaf:96,scene_scores:[{scene:0,p1:80}]},{target_lo:94})')
        for language in ['en', 'es', 'fr']:
            with self.subTest(language=language):
                self.assert_dom_translated(language, html)

    def test_dynamic_low_translations_handle_numbers_and_all_floor_anchors(self):
        for language in ['en', 'es', 'fr']:
            js, _ = self.i18n_dom(language)
            def translate(text):
                return js.eval('window.I18N.t(' + json.dumps(text) + ')')
            with self.subTest(language=language):
                for p in [1, 5, 10]:
                    for value in ['84,53', '84.53', '—']:
                        source = f'{p}%-Low: {value}'
                        self.assertNotEqual(translate(source), source)
                self.assertIn('84.53' if language == 'en' else '84,53', translate('1%-Low: 84,53'))
                for length in ['1 Frame.', '5 Frames (ca. 0,21 s).', '62 Frames (ca. 2.58 s).']:
                    source = 'Unter Grenze 89,55: 5 von 337 Frames (1,5 %) · längster Abschnitt: ' + length
                    self.assertNotEqual(translate(source), source)
                for body in ['Low-Kriterium: 1%-Low 84.53 unter 89.55',
                             'Szene 1, Low-Kriterium: 1%-Low 84.53 unter 89.55',
                             'Mittel 92.0 unter Ziel 94, Szene 1, Low-Kriterium: 1%-Low 84.53 unter 89.55']:
                    self.assertNotIn('Kriterium', translate(body))
                    result = translate('AV1: Test: ' + body + '.')
                    self.assertTrue(result.startswith('AV1: Test'))
                    self.assertNotIn('Kriterium', result)
                    self.assertNotIn('unter', result)
                    self.assertNotIn('Szene', result)
                for condition in ['nur Mittel', 'schwächste Szene ≥ Filmschnitt−8',
                                  'schwächste Szene ≥ 85', 'schwächste Szene ≥ 85 und ≥ Filmschnitt−8']:
                    source = 'Zielkriterien nicht vollständig erfüllt (Ziel 93, ' + condition + ').'
                    result = translate(source)
                    self.assertNotEqual(result, source)
                    self.assertNotIn('schwächste', result)
                    self.assertNotIn('Filmschnitt', result)
                    self.assertNotIn(' und ', result)
                for rule in ['Gesamtmittel − Abstand', 'Ziel − Abstand',
                             'höhere Grenze aus Ziel − Abstand und Gesamtmittel − Abstand']:
                    for prefix in ['', 'Berechnung der Low-Grenze: ']:
                        for suffix in ['', ' Hier hebt das Gesamtmittel die Grenze über Ziel − Abstand an.']:
                            source = prefix + rule + '; Abstand 8.' + suffix
                            self.assertNotEqual(translate(source), source)

    def test_low_numbers_follow_active_language_without_changing_values(self):
        for language in ['de', 'en', 'es', 'fr']:
            with self.subTest(language=language):
                self.js.eval('var window = {I18N:{lang:' + json.dumps(language) + '}};')
                self.assertEqual(self.evaluate('vmafLowNumber(89.55)'), '89.55' if language == 'en' else '89,55')
                self.assertEqual(self.evaluate('vmafLowNumber(23.7,1)'), '23.7' if language == 'en' else '23,7')

    def s2_context(self, language='de'):
        js, _ = self.i18n_dom(language)
        start = self.source.index('  function ssimulacra2Mark(')
        end = self.source.index('  // Nur Einordnung: 5%/10%', start)
        number_start = self.source.index('  function vmafLowNumber(')
        number_end = self.source.index('  function vmafLowContextLines(', number_start)
        js.eval(r'''
          function tt(s) { return window.I18N ? window.I18N.t(s) : s; }
          const escapeHtml = (s) => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
        ''' + self.source[start:end] + self.source[number_start:number_end])
        return js

    def test_ssimulacra2_tooltip_colors_each_value_independently(self):
        js = self.s2_context()
        for value, color in [(0, 'bad'), (34, 'bad'), (49.99, 'bad'),
                             (50, 'warn'), (69.99, 'warn'), (70, ''),
                             (84.99, ''), (85, 'good'), (100, 'good')]:
            with self.subTest(value=value):
                html = js.eval(f'ssimulacra2ValuePill("Bild 1", {value})')
                self.assertIn(f'class="metric-pill s2-tip-value {color}"', html)
        html = js.eval('''ssimulacra2TooltipHtml({ssimulacra2:88,ssimulacra2_min:40,scene_scores:[
          {scene:0,ssimulacra2:86,ssimulacra2_min:40,ssimulacra2_scores:[92,65,40]}
        ]})''')
        self.assertIn('good">Szenenmittel: 86,0', html)
        self.assertIn('good">Bild 1: 92,0', html)
        self.assertIn('warn">Bild 2: 65,0', html)
        self.assertIn('bad">Bild 3: 40,0', html)
        # Die bestehende Warnlogik der äußeren Pille wird nicht geändert.
        self.assertEqual(js.eval('ssimulacra2Mark(88,40).cls'), 'warn')

    def test_ssimulacra2_tooltip_missing_values_and_archive_minimum(self):
        js = self.s2_context()
        for value in ['null', 'NaN', 'Infinity', '"invalid"']:
            with self.subTest(value=value):
                html = js.eval(f'ssimulacra2ValuePill("Bild 1", {value})')
                self.assertIn('—', html)
                self.assertIn('Nicht gemessen', html)
                self.assertNotIn('s2-tip-value bad', html)
        html = js.eval('''ssimulacra2TooltipHtml({ssimulacra2:80,scene_scores:[
          {scene:0,ssimulacra2:80,ssimulacra2_min:45},
          {scene:1,ssimulacra2_scores:[80,null,40]}, {scene:2}
        ]})''')
        self.assertIn('bad">Schwächstes Bild: 45,0', html)
        self.assertIn('Szene 2', html)
        self.assertNotIn('Szene 3', html)
        self.assertIn('Bild 2: —', html)
        self.assertEqual(js.eval('ssimulacra2Pill({})'), '')

    def test_ssimulacra2_tooltip_has_focusable_trigger_and_safe_inert_template(self):
        js = self.s2_context()
        html = json.loads(js.eval('''JSON.stringify(ssimulacra2Pill({ssimulacra2:88,scene_scores:[
          {scene:'<img src=x onerror=alert(1)>',ssimulacra2:80,
           ssimulacra2_scores:[42,'</template><script>alert(1)</script>']}
        ]}))'''))
        self.assertIn('tabindex="0"', html)
        self.assertIn('data-tip-rich="ssimulacra2"', html)
        self.assertIn('<template class="ui-tip-content">', html)
        self.assertNotIn('title=', html)  # Kein zweiter nativer Tooltip.
        self.assertNotIn('<script', html)
        self.assertNotIn('<img', html)
        self.assertNotIn('onerror=', html)
        self.assertEqual(html.count('</template>'), 1)
        escaped = js.eval('ssimulacra2ValuePill("<img src=x>",42)')
        self.assertIn('&lt;img src=x&gt;', escaped)

    def test_ssimulacra2_tooltip_translates_all_content_in_four_languages(self):
        expected = {
          'de':['Gesamtmittel','Szenenmittel','Schwächstes Bild','Szene 1','Bild 1',
                'sichtbare Verluste','deutlich schlechter','Nicht gemessen','86,0'],
          'en':['Overall mean','Scene mean','Weakest image','Scene 1','Image 1',
                'visible differences','noticeably worse','Not measured','86.0'],
          'es':['Media global','Media de la escena','Imagen más débil','Escena 1','Imagen 1',
                'diferencias visibles','claramente peor','Sin medir','86,0'],
          'fr':['Moyenne globale','Moyenne de la scène','Image la plus faible','Scène 1','Image 1',
                'différences visibles','nettement moins bon','Non mesuré','86,0'],
        }
        for language, words in expected.items():
            with self.subTest(language=language):
                js = self.s2_context(language)
                html = json.loads(js.eval('''JSON.stringify(ssimulacra2Pill({ssimulacra2:88,ssimulacra2_min:40,scene_scores:[
                  {scene:0,ssimulacra2:86,ssimulacra2_scores:[92,65,40,null]},
                  {scene:1,ssimulacra2:88,ssimulacra2_min:40}
                ]}))'''))
                for word in words:
                    self.assertIn(word, html)
                if language != 'de':
                    for word in ['Szenen und Einzelbilder','Gesamtmittel','Szenenmittel',
                                 'Schwächstes Bild','Szene ','Bild ','Nicht gemessen',
                                 'kaum unterscheidbar','sehr gut','deutlich schlechter',
                                 'Standbild-Vergleich:','Nur zur Orientierung;','Einbruch im Standbild']:
                        self.assertNotIn(word, html)

    def tooltip_context(self):
        js = quickjs.Context()
        js.eval(r'''
          const timers = new Map(); let timerId = 0;
          function setTimeout(fn) { const id = ++timerId; timers.set(id,fn); return id; }
          function clearTimeout(id) { timers.delete(id); }
          function fireTimers() { for (const [id,fn] of [...timers]) { if(timers.delete(id)) fn(); } }
          class El {
            constructor(attrs={}) {
              this.attrs = attrs; this.children = []; this.listeners = {};
              this.isConnected = true; this.hidden = false; this.style = {};
              this.offsetWidth = 240; this.offsetHeight = 100;
              const classes = new Set();
              this.classList = {add:n => classes.add(n), remove:n => classes.delete(n), contains:n => classes.has(n)};
              this.template = null; this._text = '';
            }
            set textContent(value) { this._text = value; this.children = []; }
            get textContent() { return this._text; }
            set innerHTML(value) { throw new Error('HTML from attributes must not be interpreted'); }
            setAttribute(k,v) { this.attrs[k] = v; }
            getAttribute(k) { return this.attrs[k] || null; }
            removeAttribute(k) { delete this.attrs[k]; }
            appendChild(node) { this.children.push(node); return node; }
            contains(node) { return this === node || this.children.some(child => child === node || (child.contains && child.contains(node))); }
            querySelector() { return this.template; }
            closest() { return this.attrs['data-tip'] ? this : null; }
            getBoundingClientRect() { return {left:50,top:30,bottom:50,width:200}; }
            addEventListener(type,fn) { (this.listeners[type] ||= []).push(fn); }
            emit(type,event={}) { (this.listeners[type] || []).forEach(fn => fn(event)); }
          }
          const document = new El();
          document.body = new El();
          document.getElementById = () => null;
          document.createElement = () => new El();
          const window = new El(); window.innerWidth = 1024; window.innerHeight = 768;
          const tt = s => s;
          const plain = new El({'data-tip':'<b>plain text</b>'});
          const rich = new El({'data-tip':'SSIMULACRA2', 'data-tip-rich':'ssimulacra2','aria-describedby':'existing-help'});
          let clones = 0;
          rich.template = {content:{cloneNode(deep) { clones++; return new El({'cloned':deep}); }}};
        ''')
        start = self.source.index('  function initTooltips(')
        end = self.source.index('  async function fetchPreview(', start)
        js.eval(self.source[start:end] + '\ninitTooltips(); const tip = document.body.children[0];')
        return js

    def test_rich_tooltip_keyboard_focus_aria_escape_and_plain_fallback(self):
        js = self.tooltip_context()
        js.eval("document.emit('focusin',{target:rich});")
        self.assertFalse(js.eval('tip.hidden'))
        self.assertTrue(js.eval("tip.classList.contains('is-rich')"))
        self.assertEqual(js.eval('clones'), 1)
        self.assertEqual(js.eval('tip.children.length'), 1)
        self.assertEqual(js.eval("rich.getAttribute('aria-describedby')"), 'existing-help ui-tip')
        js.eval("document.emit('keydown',{key:'Escape'});")
        self.assertTrue(js.eval('tip.hidden'))
        self.assertEqual(js.eval("rich.getAttribute('aria-describedby')"), 'existing-help')
        js.eval("document.emit('focusin',{target:rich}); document.emit('focusin',{target:plain});")
        self.assertEqual(js.eval('tip.textContent'), '<b>plain text</b>')
        self.assertFalse(js.eval("tip.classList.contains('is-rich')"))
        self.assertEqual(js.eval('tip.children.length'), 0)
        self.assertEqual(js.eval("rich.getAttribute('aria-describedby')"), 'existing-help')
        js.eval("document.emit('focusout');")
        self.assertTrue(js.eval('tip.hidden'))
        self.assertIsNone(js.eval("plain.getAttribute('aria-describedby')"))

    def test_rich_tooltip_can_be_hovered_and_scrolled_without_closing(self):
        js = self.tooltip_context()
        js.eval("document.emit('pointerover',{target:rich}); fireTimers();")
        self.assertFalse(js.eval('tip.hidden'))
        js.eval("document.emit('pointerout',{target:rich,relatedTarget:tip});")
        self.assertFalse(js.eval('tip.hidden'))
        js.eval("window.emit('scroll',{target:tip});")
        self.assertFalse(js.eval('tip.hidden'))
        js.eval("document.emit('pointerout',{target:rich,relatedTarget:null}); document.emit('pointerover',{target:rich}); fireTimers();")
        self.assertFalse(js.eval('tip.hidden'))
        js.eval("document.emit('pointerout',{target:rich,relatedTarget:null}); tip.emit('pointerenter'); fireTimers();")
        self.assertFalse(js.eval('tip.hidden'))
        js.eval("tip.emit('pointerleave',{relatedTarget:null});")
        self.assertTrue(js.eval('tip.hidden'))
        js.eval("document.emit('focusin',{target:rich}); window.emit('scroll',{target:document});")
        self.assertTrue(js.eval('tip.hidden'))

    def test_tooltip_does_not_open_after_leaving_or_removing_trigger(self):
        js = self.tooltip_context()
        js.eval("document.emit('pointerover',{target:rich}); document.emit('pointerout',{target:rich}); fireTimers();")
        self.assertTrue(js.eval('tip.hidden'))
        js.eval("document.emit('pointerover',{target:rich}); rich.isConnected = false; fireTimers();")
        self.assertTrue(js.eval('tip.hidden'))
        self.assertEqual(js.eval('clones'), 0)
        js.eval("rich.isConnected = true; rich.attrs['data-tip-rich'] = 'untrusted'; document.emit('focusin',{target:rich});")
        self.assertEqual(js.eval('tip.textContent'), 'SSIMULACRA2')
        self.assertEqual(js.eval('clones'), 0)

    def test_runtime_observer_translates_late_low_context_and_attribute_updates(self):
        for language in ['en', 'es', 'fr']:
            with self.subTest(language=language):
                js, _ = self.i18n_dom(language)
                js.eval('''
                  const late = element({title:'Ziel − Abstand; Abstand 8.'});
                  const lateText = {nodeType:3,nodeValue:'  3 von 337 Frames  ',nextSibling:null};
                  late.firstChild = lateText;
                  mutationCallback([{type:'childList',addedNodes:[late]}]);
                ''')
                self.assertNotIn(' von ', js.eval('lateText.nodeValue'))
                self.assertTrue(js.eval('lateText.nodeValue.startsWith("  ") && lateText.nodeValue.endsWith("  ")'))
                self.assertNotIn('Abstand', js.eval('late.attrs.title'))
                js.eval('''
                  lateText.nodeValue = 'Was bedeuten die Werte?';
                  late.attrs.title = 'Frameanzahl unbekannt';
                  mutationCallback([{type:'characterData',target:lateText}, {type:'attributes',target:late}]);
                ''')
                self.assertNotEqual(js.eval('lateText.nodeValue'), 'Was bedeuten die Werte?')
                self.assertNotEqual(js.eval('late.attrs.title'), 'Frameanzahl unbekannt')
                self.assertEqual(json.loads(js.eval('JSON.stringify(observation.attributeFilter)')),
                                 ['placeholder', 'title', 'data-tip', 'aria-label'])

    def test_small_and_large_low_bases(self):
        for n, counts in [(1, [1, 1, 1]), (9, [1, 1, 1]), (20, [1, 1, 2]),
                          (99, [1, 4, 9]), (100, [1, 5, 10]),
                          (337, [3, 16, 33]), (1500, [15, 75, 150])]:
            with self.subTest(n=n):
                data = self.evaluate(f'vmafLowContextData({{p1:88.5, frame_count:{n}}}, null, 90)')
                self.assertEqual([low['count'] for low in data['lows']], counts)
                self.assertEqual([low['value'] for low in data['lows']], [88.5, None, None])
                self.assertFalse(data['complete'])

    def test_low_averages_are_means_of_worst_frames_not_percentiles(self):
        scores = [float(i) for i in range(337)]
        frames = [{'n': i, 'vmaf': v} for i, v in enumerate(reversed(scores))]
        data = self.evaluate(f'vmafLowContextData({{}}, {{count:337,frames:{json.dumps(frames)}}}, 90)')
        for low, n in zip(data['lows'], [3, 16, 33]):
            self.assertAlmostEqual(low['value'], sum(scores[:n]) / n)
        self.assertEqual([low['value'] for low in data['lows']], [1, 7.5, 16])

    def test_low_averages_use_raw_precision_and_keep_stored_p1(self):
        frames = [{'n': i, 'vmaf': 90, 'vmaf_raw': 89.9999} for i in range(40)]
        data = self.evaluate(f'vmafLowContextData({{p1:88.1234}}, {{count:40,frames:{json.dumps(frames)}}}, 90)')
        self.assertEqual(data['lows'][0]['value'], 88.1234)
        self.assertAlmostEqual(data['lows'][1]['value'], 89.9999)
        self.assertAlmostEqual(data['lows'][2]['value'], 89.9999)
        self.assertEqual(data['under'], 40)

    def test_low_averages_never_use_incomplete_or_invalid_logs(self):
        for frames in [[{'vmaf': 80}], [{'vmaf': 80}, {'vmaf': None}],
                       [{'vmaf': 80}, {'vmaf': 90, 'vmaf_raw': 'invalid'}]]:
            with self.subTest(frames=frames):
                data = self.evaluate(f'vmafLowContextData({{p1:80}}, {{count:2,frames:{json.dumps(frames)}}}, 90)')
                self.assertFalse(data['complete'])
                self.assertEqual([low['value'] for low in data['lows']], [80, None, None])
                self.assertIsNone(data['longestSeconds'])

    def test_compact_metrics_explain_all_three_lows_once(self):
        frames = [{'n': i, 'vmaf': 80 if i < 5 else 98} for i in range(337)]
        self.js.eval('''
          const r = {label:"A",vmaf:96,scene_scores:[{scene:0,p1:80,frame_count:337}]};
        ''')
        html = self.evaluate('vmafLowContextHtml(r, {target_lo:94}, new Map([[0,{series:['
                             + json.dumps({'label': 'A', 'count': 337, 'frames': frames,
                                           'low_context': {'scored_seconds': 14}}) + ']}]]))')
        self.assertIn('1%-Low: 80,00', html)
        self.assertIn('5%-Low: 92,38', html)
        self.assertIn('10%-Low: 95,27', html)
        for n in [3, 16, 33]:
            self.assertIn(f'{n} von 337 Frames', html)
        self.assertEqual(html.count('<summary>Was bedeuten die Werte?</summary>'), 1)
        self.assertIn('keine Perzentil-Grenzwerte', html)
        self.assertIn('Gruppen überlappen', html)
        self.assertIn('kein Empfehlungskriterium', html)
        self.assertIn('0,21 s', html)
        self.assertNotIn('Extremframes', html)
        self.assertNotIn('Hohe Originalbitrate', html)

    def test_new_low_values_never_change_recommendation(self):
        self.js.eval('''
          const r = {label:"A",vmaf:96,scene_scores:[{scene:0,p1:88}]};
          const before = vmafResultMiss(r,94,6);
          const frames = Array.from({length:100}, (_,n) => ({n,vmaf:n < 3 ? 88 : 98}));
          const html = vmafLowContextHtml(r,{target_lo:94},new Map([[0,{series:[{label:"A",count:100,frames}]}]]));
        ''')
        self.assertEqual(self.evaluate('vmafResultMiss(r,94,6)'), self.evaluate('before'))
        self.assertEqual(self.evaluate('Object.keys(r.scene_scores[0])'), ['scene', 'p1'])

    def test_isolated_and_contiguous_dips(self):
        isolated = " ".join(self.lines(5, [98, 89, 98, 89, 98], seconds=2))
        self.assertIn("2 von 5 Frames (40,0 %)", isolated)
        self.assertIn("längster Abschnitt: 1 Frame (ca. 0,40 s)", isolated)
        self.assertIn("keine direkt aufeinanderfolgenden", isolated)
        contiguous = " ".join(self.lines(5, [98, 89, 89, 89, 98], seconds=2))
        self.assertIn("längster Abschnitt: 3 Frames (ca. 1,20 s)", contiguous)
        self.assertIn("mindestens eine Sekunde", contiguous)

    def test_different_section_lengths_get_factual_explanations(self):
        values = [98] + [80] * 5 + [98] * 94
        for seconds, expected in [(4, 'sehr kurzer Abschnitt'), (5, 'kurzer Abschnitt'),
                                  (10, 'kurzer Abschnitt'), (20, 'mindestens eine Sekunde'),
                                  (59, 'mindestens eine Sekunde'), (60, 'mindestens drei Sekunden')]:
            with self.subTest(seconds=seconds):
                text = ' '.join(self.lines(100, values, seconds=seconds))
                self.assertIn(expected, text)
                self.assertNotIn('unsichtbar', text)
                self.assertNotIn('unbedenklich', text)
        self.assertIn('Ein einzelnes Bild', ' '.join(self.lines(3, [98, 80, 98])))
        self.assertIn('Ein einzelnes Bild', ' '.join(self.lines(1, [80])))
        self.assertIn('gesamte bewertete Szene', ' '.join(self.lines(3, [80, 80, 80])))

    def test_several_dip_runs(self):
        values = [80, 80, 98, 80, 80] + [98] * 95
        for seconds, expected in [(10, 'Mehrere sehr kurze Abschnitte'),
                                  (20, 'Mehrere kurze Abschnitte'),
                                  (60, 'mindestens einer dauert eine Sekunde'),
                                  (150, 'längste dauert mindestens drei Sekunden')]:
            with self.subTest(seconds=seconds):
                self.assertIn(expected, ' '.join(self.lines(100, values, seconds=seconds)))
        self.assertIn('Dauer ist mangels Zeitangabe unbekannt',
                      ' '.join(self.lines(100, values, seconds=0)))

    def test_boundary_is_strict_and_uses_unrounded_values(self):
        text = " ".join(self.lines(3, [90, 90, 91], p1=90))
        self.assertIn("Kein bewerteter Frame", text)
        series = {"count": 2, "frames": [{"n": 0, "vmaf": 90, "vmaf_raw": 89.9999}, {"n": 1, "vmaf": 91}]}
        lines = self.evaluate(f"vmafLowContextLines({{p1: 89.9999}}, {json.dumps(series)}, 90)")
        self.assertIn("1 von 2 Frames", " ".join(lines))

    def test_passing_low_is_not_a_minimum(self):
        text = " ".join(self.lines(4, [89, 91, 98, 98], p1=90))
        self.assertIn("1 von 4 Frames", text)
        self.assertIn("Ein einzelnes Bild", text)

    def test_disabled_floor_and_idle_scenes(self):
        text = " ".join(self.lines(3, [80, 80, 80], floor=None))
        self.assertIn("Low-Grenze ausgeschaltet", text)
        self.assertNotIn("unter dieser Grenze", text)
        text = " ".join(self.lines(3, [80, 80, 80], idle=True))
        self.assertIn("zählt nicht", text)
        self.assertNotIn("Low-Kriterium nicht erfüllt", text)

    def test_missing_or_downsampled_logs_never_supply_duration(self):
        text = " ".join(self.lines(337, [80, 81]))
        self.assertIn("vollständige Frame-Log", text)
        self.assertNotIn("längster Abschnitt", text)
        data = self.evaluate('vmafLowContextData({}, null, 90)')
        self.assertEqual(data['count'], 0)
        self.assertEqual([low['count'] for low in data['lows']], [0, 0, 0])
        self.assertEqual([low['value'] for low in data['lows']], [None, None, None])

    def test_missing_indices_break_a_run(self):
        series = {"count": 2, "frames": [{"n": 1, "vmaf": 80}, {"n": 3, "vmaf": 80}]}
        lines = self.evaluate(f"vmafLowContextLines({{p1:80}}, {json.dumps(series)}, 90)")
        self.assertIn("längster Abschnitt: 1 Frame", " ".join(lines))

    def test_floor_anchors_unchanged_and_explained(self):
        for name, expected in [("target", 88), ("mean", 90.78), ("both", 90.78)]:
            with self.subTest(anchor=name):
                value = self.evaluate(f'vmafSceneFloor({{vmaf:96.78}}, 94, 6, "{name}")')
                self.assertAlmostEqual(value, expected)
        html = self.evaluate('vmafLowContextHtml({vmaf:96.78, recommended:true, scene_scores:[{scene:0, p1:88.54, frame_count:337}]}, {target_lo:94})')
        self.assertIn("90,78", html)
        self.assertIn("hebt das Gesamtmittel", html)
        self.assertIn("Als Kompromiss empfohlen", html)
        self.assertIn("3 von 337 Frames", html)
        self.assertIn('title="höhere Grenze', html)
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
        self.assertEqual(html.count('Low-Grenze:'), 2)  # Kurzinfo + aufklappbare Herleitung.
        self.assertEqual(html.count('Nur zur Einordnung der getesteten Szenen.'), 1)
        self.assertEqual(html.count('<summary>Was bedeuten die Werte?</summary>'), 1)

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
        self.assertIn("längster Abschnitt: 2 Frames.", text)
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
