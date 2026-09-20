import test from 'node:test';
import assert from 'node:assert/strict';
import {embeddingBatch,validateOperation,validateVector,boundedJson,EMBEDDING_PREFIX,READ_TOOLS,retrievalHealth} from '../../supabase/functions/crowley-archive-vnext/core.mjs';

const tokenizer = {encode:text=>({ids:Array(Array.from(text).length+2).fill(1)})};
test('Unicode embedding coverage is contiguous, bounded and resumable',()=>{
  const text='🦊 café 中文 — '.repeat(600);
  let covered=0, calls=0;
  while (covered<Array.from(text).length) {
    const batch=embeddingBatch(text,tokenizer,{covered,maxPieces:1});
    const p=batch.pieces[0];
    assert.ok(p.start_offset<=covered);
    assert.ok(p.end_offset>covered);
    assert.ok(p.token_count<=512);
    assert.equal(p.input,EMBEDDING_PREFIX+Array.from(text).slice(p.start_offset,p.end_offset).join(''));
    covered=batch.covered; calls++;
    assert.equal(batch.complete,covered===Array.from(text).length);
  }
  assert.ok(calls>1);
});
test('invalid embedding cursor fails, never loops',()=>{
  for(const covered of [-1,1.5,100]) assert.throws(()=>embeddingBatch('short',tokenizer,{covered}));
});
test('vectors require correct dimension, finite entries and unit norm',()=>{
  const good=[1,...Array(383).fill(0)];
  assert.deepEqual(validateVector(good),good);
  for(const bad of [[1],Array(384).fill(0),[NaN,...Array(383).fill(0)],[Infinity,...Array(383).fill(0)]]) assert.throws(()=>validateVector(bad));
});
test('search is bounded and does not permit arbitrary operations',()=>{
  assert.equal(validateOperation({operation:'search_context',query:'architecture'}).limit,8);
  for(const value of [null,{operation:'sql',query:'select 1'},{operation:'search_context',query:''},{operation:'search_context',query:'x',limit:999},{operation:'search_context',query:'x',include_inactive:'false'},{operation:'search_many',queries:Array(5).fill({query:'x'})}]) assert.throws(()=>validateOperation(value));
});
test('time ranges require timezone and complete pagination cursors',()=>{
  for(const value of [{operation:'browse_time',from:'2025-01-01',to:'2026-01-01'},{operation:'browse_time',from:'2026-01-01T00:00:00Z',to:'2025-01-01T00:00:00Z'},{operation:'browse_time',from:'2025-01-01T00:00:00Z',to:'2026-01-01T00:00:00Z',after_id:1}]) assert.throws(()=>validateOperation(value));
});
test('source URI validation rejects other protocols and malformed references',()=>{
  assert.equal(validateOperation({operation:'open_context',source_uri:'archive://conversation/example/turn/12'}).offset,0);
  for(const source_uri of ['https://example.com','archive://conversation/example/turn/-1','file:///secret']) assert.throws(()=>validateOperation({operation:'open_context',source_uri}));
});
test('stream body cap works without trusting content-length',async()=>{
  const request=(body)=>new Request('https://example.invalid',{method:'POST',body});
  assert.deepEqual(await boundedJson(request('{"ok":true}')),{ok:true});
  await assert.rejects(boundedJson(request('x'.repeat(100)),16),/body_too_large/);
  await assert.rejects(boundedJson(request('{')),/invalid_json/);
});

test('unsupported scope cannot be silently discarded',()=>{
  assert.throws(()=>validateOperation({operation:'search_context',query:'project',conversation_id:'another-thread'}),/unsupported_argument/);
  assert.throws(()=>validateOperation({operation:'search_many',queries:[{query:'project',generation:'another-generation'}]}),/invalid_query/);
  assert.throws(()=>validateOperation({operation:'search_many',queries:[null]}),/invalid_query/);
});
test('calendar dates and microsecond timeline cursors round trip',()=>{
  const base={operation:'browse_time',from:'2026-01-01T00:00:00Z',to:'2026-12-31T23:59:59Z'};
  for(const from of ['2026-02-30T00:00:00Z','2025-02-29T00:00:00Z','2026-01-01T24:00:00Z']) assert.throws(()=>validateOperation({...base,from}));
  assert.throws(()=>validateOperation({...base,after_time:'2026-02-01T12:00:00Z'}),/incomplete_cursor/);
  assert.throws(()=>validateOperation({...base,after_time:'2027-01-01T00:00:00Z',after_id:1}),/cursor_outside_range/);
  const precise='2026-02-01T12:00:00.123456+00:00';
  assert.equal(validateOperation({...base,after_time:precise,after_id:42}).after_time,precise);
  assert.doesNotThrow(()=>validateOperation({...base,from:'2026-01-02T00:00:00+12:00',to:'2026-01-01T13:00:00Z'}));
  assert.doesNotThrow(()=>validateOperation({...base,from:'2024-02-29T00:00:00Z'}));
});
test('literal and multi-query schemas expose the filters the server supports',()=>{
  const literal=READ_TOOLS.find(t=>t.name==='search_text').inputSchema;
  assert.ok(literal.properties.from&&literal.properties.to);
  const many=validateOperation({operation:'search_many',generation:'fixed',queries:[{query:'proposal',role:'assistant',from:'2025-01-01T00:00:00Z',to:'2026-01-01T00:00:00Z',include_inactive:true,limit:3}]});
  assert.equal(many.queries[0].generation,'fixed');
  assert.equal(many.queries[0].include_inactive,true);
  assert.equal(many.queries[0].limit,3);
});
test('incomplete semantic coverage is visibly degraded',()=>{
  const partial={retrieval_mode:'hybrid_rrf',status:{build_status:'indexed',frames:100,embedded_frames:1}};
  assert.deepEqual(retrievalHealth(partial).degraded_reasons,['semantic_index_incomplete']);
  assert.equal(retrievalHealth({...partial,status:{...partial.status,embedded_frames:100}}).degraded,false);
  assert.equal(retrievalHealth({retrieval_mode:'lexical'},'semantic_index_incomplete').degraded,true);
  assert.equal(retrievalHealth({retrieval_mode:'lexical'}).degraded,false);
});
