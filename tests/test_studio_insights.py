"""Frontend contracts for factual process, queue, audit and archive displays."""
import json
from pathlib import Path
import unittest

try:
    import quickjs
except ImportError:
    quickjs = None

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(quickjs, "optional quickjs is not installed")
class StudioInsightsTests(unittest.TestCase):
    def setUp(self):
        self.js = quickjs.Context()
        self.js.eval('''
          globalThis.window = globalThis;
          globalThis.nodes = {};
          globalThis.document = {documentElement:{lang:'de'},activeElement:null,
            addEventListener(){},getElementById(id){return nodes[id] || null;}};
          globalThis.node = () => ({innerHTML:'',hidden:false,contains(){return false;},querySelectorAll(){return [];},addEventListener(){}});
          globalThis.alert = () => {};
        ''')
        self.js.eval((ROOT / 'static/js/insights.js').read_text(encoding='utf-8'))

    def result(self, expr):
        return json.loads(self.js.eval('JSON.stringify(' + expr + ')'))

    def test_complete_modified_scripts_parse(self):
        for name in ('app', 'player', 'editor', 'insights'):
            source = (ROOT / f'static/js/{name}.js').read_text(encoding='utf-8')
            self.js.eval('new Function(' + json.dumps(source) + ')')

    def test_all_ui_dictionary_entries_cover_all_four_languages(self):
        for group in ('TEXT', 'PHASES', 'CHECK_TEXT', 'DETAIL_TEXT'):
            entries = self.result('StudioInsights.' + group)
            for key, texts in entries.items():
                with self.subTest(group=group, key=key):
                    self.assertEqual(len(texts), 4)
                    self.assertTrue(all(isinstance(t, str) and t.strip() for t in texts))

    def test_actual_command_options_are_displayed_without_claiming_defaults(self):
        data = {'settings':{'quality':28,'codec':'av1','platform':'nvidia','rate_mode':'cq'},
                'details':{'command':{'encoder':'av1_nvenc','preset':'p5','backend':'nvidia',
                    'rate_control':{'cq':'26','crf':None},'filters':'scale=1280:-2',
                    'options':{'-spatial-aq':['1'],'-bf':['4']}}}}
        html = self.result('StudioInsights.queueDetailsHtml(' + json.dumps(data) + ')')
        for expected in ('av1_nvenc', 'cq=26', 'CQ/CRF 28', '-spatial-aq 1', '-bf 4', 'scale=1280:-2'):
            self.assertIn(expected, html)
        self.assertNotIn('crf=null', html)

    def test_report_translates_metadata_and_unknown_checks_in_four_languages(self):
        report = {'checks':[{'key':'video.codec','status':'ok','expected':'av1','actual':'av1','detail':'metadata_only'},
                            {'key':'decode.full','status':'not_checked','expected':None,'actual':None,'detail':'not_performed'},
                            {'key':'audio.0.language','status':'warn','expected':'deu','actual':'eng'}]}
        for lang, unknown in (('de','Nicht geprüft'),('en','Not checked'),('es','Sin comprobar'),('fr','Non vérifié')):
            self.js.eval('document.documentElement.lang=' + json.dumps(lang))
            html = self.result('StudioInsights.reportHtml(' + json.dumps(report) + ')')
            self.assertIn(unknown, html)
            for technical in ('video.codec', 'decode.full', 'audio.0.language', 'metadata_only', 'not_performed'):
                self.assertNotIn(technical, html)

    def test_external_data_is_escaped_in_all_html_renderers(self):
        payload = '<img src=x onerror="oops()">'
        for fn, arg in (('queueDetailsHtml',{'settings':{'quality':28},'output_path':payload}),
                        ('previewHtml',{'mode':'hls','encoder':payload}),
                        ('reportHtml',{'checks':[{'key':'video.codec','status':'warn','actual':payload}]})):
            html = self.result('StudioInsights.' + fn + '(' + json.dumps(arg) + ')')
            self.assertNotIn('<img', html)
            self.assertIn('&lt;img', html)

    def test_runtime_uses_registered_metrics_and_does_not_duplicate_fake_zero(self):
        self.js.eval("nodes['runtime-list']=node()")
        self.js.eval('StudioInsights.renderRuntime({sessions:[{id:"s",owner:"editor",mode:"hls",pid:7,cpu_percent:12.5,rss_bytes:1048576,can_stop:true,effective:{tonemap:{active:false},encoder:"libx264"}}]})')
        html = self.result("nodes['runtime-list'].innerHTML")
        self.assertIn('12.5%', html)
        self.assertIn('1.0 MiB', html)
        self.assertNotIn('HDR → SDR', html)
        self.js.eval('StudioInsights.renderRuntime({sessions:[{id:"s",mode:"direct",can_stop:false,cpu_percent:null,rss_bytes:null}]})')
        html = self.result("nodes['runtime-list'].innerHTML")
        self.assertIn('CPU —', html)
        self.assertIn('RAM —', html)
        self.assertNotIn('data-runtime-stop', html)

    def test_coverage_uses_one_result_not_sum_of_encoder_runs(self):
        self.js.eval("nodes['analysis-insights']=node(); nodes['analysis-protected']=node()")
        a = {'session':'s','results':[{'recommended':True},{'recommended':False}]}
        data = {'session':'s','analysis':a,'source_available':False,'coverage':{'results':[
            {'result_index':0,'scored_seconds':10,'frame_count':240,'film_fraction':0.01,'scenes':[{}]},
            {'result_index':1,'scored_seconds':10,'frame_count':240,'film_fraction':0.01,'scenes':[{}]}]}}
        self.js.eval('StudioInsights.analysis(' + json.dumps(a) + ',' + json.dumps(data) + ')')
        while self.js.execute_pending_job():
            pass
        html = self.result("nodes['analysis-insights'].innerHTML")
        self.assertIn('240 bewertete Frames', html)
        self.assertIn('1.00 %', html)
        self.assertNotIn('480 bewertete Frames', html)
        self.assertNotIn('extra-scene-form', html)

    def test_storage_missing_free_space_is_not_zero_bytes(self):
        html = self.result('StudioInsights.storageHtml({filesystems:[{path:"x",free_bytes:null,required_bytes:100,status:"unknown"}]})')
        self.assertIn('Frei: —', html)
        self.assertIn('nicht vollständig ermittelbar', html)

    def test_old_jobs_without_audit_remain_unknown(self):
        for report in ('null', '{}'):
            html = self.result('StudioInsights.reportHtml(' + report + ')')
            self.assertIn('kein gespeicherter Prüfbericht', html)


if __name__ == '__main__':
    unittest.main()
