-- V2 primitive foundation. Private/admin-only mutations; external actions disabled.
begin;
set local lock_timeout = '3s';
create schema if not exists crowley_v2;
revoke all on schema crowley_v2 from public,anon,authenticated,service_role;
alter default privileges in schema crowley_v2 revoke execute on functions from public;

create table crowley_v2.settings (
  singleton boolean primary key default true check(singleton),
  production_retrieval text not null default 'v3' check(production_retrieval='v3'),
  external_actions_enabled boolean not null default false check(not external_actions_enabled),
  recursive_workers_enabled boolean not null default false check(not recursive_workers_enabled),
  runtime_version text not null default 'primitive-foundation-1'
);
insert into crowley_v2.settings default values;

create table crowley_v2.records (
  kind text not null check(kind in ('state','preference')),
  key text not null check(length(key) between 1 and 200),
  version integer not null check(version>0),
  value jsonb not null,
  source_uris text[] not null check(cardinality(source_uris)>0),
  authority text not null check(authority in ('user_explicit','verified_connector','model_proposal')),
  confidence numeric not null check(confidence between 0 and 1),
  valid_from timestamptz not null,
  expires_at timestamptz,
  status text not null check(status in ('proposed','active','retracted')),
  updated_by text not null,
  updated_at timestamptz not null default now(),
  primary key(kind,key),
  check(expires_at is null or expires_at>valid_from),
  check(authority<>'model_proposal' or status<>'active'),
  check(kind<>'preference' or status<>'active' or authority='user_explicit')
);
create table crowley_v2.record_versions (
  kind text not null,
  key text not null,
  version integer not null,
  snapshot jsonb not null,
  recorded_at timestamptz not null default now(),
  primary key(kind,key,version)
);
create trigger record_versions_immutable before update or delete on crowley_v2.record_versions
  for each row execute function archive_vnext.reject_mutation();

create table crowley_v2.events (
  id uuid primary key default gen_random_uuid(),
  idempotency_key text not null unique check(length(idempotency_key) between 1 and 300),
  kind text not null check(length(kind) between 1 and 100),
  payload jsonb not null check(jsonb_typeof(payload)='object'),
  source_uris text[] not null default '{}',
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);
create index events_time_idx on crowley_v2.events(occurred_at,id);
create trigger events_immutable before update or delete on crowley_v2.events
  for each row execute function archive_vnext.reject_mutation();

create table crowley_v2.intents (
  id uuid primary key default gen_random_uuid(),
  idempotency_key text not null unique,
  objective text not null check(length(objective) between 1 and 4000),
  trigger_spec jsonb not null check(jsonb_typeof(trigger_spec)='object'),
  condition_spec jsonb not null default '{}' check(jsonb_typeof(condition_spec)='object'),
  source_uris text[] not null check(cardinality(source_uris)>0),
  status text not null default 'draft' check(status in ('draft','active','paused','fulfilled','cancelled','expired')),
  version integer not null default 1,
  due_at timestamptz,
  expires_at timestamptz,
  last_emitted_version integer,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);
create index intents_due_idx on crowley_v2.intents(due_at,id) where status='active';

create table crowley_v2.tools (
  name text primary key,
  authority text not null check(authority in ('read','internal_write','external_write')),
  requires_confirmation boolean not null,
  enabled boolean not null default false,
  timeout_ms integer not null check(timeout_ms between 1 and 120000),
  input_schema jsonb not null,
  output_schema jsonb not null,
  check(authority<>'external_write' or requires_confirmation)
);
insert into crowley_v2.tools(name,authority,requires_confirmation,enabled,timeout_ms,input_schema,output_schema) values
 ('archive.search_context','read',false,true,12000,'{"type":"object"}','{"type":"object"}'),
 ('archive.search_text','read',false,true,12000,'{"type":"object"}','{"type":"object"}'),
 ('archive.open_context','read',false,true,12000,'{"type":"object"}','{"type":"object"}'),
 ('archive.browse_time','read',false,true,12000,'{"type":"object"}','{"type":"object"}'),
 ('state.read','read',false,true,3000,'{"type":"object"}','{"type":"object"}'),
 ('state.write','internal_write',true,false,3000,'{"type":"object"}','{"type":"object"}'),
 ('preference.set','internal_write',true,false,3000,'{"type":"object"}','{"type":"object"}'),
 ('intent.create','internal_write',true,false,3000,'{"type":"object"}','{"type":"object"}'),
 ('action.propose','internal_write',false,false,3000,'{"type":"object"}','{"type":"object"}');

create table crowley_v2.workers (
  id uuid primary key default gen_random_uuid(),
  idempotency_key text not null unique,
  objective text not null check(length(objective) between 1 and 8000),
  event_id uuid references crowley_v2.events,
  allowed_tools text[] not null default '{}',
  source_uris text[] not null default '{}',
  output_schema jsonb not null check(jsonb_typeof(output_schema)='object'),
  token_budget integer not null check(token_budget between 1 and 100000),
  call_budget integer not null check(call_budget between 1 and 100),
  tokens_used integer not null default 0 check(tokens_used>=0),
  calls_used integer not null default 0 check(calls_used>=0),
  deadline timestamptz not null,
  state text not null default 'queued' check(state in ('queued','running','succeeded','failed','cancelled','expired')),
  lease_token uuid,
  lease_until timestamptz,
  attempts integer not null default 0,
  max_attempts integer not null default 3 check(max_attempts between 1 and 5),
  available_at timestamptz not null default now(),
  result jsonb,
  error_code text,
  created_at timestamptz not null default now(),
  completed_at timestamptz,
  check(tokens_used<=token_budget and calls_used<=call_budget)
);
create index workers_claim_idx on crowley_v2.workers(available_at,id) where state in ('queued','running');
create index workers_event_idx on crowley_v2.workers(event_id);

create table crowley_v2.action_proposals (
  id uuid primary key default gen_random_uuid(),
  tool_name text not null references crowley_v2.tools,
  payload jsonb not null,
  payload_hash text not null,
  status text not null default 'proposed' check(status in ('proposed','approved','rejected','expired')),
  proposed_by text not null,
  approved_by text,
  approval_hash text,
  expires_at timestamptz not null,
  created_at timestamptz not null default now(),
  check(status<>'approved' or (approved_by is not null and approval_hash=payload_hash))
);
create index action_proposals_tool_idx on crowley_v2.action_proposals(tool_name);
create table crowley_v2.runtime_runs (
  id uuid primary key default gen_random_uuid(),
  event_id uuid references crowley_v2.events,
  worker_id uuid references crowley_v2.workers,
  model text not null,
  policy_version text not null,
  state text not null check(state in ('running','completed','failed','cancelled')),
  context_manifest jsonb not null,
  started_at timestamptz not null default now(),
  completed_at timestamptz
);
create index runtime_runs_event_idx on crowley_v2.runtime_runs(event_id);
create index runtime_runs_worker_idx on crowley_v2.runtime_runs(worker_id);

create table crowley_v2.api_tokens (
  token_sha256 text primary key check(token_sha256 ~ '^[a-f0-9]{64}$'),
  label text not null unique,
  scopes text[] not null check(scopes <@ array['archive:read','embedding:write']),
  enabled boolean not null default true,
  expires_at timestamptz not null,
  minute_limit integer not null default 30 check(minute_limit between 1 and 120),
  hourly_character_limit integer not null default 300000 check(hourly_character_limit between 1000 and 2000000),
  created_at timestamptz not null default now()
);
create table crowley_v2.api_usage (
  token_sha256 text not null references crowley_v2.api_tokens,
  window_start timestamptz not null,
  requests integer not null default 0,
  characters integer not null default 0,
  primary key(token_sha256,window_start)
);

create function crowley_v2.authorize(p_hash text,p_scope text) returns boolean
language plpgsql set search_path='' as $$
declare t crowley_v2.api_tokens%rowtype; n integer;
begin
  select * into t from crowley_v2.api_tokens where token_sha256=p_hash and enabled and expires_at>now() and p_scope=any(scopes);
  if not found then return false; end if;
  insert into crowley_v2.api_usage(token_sha256,window_start,requests) values(p_hash,date_trunc('minute',now()),1)
    on conflict(token_sha256,window_start) do update set requests=crowley_v2.api_usage.requests+1 returning requests into n;
  return n<=t.minute_limit;
end $$;
create function crowley_v2.disclose(p_hash text,p_characters integer) returns boolean
language plpgsql set search_path='' as $$
declare t crowley_v2.api_tokens%rowtype; n integer;
begin
  if p_characters is null or p_characters<0 then return false; end if;
  select * into t from crowley_v2.api_tokens where token_sha256=p_hash and enabled and expires_at>now() and 'archive:read'=any(scopes) for update;
  if not found then return false; end if;
  select coalesce(sum(characters),0) into n from crowley_v2.api_usage where token_sha256=p_hash and window_start>=date_trunc('hour',now());
  if n+p_characters>t.hourly_character_limit then return false; end if;
  insert into crowley_v2.api_usage(token_sha256,window_start,characters) values(p_hash,date_trunc('minute',now()),p_characters)
    on conflict(token_sha256,window_start) do update set characters=crowley_v2.api_usage.characters+excluded.characters;
  return true;
end $$;

create function crowley_v2.emit_event(p_key text,p_kind text,p_payload jsonb,p_sources text[] default '{}') returns uuid
language plpgsql set search_path='' as $$
declare e crowley_v2.events%rowtype;
begin
  insert into crowley_v2.events(idempotency_key,kind,payload,source_uris) values(p_key,p_kind,p_payload,p_sources) on conflict do nothing returning * into e;
  if e.id is null then
    select * into strict e from crowley_v2.events where idempotency_key=p_key;
    if e.kind is distinct from p_kind or e.payload is distinct from p_payload or e.source_uris is distinct from p_sources then raise exception 'idempotency key reused with different payload'; end if;
  end if;
  return e.id;
end $$;

create function crowley_v2.write_record(p_kind text,p_key text,p_expected_version integer,p_value jsonb,
  p_sources text[],p_authority text,p_status text,p_actor text,p_expires timestamptz default null,p_confidence numeric default 1)
returns integer language plpgsql set search_path='' as $$
declare r crowley_v2.records%rowtype; v integer;
begin
  if p_actor is null or length(trim(p_actor))=0 or p_expected_version is null or p_expected_version<0 then raise exception 'actor and expected version required'; end if;
  perform pg_advisory_xact_lock(hashtextextended(p_kind||':'||p_key,0));
  select * into r from crowley_v2.records where kind=p_kind and key=p_key for update;
  if coalesce(r.version,0)<>p_expected_version then raise exception 'stale record version' using errcode='40001'; end if;
  v:=p_expected_version+1;
  insert into crowley_v2.records(kind,key,version,value,source_uris,authority,confidence,valid_from,expires_at,status,updated_by)
    values(p_kind,p_key,v,p_value,p_sources,p_authority,p_confidence,now(),p_expires,p_status,p_actor)
    on conflict(kind,key) do update set version=excluded.version,value=excluded.value,source_uris=excluded.source_uris,
      authority=excluded.authority,confidence=excluded.confidence,valid_from=excluded.valid_from,expires_at=excluded.expires_at,
      status=excluded.status,updated_by=excluded.updated_by,updated_at=now() returning * into r;
  insert into crowley_v2.record_versions values(p_kind,p_key,v,to_jsonb(r),now());
  perform crowley_v2.emit_event('record:'||p_kind||':'||p_key||':'||v,'record.changed',jsonb_build_object('kind',p_kind,'key',p_key,'version',v),p_sources);
  return v;
end $$;

create function crowley_v2.read_record(p_kind text,p_key text) returns jsonb
language sql stable set search_path='' as $$
  select to_jsonb(r)||jsonb_build_object('fresh',r.status='active' and (r.expires_at is null or r.expires_at>now()),
    'usable_as_current',r.status='active' and r.authority<>'model_proposal' and (r.expires_at is null or r.expires_at>now()))
  from crowley_v2.records r where kind=p_kind and key=p_key
$$;

create function crowley_v2.create_intent(p_key text,p_objective text,p_trigger jsonb,p_condition jsonb,p_sources text[],p_due timestamptz default null,p_expires timestamptz default null)
returns uuid language plpgsql set search_path='' as $$
declare r crowley_v2.intents%rowtype;
begin
  if p_trigger->>'type' not in ('once','event') or p_trigger->>'type' is null or (p_trigger->>'type'='once' and p_due is null) then raise exception 'unsupported trigger'; end if;
  insert into crowley_v2.intents(idempotency_key,objective,trigger_spec,condition_spec,source_uris,due_at,expires_at)
    values(p_key,p_objective,p_trigger,p_condition,p_sources,p_due,p_expires) on conflict do nothing returning * into r;
  if r.id is null then
    select * into strict r from crowley_v2.intents where idempotency_key=p_key;
    if r.objective is distinct from p_objective or r.trigger_spec is distinct from p_trigger or r.condition_spec is distinct from p_condition or r.source_uris is distinct from p_sources or r.due_at is distinct from p_due or r.expires_at is distinct from p_expires then raise exception 'idempotency conflict'; end if;
  end if;
  return r.id;
end $$;

create function crowley_v2.transition_intent(p_id uuid,p_expected_version integer,p_status text) returns integer
language plpgsql set search_path='' as $$
declare r crowley_v2.intents%rowtype;
begin
  select * into strict r from crowley_v2.intents where id=p_id for update;
  if r.version is distinct from p_expected_version then raise exception 'stale intent version' using errcode='40001'; end if;
  if not ((r.status='draft' and p_status in ('active','cancelled')) or (r.status='active' and p_status in ('paused','fulfilled','cancelled','expired')) or (r.status='paused' and p_status in ('active','cancelled','expired'))) then raise exception 'invalid intent transition'; end if;
  if p_status='active' and r.expires_at<=now() then raise exception 'intent expired'; end if;
  update crowley_v2.intents set status=p_status,version=version+1,updated_at=now() where id=p_id returning * into r;
  perform crowley_v2.emit_event('intent:'||r.id||':'||r.version,'intent.changed',jsonb_build_object('id',r.id,'status',r.status,'version',r.version),r.source_uris);
  return r.version;
end $$;

-- Due events request evaluation of conditions; they do NOT authorize actions.
create function crowley_v2.tick_intents(p_limit integer default 20) returns integer
language plpgsql set search_path='' as $$
declare r crowley_v2.intents%rowtype; n integer:=0;
begin
  if p_limit not between 1 and 100 then raise exception 'invalid batch'; end if;
  for r in select * from crowley_v2.intents where status='active' and due_at<=now()
    and (expires_at is null or expires_at>now()) and last_emitted_version is distinct from version
    order by due_at,id for update skip locked limit p_limit loop
    perform crowley_v2.emit_event('intent-due:'||r.id||':'||r.version,'intent.due',jsonb_build_object('intent_id',r.id,'version',r.version,'condition',r.condition_spec,'requires_condition_check',true),r.source_uris);
    update crowley_v2.intents set last_emitted_version=version where id=r.id;
    n:=n+1;
  end loop;
  return n;
end $$;

create function crowley_v2.spawn_worker(p_key text,p_objective text,p_tools text[],p_sources text[],p_schema jsonb,p_tokens integer,p_calls integer,p_deadline timestamptz,p_event uuid default null)
returns uuid language plpgsql set search_path='' as $$
declare r crowley_v2.workers%rowtype;
begin
  if p_deadline<=now() or p_deadline>now()+interval '24 hours' or p_deadline is null then raise exception 'invalid deadline'; end if;
  if exists(select 1 from unnest(p_tools)t(name) left join crowley_v2.tools c using(name) where c.name is null or not c.enabled or c.authority<>'read') then raise exception 'worker capability denied'; end if;
  insert into crowley_v2.workers(idempotency_key,objective,allowed_tools,source_uris,output_schema,token_budget,call_budget,deadline,event_id)
    values(p_key,p_objective,p_tools,p_sources,p_schema,p_tokens,p_calls,p_deadline,p_event) on conflict do nothing returning * into r;
  if r.id is null then
    select * into strict r from crowley_v2.workers where idempotency_key=p_key;
    if r.objective is distinct from p_objective or r.allowed_tools is distinct from p_tools or r.source_uris is distinct from p_sources or r.output_schema is distinct from p_schema or r.token_budget is distinct from p_tokens or r.call_budget is distinct from p_calls or r.deadline is distinct from p_deadline or r.event_id is distinct from p_event then raise exception 'idempotency conflict'; end if;
  end if;
  return r.id;
end $$;

create function crowley_v2.claim_worker(p_lease_seconds integer default 60) returns jsonb
language plpgsql set search_path='' as $$
declare r crowley_v2.workers%rowtype;
begin
  if p_lease_seconds not between 5 and 120 then raise exception 'invalid lease'; end if;
  update crowley_v2.workers set state='expired',completed_at=now(),lease_token=null,lease_until=null where state in ('queued','running') and deadline<=now();
  update crowley_v2.workers set state='failed',error_code='retry_budget_exhausted',completed_at=now(),lease_token=null,lease_until=null where state='running' and lease_until<now() and attempts>=max_attempts;
  select * into r from crowley_v2.workers where deadline>now() and attempts<max_attempts
    and ((state='queued' and available_at<=now()) or (state='running' and lease_until<now())) order by available_at,id for update skip locked limit 1;
  if not found then return null; end if;
  update crowley_v2.workers set state='running',attempts=attempts+1,lease_token=gen_random_uuid(),lease_until=least(deadline,now()+make_interval(secs=>p_lease_seconds)) where id=r.id returning * into r;
  return to_jsonb(r);
end $$;

-- Reserve budget BEFORE dispatch. Failures still consume reserved calls/tokens.
create function crowley_v2.reserve_worker_call(p_id uuid,p_lease uuid,p_tool text,p_tokens integer) returns boolean
language plpgsql set search_path='' as $$
declare r crowley_v2.workers%rowtype;
begin
  select * into strict r from crowley_v2.workers where id=p_id for update;
  if r.state<>'running' or r.lease_token is distinct from p_lease or r.lease_until<=now() or r.deadline<=now() then raise exception 'stale worker lease'; end if;
  if p_tokens is null or p_tokens<0 or not coalesce(p_tool=any(r.allowed_tools),false) or not exists(select 1 from crowley_v2.tools where name=p_tool and enabled and authority='read') then raise exception 'worker capability denied'; end if;
  if r.calls_used+1>r.call_budget or r.tokens_used+p_tokens>r.token_budget then raise exception 'worker budget exceeded'; end if;
  update crowley_v2.workers set calls_used=calls_used+1,tokens_used=tokens_used+p_tokens where id=p_id;
  return true;
end $$;

create function crowley_v2.complete_worker(p_id uuid,p_lease uuid,p_result jsonb) returns void
language plpgsql set search_path='' as $$
declare r crowley_v2.workers%rowtype;
begin
  select * into strict r from crowley_v2.workers where id=p_id for update;
  if r.state<>'running' or r.lease_token is distinct from p_lease or r.lease_until<=now() or r.deadline<=now() then raise exception 'stale worker lease'; end if;
  if p_result is null or jsonb_typeof(p_result)<>'object' then raise exception 'structured result required'; end if;
  update crowley_v2.workers set state='succeeded',result=p_result,completed_at=now(),lease_token=null,lease_until=null where id=p_id;
  perform crowley_v2.emit_event('worker-result:'||p_id,'worker.result',jsonb_build_object('worker_id',p_id,'result',p_result,'requires_validation',true),r.source_uris);
end $$;

create function crowley_v2.cancel_worker(p_id uuid) returns boolean
language plpgsql set search_path='' as $$
begin
  update crowley_v2.workers set state='cancelled',completed_at=now(),lease_token=null,lease_until=null where id=p_id and state in ('queued','running');
  return found;
end $$;

-- Proposals are inert. No execute_action function ships in this foundation.
create function crowley_v2.propose_action(p_tool text,p_payload jsonb,p_actor text,p_expires timestamptz) returns uuid
language plpgsql set search_path='' as $$
declare result uuid;
begin
  if p_expires is null or p_expires<=now() or p_expires>now()+interval '24 hours' then raise exception 'invalid proposal expiry'; end if;
  insert into crowley_v2.action_proposals(tool_name,payload,payload_hash,proposed_by,expires_at)
    values(p_tool,p_payload,encode(extensions.digest(p_payload::text,'sha256'),'hex'),p_actor,p_expires) returning id into result;
  return result;
end $$;

create function crowley_v2.runtime_context(p_event uuid default null) returns jsonb
language sql stable set search_path='' as $$
  select jsonb_build_object('version','primitive-foundation-1','production_retrieval','v3',
    'event',(select to_jsonb(e) from crowley_v2.events e where id=p_event),
    'records',coalesce((select jsonb_agg(crowley_v2.read_record(kind,key)) from crowley_v2.records where status='active' and (expires_at is null or expires_at>now())),'[]'),
    'tools',coalesce((select jsonb_agg(jsonb_build_object('name',name,'authority',authority,'requires_confirmation',requires_confirmation)) from crowley_v2.tools where enabled and authority='read'),'[]'),
    'archive_status',archive_vnext.status(),'external_actions_enabled',false,
    'model_execution','adapter_required','archive_is_untrusted_data',true)
$$;

do $$ declare t record; begin
  for t in select tablename from pg_tables where schemaname='crowley_v2' loop
    execute format('alter table crowley_v2.%I enable row level security',t.tablename);
  end loop;
end $$;
revoke all on all tables in schema crowley_v2 from public,anon,authenticated,service_role;
revoke all on all sequences in schema crowley_v2 from public,anon,authenticated,service_role;
revoke all on all functions in schema crowley_v2 from public,anon,authenticated,service_role;
commit;
