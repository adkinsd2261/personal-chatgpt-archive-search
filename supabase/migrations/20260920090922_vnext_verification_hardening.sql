begin;
create extension if not exists pg_jsonschema with schema extensions;

-- Validate worker output in the database as well as in the model adapter.
create or replace function crowley_v2.validate_worker_result() returns trigger
language plpgsql set search_path='' as $$
begin
  if new.state='succeeded' and not extensions.jsonb_matches_schema(new.output_schema::json,new.result) then
    raise exception 'worker result violates output schema';
  end if;
  return new;
end $$;
drop trigger if exists workers_validate_result on crowley_v2.workers;
create trigger workers_validate_result before insert or update on crowley_v2.workers
  for each row execute function crowley_v2.validate_worker_result();

create or replace function crowley_v2.protect_current_record() returns trigger
language plpgsql set search_path='' as $$
begin
  if old.status='active' and new.authority='model_proposal' then
    raise exception 'a model proposal cannot replace active state; use a separate proposal key';
  end if;
  return new;
end $$;
drop trigger if exists records_protect_current on crowley_v2.records;
create trigger records_protect_current before update on crowley_v2.records
  for each row execute function crowley_v2.protect_current_record();

create or replace function crowley_v2.approve_action(p_id uuid,p_expected_hash text,p_actor text) returns void
language plpgsql set search_path='' as $$
begin
  if p_actor is null or length(trim(p_actor))=0 then raise exception 'human approver required'; end if;
  update crowley_v2.action_proposals set status='approved',approved_by=p_actor,approval_hash=payload_hash
    where id=p_id and status='proposed' and expires_at>now() and payload_hash=p_expected_hash;
  if not found then raise exception 'stale, expired or changed proposal'; end if;
end $$;

create or replace function crowley_v2.dispatch_event_intents(p_event uuid,p_limit integer default 20) returns integer
language plpgsql set search_path='' as $$
declare e crowley_v2.events%rowtype; r crowley_v2.intents%rowtype; n integer:=0;
begin
  if p_limit not between 1 and 100 then raise exception 'invalid batch'; end if;
  select * into strict e from crowley_v2.events where id=p_event;
  if e.kind like 'intent.%' then return 0; end if;
  for r in select * from crowley_v2.intents where status='active' and trigger_spec->>'type'='event'
    and trigger_spec->>'event_kind'=e.kind and (expires_at is null or expires_at>now())
    order by id limit p_limit loop
    perform crowley_v2.emit_event('intent-event:'||r.id||':'||r.version||':'||e.id,'intent.triggered',
      jsonb_build_object('intent_id',r.id,'version',r.version,'event_id',e.id,'condition',r.condition_spec,'requires_condition_check',true),r.source_uris);
    n:=n+1;
  end loop;
  return n;
end $$;

create or replace function crowley_v2.fail_worker(p_id uuid,p_lease uuid,p_error text) returns void
language plpgsql set search_path='' as $$
declare r crowley_v2.workers%rowtype;
begin
  select * into strict r from crowley_v2.workers where id=p_id for update;
  if r.state<>'running' or r.lease_token is distinct from p_lease or r.lease_until<=now() then raise exception 'stale worker lease'; end if;
  update crowley_v2.workers set state=case when attempts>=max_attempts or deadline<=now() then 'failed' else 'queued' end,
    error_code=left(p_error,100),available_at=now()+make_interval(secs=>least(600,10*power(2,attempts)::integer)),
    lease_token=null,lease_until=null where id=p_id;
end $$;

-- Checkpoint each bounded batch. A crashed invocation cannot acknowledge a newer
-- lease. Pieces survive retry; completion requires gap-free full-source coverage.
create or replace function archive_vnext.save_embedding_progress(p_frame bigint,p_lease uuid,p_hash text,p_pieces jsonb,p_complete boolean)
returns jsonb language plpgsql set search_path='' as $$
declare j archive_vnext.embedding_jobs%rowtype; f archive_vnext.frames%rowtype;
  item jsonb; last_end integer; i integer;
begin
  select * into strict j from archive_vnext.embedding_jobs where frame_id=p_frame for update;
  select * into strict f from archive_vnext.frames where id=p_frame;
  if j.state<>'leased' or j.lease_token is distinct from p_lease or j.lease_until<=now()
    or j.content_hash is distinct from p_hash or f.content_hash is distinct from p_hash then raise exception 'stale embedding lease'; end if;
  if p_complete is null or p_pieces is null or jsonb_typeof(p_pieces)<>'array' or jsonb_array_length(p_pieces) not between 1 and 16 then raise exception 'invalid embedding batch'; end if;
  select coalesce(max(end_offset),0),count(*) into last_end,i from archive_vnext.embeddings where frame_id=p_frame;
  for item in select value from jsonb_array_elements(p_pieces) loop
    if not coalesce((item->>'start_offset')::integer<=last_end and (item->>'start_offset')::integer>=0 and
      (item->>'end_offset')::integer>last_end and (item->>'end_offset')::integer<=length(f.context_text) and
      (item->>'token_count')::integer between 1 and 512 and jsonb_array_length(item->'embedding')=384 and
      length(item->>'tokenizer_version')>0,false) then raise exception 'incomplete or invalid embedding coverage'; end if;
    insert into archive_vnext.embeddings values(p_frame,i,p_hash,'Supabase/gte-small',item->>'tokenizer_version',
      (item->>'token_count')::integer,(item->>'start_offset')::integer,(item->>'end_offset')::integer,(item->'embedding')::text::extensions.halfvec(384));
    last_end:=(item->>'end_offset')::integer; i:=i+1;
  end loop;
  if p_complete is distinct from (last_end=length(f.context_text)) then raise exception 'incorrect completion declaration'; end if;
  update archive_vnext.embedding_jobs set state=case when p_complete then 'complete' else 'queued' end,
    attempts=0,available_at=now(),completed_at=case when p_complete then now() end,
    lease_until=null,lease_token=null,last_error=null where frame_id=p_frame;
  return jsonb_build_object('complete',p_complete,'covered_characters',last_end,'pieces',i);
end $$;

create or replace function archive_vnext.complete_embedding(p_frame bigint,p_lease uuid,p_hash text,p_pieces jsonb) returns void
language plpgsql set search_path='' as $$
begin perform archive_vnext.save_embedding_progress(p_frame,p_lease,p_hash,p_pieces,true); end $$;

-- No need to ship full raw-message arrays in search results; opening a frame
-- returns exact role/source mappings separately from the paginated text.
create or replace function archive_vnext.open_context(p_source_uri text,p_generation text default null,p_offset integer default 0,p_length integer default 16000)
returns jsonb language plpgsql stable set search_path='' as $$
declare f archive_vnext.frames%rowtype; total integer; pointers jsonb;
begin
  if p_offset is null or p_offset<0 or p_length is null or p_length not between 1 and 20000 then raise exception 'invalid page'; end if;
  select * into strict f from archive_vnext.frames where generation_id=coalesce(p_generation,(select active_generation_id from archive_private.corpus_state)) and source_uri=p_source_uri;
  total:=length(f.context_text);
  if p_offset>total then raise exception 'offset beyond source'; end if;
  select jsonb_agg((m-'text')||jsonb_build_object('characters',length(m->>'text'),'source_uri',
    case when m->>'turn_index' is not null then 'archive://conversation/'||f.conversation_id||'/turn/'||(m->>'turn_index') else f.source_uri end)) into pointers
    from jsonb_array_elements(f.raw_messages)m;
  return jsonb_build_object('source_uri',f.source_uri,'generation_id',f.generation_id,'content_hash',f.content_hash,
    'source_state',f.source_state,'title',f.title,'date',f.observed_at,'message_pointers',pointers,
    'content',substr(f.context_text,p_offset+1,p_length),'offset',p_offset,'total_characters',total,
    'next_offset',case when p_offset+p_length<total then p_offset+p_length end,'truncated',p_offset+p_length<total,
    'authority','role_labels_required; assistant_text_is_discovery_only','status',archive_vnext.status(f.generation_id));
end $$;

revoke all on all functions in schema archive_vnext from public,anon,authenticated,service_role;
revoke all on all functions in schema crowley_v2 from public,anon,authenticated,service_role;
commit;
