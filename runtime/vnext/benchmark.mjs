export const GATE_VERSION='vnext-gate-1';
const mean=values=>values.reduce((a,b)=>a+b,0)/values.length;
export function recall(gold,returned,k=8) {
  if (!Array.isArray(gold) || !gold.length) return null;
  const expected=new Set(gold), hits=new Set(returned.slice(0,k));
  return [...expected].filter(x=>hits.has(x)).length/expected.size;
}
export function pairedInterval(deltas,iterations=5000) {
  if (!deltas.length) return [null,null];
  let seed=0x9e3779b9;
  const random=()=>{seed^=seed<<13;seed^=seed>>>17;seed^=seed<<5;return (seed>>>0)/4294967296;};
  const values=[];
  for(let i=0;i<iterations;i++) {
    let sum=0;
    for(let j=0;j<deltas.length;j++) sum+=deltas[Math.floor(random()*deltas.length)];
    values.push(sum/deltas.length);
  }
  values.sort((a,b)=>a-b);
  return [values[Math.floor(iterations*.025)],values[Math.floor(iterations*.975)]];
}
export function releaseGate(report) {
  const blocked=[],failed=[];
  const need=(ok,why)=>{if(ok!==true) blocked.push(why);};
  need(report.contract_version===GATE_VERSION,'contract_version');
  need(report.kind==='agentic_end_to_end','end_to_end_evaluation_required');
  need(report.case_count>=50,'minimum_50_real_cases');
  need(report.heldout_count>=20,'minimum_20_heldout_cases');
  for(const key of ['same_corpus','same_budgets','suite_frozen_before_tuning','gold_independently_checked','complete_embeddings','all_sources_reopenable','security_tests_pass','production_fingerprints_unchanged','all_categories_graded','cost_reported']) need(report[key],key);
  const numeric=['recall_gain','ci95_lower','safety_failures','candidate_error_rate','baseline_error_rate','candidate_p95_ms','baseline_p95_ms','worst_category_delta'];
  for(const key of numeric) need(Number.isFinite(report[key]),`missing_${key}`);
  if(blocked.length) return {status:'blocked',blocked,failed,production_route:'v3'};
  if(report.recall_gain<.05) failed.push('recall_gain_below_5_points');
  if(report.ci95_lower<=0) failed.push('gain_not_statistically_positive');
  if(report.worst_category_delta<-.05) failed.push('category_regression');
  if(report.safety_failures!==0) failed.push('safety_regression');
  if(report.candidate_error_rate>report.baseline_error_rate) failed.push('error_regression');
  if(report.baseline_p95_ms<=0 || report.candidate_p95_ms>1.25*report.baseline_p95_ms) failed.push('latency_regression');
  return {status:failed.length?'failed':'eligible_for_review',blocked,failed,production_route:'v3'};
}
export function compareResults(cases,results) {
  const scored=cases.map(c=>{
    const arms=results.filter(r=>r.case_key===c.case_key);
    const baseline=arms.find(r=>r.arm==='v3'),candidate=arms.find(r=>r.arm==='vnext');
    if(!baseline || !candidate) return {case_key:c.case_key,error:'missing_arm'};
    return {case_key:c.case_key,category:c.category,split:c.split,
      baseline:recall(c.expected_source_uris,baseline.source_uris),candidate:recall(c.expected_source_uris,candidate.source_uris),
      forbidden_candidate:(c.forbidden_source_uris??[]).filter(uri=>candidate.source_uris.includes(uri)),
      errors:[baseline.error,candidate.error].filter(Boolean)};
  });
  const paired=scored.filter(r=>Number.isFinite(r.baseline)&&Number.isFinite(r.candidate));
  return {case_count:cases.length,paired_count:paired.length,
    baseline_recall:paired.length?mean(paired.map(r=>r.baseline)):null,
    candidate_recall:paired.length?mean(paired.map(r=>r.candidate)):null,
    ci95:pairedInterval(paired.map(r=>r.candidate-r.baseline)),cases:scored};
}
