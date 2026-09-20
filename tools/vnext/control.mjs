#!/usr/bin/env node
import postgres from 'postgres';
import {compareResults,releaseGate,GATE_VERSION} from '../../runtime/vnext/benchmark.mjs';

const command=process.argv[2];
if(!['status','index','benchmark-lexical','tick','worker-status'].includes(command)) {
  console.error('Usage: DATABASE_URL=... node tools/vnext/control.mjs status|index|benchmark-lexical|tick|worker-status');
  process.exit(2);
}
if(!process.env.DATABASE_URL) throw new Error('DATABASE_URL must be supplied through the environment');
const sql=postgres(process.env.DATABASE_URL,{max:1,prepare:false,connect_timeout:10,idle_timeout:10});
try {
  const [{generation}]=await sql`select active_generation_id generation from archive_private.corpus_state`;
  if(command==='status') console.log(JSON.stringify((await sql`select archive_vnext.status(${generation}) result`)[0].result,null,2));
  if(command==='index') {
    await sql`select archive_vnext.begin_build(${generation})`;
    for(const kind of ['turn','history']) {
      let after=0,done=false;
      while(!done) {
        const [{result}]=kind==='turn' ? await sql`select archive_vnext.index_turn_batch(${generation},${after},250) result` : await sql`select archive_vnext.index_history_batch(${generation},${after},250) result`;
        after=result.after_id; done=result.done;
        console.log(JSON.stringify({kind,after,inserted:result.inserted,done}));
      }
    }
    console.log(JSON.stringify((await sql`select archive_vnext.finish_build(${generation}) result`)[0].result));
  }
  if(command==='tick') console.log(JSON.stringify((await sql`select crowley_v2.tick_intents(20) emitted`)[0]));
  if(command==='worker-status') console.log(JSON.stringify(await sql`select state,count(*)::int count from crowley_v2.workers group by state`));
  if(command==='benchmark-lexical') {
    // This deliberately cannot pass the end-to-end promotion gate.
    const [suite]=await sql`select * from archive_vnext.benchmark_suites where generation_id=${generation} order by frozen_at desc limit 1`;
    if(!suite) throw new Error('Freeze a private, source-checked suite before running');
    const [{id}]=await sql`insert into archive_vnext.benchmark_runs(suite_id,candidate_revision,baseline_revision,kind,budgets) values(${suite.id},${process.env.CANDIDATE_REVISION??'working-tree'},'v3-connector-search','lexical_component','{"queries":1,"results":8,"semantic":false}') returning id`;
    const results=[];
    for(const c of suite.cases) for(const arm of ['v3','vnext']) {
      const start=performance.now();let result,error;
      try {
        const rows=arm==='v3' ? await sql`select archive_private.connector_search_v1(array[${c.query}],${c.intent==='exact'?c.query:null},null,null,false,8) result` : await sql`select archive_vnext.search(${c.query},null,${generation},${c.role_scope},null,null,8,false,${c.intent==='exact'}) result`;
        result=rows[0].result;
      } catch { result={matches:[],error:'retrieval_failed'};error='retrieval_failed'; }
      const elapsed=performance.now()-start;
      await sql`insert into archive_vnext.benchmark_results(run_id,case_key,arm,result,elapsed_ms) values(${id},${c.case_key},${arm},${sql.json(result)},${elapsed})`;
      results.push({case_key:c.case_key,arm,source_uris:(result.matches??[]).map(x=>x.source_uri),error});
    }
    const comparison=compareResults(suite.cases,results);
    const gate=releaseGate({contract_version:GATE_VERSION,kind:'lexical_component',case_count:suite.cases.length,heldout_count:0});
    const metrics={...comparison,gate,warning:'Lexical component regression only; not the v3 end-to-end retrieval or frontier-model benchmark.'};
    await sql`update archive_vnext.benchmark_runs set status='completed',completed_at=now(),metrics=${sql.json(metrics)},gate_status='blocked' where id=${id}`;
    const {cases,...aggregate}=comparison;
    console.log(JSON.stringify({run_id:id,...aggregate,gate},null,2));
  }
} finally {await sql.end();}
