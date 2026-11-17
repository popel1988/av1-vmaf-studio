"""Exercise the media browser's public render contract and its delegated UI events."""
from html.parser import HTMLParser
import json
from pathlib import Path
import unittest

try:
    import quickjs
except ImportError:
    quickjs = None

SCRIPT = Path(__file__).resolve().parents[1] / "static/js/library_media.js"


class Controls(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.items = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "data-lm" in attrs:
            self.items.append(attrs)


def row(path, title="Transit", **meta):
    return {"path": path, "name": path.split("/")[-1], "resolution": "1920×1080",
            "hdr_type": "HDR10", "is_hdr": True,
            "media": {"title": title, "kind": "film", "source": "suggestion", **meta}}


@unittest.skipUnless(quickjs, "Install quickjs-ng for frontend tests")
class MediaBrowserTests(unittest.TestCase):
    def setUp(self):
        self.js = quickjs.Context()
        self.js.eval('''
          globalThis.window = globalThis;
          const handlers = {}, requests = [], events = [];
          const mount = {innerHTML:'', contains(n){return !!n;}, querySelectorAll(){return [];},
            addEventListener(k,f){handlers[k]=f;}, dispatchEvent(e){events.push(e.type);}};
          globalThis.document = {activeElement:null,getElementById(id){return id==='lib-media-browser'?mount:null;},
            dispatchEvent(e){events.push(e.type);}};
          globalThis.CustomEvent = class {constructor(type){this.type=type;}};
          globalThis.confirm = () => true;
          globalThis.fetch = async (url, opts={}) => {
            requests.push([url,opts.method||'GET',opts.body?JSON.parse(opts.body):null]);
            return {ok:true,json:async()=>({results:[{id:'Q1',title:'Verified',description:'Description'}]})};
          };
          function fire(event, action, key, value, checked) {
            const node={dataset:{lm:action,key},value,checked,
              closest(sel){return sel==='[data-lm]'?this:null;}};
            handlers[event]({type:event,target:node,preventDefault(){}});
          }
        ''')
        self.js.eval(SCRIPT.read_text(encoding="utf-8"))
        self.rows, self.catalog = [], {}

    @property
    def html(self):
        return self.js.eval("mount.innerHTML")

    def render(self, rows=None, catalog=None, root="media"):
        if rows is not None:
            self.rows = rows
        if catalog is not None:
            self.catalog = catalog
        args = ",".join(json.dumps(arg) for arg in (self.rows, self.catalog, root))
        self.js.eval("LibraryMedia.render(" + args + ")")

    def controls(self, action):
        return [a for a in Controls(self.html).items if a['data-lm'] == action]

    def fire(self, action, key=None, *, event="click", value="", checked=False):
        visible = self.controls(action)
        candidates = [a for a in visible if key is None or a.get('data-key', '') == key]
        self.assertTrue(candidates, f"Missing control: {action} {key}")
        self.assertNotIn("disabled", candidates[0])
        args = (event, action, candidates[0].get('data-key', ''), value, checked)
        self.js.eval("fire(" + ",".join(map(json.dumps, args)) + ")")
        while self.js.execute_pending_job():
            pass

    @property
    def requests(self):
        return json.loads(self.js.eval("JSON.stringify(requests)"))

    def open_all(self):
        for control in self.controls('files'):
            if control.get('aria-expanded') == 'false':
                self.fire('files', control['data-key'])

    def test_only_strong_identity_or_explicit_versions_merge(self):
        rows = [row(p, identity='nfo-safe' if p in 'cdef' else '') for p in 'abcdefgh']
        self.render(rows, {'decisions':{'e':{'status':'standalone'}, 'f':{'status':'confirm','title':'Corrected'},
                                       'g':{'status':'version','group':'manual'}, 'h':{'status':'version','group':'manual'}}})
        self.assertEqual(len(self.controls('files')), 6)
        self.assertIn('Corrected', self.html)
        self.open_all()
        self.assertEqual(sorted(c['data-key'] for c in self.controls('select-file')), list('abcdefgh'))
        self.assertTrue(all('checked' not in c for c in self.controls('select-file')))

    def test_show_navigation_scopes_name_to_directory_and_year(self):
        def ep(path, year, show='Pilot'):
            return row(path, 'Episode', kind='serie', show=show, year=year, season=1, episode=1)
        self.render([ep('A/Season 1/a',2020), ep('A/Season 1/b',2020), ep('B/Season 1/a',2020),
                     ep('A/Season 1/c',2024), ep('U/a',2020,''), ep('U/b',2020,'')])
        # Unknown shows remain separate, whether presented as files or single-show branches.
        self.assertEqual(len(self.controls('show')) + len(self.controls('files')), 5)
        self.assertGreaterEqual(len(self.controls('show')), 3)
        found = []
        for control in self.controls('show'):
            self.fire('show', control['data-key'])
            self.fire('season')
            self.open_all()
            found.append(sorted(c['data-key'] for c in self.controls('select-file')))
            self.fire('home')
        self.assertIn(['A/Season 1/a','A/Season 1/b'], found)

    def test_cover_is_local_and_html_is_escaped(self):
        self.render([row('a&b.mkv', '<img src=x onerror=oops()>', cover=True, source='nfo', description='<script>oops()</script>')])
        self.assertIn('/api/library/media-cover?path=a%26b.mkv', self.html)
        self.assertNotIn('<img src=x', self.html)
        self.assertNotIn('<script>', self.html)
        self.assertIn('&lt;script&gt;', self.html)
        self.open_all()
        self.assertIn('a&amp;b.mkv', self.html)
        self.assertIn('1920×1080', self.html)
        self.assertIn('HDR10', self.html)

    def test_selection_survives_refresh_but_not_hidden_paths_or_other_root(self):
        self.render([row('a','Alpha'),row('b','Beta')], {'collections':[{'id':'C','name':'Club'}],
                                                         'assignments':{'b':{'collection_id':'C'}}})
        self.open_all()
        for path in ['a','b']:
            self.fire('select-file', path, event='change', checked=True)
        self.render()
        self.assertEqual(len([c for c in self.controls('select-file') if 'checked' in c]), 2)
        self.fire('collection-filter', event='change', value='C')
        self.assertEqual([c['data-key'] for c in self.controls('select-file') if 'checked' in c], ['b'])
        self.fire('unassign')
        self.assertEqual(self.requests[-1], ['/api/library/catalog/assign','POST',{'paths':['b'],'collection_id':''}])
        self.render(root='different')
        self.open_all()
        self.assertFalse(any('checked' in c for c in self.controls('select-file')))

    def test_title_search_sends_only_explicit_query_and_requires_save(self):
        path = 'private/location/video.mkv'
        self.render([row(path)]); self.open_all(); self.fire('edit', path)
        self.fire('query', path, event='input', value='Edited title')
        self.assertEqual(self.requests, [])
        self.fire('lookup', path)
        self.assertEqual(self.requests, [['/api/library/title-search?q=Edited%20title', 'GET', None]])
        self.fire('candidate')
        self.assertEqual(len(self.requests), 1)
        self.fire('confirm', path)
        self.assertEqual(self.requests[-1], ['/api/library/catalog/decision','POST',
                                              {'action':'confirm','paths':[path],'title':'Verified'}])
        self.assertEqual(self.js.eval("events.join()"), 'library-catalog-changed')

    def test_version_requires_multiple_checked_files_and_explicit_submission(self):
        self.render([row('a','A'),row('b','B'),row('c','C')]); self.open_all()
        self.fire('select-file','a',event='change',checked=True)
        self.assertTrue(all('disabled' in c for c in self.controls('merge')))
        self.fire('select-file','b',event='change',checked=True)
        self.fire('merge'); self.fire('group-title',event='input',value='My film')
        self.assertEqual(self.requests, [])
        self.fire('version')
        self.assertEqual(self.requests[-1], ['/api/library/catalog/decision','POST',
                                              {'action':'version','paths':['a','b'],'title':'My film'}])

    def test_collections_organize_but_do_not_merge_and_require_explicit_selection(self):
        self.render([row('one'),row('two')], {'collections':[{'id':'C','name':'Island'}],
                    'assignments':{'one':{'collection_id':'C'},'two':{'collection_id':'C'}}})
        self.fire('collection-filter', event='change', value='C')
        self.assertEqual(len(self.controls('files')), 2)
        self.open_all()
        self.assertFalse(any('checked' in c for c in self.controls('select-file')))
        self.fire('select-file','one',event='change',checked=True)
        self.fire('target',event='change',value='C'); self.fire('assign')
        self.assertEqual(self.requests[-1], ['/api/library/catalog/assign','POST',{'paths':['one'],'collection_id':'C'}])
        self.fire('manage'); self.fire('new-name', event='input', value='Archiv'); self.fire('create')
        self.assertEqual(self.requests[-1], ['/api/library/catalog','POST',{'name':'Archiv','kind':'sammlung'}])

    def test_large_results_paginate_after_filtering(self):
        self.render([row(f'f{i}',f'Title {i:04d}') for i in range(250)])
        self.assertLess(len(self.controls('files')), 100)
        self.fire('next')
        self.fire('search', event='input', value='Title 0249')
        self.assertEqual(len(self.controls('files')), 1)
        self.render()
        self.assertIn('Title 0249', self.html)
        self.assertEqual(len(self.controls('files')), 1)
