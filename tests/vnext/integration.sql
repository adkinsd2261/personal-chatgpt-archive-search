-- Run ONLY on a development database. Entire synthetic corpus is rolled back.
begin;
set local statement_timeout='90s';
insert into archive_private.generations(generation_id,status,source_database_sha256,source_schema_version,corpus_cutoff,expected_turns,expected_user_chunks,expected_history_messages)
  values('vnext-synthetic-test','ready',repeat('a',64),2,now()-interval '90 days',2,0,1);
insert into archive_private.turns(generation_id,conversation_id,turn_index,create_time,title,user_text,assistant_text) values
 ('vnext-synthetic-test','vnext-fixture',0,'2025-01-01','Synthetic fixture','Please suggest an architecture.','The violet lighthouse is a proposal, not a fact.'),
 ('vnext-synthetic-test','vnext-fixture',1,'2025-01-02','Synthetic fixture','No. Use the amber harbor instead.','Understood. '||repeat('Long Unicode text: 🦊 café 中文. ',900));
insert into archive_private.history_messages(generation_id,conversation_id,message_id,create_time,title,role,content_type,text,content_hash,source_state)
  values('vnext-synthetic-test','vnext-fixture','inactive-message','2025-01-01','Synthetic fixture','user','text','The secret otter branch was abandoned.',repeat('b',64),'inactive_branch');

do $$
declare x jsonb; j jsonb; v integer; e uuid; w uuid; proposal uuid; lease uuid; bad boolean; body text; full_body text; cursor_pos integer:=0; intent_id uuid;
begin
  perform archive_vnext.begin_build('vnext-synthetic-test');
  x:=archive_vnext.index_turn_batch('vnext-synthetic-test');
  assert (x->>'inserted')::integer=2,'index count';
  x:=archive_vnext.index_turn_batch('vnext-synthetic-test');
  assert (x->>'inserted')::integer=0,'index retry must be idempotent';
  perform archive_vnext.index_history_batch('vnext-synthetic-test');
  perform archive_vnext.finish_build('vnext-synthetic-test');
  assert (archive_vnext.status('vnext-synthetic-test')->>'frames')::integer=3,'coverage';
  assert archive_vnext.status('vnext-synthetic-test')->>'freshness_status'='stale','freshness';
  assert not (archive_vnext.status('vnext-synthetic-test')->>'current_state_safe')::boolean,'stale is not current';

  x:=archive_vnext.search('violet lighthouse',null,'vnext-synthetic-test','assistant');
  assert jsonb_array_length(x->'matches')=2,'assistant-before discovery';
  assert x->>'coverage'='representative','no exhaustive claim';
  x:=archive_vnext.search('violet lighthouse',null,'vnext-synthetic-test','user');
  assert jsonb_array_length(x->'matches')=0,'role isolation';
  x:=archive_vnext.search('secret otter',null,'vnext-synthetic-test');
  assert jsonb_array_length(x->'matches')=0,'inactive branch default hidden';
  x:=archive_vnext.search('secret otter',null,'vnext-synthetic-test','both',null,null,8,true);
  assert x->'matches'->0->>'source_state'='inactive_branch','branch label';
  x:=archive_vnext.search('amber harbor',null,'vnext-synthetic-test','user',null,null,8,false,true);
  assert jsonb_array_length(x->'matches')=1,'exact user quote';

  body:='';
  loop
    x:=archive_vnext.open_context('archive://conversation/vnext-fixture/turn/1','vnext-synthetic-test',cursor_pos,700);
    body:=body||(x->>'content');
    exit when x->>'next_offset' is null;
    cursor_pos:=(x->>'next_offset')::integer;
  end loop;
  select context_text into full_body from archive_vnext.frames where generation_id='vnext-synthetic-test' and turn_index=1;
  assert body=full_body,'Unicode pagination must round trip';
  assert x->'message_pointers'->0->>'source_uri'='archive://conversation/vnext-fixture/turn/0','before-message provenance';
  bad:=false;
  begin update archive_vnext.frames set title='mutated' where generation_id='vnext-synthetic-test'; exception when others then bad:=true; end;
  assert bad,'immutable frame';

  j:=archive_vnext.claim_embedding();
  bad:=false;
  begin perform archive_vnext.complete_embedding((j->>'frame_id')::bigint,null,j->>'content_hash','[]'); exception when others then bad:=true; end;
  assert bad,'null lease must fail';
  perform archive_vnext.fail_embedding((j->>'frame_id')::bigint,(j->>'lease_token')::uuid,'synthetic_failure');
  assert (select state='queued' and attempts=1 from archive_vnext.embedding_jobs where frame_id=(j->>'frame_id')::bigint),'retry state';

  v:=crowley_v2.write_record('state','test.project',0,'{"status":"active"}',array['synthetic://user/1'],'user_explicit','active','test-human',now()+interval '1 hour');
  assert v=1,'first version';
  v:=crowley_v2.write_record('state','test.project',1,'{"status":"paused"}',array['synthetic://user/2'],'user_explicit','active','test-human',now()+interval '1 hour');
  assert v=2 and (select count(*)=2 from crowley_v2.record_versions where key='test.project'),'append-only history';
  bad:=false;
  begin perform crowley_v2.write_record('state','test.project',1,'{}',array['synthetic://user/3'],'user_explicit','active','test-human'); exception when serialization_failure then bad:=true; end;
  assert bad,'stale state write';
  bad:=false;
  begin perform crowley_v2.write_record('state','test.model',0,'{}',array['synthetic://assistant/1'],'model_proposal','active','test-model'); exception when check_violation then bad:=true; end;
  assert bad,'model cannot promote itself';
  bad:=false;
  begin perform crowley_v2.write_record('state','test.project',2,'{}',array['synthetic://assistant/1'],'model_proposal','proposed','test-model'); exception when others then bad:=true; end;
  assert bad,'proposal cannot overwrite active truth';
  bad:=false;
  begin perform crowley_v2.write_record('preference','test.style',0,'{}',array['synthetic://connector/1'],'verified_connector','active','connector'); exception when check_violation then bad:=true; end;
  assert bad,'active preferences require user authority';

  e:=crowley_v2.emit_event('synthetic-event','test.signal','{"value":1}');
  assert crowley_v2.emit_event('synthetic-event','test.signal','{"value":1}')=e,'event dedupe';
  bad:=false;
  begin perform crowley_v2.emit_event('synthetic-event','test.signal','{"value":2}'); exception when others then bad:=true; end;
  assert bad,'idempotency payload conflict';
  intent_id:=crowley_v2.create_intent('synthetic-intent','Test follow-up','{"type":"once"}','{"requires_no_reply":true}',array['synthetic://user/1'],now()-interval '1 minute');
  perform crowley_v2.transition_intent(intent_id,1,'active');
  assert crowley_v2.tick_intents()=1,'due intent';
  assert crowley_v2.tick_intents()=0,'due intent deduplication';
  perform crowley_v2.transition_intent(intent_id,2,'cancelled');
  bad:=false;
  begin perform crowley_v2.transition_intent(intent_id,3,'active'); exception when others then bad:=true; end;
  assert bad,'terminal intent cannot restart';

  bad:=false;
  begin perform crowley_v2.spawn_worker('denied-worker','No shell',array['shell.exec'],'{}','{"type":"object"}',100,1,now()+interval '1 hour'); exception when others then bad:=true; end;
  assert bad,'arbitrary tools denied';
  w:=crowley_v2.spawn_worker('synthetic-worker','Read only',array['archive.search_context'],array['synthetic://user/1'],'{"type":"object","required":["answer"],"properties":{"answer":{"type":"string"}}}',100,1,now()+interval '1 hour');
  x:=crowley_v2.claim_worker(); lease:=(x->>'lease_token')::uuid;
  assert (x->>'id')::uuid=w,'worker lease';
  perform crowley_v2.reserve_worker_call(w,lease,'archive.search_context',100);
  bad:=false;
  begin perform crowley_v2.reserve_worker_call(w,lease,'archive.search_context',1); exception when others then bad:=true; end;
  assert bad,'budget before dispatch';
  bad:=false;
  begin perform crowley_v2.complete_worker(w,lease,'{"wrong":true}'); exception when others then bad:=true; end;
  assert bad,'output schema';
  assert crowley_v2.cancel_worker(w),'worker cancellation';
  bad:=false;
  begin perform crowley_v2.complete_worker(w,lease,'{"answer":"late"}'); exception when others then bad:=true; end;
  assert bad,'cancelled completion rejected';

  proposal:=crowley_v2.propose_action('state.write','{"key":"test"}','test-model',now()+interval '1 hour');
  bad:=false;
  begin perform crowley_v2.approve_action(proposal,'wrong-hash','test-human'); exception when others then bad:=true; end;
  assert bad,'approval bound to payload';
  perform crowley_v2.approve_action(proposal,(select payload_hash from crowley_v2.action_proposals where id=proposal),'test-human');
  assert not (select external_actions_enabled from crowley_v2.settings),'approval does not enable execution';
  assert (select production_retrieval='v3' from crowley_v2.settings),'v3 remains default';

  insert into crowley_v2.api_tokens(token_sha256,label,scopes,expires_at,minute_limit,hourly_character_limit)
    values(repeat('c',64),'synthetic-token',array['archive:read'],now()+interval '1 hour',1,1000);
  assert not crowley_v2.authorize(repeat('c',64),'embedding:write'),'scope boundary';
  assert crowley_v2.authorize(repeat('c',64),'archive:read'),'valid auth';
  assert not crowley_v2.authorize(repeat('c',64),'archive:read'),'rate limit';
  assert crowley_v2.disclose(repeat('c',64),900),'within disclosure';
  assert not crowley_v2.disclose(repeat('c',64),101),'disclosure limit';
  assert not crowley_v2.disclose(repeat('c',64),-1),'negative disclosure';
end $$;

set local role anon;
do $$ declare bad boolean:=false; begin
  begin perform archive_vnext.status(); exception when insufficient_privilege then bad:=true; end;
  assert bad,'anon cannot invoke private API';
end $$;
reset role;
set local role authenticated;
do $$ declare bad boolean:=false; begin
  begin perform crowley_v2.runtime_context(); exception when insufficient_privilege then bad:=true; end;
  assert bad,'authenticated is not authorization';
end $$;
reset role;
select 'PASS: synthetic retrieval, provenance, freshness, immutability, state, events, intents, workers, approvals, auth, limits, and grants' result;
rollback;
