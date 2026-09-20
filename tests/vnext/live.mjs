// Explicitly opt in with a shadow URL and dedicated expiring reader credential.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
const endpoint=process.env.ARCHIVE_QA_ENDPOINT, token=(readFileSync(process.env.ARCHIVE_QA_TOKEN_FILE,'utf8')).trim();
const generation=process.env.ARCHIVE_QA_GENERATION;
if(!/^https:\/\/[^/]+\.supabase\.co\/functions\/v1\/crowley-archive-vnext$/.test(endpoint??'')||!generation) throw Error('shadow endpoint and explicit generation required');
let id=0;
async function request(body,credential=token,method='POST') {
  const r=await fetch(endpoint,{method,headers:{Authorization:'Bearer '+credential,'Content-Type':'application/json'},body:method==='POST'?JSON.stringify(body):undefined,signal:AbortSignal.timeout(30000)});
  return {status:r.status,body:await r.json()};
}
async function call(name,args={}) {
  return request({jsonrpc:'2.0',id:++id,method:'tools/call',params:{name,arguments:{generation,...args}}});
}
function value(r){assert.equal(r.status,200);assert.equal(r.body.error,undefined);return JSON.parse(r.body.result.content[0].text);}
const checks=[];
async function check(name,fn){await fn();checks.push(name);console.log('PASS '+name);}
await check('unauthenticated denied',async()=>assert.equal((await request({operation:'status'},'invalid')).status,401));
await check('reader cannot embed',async()=>assert.equal((await request({operation:'embed_next'})).status,401));
await check('GET cannot disclose',async()=>assert.equal((await request({},token,'GET')).status,405));
await check('MCP cannot expose writer',async()=>assert.ok((await call('embed_next')).body.error));
await check('status keeps v3 route',async()=>assert.equal(value(await call('archive_status')).production_route,'v3'));
await check('unsupported scope fails explicitly',async()=>assert.equal((await call('search_text',{query:'test',conversation_id:'wrong'})).body.error.message,'unsupported_argument'));
await check('impossible date rejected',async()=>assert.equal((await call('search_text',{query:'test',from:'2026-02-30T00:00:00Z'})).body.error.message,'invalid_from'));
await check('incomplete cursor rejected',async()=>assert.equal((await call('browse_time',{from:'2025-01-01T00:00:00Z',to:'2026-01-01T00:00:00Z',after_time:'2025-01-01T12:00:00Z'})).body.error.message,'incomplete_cursor'));
if(process.env.ARCHIVE_QA_SYNTHETIC==='1') {
  await check('partial semantics disclosed',async()=>{const x=value(await call('search_context',{query:'amber harbor'}));assert.equal(x.retrieval_mode,'lexical');assert.ok(x.degraded_reasons.includes('semantic_index_incomplete'));});
  await check('assistant words not attributed to user',async()=>assert.equal(value(await call('search_text',{query:'violet lighthouse',role:'user'})).matches.length,0));
  await check('assistant proposal discoverable',async()=>assert.ok(value(await call('search_text',{query:'violet lighthouse',role:'assistant'})).matches.length));
  await check('literal time filter honored',async()=>assert.equal(value(await call('search_text',{query:'amber harbor',from:'2025-02-01T00:00:00Z'})).matches.length,0));
  await check('timeline branches opt in',async()=>{
    const args={from:'2025-01-01T00:00:00Z',to:'2025-01-03T00:00:00Z'};
    const active=value(await call('browse_time',args));assert.equal(active.matches.length,2);assert.ok(active.matches.every(x=>x.source_state==='active_path'));
    const all=value(await call('browse_time',{...args,include_inactive:true}));assert.equal(all.matches.length,3);
  });
  await check('open returns correction navigation and modality limit',async()=>{
    const x=value(await call('open_context',{source_uri:'archive://conversation/vnext-fixture/turn/0',length:20000}));
    assert.ok(x.neighbors.some(n=>n.source_uri==='archive://conversation/vnext-fixture/turn/1'));
    assert.equal(x.attachment_bytes_available,false);
  });
}
console.log(JSON.stringify({checks:checks.length,passed:checks.length,production_route:'v3'}));
