-- Reproduced by messaging-lane QA. Additive shadow changes only.
set lock_timeout='3s';

create or replace function crowley_v2.claim_worker(p_lease_seconds integer default 60) returns jsonb
language plpgsql set search_path='' as $$
declare r crowley_v2.workers%rowtype;
begin
  if p_lease_seconds is null or p_lease_seconds not between 5 and 120 then raise exception 'invalid lease'; end if;
  update crowley_v2.workers set state='expired',completed_at=now(),lease_token=null,lease_until=null where state in ('queued','running') and deadline<=now();
  update crowley_v2.workers set state='failed',error_code='retry_budget_exhausted',completed_at=now(),lease_token=null,lease_until=null where state='running' and lease_until<now() and attempts>=max_attempts;
  select * into r from crowley_v2.workers where deadline>now() and attempts<max_attempts
    and ((state='queued' and available_at<=now()) or (state='running' and lease_until<now())) order by available_at,id for update skip locked limit 1;
  if not found then return null; end if;
  update crowley_v2.workers set state='running',attempts=attempts+1,lease_token=gen_random_uuid(),lease_until=least(deadline,now()+make_interval(secs=>p_lease_seconds)) where id=r.id returning * into r;
  return to_jsonb(r);
end $$;

create or replace function archive_vnext.claim_embedding(p_lease_seconds integer default 120) returns jsonb
language plpgsql set search_path = '' as $$
declare j archive_vnext.embedding_jobs%rowtype; f archive_vnext.frames%rowtype;
begin
  if p_lease_seconds is null or p_lease_seconds not between 30 and 300 then raise exception 'invalid lease'; end if;
  update archive_vnext.embedding_jobs set state='dead',lease_token=null,lease_until=null,last_error='retry_budget_exhausted'
    where state='leased' and lease_until<now() and attempts>=5;
  select * into j from archive_vnext.embedding_jobs
    where attempts<5 and ((state='queued' and available_at<=now()) or (state='leased' and lease_until<now()))
    order by available_at,frame_id for update skip locked limit 1;
  if not found then return null; end if;
  update archive_vnext.embedding_jobs set state='leased',attempts=attempts+1,lease_token=gen_random_uuid(),lease_until=now()+make_interval(secs=>p_lease_seconds)
    where frame_id=j.frame_id returning * into j;
  select * into strict f from archive_vnext.frames where id=j.frame_id;
  return jsonb_build_object('frame_id',f.id,'content_hash',f.content_hash,'context_text',f.context_text,'lease_token',j.lease_token,'lease_until',j.lease_until);
end $$;

create or replace function crowley_v2.create_intent(p_key text,p_objective text,p_trigger jsonb,p_condition jsonb,p_sources text[],p_due timestamptz default null,p_expires timestamptz default null)
returns uuid language plpgsql set search_path='' as $$
declare r crowley_v2.intents%rowtype;
begin
  if p_trigger->>'type'='event' and (jsonb_typeof(p_trigger->'event_kind') is distinct from 'string'
    or length(trim(p_trigger->>'event_kind')) not between 1 and 100 or p_due is not null) then
    raise exception 'event trigger requires event_kind and cannot have a due time';
  end if;
  if p_expires is not null and (p_expires<=now() or p_due>=p_expires) then raise exception 'invalid intent expiry'; end if;
  if p_trigger->>'type' not in ('once','event') or p_trigger->>'type' is null or (p_trigger->>'type'='once' and p_due is null) then raise exception 'unsupported trigger'; end if;
  insert into crowley_v2.intents(idempotency_key,objective,trigger_spec,condition_spec,source_uris,due_at,expires_at)
    values(p_key,p_objective,p_trigger,p_condition,p_sources,p_due,p_expires) on conflict do nothing returning * into r;
  if r.id is null then
    select * into strict r from crowley_v2.intents where idempotency_key=p_key;
    if r.objective is distinct from p_objective or r.trigger_spec is distinct from p_trigger or r.condition_spec is distinct from p_condition or r.source_uris is distinct from p_sources or r.due_at is distinct from p_due or r.expires_at is distinct from p_expires then raise exception 'idempotency conflict'; end if;
  end if;
  return r.id;
end $$;

-- Event fanout must drain successive bounded pages, not replay the first page.
create or replace function crowley_v2.dispatch_event_intents(p_event uuid,p_limit integer default 20) returns integer
language plpgsql set search_path='' as $$
declare e crowley_v2.events%rowtype; r crowley_v2.intents%rowtype; n integer:=0;
begin
  if p_limit is null or p_limit not between 1 and 100 then raise exception 'invalid batch'; end if;
  select * into strict e from crowley_v2.events where id=p_event;
  if e.kind like 'intent.%' then return 0; end if;
  for r in select i.* from crowley_v2.intents i where i.status='active' and i.trigger_spec->>'type'='event'
    and i.trigger_spec->>'event_kind'=e.kind and (i.expires_at is null or i.expires_at>now())
    and not exists(select 1 from crowley_v2.events d where d.idempotency_key='intent-event:'||i.id||':'||i.version||':'||e.id)
    order by i.id for update of i skip locked limit p_limit loop
    perform crowley_v2.emit_event('intent-event:'||r.id||':'||r.version||':'||e.id,'intent.triggered',
      jsonb_build_object('intent_id',r.id,'version',r.version,'event_id',e.id,'condition',r.condition_spec,'requires_condition_check',true),r.source_uris);
    n:=n+1;
  end loop;
  return n;
end $$;

-- Approval binds an immutable tool, payload, proposer and expiry. Changing an
-- approved payload or extending its lifetime requires a new proposal.
create function crowley_v2.protect_action_identity() returns trigger
language plpgsql set search_path='' as $$
begin
  if (new.tool_name,new.payload,new.payload_hash,new.proposed_by,new.expires_at,new.created_at)
    is distinct from (old.tool_name,old.payload,old.payload_hash,old.proposed_by,old.expires_at,old.created_at) then
    raise exception 'action identity is immutable; create a new proposal';
  end if;
  return new;
end $$;
create trigger actions_protect_identity before update on crowley_v2.action_proposals
  for each row execute function crowley_v2.protect_action_identity();
alter table crowley_v2.action_proposals add constraint action_payload_hash_matches
  check(payload_hash=encode(extensions.digest(payload::text,'sha256'),'hex'));
alter table crowley_v2.action_proposals add constraint action_payload_is_object
  check(jsonb_typeof(payload)='object' and length(trim(proposed_by))>0);

-- Older callers retain the six-argument signature. The extended path makes
-- inactive branches opt-in and provides a frozen-generation cursor and status.
create function archive_vnext.browse_time_filtered(p_from timestamptz,p_to timestamptz,p_generation text default null,
  p_after_time timestamptz default null,p_after_id bigint default 0,p_limit integer default 20,
  p_include_inactive boolean default false)
returns jsonb language plpgsql stable set search_path='' as $$
declare result jsonb; g text; last_row jsonb;
begin
  if p_from is null or p_to is null or p_from>p_to or p_limit is null or p_limit not between 1 and 100
    or p_include_inactive is null or p_after_id is null or p_after_id<0
    or (p_after_time is null and p_after_id<>0) or (p_after_time is not null and (p_after_id=0 or p_after_time<p_from or p_after_time>p_to)) then
    raise exception 'invalid timeline bounds or cursor';
  end if;
  g:=coalesce(p_generation,(select active_generation_id from archive_private.corpus_state));
  if not exists(select 1 from archive_vnext.builds where generation_id=g and status='indexed') then raise exception 'generation not indexed'; end if;
  select coalesce(jsonb_agg(jsonb_build_object('id',id,'source_uri',source_uri,'title',title,'date',observed_at,'source_state',source_state) order by observed_at,id),'[]') into result
    from (select id,source_uri,title,observed_at,source_state from archive_vnext.frames where generation_id=g
      and (p_include_inactive or source_state='active_path')
      and observed_at between p_from and p_to and (p_after_time is null or (observed_at,id)>(p_after_time,p_after_id))
      order by observed_at,id limit p_limit)f;
  last_row:=result->(jsonb_array_length(result)-1);
  return jsonb_build_object('matches',result,'generation_id',g,'status',archive_vnext.status(g),
    'page_full',jsonb_array_length(result)=p_limit,'next_cursor',case when jsonb_array_length(result)=p_limit then jsonb_build_object('generation',g,'after_time',last_row->>'date','after_id',(last_row->>'id')::bigint) end,
    'include_inactive',p_include_inactive,'cursor_fields',jsonb_build_array('date','id'),'coverage','paginated_not_exhaustive','exhaustive',false);
end $$;
create or replace function archive_vnext.browse_time(p_from timestamptz,p_to timestamptz,p_generation text default null,
  p_after_time timestamptz default null,p_after_id bigint default 0,p_limit integer default 20)
returns jsonb language sql stable set search_path='' as $$
  select archive_vnext.browse_time_filtered(p_from,p_to,p_generation,p_after_time,p_after_id,p_limit,false)
$$;

-- Adjacent turns help verify short replies and subsequent adoption/correction.
create or replace function archive_vnext.open_context(p_source_uri text,p_generation text default null,p_offset integer default 0,p_length integer default 16000)
returns jsonb language plpgsql stable set search_path='' as $$
declare f archive_vnext.frames%rowtype; total integer; pointers jsonb; neighbors jsonb;
begin
  if p_offset is null or p_offset<0 or p_length is null or p_length not between 1 and 20000 then raise exception 'invalid page'; end if;
  select * into strict f from archive_vnext.frames where generation_id=coalesce(p_generation,(select active_generation_id from archive_private.corpus_state)) and source_uri=p_source_uri;
  total:=length(f.context_text);
  if p_offset>total then raise exception 'offset beyond source'; end if;
  select jsonb_agg((m-'text')||jsonb_build_object('characters',length(m->>'text'),'source_uri',
    case when m->>'turn_index' is not null then 'archive://conversation/'||f.conversation_id||'/turn/'||(m->>'turn_index') else f.source_uri end)) into pointers
    from jsonb_array_elements(f.raw_messages)m;
  select coalesce(jsonb_agg(jsonb_build_object('source_uri',n.source_uri,'turn_index',n.turn_index,
    'date',n.observed_at,'user_excerpt',left(n.user_text,300),'must_open_source',true) order by n.turn_index),'[]') into neighbors
    from archive_vnext.frames n where f.source_state='active_path' and n.source_state='active_path'
      and n.generation_id=f.generation_id and n.conversation_id=f.conversation_id
      and n.turn_index between f.turn_index-2 and f.turn_index+2 and n.id<>f.id;
  return jsonb_build_object('source_uri',f.source_uri,'generation_id',f.generation_id,'content_hash',f.content_hash,
    'neighbors',neighbors,'modality','text_mirror_only','attachment_bytes_available',false,'source_state',f.source_state,'title',f.title,'date',f.observed_at,'message_pointers',pointers,
    'content',substr(f.context_text,p_offset+1,p_length),'offset',p_offset,'total_characters',total,
    'next_offset',case when p_offset+p_length<total then p_offset+p_length end,'truncated',p_offset+p_length<total,
    'authority','role_labels_required; assistant_text_is_discovery_only','status',archive_vnext.status(f.generation_id));
end $$;

revoke all on all functions in schema archive_vnext from public,anon,authenticated,service_role;
revoke all on all functions in schema crowley_v2 from public,anon,authenticated,service_role;
