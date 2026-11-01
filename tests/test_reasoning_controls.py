"""Only server-advertised reasoning controls are sent; defaults remain untouched."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import providers
from routing import reasoning_request_kwargs,check_reasoning_response

class ReasoningTests(unittest.TestCase):
    def test_defaults_make_no_capability_request(self):
        with patch.object(providers,'local_reasoning_capabilities') as query:
            result=providers.prepare_reasoning_requests('unused','m',{'vision':'Server default','routing':'Server default'})
        self.assertEqual(result,{'vision':{},'routing':{}});query.assert_not_called()
    def test_metadata_uses_loaded_alias_and_proxy_prefix(self):
        response=Mock();response.json.return_value={'models':[{'key':'canonical','loaded_instances':[{'id':'alias'}],'capabilities':{'reasoning':{'allowed_options':['off','on'],'default':'on'}}}]}
        session=Mock();session.get.return_value=response
        with patch.object(providers,'get_http_session',return_value=session):caps=providers.local_reasoning_capabilities('http://localhost/proxy/v1')
        self.assertEqual(caps['alias']['options'],['off','on'])
        self.assertEqual(session.get.call_args.args[0],'http://localhost/proxy/api/v1/models')
    def test_verified_modes_are_independent_and_unsupported_is_not_silently_dropped(self):
        with patch.object(providers,'local_reasoning_capabilities',return_value={'m':{'options':['off','on']}}):
            result=providers.prepare_reasoning_requests('unused','m',{'vision':'Off','routing':'On'})
            self.assertEqual(result,{'vision':{'reasoning_effort':'none'},'routing':{'reasoning_effort':'medium'}})
            with self.assertRaisesRegex(ValueError,'not advertised'):providers.prepare_reasoning_requests('unused','m',{'vision':'High'})
    def test_separate_models_use_their_own_capabilities(self):
        with patch.object(providers, 'local_reasoning_capabilities', return_value={'vision': {'options': ['off']}, 'text': {'options': ['low']}}) as query:
            result = providers.prepare_reasoning_requests('unused', {'vision': 'vision', 'routing': 'text'}, {'vision': 'Off', 'routing': 'Low'})
        self.assertEqual(result, {'vision': {'reasoning_effort': 'none'}, 'routing': {'reasoning_effort': 'low'}})
        query.assert_called_once()

    def test_warning_reports_ignored_off_without_logging_reasoning(self):
        logs=[];owner=SimpleNamespace(log=lambda message,**kwargs:logs.append(message),_run_reasoning_requests={'vision':{'reasoning_effort':'none'}})
        kwargs=reasoning_request_kwargs(owner,'vision')
        response=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(reasoning_content='private reasoning'))])
        check_reasoning_response(owner,'vision',response,kwargs)
        self.assertIn('server returned reasoning',' '.join(logs));self.assertNotIn('private reasoning',' '.join(logs))

if __name__=='__main__':unittest.main()
