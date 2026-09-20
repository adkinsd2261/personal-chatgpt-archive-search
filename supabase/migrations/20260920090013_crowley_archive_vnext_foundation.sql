-- Additive only. Depends on the existing archive_private raw mirror.
-- No UPDATE/DELETE/TRIGGER on v3 tables; no public RPC surface or routing change.
begin;
set local lock_timeout = '3s';
create schema if not exists archive_vnext;
revoke all on schema archive_vnext from public, anon, authenticated, service_role;
alter default privileges in schema archive_vnext revoke execute on functions from public;
create extension if not exists vector with schema extensions;

create table archive_vnext.builds (
  generation_id text primary key,
  recipe_version text not null default 'context-frame-v1',
  source_sha256 text not null,
  corpus_cutoff timestamptz,
  expected_turns integer not null check (expected_turns >= 0),
  expected_history integer not null check (expected_history >= 0),
  status text not null default 'building' check (status in ('building','indexed','failed')),
  created_at timestamptz not null default now(),
  completed_at timestamptz
);

create table archive_vnext.frames (
  id bigint generated always as identity primary key,
  generation_id text not null references archive_vnext.builds,
  source_uri text not null,
  conversation_id text not null,
  turn_index integer,
  message_id text,
  source_state text not null check (source_state in ('active_path','inactive_branch')),
  title text not null,
  observed_at timestamptz,
  raw_messages jsonb not null check (jsonb_typeof(raw_messages) = 'array'),
  user_text text not null,
  assistant_text text not null,
  context_text text not null,
  content_hash text not null check (length(content_hash) = 64),
  fts tsvector generated always as (
    setweight(to_tsvector('english', user_text),'A') ||
    setweight(to_tsvector('english', assistant_text),'B') ||
    setweight(to_tsvector('english', title),'C')) stored,
  created_at timestamptz not null default now(),
  unique (generation_id, source_uri)
);
create index frames_fts_idx on archive_vnext.frames using gin(fts);
create index frames_time_idx on archive_vnext.frames (generation_id, observed_at, id);
create index frames_conversation_idx on archive_vnext.frames (generation_id, conversation_id, turn_index);

-- Segments are navigation aids, not generated facts or semantic episodes.
create table archive_vnext.episodes (
  id bigint generated always as identity primary key,
  generation_id text not null references archive_vnext.builds,
  conversation_id text not null,
  segment integer not null,
  title text not null,
  start_at timestamptz,
  end_at timestamptz,
  source_uris text[] not null,
  kind text not null default 'deterministic_navigation_segment'
    check (kind = 'deterministic_navigation_segment'),
  summary text,
  summary_model text,
  unique (generation_id, conversation_id, segment)
);

create table archive_vnext.embedding_jobs (
  frame_id bigint primary key references archive_vnext.frames,
  content_hash text not null,
  state text not null default 'queued' check (state in ('queued','leased','complete','dead','cancelled')),
  attempts integer not null default 0,
  available_at timestamptz not null default now(),
  lease_token uuid,
  lease_until timestamptz,
  last_error text,
  completed_at timestamptz
);
create index embedding_jobs_claim_idx on archive_vnext.embedding_jobs (available_at,frame_id)
  where state in ('queued','leased');

create table archive_vnext.embeddings (
  frame_id bigint not null references archive_vnext.frames,
  piece_index integer not null check (piece_index >= 0),
  content_hash text not null,
  model text not null check (model = 'Supabase/gte-small'),
  tokenizer_version text not null,
  token_count integer not null check (token_count between 1 and 512),
  start_offset integer not null check (start_offset >= 0),
  end_offset integer not null check (end_offset > start_offset),
  embedding extensions.halfvec(384) not null,
  primary key (frame_id, piece_index)
);
create index embeddings_hnsw_idx on archive_vnext.embeddings using hnsw (embedding extensions.halfvec_cosine_ops);

create table archive_vnext.benchmark_suites (
  id uuid primary key default gen_random_uuid(),
  version text not null unique,
  generation_id text not null,
  contract_version text not null default 'vnext-gate-1',
  suite_hash text not null,
  cases jsonb not null check (jsonb_typeof(cases) = 'array'),
  frozen_at timestamptz not null default now()
);
create table archive_vnext.benchmark_runs (
  id uuid primary key default gen_random_uuid(),
  suite_id uuid not null references archive_vnext.benchmark_suites,
  candidate_revision text not null,
  baseline_revision text not null,
  kind text not null check (kind in ('lexical_component','hybrid_component','agentic_end_to_end')),
  status text not null default 'running' check (status in ('running','completed','blocked','failed')),
  budgets jsonb not null,
  metrics jsonb not null default '{}',
  gate_status text not null default 'blocked' check (gate_status in ('blocked','failed','eligible_for_review')),
  started_at timestamptz not null default now(),
  completed_at timestamptz
);
create table archive_vnext.benchmark_results (
  run_id uuid not null references archive_vnext.benchmark_runs,
  case_key text not null,
  arm text not null check (arm in ('v3','vnext')),
  result jsonb not null,
  elapsed_ms numeric not null check (elapsed_ms >= 0),
  primary key (run_id,case_key,arm)
);

create function archive_vnext.reject_mutation() returns trigger
language plpgsql set search_path = '' as $$
begin raise exception 'immutable evidence or frozen evaluation'; end $$;
create trigger frames_immutable before update or delete on archive_vnext.frames
  for each row execute function archive_vnext.reject_mutation();
create trigger suites_immutable before update or delete on archive_vnext.benchmark_suites
  for each row execute function archive_vnext.reject_mutation();

create function archive_vnext.begin_build(p_generation text) returns jsonb
language plpgsql set search_path = '' as $$
declare g archive_private.generations%rowtype;
begin
  select * into strict g from archive_private.generations where generation_id=p_generation;
  if g.status not in ('active','ready','superseded') then raise exception 'source generation is not ready'; end if;
  insert into archive_vnext.builds(generation_id,source_sha256,corpus_cutoff,expected_turns,expected_history)
    values(g.generation_id,g.source_database_sha256,g.corpus_cutoff,g.expected_turns,g.expected_history_messages)
    on conflict do nothing;
  return jsonb_build_object('generation_id',p_generation,'mode','shadow');
end $$;

create function archive_vnext.index_turn_batch(p_generation text,p_after bigint default 0,p_limit integer default 250)
returns jsonb language plpgsql set search_path = '' set statement_timeout = '45s' as $$
declare n integer; last_id bigint; selected integer;
begin
  if p_limit not between 1 and 1000 then raise exception 'batch size out of bounds'; end if;
  if not exists(select 1 from archive_vnext.builds where generation_id=p_generation) then raise exception 'begin build first'; end if;
  with batch as materialized (
    select t.*, p.assistant_text previous_assistant, p.turn_index previous_index
    from (select * from archive_private.turns where generation_id=p_generation and id>p_after order by id limit p_limit)t
    left join archive_private.turns p on p.generation_id=t.generation_id and p.conversation_id=t.conversation_id and p.turn_index=t.turn_index-1
  ), docs as (
    select b.*, 'archive://conversation/'||conversation_id||'/turn/'||turn_index uri,
      jsonb_build_array(
        jsonb_build_object('role','assistant','position','before','turn_index',previous_index,'text',coalesce(previous_assistant,''),'authority','discovery_only'),
        jsonb_build_object('role','user','position','anchor','turn_index',turn_index,'text',coalesce(user_text,''),'authority','primary_user_evidence'),
        jsonb_build_object('role','assistant','position','after','turn_index',turn_index,'text',coalesce(assistant_text,''),'authority','discovery_only')) messages,
      'Title: '||coalesce(title,'')||E'\nDate: '||coalesce(create_time::text,'unknown')||
      E'\n[assistant before; discovery only]\n'||coalesce(previous_assistant,'')||
      E'\n[user; primary evidence]\n'||coalesce(user_text,'')||
      E'\n[assistant after; discovery only]\n'||coalesce(assistant_text,'') body
    from batch b
  ), ins as (
    insert into archive_vnext.frames(generation_id,source_uri,conversation_id,turn_index,source_state,title,observed_at,
      raw_messages,user_text,assistant_text,context_text,content_hash)
    select p_generation,uri,conversation_id,turn_index,'active_path',coalesce(title,''),create_time,messages,
      coalesce(user_text,''),coalesce(previous_assistant,'')||E'\n'||coalesce(assistant_text,''),body,
      encode(extensions.digest(body,'sha256'),'hex') from docs
    on conflict do nothing returning id,content_hash
  ), jobs as (
    insert into archive_vnext.embedding_jobs(frame_id,content_hash) select id,content_hash from ins on conflict do nothing returning frame_id
  ) select (select count(*) from ins),(select max(id) from batch),(select count(*) from batch) into n,last_id,selected;
  return jsonb_build_object('inserted',n,'selected',selected,'after_id',coalesce(last_id,p_after),'done',selected<p_limit);
end $$;

create function archive_vnext.index_history_batch(p_generation text,p_after bigint default 0,p_limit integer default 250)
returns jsonb language plpgsql set search_path = '' set statement_timeout = '45s' as $$
declare n integer; last_id bigint; selected integer;
begin
  if p_limit not between 1 and 1000 then raise exception 'batch size out of bounds'; end if;
  with batch as materialized (
    select * from archive_private.history_messages where generation_id=p_generation and id>p_after order by id limit p_limit
  ), docs as (
    select b.*, 'Title: '||coalesce(title,'')||E'\nDate: '||coalesce(create_time::text,'unknown')||
      E'\n[inactive branch; '||role||E']\n'||coalesce(text,'') body from batch b
  ), ins as (
    insert into archive_vnext.frames(generation_id,source_uri,conversation_id,message_id,source_state,title,observed_at,
      raw_messages,user_text,assistant_text,context_text,content_hash)
    select p_generation,'archive://conversation/'||conversation_id||'/message/'||message_id,conversation_id,message_id,
      'inactive_branch',coalesce(title,''),create_time,
      jsonb_build_array(jsonb_build_object('role',role,'text',text,'message_id',message_id,'parent_id',parent_id,
        'authority',case when role='user' then 'primary_user_evidence' else 'discovery_only' end)),
      case when role='user' then coalesce(text,'') else '' end,
      case when role='assistant' then coalesce(text,'') else '' end,body,encode(extensions.digest(body,'sha256'),'hex')
    from docs on conflict do nothing returning id,content_hash
  ), jobs as (
    insert into archive_vnext.embedding_jobs(frame_id,content_hash) select id,content_hash from ins on conflict do nothing returning frame_id
  ) select (select count(*) from ins),(select max(id) from batch),(select count(*) from batch) into n,last_id,selected;
  return jsonb_build_object('inserted',n,'selected',selected,'after_id',coalesce(last_id,p_after),'done',selected<p_limit);
end $$;

create function archive_vnext.finish_build(p_generation text) returns jsonb
language plpgsql set search_path = '' set statement_timeout = '45s' as $$
declare b archive_vnext.builds%rowtype; a integer; h integer;
begin
  select * into strict b from archive_vnext.builds where generation_id=p_generation for update;
  select count(*) filter(where source_state='active_path'),count(*) filter(where source_state='inactive_branch')
    into a,h from archive_vnext.frames where generation_id=p_generation;
  if a<>b.expected_turns or h<>b.expected_history then raise exception 'incomplete frame coverage: %, %',a,h; end if;
  insert into archive_vnext.episodes(generation_id,conversation_id,segment,title,start_at,end_at,source_uris)
    select generation_id,conversation_id,turn_index/8,min(title),min(observed_at),max(observed_at),array_agg(source_uri order by turn_index)
    from archive_vnext.frames where generation_id=p_generation and source_state='active_path'
    group by generation_id,conversation_id,turn_index/8 on conflict do nothing;
  update archive_vnext.builds set status='indexed',completed_at=now() where generation_id=p_generation;
  return jsonb_build_object('frames',a+h,'lexical_complete',true,'semantic_complete',false,'production_route','v3');
end $$;

create function archive_vnext.status(p_generation text default null) returns jsonb
language sql stable set search_path = '' as $$
  select jsonb_build_object('mode','shadow','production_route','v3','generation_id',b.generation_id,
    'corpus_cutoff',b.corpus_cutoff,'corpus_age_hours',extract(epoch from (now()-b.corpus_cutoff))/3600,
    'freshness_status',case when b.corpus_cutoff>now()-interval '24 hours' then 'fresh' when b.corpus_cutoff>now()-interval '72 hours' then 'aging' else 'stale' end,
    'current_state_safe',coalesce(b.corpus_cutoff>now()-interval '72 hours',false),
    'build_status',b.status,'frames',(select count(*) from archive_vnext.frames f where f.generation_id=b.generation_id),
    'embedded_frames',(select count(*) from archive_vnext.embedding_jobs j join archive_vnext.frames f on f.id=j.frame_id where f.generation_id=b.generation_id and j.state='complete'),
    'dead_jobs',(select count(*) from archive_vnext.embedding_jobs j join archive_vnext.frames f on f.id=j.frame_id where f.generation_id=b.generation_id and j.state='dead'),
    'episode_kind','deterministic_navigation_segment','exhaustive',false)
  from archive_vnext.builds b where b.generation_id=coalesce(p_generation,(select active_generation_id from archive_private.corpus_state));
$$;

create function archive_vnext.search(p_query text,p_embedding extensions.halfvec(384) default null,
  p_generation text default null,p_role text default 'both',p_from timestamptz default null,p_to timestamptz default null,
  p_limit integer default 8,p_include_inactive boolean default false,p_exact boolean default false)
returns jsonb language plpgsql stable set search_path = '' set statement_timeout = '12s' as $$
declare g text; hits jsonb; tsq tsquery;
begin
  if p_query is null or length(trim(p_query)) not between 1 and 2000 or p_role not in ('user','assistant','both')
    or p_limit not between 1 and 20 or (p_from is not null and p_to is not null and p_from>p_to) then raise exception 'invalid search parameters'; end if;
  g := coalesce(p_generation,(select active_generation_id from archive_private.corpus_state));
  if not exists(select 1 from archive_vnext.builds where generation_id=g) then raise exception 'generation not indexed'; end if;
  tsq := websearch_to_tsquery('english',p_query);
  with lexical as materialized (
    select f.id, row_number() over(order by ts_rank_cd(f.fts,tsq) desc,f.id) rank
    from archive_vnext.frames f where f.generation_id=g
      and (p_include_inactive or f.source_state='active_path')
      and (p_from is null or f.observed_at>=p_from) and (p_to is null or f.observed_at<=p_to)
      and case when p_exact then position(lower(p_query) in lower(case p_role when 'user' then f.user_text when 'assistant' then f.assistant_text else f.user_text||E'\n'||f.assistant_text end))>0
        else f.fts@@tsq and (p_role='both' or to_tsvector('english',case p_role when 'user' then f.user_text else f.assistant_text end)@@tsq) end
    order by ts_rank_cd(f.fts,tsq) desc,f.id limit 80
  ), nearest as materialized (
    select e.frame_id, min(e.embedding operator(extensions.<=>) p_embedding) distance
    from (select * from archive_vnext.embeddings where p_embedding is not null order by embedding operator(extensions.<=>) p_embedding limit 400)e
    join archive_vnext.frames f on f.id=e.frame_id where f.generation_id=g and not p_exact
      and (p_include_inactive or f.source_state='active_path')
      and (p_from is null or f.observed_at>=p_from) and (p_to is null or f.observed_at<=p_to)
      and (p_role='both' or length(case p_role when 'user' then f.user_text else f.assistant_text end)>0)
    group by e.frame_id
  ), semantic as (select frame_id id,row_number() over(order by distance,id) rank from (select frame_id,distance,frame_id id from nearest)n order by distance limit 80),
  fused as (
    select id,sum(score) score from (select id,1.0/(60+rank) score from lexical union all select id,1.0/(60+rank) score from semantic)s group by id
  ), selected as (
    select f.*,s.score from fused s join archive_vnext.frames f using(id) order by s.score desc,f.id limit p_limit
  ) select coalesce(jsonb_agg(jsonb_build_object('source_uri',source_uri,'generation_id',generation_id,
      'content_hash',content_hash,'title',title,'date',observed_at,'source_state',source_state,
      'score',score,'user_excerpt',left(user_text,1000),'assistant_excerpt',left(assistant_text,1000),
      'excerpt_only',true,'assistant_authority','discovery_only','must_open_source',true) order by score desc,id),'[]') into hits from selected;
  return jsonb_build_object('schema','ArchiveSearch/vNext1','matches',hits,'support_state',case when jsonb_array_length(hits)>0 then 'candidates_only' else 'under_supported' end,
    'retrieval_mode',case when p_embedding is null or p_exact then 'lexical' else 'hybrid_rrf' end,'role_scope',p_role,
    'semantic_role_scope','contextual_discovery_not_authorship','coverage','representative','exhaustive',false,'status',archive_vnext.status(g));
end $$;

create function archive_vnext.open_context(p_source_uri text,p_generation text default null,p_offset integer default 0,p_length integer default 16000)
returns jsonb language plpgsql stable set search_path = '' as $$
declare f archive_vnext.frames%rowtype; total integer;
begin
  if p_offset<0 or p_length not between 1 and 20000 then raise exception 'invalid page'; end if;
  select * into strict f from archive_vnext.frames where generation_id=coalesce(p_generation,(select active_generation_id from archive_private.corpus_state)) and source_uri=p_source_uri;
  total := length(f.context_text);
  return jsonb_build_object('source_uri',f.source_uri,'generation_id',f.generation_id,'content_hash',f.content_hash,
    'source_state',f.source_state,'title',f.title,'date',f.observed_at,
    'content',substr(f.context_text,p_offset+1,p_length),'offset',p_offset,'total_characters',total,
    'next_offset',case when p_offset+p_length<total then p_offset+p_length end,
    'truncated',p_offset+p_length<total,'authority','role_labels_required; assistant_text_is_discovery_only',
    'status',archive_vnext.status(f.generation_id));
end $$;

create function archive_vnext.browse_time(p_from timestamptz,p_to timestamptz,p_generation text default null,
  p_after_time timestamptz default null,p_after_id bigint default 0,p_limit integer default 20)
returns jsonb language plpgsql stable set search_path = '' as $$
declare result jsonb;
begin
  if p_from is null or p_to is null or p_from>p_to or p_limit not between 1 and 100 then raise exception 'invalid timeline bounds'; end if;
  select coalesce(jsonb_agg(jsonb_build_object('id',id,'source_uri',source_uri,'title',title,'date',observed_at,'source_state',source_state) order by observed_at,id),'[]') into result
    from (select * from archive_vnext.frames where generation_id=coalesce(p_generation,(select active_generation_id from archive_private.corpus_state))
      and observed_at between p_from and p_to and (p_after_time is null or (observed_at,id)>(p_after_time,p_after_id))
      order by observed_at,id limit p_limit)f;
  return jsonb_build_object('matches',result,'page_full',jsonb_array_length(result)=p_limit,'cursor_fields',jsonb_build_array('date','id'),'coverage','paginated_not_exhaustive');
end $$;

create function archive_vnext.claim_embedding(p_lease_seconds integer default 120) returns jsonb
language plpgsql set search_path = '' as $$
declare j archive_vnext.embedding_jobs%rowtype; f archive_vnext.frames%rowtype;
begin
  if p_lease_seconds not between 30 and 300 then raise exception 'invalid lease'; end if;
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

create function archive_vnext.complete_embedding(p_frame bigint,p_lease uuid,p_hash text,p_pieces jsonb) returns void
language plpgsql set search_path = '' as $$
declare j archive_vnext.embedding_jobs%rowtype; f archive_vnext.frames%rowtype; item jsonb; last_end integer:=0; i integer:=0;
begin
  select * into strict j from archive_vnext.embedding_jobs where frame_id=p_frame for update;
  select * into strict f from archive_vnext.frames where id=p_frame;
  if j.state<>'leased' or j.lease_token<>p_lease or j.lease_until<now() or j.content_hash<>p_hash or f.content_hash<>p_hash then raise exception 'stale embedding lease'; end if;
  if jsonb_typeof(p_pieces)<>'array' or jsonb_array_length(p_pieces) not between 1 and 10000 then raise exception 'invalid embedding pieces'; end if;
  for item in select value from jsonb_array_elements(p_pieces) loop
    if (item->>'start_offset')::integer>last_end or (item->>'end_offset')::integer<=last_end or
       (item->>'start_offset')::integer<0 or (item->>'end_offset')::integer>length(f.context_text) or
       (item->>'token_count')::integer not between 1 and 512 or jsonb_array_length(item->'embedding')<>384 or
       item->>'tokenizer_version' is null then raise exception 'incomplete or invalid embedding coverage'; end if;
    insert into archive_vnext.embeddings values(p_frame,i,p_hash,'Supabase/gte-small',item->>'tokenizer_version',
      (item->>'token_count')::integer,(item->>'start_offset')::integer,(item->>'end_offset')::integer,(item->'embedding')::text::extensions.halfvec(384));
    last_end:=(item->>'end_offset')::integer; i:=i+1;
  end loop;
  if last_end<>length(f.context_text) then raise exception 'embedding coverage gap'; end if;
  update archive_vnext.embedding_jobs set state='complete',completed_at=now(),lease_until=null,lease_token=null,last_error=null where frame_id=p_frame;
end $$;

create function archive_vnext.fail_embedding(p_frame bigint,p_lease uuid,p_error text) returns void
language plpgsql set search_path = '' as $$
begin
  update archive_vnext.embedding_jobs set state=case when attempts>=5 then 'dead' else 'queued' end,
    available_at=now()+make_interval(secs=>least(3600,30*power(2,attempts)::integer)),lease_token=null,lease_until=null,last_error=left(p_error,300)
    where frame_id=p_frame and state='leased' and lease_token=p_lease and lease_until>now();
  if not found then raise exception 'stale embedding lease'; end if;
end $$;

do $$ declare t record; begin
  for t in select tablename from pg_tables where schemaname='archive_vnext' loop
    execute format('alter table archive_vnext.%I enable row level security',t.tablename);
  end loop;
end $$;
revoke all on all tables in schema archive_vnext from public,anon,authenticated,service_role;
revoke all on all sequences in schema archive_vnext from public,anon,authenticated,service_role;
revoke all on all functions in schema archive_vnext from public,anon,authenticated,service_role;
commit;
