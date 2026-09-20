-- Development only. Exercises real primitive transitions; all fixtures roll back.
begin;
set local statement_timeout='30s';
do $$
declare w uuid; p uuid; e uuid; x jsonb; i integer; bad boolean; lease uuid; v integer;
begin
  w:=crowley_v2.spawn_worker('qa-workflow-worker','Synthetic read',array['state.read'],array['synthetic://user/1'],'{"type":"object","required":["answer"],"properties":{"answer":{"type":"string"}},"additionalProperties":false}',100,1,now()+interval '1 hour');
  bad:=false;
  begin perform crowley_v2.claim_worker(null); exception when others then bad:=true; end;
  assert bad,'null worker lease must fail without claiming work';
  bad:=false;
  begin perform archive_vnext.claim_embedding(null); exception when others then bad:=true; end;
  assert bad,'null embedding lease must fail without claiming work';
  x:=crowley_v2.claim_worker(); lease:=(x->>'lease_token')::uuid;
  assert (x->>'id')::uuid=w,'claim the intended worker';
  perform crowley_v2.reserve_worker_call(w,lease,'state.read',100);
  perform crowley_v2.complete_worker(w,lease,'{"answer":"Synthetic read completed"}');
  assert exists(select 1 from crowley_v2.events where idempotency_key='worker-result:'||w and (payload->>'requires_validation')::boolean),'completion wakes a validation event';
  assert (select not external_actions_enabled from crowley_v2.settings),'worker result is not an external send';

  p:=crowley_v2.propose_action('state.write','{"key":"original"}','qa-model',now()+interval '1 hour');
  perform crowley_v2.approve_action(p,(select payload_hash from crowley_v2.action_proposals where id=p),'qa-human');
  bad:=false;
  begin update crowley_v2.action_proposals set payload='{"key":"changed"}' where id=p; exception when others then bad:=true; end;
  assert bad,'approved payload immutable';
  bad:=false;
  begin update crowley_v2.action_proposals set expires_at=now()+interval '3 hours' where id=p; exception when others then bad:=true; end;
  assert bad,'approval lifetime cannot be extended';

  for i in 1..21 loop
    p:=crowley_v2.create_intent('qa-workflow-event-'||i,'Synthetic follow-up','{"type":"event","event_kind":"qa.workflow.signal"}','{"requires_no_reply":true}',array['synthetic://user/1']);
    perform crowley_v2.transition_intent(p,1,'active');
  end loop;
  e:=crowley_v2.emit_event('qa-workflow-signal','qa.workflow.signal','{}');
  assert crowley_v2.dispatch_event_intents(e,20)=20,'first bounded event page';
  assert crowley_v2.dispatch_event_intents(e,20)=1,'second page reaches remaining intent';
  assert crowley_v2.dispatch_event_intents(e,20)=0,'event replay is a no-op';
  assert (select count(*)=21 from crowley_v2.events where kind='intent.triggered' and payload->>'event_id'=e::text),'all 21 emitted exactly once';
  assert (select bool_and((payload->>'requires_condition_check')::boolean) from crowley_v2.events where kind='intent.triggered' and payload->>'event_id'=e::text),'triggers require condition checks';
  bad:=false;
  begin perform crowley_v2.create_intent('qa-invalid-event','Cannot fire','{"type":"event"}','{}',array['synthetic://user/1']); exception when others then bad:=true; end;
  assert bad,'missing event kind rejected';
  bad:=false;
  begin perform crowley_v2.create_intent('qa-expired-event','Expired','{"type":"once"}','{}',array['synthetic://user/1'],now()+interval '2 hours',now()+interval '1 hour'); exception when others then bad:=true; end;
  assert bad,'due date after expiry rejected';

  p:=crowley_v2.create_intent('qa-workflow-once','Follow up if still unanswered','{"type":"once"}','{"requires_no_reply":true}',array['synthetic://user/1'],now()-interval '1 minute');
  perform crowley_v2.transition_intent(p,1,'active');
  assert crowley_v2.tick_intents()=1,'due reminder emits once';
  assert crowley_v2.tick_intents()=0,'retry does not duplicate reminder';
  perform crowley_v2.transition_intent(p,2,'cancelled');
  assert crowley_v2.tick_intents()=0,'cancellation persists';

  v:=crowley_v2.write_record('preference','qa.style',0,'{"verbosity":"concise"}',array['synthetic://user/explicit'],'user_explicit','active','qa-user');
  v:=crowley_v2.write_record('preference','qa.style',v,'{"verbosity":"deep","scope":"learning"}',array['synthetic://user/correction'],'user_explicit','active','qa-user');
  assert (crowley_v2.read_record('preference','qa.style')->'value'->>'scope')='learning','scope retained after correction';
  assert (select count(*)=2 from crowley_v2.record_versions where kind='preference' and key='qa.style'),'earlier preference preserved';
  bad:=false;
  begin perform crowley_v2.write_record('preference','qa.style',1,'{}',array['synthetic://user/late'],'user_explicit','active','qa-user'); exception when serialization_failure then bad:=true; end;
  assert bad,'late correction cannot overwrite newer preference';
end $$;

insert into archive_private.generations(generation_id,status,source_database_sha256,source_schema_version,corpus_cutoff,expected_turns,expected_user_chunks,expected_history_messages)
  values('vnext-workflow-test','ready',repeat('d',64),2,'2025-01-02',3,0,1);
insert into archive_private.turns(generation_id,conversation_id,turn_index,create_time,title,user_text,assistant_text) values
 ('vnext-workflow-test','qa-turn-chain',0,'2025-01-01T00:00:00.123456Z','Synthetic chain','Suggest a name','Consider Opal or Amber.'),
 ('vnext-workflow-test','qa-turn-chain',1,'2025-01-01T00:00:00.123457Z','Synthetic chain','Use Amber','Confirmed Amber.'),
 ('vnext-workflow-test','qa-turn-chain',2,'2025-01-01T00:00:00.123458Z','Synthetic chain','Actually call it Coral','The correction is Coral.');
insert into archive_private.history_messages(generation_id,conversation_id,message_id,create_time,title,role,content_type,text,content_hash,source_state)
  values('vnext-workflow-test','qa-turn-chain','inactive','2025-01-01','Synthetic branch','user','text','Use Opal',repeat('e',64),'inactive_branch');
do $$ declare x jsonb; y jsonb; bad boolean:=false; begin
  perform archive_vnext.begin_build('vnext-workflow-test');
  perform archive_vnext.index_turn_batch('vnext-workflow-test');
  perform archive_vnext.index_history_batch('vnext-workflow-test');
  perform archive_vnext.finish_build('vnext-workflow-test');
  x:=archive_vnext.browse_time('2025-01-01','2025-01-02','vnext-workflow-test',null,0,1);
  assert x->'matches'->0->>'source_state'='active_path','inactive path is opt-in';
  y:=archive_vnext.browse_time('2025-01-01','2025-01-02','vnext-workflow-test',(x->'next_cursor'->>'after_time')::timestamptz,(x->'next_cursor'->>'after_id')::bigint,1);
  assert x->'matches'->0->>'id'<>y->'matches'->0->>'id','microsecond cursor advances';
  assert y->'status'->>'production_route'='v3','timeline returns route/freshness';
  x:=archive_vnext.browse_time_filtered('2025-01-01','2025-01-02','vnext-workflow-test',null,0,10,true);
  assert jsonb_array_length(x->'matches')=4,'explicit inactive path is available';
  begin perform archive_vnext.browse_time('2025-01-01','2025-01-02','missing-generation'); exception when others then bad:=true; end;
  assert bad,'missing generation is not an empty history';
  x:=archive_vnext.open_context('archive://conversation/qa-turn-chain/turn/1','vnext-workflow-test');
  assert jsonb_array_length(x->'neighbors')=2,'both proposal and later correction discoverable';
  assert x->'neighbors'->1->>'source_uri'='archive://conversation/qa-turn-chain/turn/2','next user correction linked';
  assert x->>'modality'='text_mirror_only' and not (x->>'attachment_bytes_available')::boolean,'missing attachment bytes explicit';
end $$;
select 'PASS: 28 workflow assertions; reminder/worker/state/approval/timeline/continuity mechanics, no delivery claims' result;
rollback;
