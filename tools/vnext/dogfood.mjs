// Private input/output only. This exercises the deployed MCP surface; it is not
// a model adapter, an answer-quality judge, or a promotion benchmark.
import {readFile,writeFile,mkdir} from 'node:fs/promises';
import {resolve,dirname} from 'node:path';
import {createHash} from 'node:crypto';

const [suitePath,outputPath,...flags]=process.argv.slice(2);
if(!suitePath||!outputPath) throw Error('usage: dogfood.mjs PRIVATE_SUITE PRIVATE_OUTPUT [--split=development|heldout] [--cases=lane-01,lane-02]');
const endpoint=process.env.ARCHIVE_QA_ENDPOINT;
const tokenFile=process.env.ARCHIVE_QA_TOKEN_FILE;
if(!endpoint||!tokenFile||!/^https:\/\/[^/]+\.supabase\.co\/functions\/v1\/crowley-archive-vnext$/.test(endpoint)) throw Error('dedicated shadow endpoint and token file required');
const output=resolve(outputPath),privateRoot=resolve('private')+'/';
if(!output.startsWith(privateRoot)||!resolve(suitePath).startsWith(privateRoot)) throw Error('suite and output must stay in ignored private/');
if(output===resolve(suitePath)) throw Error('output cannot replace the frozen suite');
const token=(await readFile(tokenFile,'utf8')).trim();
const suite=JSON.parse(await readFile(suitePath,'utf8'));
const {suite_hash,...frozen}=suite;
if(createHash('sha256').update(JSON.stringify(frozen)).digest('hex')!==suite_hash) throw Error('frozen suite hash mismatch');
const split=flags.find(f=>f.startsWith('--split='))?.slice(8);
const ids=flags.find(f=>f.startsWith('--cases='))?.slice(8).split(',');
let sequence=0;
async function rpc(method,params) {
  const id=++sequence,start=performance.now();
  const response=await fetch(endpoint,{method:'POST',headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({jsonrpc:'2.0',id,method,params}),signal:AbortSignal.timeout(30000)});
  const body=await response.json();
  if(!response.ok||body.id!==id||body.error||body.result?.isError) throw Error('mcp_failure:'+response.status+':'+(body.error?.message??body.error??'invalid_response'));
  const result=body.result?.content ? JSON.parse(body.result.content.find(c=>c.type==='text').text) : body.result;
  return {result,elapsed_ms:performance.now()-start};
}
const init=await rpc('initialize',{protocolVersion:'2025-03-26',capabilities:{},clientInfo:{name:'crowley-lane-qa',version:'1'}});
const discovery=await rpc('tools/list',{});
const status=await rpc('tools/call',{name:'archive_status',arguments:{}});
const generation=status.result.generation_id;
const report={suite_version:suite.version,suite_hash:suite.suite_hash,kind:'mcp_retrieval_dogfood',generation,started_at:new Date().toISOString(),init,tools:discovery.result.tools.map(t=>t.name),initial_status:status.result,cases:[]};
await mkdir(dirname(output),{recursive:true});
const checkpoint=()=>writeFile(output,JSON.stringify(report,null,2),{mode:0o600});
await writeFile(output,JSON.stringify(report,null,2),{mode:0o600,flag:'wx'});
for(const c of suite.cases.filter(c=>(!split||c.split===split)&&(!ids||ids.includes(c.case_key)))) {
  const row={case_key:c.case_key,category:c.category,split:c.split,searches:[],opened:[],errors:[]};
  const ranked=new Map();
  for(const query of c.formulations.slice(0,suite.budgets.max_formulations)) {
    try {
      const response=await rpc('tools/call',{name:'search_context',arguments:{query,role:c.role_scope,generation,limit:suite.budgets.result_limit}});
      row.searches.push({query,...response});
      for(const [i,m] of response.result.matches.entries()) {
        const prior=ranked.get(m.source_uri);
        ranked.set(m.source_uri,{match:m,score:(prior?.score??0)+1/(60+i+1)});
      }
    } catch(e){row.errors.push({operation:'search_context',error:String(e.message)});}
  }
  const candidates=[...ranked.values()].sort((a,b)=>b.score-a.score).map(x=>x.match);
  row.source_uris=candidates.slice(0,8).map(x=>x.source_uri);
  let remaining=suite.budgets.open_character_budget;
  // Selection is independent of gold IDs: the top three fused candidates.
  for(const candidate of candidates.slice(0,3)) {
    if(remaining<=0) break;
    let offset=0;const pages=[];
    try {
      do {
        const response=await rpc('tools/call',{name:'open_context',arguments:{source_uri:candidate.source_uri,generation,offset,length:Math.min(12000,remaining)}});
        pages.push(response);remaining-=Array.from(response.result.content).length;
        offset=response.result.next_offset;
      } while(offset!==null&&remaining>0);
      const content=pages.map(p=>p.result.content).join('');
      row.opened.push({source_uri:candidate.source_uri,pages,complete:offset===null,roundtrip:offset===null?createHash('sha256').update(content).digest('hex')===candidate.content_hash:null});
    } catch(e){row.errors.push({operation:'open_context',error:String(e.message)});}
  }
  row.gold_recall_at8=c.expected_source_uris.filter(u=>row.source_uris.includes(u)).length/c.expected_source_uris.length;
  row.full_open_gold=c.expected_source_uris.every(u=>row.opened.some(o=>o.source_uri===u&&o.complete));
  row.answer_review='required';
  report.cases.push(row);await checkpoint();
  console.log(JSON.stringify({case_key:row.case_key,returned:row.source_uris.length,opened:row.opened.length,errors:row.errors.length,recall:row.gold_recall_at8}));
}
report.completed_at=new Date().toISOString();
report.summary={cases:report.cases.length,errors:report.cases.filter(r=>r.errors.length).length,gold_recalled:report.cases.filter(r=>r.gold_recall_at8===1).length,complete_opens:report.cases.reduce((s,r)=>s+r.opened.filter(o=>o.complete).length,0),roundtrip_failures:report.cases.flatMap(r=>r.opened).filter(o=>o.roundtrip===false).length,answer_quality:'not_automatically_graded',promotion:'blocked'};
await checkpoint();console.log(JSON.stringify(report.summary));
