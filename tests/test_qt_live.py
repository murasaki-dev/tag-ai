"""Live Qt integration: real files/checkpoints with network providers replaced."""
import sys, tempfile, threading, time, unittest, json, sqlite3
import os
from pathlib import Path
from types import SimpleNamespace
from contextlib import nullcontext, closing
from unittest.mock import patch, Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Unit tests render offscreen; frozen_app_smoke exercises the real native window.
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
os.environ.setdefault('QT_QUICK_BACKEND', 'software')
try:
    import qt_app as q
except ModuleNotFoundError as exc:
    if exc.name.startswith('PySide6'): raise unittest.SkipTest('Qt dependencies not installed')
    raise
from PIL import Image
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtQuick import QQuickItem
from storage import extract_embedded_tags
from routing import UNMATCHED_ACTIONS
from qt_core import QDesktopServices
from qt_core import shared_image_stream, ThumbnailCache

class LiveTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app=q.QGuiApplication.instance() or q.QGuiApplication([])
        cls.app.setQuitOnLastWindowClosed(False)
        q.QQuickStyle.setStyle('Fusion')
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.source=self.root/'source';self.source.mkdir()
        self.library=self.root/'library';(self.library/'Series').mkdir(parents=True)
        self.b=q.LiveBackend(self.root);self.e=self.p=None
        self.b.config.update(source_folder=str(self.source),dest_folder=str(self.library),booru_hash_lookup=False,booru_alias_lookup=False)
        self.b.toggleFlow('search','SauceNAO',False)
        self.release=threading.Event()
        client=SimpleNamespace(models=SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id='test')])),close=lambda:None)
        self.mock=patch.object(q.providers,'OpenAI',return_value=client);self.mock.start()
        self.capmock=patch.object(q.providers,'local_reasoning_capabilities',return_value={'test':{'options':['off','on'],'default':'on','protocol':'lmstudio'}});self.capmock.start()
    def pump(self, predicate=lambda:False, seconds=5):
        until=time.monotonic()+seconds
        while time.monotonic()<until:
            self.app.processEvents();time.sleep(.005)
            if predicate():return
        self.assertTrue(predicate(),'Timed out')
    def tearDown(self):
        self.release.set();self.b.stopSorting()
        if self.b.running:self.pump(lambda:not self.b.running)
        self.b.close()
        if self.e:
            for w in self.e.rootObjects():w.setVisible(False)
        if self.p:self.p.close()
        if self.e:q.delete_qt_object(self.e)
        self.mock.stop();self.capmock.stop();self.temp.cleanup()
    def image(self,name='image.png',color='red'):
        p=self.source/name;Image.new('RGB',(120,80),color).save(p);return p
    def test_shared_thumbnail_does_not_lock_sort_move(self):
        p=self.image();target=self.library/'Series'/'image.png'
        with shared_image_stream(p) as stream:
            p.rename(target)
            with Image.open(stream) as image:self.assertEqual(image.getpixel((0,0)),(255,0,0))
        decoded=ThumbnailCache().load('local',str(target))
        self.assertEqual(decoded.pixelColor(10,10).red(),255)
    def test_real_sort_collision_previews_history_and_undo(self):
        p=self.image();self.image('no-match.png','blue')
        Image.new('RGB',(12,12),'green').save(self.library/'Series'/'image.png')
        match=dict(engine='IQDB',title='match',similarity=99,booru_ids={'danbooru':'1'},thumb='https://example.com/thumb.png',fallback_url='https://example.com/post/1')
        def search(runner,path,is_fallback=False):return [match] if Path(path).name=='image.png' else []
        tags=dict(combined='series',copyright='series',character='',artist='',general='',meta='')
        with patch.object(q.SortRunner,'search_iqdb_engine',search),patch.object(q.SortRunner,'fetch_tags',return_value=tags):
            self.b.startSorting();self.pump(lambda:not self.b.running)
        self.assertFalse(p.exists());self.assertTrue((self.source/'no-match.png').exists())
        self.assertEqual(len(self.b.raw),2)
        self.assertFalse(any('Error' in l for l in self.b._log_lines),self.b._log_lines)
        moved=next(r for r in self.b.raw if not r['is_unmatched'])
        self.assertNotEqual(Path(moved['current_path']).name,'image.png')
        self.assertIn('example.com/post',moved['match_url'])
        self.assertEqual(moved['match_thumb_url'],match['thumb'])
        self.assertIsNone(self.b.session_store.load())
        self.b.historyAction(moved['id'],'undo');self.pump(lambda:not self.b.editing)
        self.assertTrue(p.exists());self.assertIn(moved['id'],self.b.store.rows)
        restored=self.b.store.rows[moved['id']]
        self.assertTrue(restored['move_undone']);self.assertTrue(restored['is_unmatched'])
        self.assertEqual(restored['current_path'],str(p));self.assertEqual(restored['match_thumb_url'],match['thumb'])
        with closing(sqlite3.connect(self.b.database)) as connection:
            saved=json.loads(connection.execute('SELECT payload FROM history WHERE id=?',(moved['id'],)).fetchone()[0])
        self.assertTrue(saved['move_undone'])
    def test_stop_resume_retries_interrupted_file_same_session(self):
        self.image('a.png');self.image('b.png','blue');started=threading.Event();calls=[]
        def search(runner,path,is_fallback=False):
            calls.append(Path(path).name)
            if len(calls)==2:started.set();self.release.wait(5)
            return []
        with patch.object(q.SortRunner,'search_iqdb_engine',search):
            self.b.startSorting();self.pump(started.is_set)
            self.b.stopSorting();oldrunner=self.b.runner;self.b.startSorting();self.assertIs(self.b.runner,oldrunner)
            ticks=[];timer=q.QTimer();timer.setInterval(16);timer.timeout.connect(lambda:ticks.append(1));timer.start()
            try:self.pump(lambda:len(ticks)>3)
            finally:timer.stop()
            self.assertTrue(self.b.running);self.release.set();self.pump(lambda:not self.b.running)
            record=self.b.session_store.load();self.assertEqual(record['completed'],1)
            identity=record['session_id'];self.b.resumeSorting();self.pump(lambda:not self.b.running)
        self.assertEqual(calls,['a.png','b.png','b.png'])
        self.assertEqual({r['session_id'] for r in self.b.raw},{identity})
        self.assertIsNone(self.b.pending)
    def test_metadata_survives_abstention_failed_routing_and_unmatched_move(self):
        match=dict(engine='IQDB',title='booru match',similarity=99,booru_ids={'danbooru':'1'})
        tags=dict(combined='unrepresented_series unused_artist red',copyright='unrepresented_series',artist='unused_artist',character='',general='red',meta='')
        for i,(action,failure,enabled) in enumerate([(UNMATCHED_ACTIONS[0],False,1),(UNMATCHED_ACTIONS[1],False,1),(UNMATCHED_ACTIONS[0],True,1),(UNMATCHED_ACTIONS[0],False,0)]):
            with self.subTest(action=action,failure=failure,enabled=enabled):
                p=self.image(f'metadata-{i}.png');before=p.read_bytes()
                self.b.config.update(exec_mode='Single File Review Mode',source_folder=str(p),embed_metadata=enabled,matching_mode='Flexible',unmatched_action=action,meta_artist=0,meta_general=0)
                route=patch.object(q.SortRunner,'route_with_model',side_effect=RuntimeError('routing test failure')) if failure else patch.object(q.SortRunner,'route_with_model',return_value=(None,'Model abstained',[], '{"candidate_id":0}'))
                with patch.object(q.SortRunner,'search_iqdb_engine',return_value=[match]),patch.object(q.SortRunner,'fetch_tags',return_value=tags.copy()),route:
                    self.b.startSorting();self.pump(lambda:not self.b.running)
                row=next(r for r in self.b.raw if r['original_path']==str(p))
                self.assertTrue(row['is_unmatched']);current=Path(row['current_path'])
                self.assertTrue(current.exists())
                if enabled:
                    text=extract_embedded_tags(current);self.assertIn('unrepresented_series',text,self.b._log_lines);self.assertNotIn('unused_artist',text);self.assertNotIn('red',text)
                else:self.assertEqual(current.read_bytes(),before)
                with Image.open(current) as image:self.assertEqual(image.getpixel((0,0)),(255,0,0))
    def test_native_title_tracks_progress_and_resets_after_batch(self):
        self.e,self.p=q.create_engine(self.b,'App.qml');window=self.e.rootObjects()[0]
        self.image();started=threading.Event()
        def search(*args,**kwargs):started.set();self.release.wait(4);return []
        with patch.object(q.SortRunner,'search_iqdb_engine',search):
            self.b.startSorting();self.pump(started.is_set)
            self.pump(lambda:window.title()=='Tag-AI 1/1')
            self.release.set();self.pump(lambda:not self.b.running)
            self.pump(lambda:window.title()=='Tag-AI Booru Image Sorter')

    def test_incomplete_vision_is_not_negative_cached_and_has_specific_history_reason(self):
        self.image();self.b.toggleFlow('search','IQDB',False);self.b.toggleFlow('search','Local Vision LLM',True)
        calls=[];incomplete=True
        def search(runner,*args):
            calls.append(1);runner._vision_last_outcome='unavailable' if incomplete else 'miss'
            runner._last_vision_error='Local vision answer was cut off before final tag JSON' if incomplete else ''
            return []
        with patch.object(q.SortRunner,'search_local_vision_engine',search):
            for i in range(4):
                if i==2:incomplete=False
                self.b.startSorting();self.pump(lambda:not self.b.running)
                if i<2:self.assertIn('cut off',self.b.raw[-1]['reason'])
        self.assertEqual(len(calls),3)  # Only a complete, empty identification is cached.

    def test_history_edits_work_during_batch_but_active_image_is_protected(self):
        from storage import write_image_tags
        active=self.image('a.png');pending=self.image('z.png','blue')
        moved=self.library/'Series'/'old.png';Image.new('RGB',(12,12),'green').save(moved)
        original=self.source/'old.png';write_image_tags(str(pending),'remove_me')
        rows=[dict(id='old',original_path=str(original),current_path=str(moved),is_unmatched=False),
              dict(id='active',original_path=str(active),current_path=str(active),is_unmatched=True),
              dict(id='pending',original_path=str(pending),current_path=str(pending),is_unmatched=True)]
        for row in rows:self.b.store.record(row)
        self.b._history_delta(rows,[]);started=threading.Event();calls=[]
        def search(runner,path,is_fallback=False):
            calls.append(Path(path).name);started.set();self.release.wait(4);return []
        with patch.object(q.SortRunner,'search_iqdb_engine',search):
            self.b.startSorting();self.pump(started.is_set)
            self.b.historyAction('old','undo');self.pump(lambda:not self.b.editing)
            self.assertTrue(original.exists());self.assertFalse(moved.exists());self.assertTrue(self.b.running)
            self.b.historyAction('active','tags');self.pump(lambda:not self.b.editing)
            self.assertIn('currently being processed',self.b.status)
            self.b.historyAction('pending','tags');self.pump(lambda:not self.b.editing)
            self.assertEqual(extract_embedded_tags(pending),'');self.assertTrue(self.b.running)
            self.release.set();self.pump(lambda:not self.b.running)
        self.assertEqual(self.b.runProgress,1.)
        self.assertEqual(calls,['a.png'])  # Neither the restored file nor manually edited queued file is re-sorted.
        self.assertFalse(self.b.store.active_paths)

    def test_history_palette_validation_persistence_and_color_dialog(self):
        from PySide6.QtCore import QObject, QMetaObject
        from PySide6.QtGui import QColor
        self.e,self.p=q.create_engine(self.b,'App.qml');window=self.e.rootObjects()[0];window.setProperty('page',2)
        self.assertTrue(QTest.qWaitForWindowExposed(window,3000));QTest.qWait(50)
        def walk(item):
            yield item
            for child in item.childItems():yield from walk(child)
        settings=next(i for i in walk(window.contentItem()) if i.metaObject().className().startswith('SettingsPane'))
        settings.setProperty('section',3);self.app.processEvents()
        swatch=next((i for i in walk(window.contentItem()) if i.objectName()=='methodColor_IQDB'),None);self.assertIsNotNone(swatch)
        scroll=window.findChild(QQuickItem,'maintenanceSettingsScroll').property('contentItem')
        scroll.setProperty('contentY',max(0,swatch.mapToItem(scroll.property('contentItem'),QPointF(0,0)).y()-40));QTest.qWait(30)
        QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,swatch.mapToScene(QPointF(swatch.width()/2,swatch.height()/2)).toPoint())
        dialog=window.findChild(QObject,'historyColorDialog');self.assertIsNotNone(dialog)
        self.assertEqual(dialog.property('methodKey'),'IQDB')
        dialog.setProperty('selectedColor',QColor('#123456'));QMetaObject.invokeMethod(dialog,'accepted')
        self.pump(lambda:self.b.historyColors['IQDB']=='#123456')
        self.assertTrue(self.b.saveSettings())
        saved=json.loads(self.b.config_path.read_text());self.assertEqual(saved['history_colors']['IQDB'],'#123456')
        self.b.setHistoryColor('IQDB','invalid');self.assertEqual(self.b.historyColors['IQDB'],'#123456')
        self.assertEqual(q.normalized_config({'history_colors':{'IQDB':'invalid','SauceNAO':'#ABCDEF'}},self.root)['history_colors']['IQDB'],q.HISTORY_COLORS['IQDB'])
        self.b.resetHistoryColors();self.assertEqual(self.b.historyColors,q.HISTORY_COLORS)
    def test_history_method_color_provenance_legacy_unknown_and_unmatched(self):
        from qt_core import model_row
        p=str(self.image())
        base=dict(id='x',original_path=p,current_path=p,is_unmatched=False)
        for method in ['IQDB','SauceNAO (#2)','Local Vision LLM','Embedded Metadata']:
            row=model_row(dict(base,identification_method=method,from_cache=True),1)
            key='SauceNAO' if method.startswith('Sauce') else method
            self.assertEqual(row['methodColorKey'],key);self.assertTrue(row['cachedResult'])
            unresolved=model_row(dict(base,identification_method=method,is_unmatched=True),1)
            self.assertEqual(unresolved['methodColorKey'],'Unmatched');self.assertIn('needs attention',unresolved['methodLabel'])
        self.assertEqual(model_row(dict(base,reason='IQDB · 99% similarity'),1)['methodColorKey'],'IQDB')
        unknown=model_row(dict(base,reason='Matched and routed'),1)
        self.assertEqual(unknown['methodLabel'],'Method not recorded');self.assertFalse(unknown['cachedResult'])
    def test_successful_identification_and_cache_provenance_survive_failed_route(self):
        p=self.image();self.b.config.update(matching_mode='Flexible')
        match=dict(engine='SauceNAO (#2)',title='match',similarity=99,booru_ids={'danbooru':'1'})
        tags=dict(combined='other_series',copyright='other_series',character='',artist='',general='',meta='')
        with patch.object(q.SortRunner,'search_iqdb_engine',return_value=[match]) as search,patch.object(q.SortRunner,'fetch_tags',return_value=tags),patch.object(q.SortRunner,'route_with_model',return_value=(None,'Model abstained',[],'')):
            self.b.startSorting();self.pump(lambda:not self.b.running)
            self.assertEqual(self.b.raw[-1]['identification_method'],'SauceNAO (#2)');self.assertFalse(self.b.raw[-1]['from_cache'])
            self.b.startSorting();self.pump(lambda:not self.b.running)
            self.assertEqual(search.call_count,1);self.assertTrue(self.b.raw[-1]['from_cache'])
            self.assertEqual(self.b.raw[-1]['identification_method'],'SauceNAO (#2)')
        self.assertTrue(p.exists())

    def test_log_mode_switch_follows_bottom_and_preserves_reading_position(self):
        self.e,self.p=q.create_engine(self.b,'App.qml');window=self.e.rootObjects()[0]
        window.findChild(QQuickItem,'logsToggle').setProperty('checked',True)
        self.b._log_lines=[f'Detailed message {i} '+('words '*12) for i in range(220)]
        self.b.summary_lines=[f'Summary {i} '+('words '*10) for i in range(90)];self.b.logsChanged.emit()
        view=window.findChild(QQuickItem,'logScroll').property('contentItem')
        detailed=window.findChild(QQuickItem,'detailedLog');follow=window.findChild(QQuickItem,'followLog')
        def end():return max(0.,float(view.property('contentHeight'))-view.height())
        self.pump(lambda:end()>100 and abs(float(view.property('contentY'))-end())<2)
        def click(control):QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,control.mapToScene(QPointF(control.width()/2,control.height()/2)).toPoint())
        click(detailed);self.pump(lambda:bool(detailed.property('checked')) and abs(float(view.property('contentY'))-end())<2)
        click(detailed);self.pump(lambda:not detailed.property('checked') and abs(float(view.property('contentY'))-end())<2)
        click(follow);QTest.qWait(30);view.setProperty('contentY',end()*.4)
        click(detailed);QTest.qWait(100)
        self.assertAlmostEqual(float(view.property('contentY'))/end(),.4,delta=.04)
    def test_reasoning_options_match_model_capabilities_and_modes_persist(self):
        with patch.object(q.providers,'local_model_client',side_effect=lambda *a,**k:nullcontext(q.providers.OpenAI())):
            self.b.refreshModels();self.pump(lambda:'On' in self.b.reasoningOptions)
        self.assertEqual(self.b.reasoningOptions,['Server default','Off','On'])
        self.b.setSetting('vision_reasoning','Off');self.b.setSetting('routing_reasoning','On')
        self.assertTrue(self.b.saveSettings());saved=json.loads(self.b.config_path.read_text())
        self.assertEqual(saved['vision_reasoning'],'Off');self.assertEqual(saved['routing_reasoning'],'On')
        self.b._reasoning_caps={};self.assertIn('Unverified',self.b.reasoningSupport)
        self.assertIn('Off',self.b.reasoningOptions)
    def test_reruns_refresh_routing_but_reuse_vision_cache(self):
        self.b.toggleFlow('search','IQDB',False);self.b.toggleFlow('search','Local Vision LLM',True)
        seen_vision=[];seen_route=[]
        def vision(runner,*args):
            seen_vision.append(runner._run_reasoning_requests['vision'].copy())
            return [dict(engine='Local Vision LLM',title='vision tags',booru_ids={},tag_dict_override=dict(copyright='series',combined='series',character='',artist='',general='',meta='',_identity_source='vision'))]
        def route(runner,*args):seen_route.append(runner._run_reasoning_requests['routing'].copy());return 'Series','test route',[],''
        with patch.object(q.SortRunner,'search_local_vision_engine',vision),patch.object(q.SortRunner,'route_with_model',route):
            for v,r in [('Off','Server default'),('Off','On'),('On','On'),('On','On')]:
                self.image();self.b.config.update(vision_reasoning=v,routing_reasoning=r)
                self.b.startSorting();self.pump(lambda:not self.b.running)
        self.assertEqual(seen_vision,[{'reasoning_effort':'none'},{'reasoning_effort':'medium'}])
        self.assertEqual(seen_route,[{},{'reasoning_effort':'medium'},{'reasoning_effort':'medium'},{'reasoning_effort':'medium'}])

    def test_separate_models_and_router_change_reuse_vision_cache(self):
        self.b.toggleFlow('search','IQDB',False);self.b.toggleFlow('search','Local Vision LLM',True)
        self.b.config.update(selected_model='text-a', vision_model='vision')
        client=SimpleNamespace(models=SimpleNamespace(list=lambda: SimpleNamespace(data=[SimpleNamespace(id=v) for v in ['text-a','text-b','vision']])))
        seen_vision=[];seen_route=[]
        def vision(runner,path,client,model):
            seen_vision.append(model)
            return [dict(engine='Local Vision LLM',title='vision tags',booru_ids={},tag_dict_override=dict(copyright='series',combined='series',character='',artist='',general='',meta='',_identity_source='vision'))]
        def route(runner,client,model,plan):
            seen_route.append(model);return 'Series','test route',[],''
        with patch.object(q.providers,'OpenAI',return_value=client), patch.object(q.SortRunner,'search_local_vision_engine',vision), patch.object(q.SortRunner,'route_with_model',route):
            for model in ['text-a','text-b']:
                self.image();self.b.config['selected_model']=model
                self.b.startSorting();self.pump(lambda:not self.b.running)
        self.assertEqual(seen_vision,['vision'])
        self.assertEqual(seen_route,['text-a','text-b'])

    def test_clear_one_image_cache_forces_search_despite_embedded_tags(self):
        from storage import stream_file_md5, write_image_tags
        image=self.image();other=self.image('other.png','blue')
        original_hash=stream_file_md5(image)
        write_image_tags(str(image),'old_embedded_tags')
        current_hash=stream_file_md5(image);other_hash=stream_file_md5(other)
        self.b.cache.put(original_hash,'old','resolved',{'tags':'old'})
        self.b.cache.put('context:serpapi:'+original_hash,'ctx','resolved',{'context':'old'})
        self.b.cache.put(current_hash,'new','miss')
        self.b.cache.put('context:serpapi:'+current_hash,'ctx','resolved',{'context':'current'})
        self.b.cache.put(other_hash,'other','resolved',{'tags':'keep'})
        row=dict(id='clear-me',original_path=str(image),current_path=str(image),image_hash=original_hash,is_unmatched=True)
        self.b.store.record(row)
        self.b.store.active_paths.add(q.path_key(image))
        self.b.historyAction('clear-me','cache');self.pump(lambda:not self.b.editing)
        self.assertIn('currently being processed',self.b.status)
        self.assertIsNotNone(self.b.cache.get(original_hash,'old'))
        self.b.store.active_paths.clear()
        self.b.historyAction('clear-me','cache');self.pump(lambda:not self.b.editing)
        for key,signature in [(original_hash,'old'),(current_hash,'new'),('context:serpapi:'+original_hash,'ctx'),('context:serpapi:'+current_hash,'ctx')]:
            self.assertIsNone(self.b.cache.get(key,signature))
        self.assertEqual(self.b.cache.get(other_hash,'other')['tags'],'keep')
        self.assertTrue(self.b.cache.needs_refresh(current_hash))
        self.assertIn('clear-me',self.b.store.rows)
        self.assertEqual(extract_embedded_tags(image),'old_embedded_tags')
        self.b.config.update(exec_mode='Single File Review Mode',source_folder=str(image),use_embedded_tags=1,skip_no_embedded=1)
        with patch.object(q.SortRunner,'search_iqdb_engine',return_value=[]) as search:
            self.b.startSorting();self.pump(lambda:not self.b.running)
            search.assert_called_once()
        self.assertFalse(self.b.cache.needs_refresh(current_hash))
        self.assertTrue(image.exists())

    def test_configuration_nested_flows_and_model_refresh(self):
        self.b.addKey('serpapi_keys');self.b.editKey('serpapi_keys',0,'private-test-value')
        self.b.moveFlow('search','Local Vision LLM',0);self.b.toggleFlow('search','Local Vision LLM',True)
        self.b.setSetting('matching_mode','Flexible');self.assertTrue(self.b.saveSettings())
        saved=json.loads(self.b.config_path.read_text());self.assertEqual(saved['search_pipeline'][0]['method'],'Local Vision LLM')
        self.assertEqual(saved['serpapi_keys'],['private-test-value'])
        self.assertEqual(saved['matching_mode'],'Flexible')
        with patch.object(q.providers,'local_model_client',side_effect=lambda *a,**k: nullcontext(q.providers.OpenAI())):
            self.b.refreshModels();self.pump(lambda:'test' in self.b.modelOptions)
        self.assertNotIn('private-test-value',self.b.actualUsage)
    def test_skip_button_acknowledges_request_and_leaves_image_untouched(self):
        first=self.image('a.png');second=self.image('b.png','blue')
        entered=threading.Event();calls=[]
        def search(runner,path,is_fallback=False):
            calls.append(Path(path).name)
            if len(calls)==1: entered.set();self.release.wait(4)
            return [dict(engine='IQDB',title='match',booru_ids={'danbooru':'1'})]
        tags=dict(copyright='series',character='',artist='',general='tag',meta='',combined='series tag')
        self.e,self.p=q.create_engine(self.b,'App.qml');window=self.e.rootObjects()[0]
        with patch.object(q.SortRunner,'search_iqdb_engine',search),patch.object(q.SortRunner,'fetch_tags',return_value=tags):
            self.b.startSorting();self.pump(lambda:entered.is_set() and self.b.canSkip)
            button=window.findChild(QQuickItem,'sortSkipButton')
            QTest.mouseClick(window,Qt.LeftButton,Qt.NoModifier,button.mapToScene(QPointF(button.width()/2,button.height()/2)).toPoint())
            self.pump(lambda:self.b.skipPending)
            self.assertFalse(self.b.canSkip);self.assertIn('Skipping',self.b.status)
            self.release.set();self.pump(lambda:not self.b.running)
        self.assertTrue(first.exists());self.assertFalse(second.exists())
        self.assertEqual(calls,['a.png','b.png'])
        self.assertTrue(any(row['reason']=='Skipped by user; left in source' for row in self.b.raw))
        self.assertFalse(self.b.skipPending)

    def test_skip_cancels_active_model_task_without_waiting_for_generation(self):
        self._exercise_model_interrupt(False)

    def test_stop_cancels_active_model_task_without_waiting_for_generation(self):
        self._exercise_model_interrupt(True)

    def _exercise_model_interrupt(self, stop):
        import asyncio
        entered=threading.Event();cancelled=threading.Event();errors=[]
        runner=q.SortRunner(self.b,self.b.config,None,'skip-model')
        class FakeAsyncClient:
            async def __aenter__(self):return self
            async def __aexit__(self,*args):pass
            @property
            def chat(self):return self
            @property
            def completions(self):return self
            async def create(self,**kwargs):
                entered.set()
                try:await asyncio.sleep(30)
                finally:cancelled.set()
        client=q.providers.REAL_OPENAI_CLASS(base_url='http://127.0.0.1:1/v1',api_key='unused')
        def call():
            try:runner._completion_request(client,model='test',messages=[])
            except BaseException as exc:errors.append(exc)
        with patch.object(q,'AsyncOpenAI',return_value=FakeAsyncClient()):
            worker=threading.Thread(target=call);worker.start();self.assertTrue(entered.wait(2))
            if stop:runner.stop_requested=True
            else:runner.skip_requested=True
            worker.join(2)
        client.close();self.assertFalse(worker.is_alive());self.assertTrue(cancelled.is_set())
        if stop:self.assertIsInstance(errors[0],q.providers.ProcessingStopped)
        else:self.assertIn('cancelled',' '.join(map(str,errors)))

    def test_skip_disconnects_stalled_http_and_processes_next_image(self):
        self._exercise_http_interrupt(False)

    def test_stop_disconnects_stalled_http_and_resume_retries_interrupted_image(self):
        self._exercise_http_interrupt(True)

    def _exercise_http_interrupt(self, stop):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from stage_timing import scoped
        entered=threading.Event();release=threading.Event();calls=[]
        class Handler(BaseHTTPRequestHandler):
            def do_POST(server_self):
                server_self.rfile.read(int(server_self.headers.get('Content-Length', 0)))
                if server_self.path == '/a.png': entered.set();release.wait(8)
                try:
                    server_self.send_response(200);server_self.send_header('Content-Type','application/json');server_self.end_headers();server_self.wfile.write(b'{"ok":true}')
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError): pass
            def log_message(self,*args): pass
        server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        server.daemon_threads=True;thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        first=self.image('a.png');second=self.image('b.png','blue')
        @scoped
        def search(runner,path,is_fallback=False):
            calls.append(Path(path).name)
            reply=q.providers.get_http_session().post(f'http://127.0.0.1:{server.server_port}/{Path(path).name}',json={'test':True},timeout=20)
            self.assertTrue(reply.json()['ok'])
            return [dict(engine='IQDB',title='match',booru_ids={'danbooru':'1'})]
        tags=dict(copyright='series',character='',artist='',general='tag',meta='',combined='series tag')
        try:
            with patch.object(q.SortRunner,'search_iqdb_engine',search),patch.object(q.SortRunner,'fetch_tags',return_value=tags):
                self.b.startSorting();self.pump(lambda:entered.is_set() and self.b.canSkip)
                started=time.monotonic()
                if stop:self.b.stopSorting()
                else:self.b.skipSorting()
                self.pump(lambda:not self.b.running,seconds=3)
                self.assertLess(time.monotonic()-started,3)
                if stop:
                    self.assertTrue(first.exists());self.assertTrue(second.exists())
                    self.assertEqual(self.b.session_store.load()['completed'],0)
                    self.assertFalse(self.b.raw)
                    release.set();self.b.resumeSorting();self.pump(lambda:not self.b.running)
                    self.assertFalse(first.exists());self.assertFalse(second.exists())
                    self.assertEqual(calls,['a.png','a.png','b.png'])
                    return
            self.assertTrue(first.exists());self.assertFalse(second.exists());self.assertEqual(calls,['a.png','b.png'])
            skipped=[r for r in self.b.raw if r['filename']=='a.png']
            self.assertEqual(len(skipped),1);self.assertEqual(skipped[0]['reason'],'Skipped by user; left in source')
        finally:
            release.set();server.shutdown();server.server_close();thread.join(2)

    def test_skip_interrupts_rate_limit_cooldown_without_recording_request(self):
        from stage_timing import timing_scope
        runner=q.SortRunner(self.b,self.b.config,None,'skip-rate-limit')
        limiter=q.providers.GentleRateLimiter(60);limiter.last_call_time=time.time()
        entered=threading.Event();cancelled=threading.Event()
        def wait():
            with timing_scope(runner):
                entered.set()
                try:limiter.record()
                except q.providers.ImageSkipped:cancelled.set()
        worker=threading.Thread(target=wait,daemon=True);worker.start();self.assertTrue(entered.wait(1))
        runner.skip_requested=True;worker.join(1)
        self.assertFalse(worker.is_alive());self.assertTrue(cancelled.is_set());self.assertEqual(limiter.total_calls,0)

    def test_live_settings_change_current_image_routing_destination_and_metadata(self):
        image=self.image();entered=threading.Event();observed=[]
        destination=self.root/'new-library';(destination/'Broad category').mkdir(parents=True)
        tags=dict(copyright='unlisted_series',character='',artist='',general='blue_hair',meta='',combined='unlisted_series blue_hair')
        def fetch(runner,*args):entered.set();self.release.wait(5);return tags.copy()
        def route(runner,client,model,plan):
            observed.append((plan['matching_mode'],runner._run_routing_output_tokens))
            return 'Broad category','test route',[], '{"candidate_id":1}'
        with patch.object(q.SortRunner,'search_iqdb_engine',return_value=[dict(engine='IQDB',title='match',booru_ids={'danbooru':'1'})]),patch.object(q.SortRunner,'fetch_tags',fetch),patch.object(q.SortRunner,'route_with_model',route):
            self.b.startSorting();self.pump(lambda:entered.is_set())
            self.b.setSetting('matching_mode','Flexible');self.b.setSetting('routing_output_tokens',4096)
            self.b.setSetting('dest_folder',str(destination));self.b.setSetting('embed_metadata',1)
            self.release.set();self.pump(lambda:not self.b.running)
        target=destination/'Broad category'/'image.png'
        self.assertFalse(image.exists());self.assertTrue(target.exists());self.assertEqual(observed,[('Flexible',4096)])
        self.assertIn('blue_hair',extract_embedded_tags(target))

    def test_live_search_flow_changes_before_next_method_without_rerunning_active_method(self):
        image=self.image();entered=threading.Event();calls=[]
        def iqdb(runner,*args,**kwargs):calls.append('IQDB');entered.set();self.release.wait(5);return []
        def sauce(runner,*args,**kwargs):calls.append('SauceNAO');return [dict(engine='SauceNAO',title='match',booru_ids={'danbooru':'1'})]
        tags=dict(copyright='series',character='',artist='',general='tag',meta='',combined='series tag')
        with patch.object(q.SortRunner,'search_iqdb_engine',iqdb),patch.object(q.SortRunner,'search_saucenao_engine',sauce),patch.object(q.SortRunner,'fetch_tags',return_value=tags):
            self.b.startSorting();self.pump(lambda:entered.is_set())
            self.b.toggleFlow('search','SauceNAO',True);self.b.toggleFlow('search','IQDB',False)
            self.release.set();self.pump(lambda:not self.b.running)
        self.assertEqual(calls,['IQDB','SauceNAO']);self.assertFalse(image.exists())
        with closing(sqlite3.connect(self.root/'image_result_cache.sqlite3')) as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM image_results").fetchone()[0],0)

    def test_live_vision_model_change_after_evidence_before_model_request(self):
        image=self.image();entered=threading.Event();models=[]
        self.b.toggleFlow('search','IQDB',False);self.b.toggleFlow('search','Local Vision LLM',True)
        client=SimpleNamespace(models=SimpleNamespace(list=lambda:SimpleNamespace(data=[SimpleNamespace(id='test'),SimpleNamespace(id='vision-new')])),close=lambda:None)
        def context(runner,path):entered.set();self.release.wait(5);return '',0
        def completion(runner,unused,**kwargs):
            models.append(kwargs['model'])
            content=json.dumps(dict(copyright='series',character='',artist='',general='tag',meta=''))
            return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',message=SimpleNamespace(content=content,reasoning_content=None))])
        with patch.object(q.providers,'OpenAI',return_value=client),patch.object(q.SortRunner,'_fetch_vision_context',context),patch.object(q.SortRunner,'_completion_request',completion),patch.object(q.SortRunner,'route_with_model',return_value=('Series','test route',[],'')):
            self.b.startSorting();self.pump(lambda:entered.is_set());self.b.setSetting('vision_model','vision-new')
            self.release.set();self.pump(lambda:not self.b.running)
        self.assertEqual(models[0],'vision-new');self.assertFalse(image.exists())

    def test_evidence_flow_controls_persist_and_snapshot(self):
        self.e,self.p=q.create_engine(self.b,'App.qml');w=self.e.rootObjects()[0];w.setProperty('page',2)
        def walk(item):
            yield item
            for child in item.childItems(): yield from walk(child)
        settings=next(i for i in walk(w.contentItem()) if i.metaObject().className().startswith('SettingsPane'))
        settings.setProperty('section',2)
        self.app.processEvents()
        enabled=w.findChild(QQuickItem,'taggerEnabled');self.assertIsNotNone(enabled)
        self.b.setSetting('tagger_enabled',True);self.b.setSetting('tagger_guided_lens',True)
        self.b.setSetting('tagger_model','my-compatible-tagger');self.b.setSetting('context_mode','Combine enabled')
        self.app.processEvents();self.assertTrue(enabled.property('checked'))
        self.assertTrue(self.b.saveSettings())
        saved=json.loads(self.b.config_path.read_text())
        self.assertEqual(saved['tagger_model'],'my-compatible-tagger');self.assertEqual(saved['context_mode'],'Combine enabled')
        runner=q.SortRunner(self.b,self.b.config,None,'test-evidence')
        self.b.setSetting('tagger_guided_lens',False)
        self.assertTrue(runner._run_evidence_options['tagger_guided_lens'])
        self.assertIn('my-compatible-tagger',self.b.taggerModelOptions)
        w.resize(850,760);self.app.processEvents();self.assertFalse(w.grabWindow().isNull())

    def test_tagger_management_buttons_receive_mouse_clicks_and_report_errors(self):
        self.e,self.p=q.create_engine(self.b,'App.qml');w=self.e.rootObjects()[0];w.setProperty('page',2)
        def walk(item):
            yield item
            for child in item.childItems(): yield from walk(child)
        settings=next(i for i in walk(w.contentItem()) if i.metaObject().className().startswith('SettingsPane'))
        settings.setProperty('section',2)
        self.app.processEvents()
        download=w.findChild(QQuickItem,'downloadTaggerModel')
        # Scroll the actual viewport; invoking clicked directly would miss overlapping controls.
        view=next(i for i in walk(w.contentItem()) if i.metaObject().className().startswith('ScrollView') and download.isAncestorOf(i) is False and i.isAncestorOf(download))
        flick=view.property('contentItem')
        flick.setProperty('contentY',flick.property('contentY')+download.mapToItem(flick,QPointF(0,0)).y()-80)
        self.pump(lambda:download.mapToScene(QPointF(0,0)).y()<w.height()-100)
        with patch.object(self.b.tagger_models,'download',side_effect=RuntimeError('test download failure')) as fetch:
            pos=download.mapToScene(QPointF(download.width()/2,download.height()/2)).toPoint()
            QTest.mouseClick(w,Qt.LeftButton,Qt.NoModifier,pos)
            self.pump(lambda:'test download failure' in self.b.taggerMessage)
            fetch.assert_called_once();self.assertFalse(self.b.taggerBusy)
        with patch.object(self.b.tagger_manager,'close'),patch.object(self.b,'_run_tagger_install',side_effect=RuntimeError('test installer failure')) as install:
            button=w.findChild(QQuickItem,'installPixaiRuntime')
            pos=button.mapToScene(QPointF(button.width()/2,button.height()/2)).toPoint()
            QTest.mouseClick(w,Qt.LeftButton,Qt.NoModifier,pos)
            self.pump(lambda:'test installer failure' in self.b.taggerMessage)
            self.assertTrue(install.called);self.assertFalse(self.b.taggerBusy)

    def test_close_does_not_wait_for_read_only_background_job(self):
        entered=threading.Event()
        def stalled_read():entered.set();self.release.wait(5)
        future=self.b.pool.submit(stalled_read);self.assertTrue(entered.wait(1))
        started=time.monotonic();self.b.close()
        self.assertLess(time.monotonic()-started,.5)
        self.release.set();future.result(2)

    def test_clean_shutdown_is_not_held_open_by_executor_exit_join(self):
        program = (f'import sys;sys.path[:0]={sys.path!r};import qt_app as q,time;'
                   'from concurrent.futures import ThreadPoolExecutor;'
                   'pool=ThreadPoolExecutor(1);pool.submit(time.sleep,30);'
                   'q.main=lambda:0;sys.exit(q.entrypoint())')
        started=time.monotonic()
        q.subprocess.run([sys.executable,'-c',program],check=True,capture_output=True,timeout=7)
        self.assertLess(time.monotonic()-started,7)

    def test_runtime_installer_is_terminated_when_app_closes(self):
        process=Mock();process.poll.return_value=None
        with patch.object(q.subprocess,'Popen',return_value=process),patch.object(self.b.tagger_manager,'guard_process'),patch.object(q.TaggerManager,'terminate_process') as terminate:
            errors=[]
            def install():
                try:self.b._run_tagger_install(['test-install'])
                except RuntimeError as exc:errors.append(str(exc))
            worker=threading.Thread(target=install);worker.start()
            self.pump(lambda:self.b._install_process is not None)
            self.b.stop.set();worker.join(1)
            self.assertFalse(worker.is_alive());terminate.assert_called_once_with(process)
            self.assertIn('cancelled',errors[0])

    def test_window_only_startup_provides_streams_for_download_libraries(self):
        # PyInstaller --windowed supplies None; a normal test runner hides this failure.
        program = (f'import sys;sys.path[:0]={sys.path!r};sys.stdout=sys.stderr=None;'
                   'import qt_app;assert sys.stdout is not None and sys.stderr is not None;'
                   'sys.stderr.write("download progress");sys.stderr.flush()')
        q.subprocess.run([sys.executable, '-c', program], check=True, capture_output=True, timeout=20)

    def test_flowchart_links_open_sidebar_pages_and_theme_updates(self):
        self.e,self.p=q.create_engine(self.b,'App.qml');w=self.e.rootObjects()[0];w.setProperty('page',2)
        def walk(item):
            yield item
            for child in item.childItems():yield from walk(child)
        settings=next(i for i in walk(w.contentItem()) if i.metaObject().className().startswith('SettingsPane'))
        self.app.processEvents()
        source=w.findChild(QQuickItem,'flowStep_source')
        QTest.mouseClick(w,Qt.LeftButton,Qt.NoModifier,source.mapToScene(QPointF(source.width()/2,source.height()/2)).toPoint())
        self.pump(lambda:settings.property('section')==3)
        self.assertEqual(settings.property('focusedGroup'),'sources')
        self.b.setSetting('ui_theme','Light');self.pump(lambda:not w.property('dark'))
        self.assertEqual(w.findChild(QQuickItem,'appTheme').property('currentIndex'),1)
        self.assertTrue(self.b.saveSettings());self.assertEqual(json.loads(self.b.config_path.read_text())['ui_theme'],'Light')
        settings.setProperty('section',0);self.app.processEvents()
        scroll=w.findChild(QQuickItem,'searchSettingsScroll').property('contentItem')
        web=w.findChild(QQuickItem,'flowStep_guided')
        y=web.mapToItem(scroll.property('contentItem'),QPointF(0,0)).y()
        scroll.setProperty('contentY',max(0,y-40));self.app.processEvents()
        webButton=next(i for i in walk(web) if i.property('text')=='Settings')
        QTest.mouseClick(w,Qt.LeftButton,Qt.NoModifier,webButton.mapToScene(QPointF(webButton.width()/2,webButton.height()/2)).toPoint())
        self.pump(lambda:settings.property('section')==1)
        self.assertEqual(settings.property('focusedGroup'),'web')

    def test_flow_timing_cards_update_and_open_substep_details(self):
        from PySide6.QtCore import QObject
        self.e,self.p=q.create_engine(self.b,'App.qml');w=self.e.rootObjects()[0];w.setProperty('page',2)
        self.b.timings.record('source',2.0);self.b.timings.record('source.hash',1.5);self.b._publish_timings()
        def walk(item):
            yield item
            for child in item.childItems():yield from walk(child)
        self.app.processEvents()
        source=w.findChild(QQuickItem,'flowStep_source')
        self.assertTrue(any('Average 2.0 s' in str(i.property('text') or '') for i in walk(source)))
        button=next(i for i in walk(source) if i.property('text')=='Timings')
        QTest.mouseClick(w,Qt.LeftButton,Qt.NoModifier,button.mapToScene(QPointF(button.width()/2,button.height()/2)).toPoint())
        dialog=w.findChild(QObject,'stageTimingDialog')
        self.pump(lambda:dialog.property('visible'))
        self.assertEqual(dialog.property('stageKey'),'source')
        self.assertIn('Hash image',str(self.b.timingSteps))
        self.assertEqual(self.b.stageTimings['source.hash']['average'],1.5)

    def test_live_file_operations_publish_nested_timing_samples(self):
        self.image();self.b.config['embed_metadata']=1
        match=dict(engine='IQDB',title='match',booru_ids={'danbooru':'1'})
        tags=dict(copyright='series',character='',artist='',general='tag',meta='',combined='series tag')
        with patch.object(q.SortRunner,'search_iqdb_engine',return_value=[match]),patch.object(q.SortRunner,'fetch_tags',return_value=tags):
            self.b.startSorting();self.pump(lambda:not self.b.running)
        self.b._publish_timings()
        for key in ['setup.files','source.hash','source.cache.read','routing.plan','move','metadata.write','history.persist','checkpoint.advance']:
            self.assertGreater(self.b.stageTimings[key]['count'],0,key)
        self.assertFalse(any(row['active'] for row in self.b.stageTimings.values()))

    def test_exact_booru_hash_skips_reverse_search_and_models_but_embeds_tags(self):
        image=self.image();digest=q.stream_file_md5(image)
        self.b.config.update(booru_hash_lookup=True,embed_metadata=1)
        response=Mock(status_code=200)
        response.json.return_value=[dict(id=5,md5=digest,tag_string='series blue_hair',tag_string_copyright='series',tag_string_general='blue_hair')]
        with patch.object(q.SortRunner,'safe_get_request',return_value=response) as get,patch.object(q.SortRunner,'search_iqdb_engine') as reverse,patch.object(q.SortRunner,'search_local_vision_engine') as vision,patch.object(q.SortRunner,'route_with_model') as route,patch.object(q.providers.tracker.danbooru_limiter,'record',return_value=1):
            self.b.startSorting();self.pump(lambda:not self.b.running)
        target=self.library/'Series'/'image.png'
        self.assertTrue(target.exists());self.assertFalse(image.exists());self.assertIn('blue_hair',extract_embedded_tags(target))
        self.assertEqual(get.call_count,1);reverse.assert_not_called();vision.assert_not_called();route.assert_not_called()
        self.assertEqual(self.b.raw[0]['identification_method'],'Booru hash')

    def test_booru_aliases_route_to_existing_alternate_folder_names(self):
        image=self.image();target=self.library/'old_series'/'old_hero'/'image.png';target.parent.mkdir(parents=True)
        self.b.config.update(booru_alias_lookup=True,scan_subfolders=1,embed_metadata=1)
        tags=dict(copyright='new_series',character='new_hero',artist='',general='',meta='',combined='new_series new_hero')
        def reply(a,b,category):
            value=Mock(status_code=200);value.json.return_value=[dict(antecedent_name=a,consequent_name=b,status='active',consequent_tag={'category':category})];return value
        with patch.object(q.SortRunner,'search_iqdb_engine',return_value=[dict(engine='IQDB',title='match',booru_ids={'danbooru':'1'})]),patch.object(q.SortRunner,'fetch_tags',return_value=tags),patch.object(q.SortRunner,'safe_get_request',side_effect=[reply('old_series','new_series',3),reply('old_hero','new_hero',4)]) as get,patch.object(q.SortRunner,'route_with_model') as route,patch.object(q.providers.tracker.danbooru_limiter,'record',return_value=1):
            self.b.startSorting();self.pump(lambda:not self.b.running)
        self.assertTrue(target.exists());self.assertFalse(image.exists());route.assert_not_called();self.assertEqual(get.call_count,2)
        embedded=extract_embedded_tags(target);self.assertIn('new_series',embedded);self.assertNotIn('old_series',embedded)

    def test_full_qml_all_tabs_render_and_live_load(self):
        self.e,self.p=q.create_engine(self.b,'App.qml');self.assertEqual(len(self.e.rootObjects()),1)
        w=self.e.rootObjects()[0];self.b.loadSaved();self.pump(lambda:not self.b.loading)
        for page in (0,1,2,0,2,1):
            w.setProperty('page',page);w.resize(1100+page*70,760+page*20)
            for _ in range(5):self.app.processEvents();time.sleep(.01)
        self.assertFalse(w.grabWindow().isNull())
        self.assertNotIn('read-only',self.b.status)
        self.assertEqual(self.b.alias_draft,'{}')
        self.assertTrue(self.b.saveSettings())
    def test_flowchart_mouse_drag_reorders_real_configuration(self):
        self.e,self.p=q.create_engine(self.b,'App.qml');w=self.e.rootObjects()[0];w.setProperty('page',2)
        def walk(item):
            yield item
            for child in item.childItems():yield from walk(child)
        for _ in range(20):self.app.processEvents();time.sleep(.01)
        flow=next(i for i in walk(w.contentItem()) if i.property('flowKey')=='search')
        scroll=w.findChild(QQuickItem,'searchSettingsScroll').property('contentItem')
        y=flow.mapToItem(scroll.property('contentItem'),QPointF(0,0)).y()
        scroll.setProperty('contentY',max(0,y-40));self.app.processEvents()
        cards=[i for i in walk(flow) if i.property('dropOffset') is not None]
        start=cards[0].mapToScene(QPointF(70,65)).toPoint()
        QTest.mousePress(w,Qt.LeftButton,Qt.NoModifier,start)
        for step in range(1,15):QTest.mouseMove(w,start+QPoint(step*15,0),15)
        QTest.mouseRelease(w,Qt.LeftButton,Qt.NoModifier,start+QPoint(210,0))
        self.pump(lambda:self.b.flows['search'][0]['method']=='SauceNAO')
        self.assertTrue(self.b.saveSettings())
    def test_current_session_scope_does_not_show_previous_batch(self):
        p=self.image();row=dict(id='old',original_path=str(p),current_path=str(p),is_unmatched=True,session_id='old')
        self.b.raw=[row];self.b.current_session_id='new'
        self.b.setFilters('All results','This session',False)
        self.pump(lambda:'0 shown' in self.b.summary)
        self.assertEqual(self.b.model.rowCount(),0)

    def history_rows(self, count=15):
        rows=[]
        for i in range(count):
            p=self.image(f'{i}.png','blue' if i>=8 else 'red')
            rows.append(dict(id=str(i), original_path=str(p), current_path=str(p), is_unmatched=True,
                recorded_at=1000+i, session_id='test', match_url=f'https://example.invalid/{i}'))
        self.b.raw=rows;self.b.lookup={r['id']:r for r in rows}
        return rows
    def test_history_pages_sort_before_slicing_and_reset_on_filter_change(self):
        self.history_rows();self.b._request_view();self.pump(lambda:self.b.historyTotal==15)
        self.assertEqual(self.b.historyPages,3);self.assertEqual(self.b.model.rows[0]['id'],'14')
        self.b.changeHistoryPage(1);self.pump(lambda:self.b.model.rows[0]['id']=='8')
        self.assertEqual(self.b.model.rowCount(),6)
        self.b.changeHistoryPage(1);self.pump(lambda:self.b.model.rows[0]['id']=='2')
        self.assertEqual(self.b.model.rowCount(),3)
        self.b.setHistoryOrder('Oldest first');self.pump(lambda:self.b.model.rows[0]['id']=='0')
        self.assertEqual(self.b.historyPage,1)
        self.b.changeHistoryPage(1);self.pump(lambda:self.b.model.rows[0]['id']=='6')
        self.b.setFilters('Moved','Overall history',False);self.pump(lambda:self.b.historyTotal==0)
        self.assertEqual(self.b.historyPage,1);self.assertEqual(self.b.historyPages,1)
    def test_history_dates_handle_ties_and_missing_legacy_dates(self):
        rows=[dict(id='old',recorded_at=100),dict(id='new',recorded_at=200),dict(id='legacy'),dict(id='tie',recorded_at=200),dict(id='bad',recorded_at='nan')]
        self.assertEqual([r['id'] for r in q.ordered_history(rows)],['tie','new','old','bad','legacy'])
        self.assertEqual([r['id'] for r in q.ordered_history(rows,False)],['old','new','tie','legacy','bad'])
    def test_history_next_button_keeps_comparison_thumbnails_and_actions_correct(self):
        rows=self.history_rows(13)
        for row in rows:row['_match_preview_path']=str(self.source/('0.png' if int(row['id'])>=8 else '12.png'))
        self.e,self.p=q.create_engine(self.b,'App.qml');w=self.e.rootObjects()[0];w.setProperty('page',1)
        self.b._request_view();self.pump(lambda:self.b.historyTotal==13)
        w.setPosition(20,20)
        self.assertTrue(QTest.qWaitForWindowExposed(w,3000))
        QTest.qWait(50)  # Let the first native frame and layout settle before input.
        button=w.findChild(QQuickItem,'historyNext')
        self.assertIsNotNone(button)
        point=button.mapToScene(QPointF(button.width()/2,button.height()/2)).toPoint()
        QTest.mouseClick(w,Qt.LeftButton,Qt.NoModifier,point)
        self.pump(lambda:self.b.model.rows and self.b.model.rows[0]['id']=='6')
        def walk(item):
            yield item
            for child in item.childItems():yield from walk(child)
        def thumb(name):return next((i for i in walk(w.contentItem()) if i.objectName()==name and i.property('recordId')=='6'),None)
        self.pump(lambda:thumb('localThumb') is not None and thumb('localThumb').property('ready') and thumb('matchedThumb').property('ready'))
        self.app.processEvents();capture=w.grabWindow()
        for name,expected in [('localThumb','red'),('matchedThumb','blue')]:
            item=thumb(name);point=item.mapToScene(QPointF(66,66))
            color=capture.pixelColor(round(point.x()*capture.width()/w.width()),round(point.y()*capture.height()/w.height()))
            self.assertGreater(color.red(),color.blue()) if expected=='red' else self.assertGreater(color.blue(),color.red())
            with patch.object(QDesktopServices,'openUrl',return_value=True) as opened:
                QTest.mouseClick(w,Qt.LeftButton,Qt.NoModifier,point.toPoint())
                self.assertTrue(opened.called)
                url=opened.call_args.args[0]
                self.assertEqual(Path(url.toLocalFile()),Path(rows[6]['current_path'])) if expected=='red' else self.assertEqual(url.toString(),rows[6]['match_url'])

    def test_manual_unmatched_updates_previous_locations(self):
        p=self.image();row=dict(id='one',original_path=str(p),current_path=str(p),filename=p.name,is_unmatched=True,session_id='s')
        self.b.store.record(row);self.b._history_delta([row],[])
        self.b.historyAction('one','unmatched');self.pump(lambda:not self.b.editing)
        target=Path(self.b.store.rows['one']['current_path']);self.assertTrue(target.exists());self.assertFalse(p.exists())
        self.b.historyAction('one','undo');self.pump(lambda:not self.b.editing)
        self.assertTrue(p.exists());self.assertFalse(target.exists())

    def test_undo_collision_retains_history_and_does_not_offer_a_second_undo(self):
        original=self.image();moved=self.library/'Series'/'image.png';original.rename(moved)
        self.image(color='blue')
        row=dict(id='undo-collision',original_path=str(original),current_path=str(moved),filename='image.png',is_unmatched=False,session_id='s')
        self.b.store.record(row);self.b._history_delta([row],[])
        self.b.historyAction(row['id'],'undo');self.pump(lambda:not self.b.editing)
        restored=self.b.store.rows[row['id']]
        self.assertNotEqual(restored['current_path'],str(original));self.assertTrue(Path(restored['current_path']).exists())
        with Image.open(original) as image:self.assertEqual(image.getpixel((0,0)),(0,0,255))
        display=q.model_row(restored,0)
        self.assertFalse(display['canUndo']);self.assertTrue(display['canUnmatched'])
        selected,counts,_=q.history_view([restored])
        self.assertEqual(len(selected),1);self.assertEqual(counts['Needs attention'],1);self.assertEqual(counts['Moved'],0)

    def test_repeated_history_edits_with_live_thumbnail_delegates(self):
        self.e,self.p=q.create_engine(self.b,'App.qml');w=self.e.rootObjects()[0];w.setProperty('page',1)
        for index in range(12):
            image=self.image('edit-'+str(index)+'.png')
            row=dict(id='edit-'+str(index),original_path=str(image),current_path=str(image),filename=image.name,is_unmatched=True,session_id='s')
            self.b.store.record(row);self.b._history_delta([row],[])
            self.pump(lambda:self.b.model.rowCount()==min(index+1,6))
            for action in ('tags','unmatched','undo'):
                self.b.historyAction(row['id'],action);self.pump(lambda:not self.b.editing)
            self.pump(lambda:self.b.model.rowCount()==min(index+1,6))
            self.assertTrue(image.exists())
        self.assertFalse(w.grabWindow().isNull())

if __name__=='__main__':unittest.main()


