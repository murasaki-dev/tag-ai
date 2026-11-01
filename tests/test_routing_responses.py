"""Local servers may return no final answer when generation runs out of room."""
import sys
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import routing


class RoutingResponseTests(unittest.TestCase):
    def setUp(self):
        self.plan = routing.routing_plan({'copyright': 'frieren', 'character': 'fern'}, ['Frieren', 'Frieren/Fern'])
        self.harness = SimpleNamespace(log=lambda message, **kwargs: None, stop_requested=False, skip_requested=False)

    @staticmethod
    def response(content, finish='stop', reasoning=None):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish, message=SimpleNamespace(content=content, reasoning_content=reasoning))])

    def call(self, *responses):
        create = Mock(side_effect=responses)
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
        result = routing.RoutingMixin.route_with_model(self.harness, client, 'small-local-model', self.plan)
        return result, create

    def test_detailed_context_summarizes_actual_payload_without_hidden_reasoning(self):
        self.plan=routing.routing_plan({'copyright':'some_series','character':'a_character','general':'red coat','_identity_source':'vision','_web_context':'Web series hint '+('x'*600),'_web_context_provider':'serpapi'},['Destination','Other'],{},'Flexible')
        logged=[];self.harness.log=lambda message,**kwargs:logged.append((message,kwargs))
        result,create=self.call(self.response('{"candidate_id":0}',reasoning='hidden private analysis'))
        text='\n'.join(message for message,kwargs in logged)
        self.assertIn('mode=Flexible',text);self.assertIn('some_series',text);self.assertIn('a_character',text)
        self.assertIn('provider=serpapi',text);self.assertIn('2 candidates',text);self.assertIn('result=Model could not confidently choose a supported folder',text)
        self.assertNotIn('hidden private analysis',text);self.assertNotIn('x'*300,text)
        self.assertTrue(all(kwargs.get('full_only') for message,kwargs in logged))
        payload=json.loads(create.call_args.kwargs['messages'][1]['content'])
        self.assertEqual(payload['identity_evidence'],self.plan['evidence'])

    def test_routing_reasoning_override_is_sent(self):
        self.harness._run_reasoning_requests={'routing':{'reasoning_effort':'medium'}}
        result,create=self.call(self.response('{"candidate_id":1}'))
        self.assertEqual(create.call_args.kwargs['reasoning_effort'],'medium')
        self.assertTrue(result[0])

    def test_empty_reasoning_only_answer_retries_once_with_more_room(self):
        result, create = self.call(self.response('', 'length', 'hidden reasoning'), self.response('{"candidate_id":2}'))
        self.assertEqual(result[0], 'Frieren/Fern')
        self.assertEqual(create.call_count, 2)
        self.assertEqual([call.kwargs['max_tokens'] for call in create.call_args_list], [1024, 2048])
        self.assertTrue(self.harness._last_routing_metadata['attempts'][0]['reasoning_present'])
        self.assertNotIn('hidden reasoning', repr(self.harness._last_routing_metadata))

    def test_repeated_empty_answers_have_specific_reason_and_no_third_request(self):
        result, create = self.call(self.response(None), self.response(''))
        self.assertIsNone(result[0])
        self.assertIn('empty routing answer', result[1])
        self.assertEqual(create.call_count, 2)

    def test_configurable_output_budget_and_reasoning_exhaustion_diagnostic(self):
        self.harness._run_routing_output_tokens = 4096
        result, create = self.call(self.response('', 'length', 'private reasoning'), self.response('', 'length', 'private reasoning'))
        self.assertEqual([call.kwargs['max_tokens'] for call in create.call_args_list], [4096, 8192])
        self.assertIn('reasoning returned but no final JSON', result[1])
        self.assertNotIn('private reasoning', repr(self.harness._last_routing_metadata))

    def test_repeated_truncation_is_not_accepted_even_if_partial_answer_looks_valid(self):
        result, create = self.call(self.response('{"candidate_id":2}', 'length'), self.response('{"candidate_id":2}', 'length'))
        self.assertIsNone(result[0]); self.assertIn('output limit', result[1])
        self.assertEqual(create.call_count, 2)

    def test_one_json_fence_is_allowed_but_prose_and_reasoning_are_not_extracted(self):
        result, create = self.call(self.response('```json\n{"candidate_id":2}\n```'))
        self.assertEqual(result[0], 'Frieren/Fern'); self.assertEqual(create.call_count, 1)
        for content in ('I choose {"candidate_id":2}', '<think>reason</think>{"candidate_id":2}',
                        '{"candidate_id":2}\n{"candidate_id":1}', '```json\n{"candidate_id":2}\n```\nMore text'):
            self.assertIsNone(routing.validate_routing_response(content, self.plan)[0])

    def test_invalid_or_unsupported_choices_and_abstention_do_not_get_retried(self):
        for content in ('not JSON', '{"candidate_id":999}', '{"candidate_id":"2"}', '{"folder":"arbitrary/path"}', '{"candidate_id":0}'):
            result, create = self.call(self.response(content))
            self.assertIsNone(result[0]); self.assertEqual(create.call_count, 1)

    def test_stop_or_skip_suppresses_empty_answer_retry(self):
        for flag in ('stop_requested', 'skip_requested'):
            setattr(self.harness, flag, True)
            result, create = self.call(self.response('', 'length'))
            self.assertIsNone(result[0]); self.assertEqual(create.call_count, 1)
            setattr(self.harness, flag, False)


class MatchingModeTests(unittest.TestCase):
    def test_flexible_offers_existing_folders_without_a_literal_name_match(self):
        tags = {'copyright': 'unlisted_series', 'character': 'known_person'}
        folders = ['Broader category', 'Other', 'Broader category/known_person']
        exact = routing.routing_plan(tags, folders)
        flexible = routing.routing_plan(tags, folders, matching_mode='Flexible')
        self.assertFalse(exact['candidates'])
        self.assertEqual({c['folder'] for c in flexible['candidates']}, set(folders))
        self.assertIsNone(flexible['direct'])
        self.assertEqual(routing.validate_routing_response('{"candidate_id":1}', flexible)[0], folders[0])
        self.assertIsNone(routing.validate_routing_response('{"candidate_id":999}', flexible)[0])

    def test_modes_have_distinct_general_prompts_without_named_category_exceptions(self):
        tags = {'copyright': 'unlisted_series', 'character': 'known_person'}
        exact = routing.routing_messages(routing.routing_plan(tags, ['Broad category']))[0]['content']
        flexible = routing.routing_messages(routing.routing_plan(tags, ['Broad category'], matching_mode='Flexible'))[0]['content']
        self.assertNotEqual(exact, flexible)
        self.assertIn('general knowledge', flexible)
        self.assertIn('Do not require literal name equality', flexible)
        self.assertIn('choose 0', flexible)
        self.assertNotIn('unlisted_series', flexible)
        self.assertNotIn('Broad category', flexible)

    def test_exact_identity_and_aliases_stay_preferred_in_both_modes(self):
        tags = {'copyright': 'unlisted_series', 'character': 'known_person'}
        folders = ['Series', 'Series/Person', 'Broad category']
        aliases = {'Series': {'series': ['unlisted_series']}, 'Series/Person': {'character': ['known_person']}}
        for mode in routing.MATCHING_MODES:
            plan = routing.routing_plan(tags, folders, aliases, mode)
            self.assertEqual(plan['direct'], 'Series/Person')
            self.assertEqual(plan['matching_mode'], mode)

    def test_flexible_keeps_supported_identity_out_of_unrelated_folders(self):
        tags = {'copyright': 'vocaloid', 'character': 'hatsune_miku', '_identity_source': 'vision'}
        plan = routing.routing_plan(tags, ['Atelier', 'Vocaloid', 'Other'], matching_mode='Flexible')
        self.assertEqual([c['folder'] for c in plan['candidates']], ['Vocaloid'])
        self.assertIsNone(plan['direct'])
        self.assertIsNone(routing.validate_routing_response('{"candidate_id":2}', plan)[0])

    def test_web_hints_recover_incomplete_generated_series_without_special_cases(self):
        tags = {'copyright': 'hatsune_miku', 'character': 'hatsune_miku', '_identity_source': 'vision', '_web_context': 'Hatsune Miku artwork from Vocaloid'}
        plan = routing.routing_plan(tags, ['Atelier', 'Vocaloid', 'Other'], matching_mode='Flexible')
        self.assertEqual([c['folder'] for c in plan['candidates']], ['Vocaloid'])
        self.assertEqual(plan['evidence']['series'], ['hatsune_miku'])
        self.assertIsNone(plan['direct'])

    def test_empty_evidence_does_not_invite_a_random_flexible_destination(self):
        plan = routing.routing_plan({}, ['Any category'], matching_mode='Flexible')
        self.assertFalse(plan['candidates'])
        invalid = routing.routing_plan({'copyright': 'unlisted'}, ['Any category'], matching_mode='bad-setting')
        self.assertEqual(invalid['matching_mode'], 'Exact')
        self.assertFalse(invalid['candidates'])


if __name__ == '__main__':
    unittest.main()
