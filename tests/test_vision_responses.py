"""Vision reasoning can exhaust generation before a final JSON answer exists."""
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import providers
from PIL import Image

class Harness(providers.ProviderMixin):
    stop_requested=skip_requested=False
    switch_skip_on_web_fail=0
    def __init__(self): self.logs=[];self._last_vision_context_provider='serpapi'
    def _fetch_vision_context(self,path): return 'Series: Sousou no Frieren; characters: Frieren and Himmel.',1
    def _read_widget(self,value): return value
    def log(self,message,**kwargs): self.logs.append((message,kwargs))

class VisionResponseTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.image=Path(self.temp.name)/'image.png'
        Image.new('RGB',(16,16),'red').save(self.image);self.h=Harness()
    def tearDown(self):self.temp.cleanup()
    @staticmethod
    def response(content,finish='stop',reasoning=None):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,message=SimpleNamespace(content=content,reasoning_content=reasoning))])
    def call(self,*responses):
        create=Mock(side_effect=responses);client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        return self.h.search_local_vision_engine(str(self.image),client,'small-model'),create
    def valid(self):return json.dumps(dict(artist='',copyright='sousou_no_frieren',character='frieren himmel',general='walking',meta=''))
    def test_reasoning_only_truncation_retries_and_preserves_actual_context(self):
        results,create=self.call(self.response(None,'length','private analysis'),self.response(self.valid()))
        self.assertEqual([c.kwargs['max_tokens'] for c in create.call_args_list],[2048,4096])
        self.assertEqual(results[0]['tag_dict_override']['character'],'frieren himmel')
        self.assertEqual(self.h._vision_last_outcome,'resolved');self.assertEqual(self.h._last_vision_error,'')
        logs='\n'.join(m for m,k in self.h.logs)
        self.assertIn('[Vision web hints]',logs);self.assertIn('Frieren and Himmel',logs);self.assertIn('separate_reasoning=True',logs)
        self.assertNotIn('private analysis',logs)
        for call in create.call_args_list:
            self.assertIn('Frieren and Himmel',call.kwargs['messages'][1]['content'][0]['text'])
        self.assertEqual(results[0]['tag_dict_override']['_web_context_provider'],'serpapi')
    def test_repeated_truncation_is_unavailable_not_a_definitive_no_match(self):
        results,create=self.call(self.response(self.valid(),'length'),self.response(None,'length','private'))
        self.assertEqual(results,[]);self.assertEqual(create.call_count,2)
        self.assertEqual(self.h._vision_last_outcome,'unavailable');self.assertIn('cut off',self.h._last_vision_error)
    def test_empty_answers_without_choices_do_not_raise_strip_error(self):
        results,create=self.call(SimpleNamespace(choices=[]),self.response(''))
        self.assertEqual(results,[]);self.assertIn('no final tag JSON',self.h._last_vision_error)
    def test_reasoning_override_survives_truncation_retry(self):
        self.h._run_reasoning_requests={'vision':{'reasoning_effort':'none'}}
        results,create=self.call(self.response(None,'length'),self.response(self.valid()))
        self.assertTrue(results)
        self.assertTrue(all(c.kwargs['reasoning_effort']=='none' for c in create.call_args_list))

    def test_actual_server_usage_is_distinct_from_budget_and_missing_usage(self):
        response=self.response(self.valid());response.usage=SimpleNamespace(prompt_tokens=1200,completion_tokens=350,completion_tokens_details=SimpleNamespace(reasoning_tokens=200))
        self.call(response);logs='\n'.join(message for message,kwargs in self.h.logs)
        self.assertIn('output_budget=2048',logs);self.assertIn('server_generated_tokens=350',logs);self.assertIn('server_reasoning_tokens=200',logs)

    def test_valid_first_answer_needs_no_retry_and_logs_tag_summary(self):
        results,create=self.call(self.response(self.valid(),reasoning='private'))
        self.assertEqual(create.call_count,1);self.assertTrue(results)
        self.assertIn('[Vision tags]', '\n'.join(m for m,k in self.h.logs))
    def test_stop_prevents_retry(self):
        def stop(**kwargs):self.h.stop_requested=True;return self.response(None,'length')
        client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=Mock(side_effect=stop))))
        self.assertEqual(self.h.search_local_vision_engine(str(self.image),client,'small-model'),[])
        self.assertEqual(client.chat.completions.create.call_count,1)
    def test_malformed_final_json_has_no_retry_and_web_hints_still_logged(self):
        results,create=self.call(self.response('invalid json'))
        self.assertFalse(results);self.assertEqual(create.call_count,1);self.assertEqual(self.h._vision_last_outcome,'unavailable')
        self.assertTrue(self.h.logs[1][0].startswith('  [Vision web hints]'))
    def test_empty_categories_are_a_real_miss(self):
        results,create=self.call(self.response(json.dumps(dict(artist='',copyright='',character='',general='',meta=''))))
        self.assertFalse(results);self.assertEqual(self.h._vision_last_outcome,'miss')

if __name__=='__main__':unittest.main()
