import test from 'node:test';
import assert from 'node:assert/strict';
import {recall,pairedInterval,releaseGate} from '../../runtime/vnext/benchmark.mjs';
const valid={contract_version:'vnext-gate-1',kind:'agentic_end_to_end',case_count:50,heldout_count:20,same_corpus:true,same_budgets:true,suite_frozen_before_tuning:true,gold_independently_checked:true,complete_embeddings:true,all_sources_reopenable:true,security_tests_pass:true,production_fingerprints_unchanged:true,all_categories_graded:true,cost_reported:true,recall_gain:.1,ci95_lower:.04,safety_failures:0,candidate_error_rate:0,baseline_error_rate:0,candidate_p95_ms:100,baseline_p95_ms:100,worst_category_delta:0};
test('recall uses unique gold and capped results',()=>{assert.equal(recall(['a','b','b'],['a','a','c']),.5);assert.equal(recall([],[]),null);});
test('paired intervals are deterministic',()=>{assert.deepEqual(pairedInterval([.1,.1]),[.1,.1]);});
test('successful metrics permit review, never automatic cutover',()=>{assert.equal(releaseGate(valid).status,'eligible_for_review');assert.equal(releaseGate(valid).production_route,'v3');});
test('missing, partial and component results cannot pass',()=>{
  for(const report of [{},{...valid,kind:'lexical_component'},{...valid,heldout_count:0},{...valid,complete_embeddings:false},{...valid,same_budgets:false},{...valid,ci95_lower:NaN}]) assert.equal(releaseGate(report).status,'blocked');
});
test('quality, safety, category and latency regressions block promotion',()=>{
  for(const override of [{recall_gain:.01},{ci95_lower:0},{safety_failures:1},{candidate_p95_ms:130},{worst_category_delta:-.1},{candidate_error_rate:.1}]) assert.equal(releaseGate({...valid,...override}).status,'failed');
});
