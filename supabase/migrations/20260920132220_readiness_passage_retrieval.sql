-- Small literal passages prevent lossy trigram rechecks from reading full frames.
-- Original frames and the v3 route are untouched. Existing search remains the
-- fallback until every frame in a generation has been indexed.
begin;
set local lock_timeout='3s';
create table archive_vnext.literal_frames (
  frame_id bigint primary key references archive_vnext.frames,
  generation_id text not null,
  indexed_at timestamptz not null default now()
);
create index literal_frames_generation_idx on archive_vnext.literal_frames(generation_id,frame_id);
create table archive_vnext.literal_passages (
  frame_id bigint not null references archive_vnext.frames,
  role text not null check(role in ('user','assistant','boundary')),
  start_offset integer not null check(start_offset>=0),
  text text not null check(length(text) between 1 and 4096),
  primary key(frame_id,role,start_offset)
);
create index literal_passages_trgm_idx on archive_vnext.literal_passages using gin(text extensions.gin_trgm_ops);

create function archive_vnext.index_literal_batch(p_generation text,p_limit integer default 100) returns jsonb
language plpgsql set search_path='' as $$
declare f record; r record; pos integer; n integer:=0;
begin
  if p_limit not between 1 and 500 then raise exception 'invalid batch size'; end if;
  if not pg_try_advisory_xact_lock(hashtextextended('archive_vnext.literal.'||p_generation,0)) then return '{"state":"busy"}'; end if;
  if not exists(select 1 from archive_vnext.builds where generation_id=p_generation) then raise exception 'generation not indexed'; end if;
  for f in select a.id,a.user_text,a.assistant_text from archive_vnext.frames a
    where a.generation_id=p_generation and not exists(select 1 from archive_vnext.literal_frames b where b.frame_id=a.id)
    order by a.id limit p_limit
  loop
    for r in select * from (values('user',f.user_text),('assistant',f.assistant_text))v(role,body)
    loop
      pos:=1;
      while pos<=length(r.body) loop
        insert into archive_vnext.literal_passages values(f.id,r.role,pos-1,substr(r.body,pos,4096));
        exit when pos+4095>=length(r.body);
        -- A literal query is at most 2000 code points. This overlap guarantees
        -- that a match crossing a passage edge is fully present in one passage.
        pos:=pos+2097;
      end loop;
    end loop;
    if length(f.user_text)>0 and length(f.assistant_text)>0 then
      insert into archive_vnext.literal_passages values(f.id,'boundary',greatest(0,length(f.user_text)-1999),right(f.user_text,1999)||E'\n'||left(f.assistant_text,1999));
    end if;
    insert into archive_vnext.literal_frames(frame_id,generation_id) values(f.id,p_generation);
    n:=n+1;
  end loop;
  return jsonb_build_object('inserted',n,'complete',archive_vnext.literal_ready(p_generation));
end $$;

create function archive_vnext.literal_ready(p_generation text) returns boolean
language sql stable set search_path='' as $$
  select exists(select 1 from archive_vnext.frames where generation_id=p_generation)
    and not exists(select 1 from archive_vnext.frames f where f.generation_id=p_generation
      and not exists(select 1 from archive_vnext.literal_frames l where l.frame_id=f.id));
$$;

create function archive_vnext.search_literal(p_query text,p_generation text,p_role text,
  p_from timestamptz,p_to timestamptz,p_limit integer,p_include_inactive boolean) returns jsonb
language plpgsql stable set search_path='' set plan_cache_mode='force_custom_plan' as $$
declare hits jsonb; pattern text;
begin
  if p_query is null or length(trim(p_query)) not between 1 and 2000 or p_role not in ('user','assistant','both')
    or p_limit not between 1 and 20 or (p_from is not null and p_to is not null and p_from>p_to) then raise exception 'invalid search parameters'; end if;
  pattern:='%'||replace(replace(replace(p_query,E'\\',E'\\\\'),'%',E'\\%'),'_',E'\\_')||'%';
  with candidates as materialized (
    select distinct p.frame_id from archive_vnext.literal_passages p
      where p.text ilike pattern escape E'\\' and (p_role='both' or p.role=p_role)
  ), selected as (
    select f.* from candidates c join archive_vnext.frames f on f.id=c.frame_id
    where f.generation_id=p_generation and (p_include_inactive or f.source_state='active_path')
      and (p_from is null or f.observed_at>=p_from) and (p_to is null or f.observed_at<=p_to)
    order by f.observed_at desc nulls last,f.id limit p_limit
  ) select coalesce(jsonb_agg(jsonb_build_object('source_uri',source_uri,'generation_id',generation_id,
      'content_hash',content_hash,'title',title,'date',observed_at,'source_state',source_state,
      'score',1,'user_excerpt',left(user_text,1000),'assistant_excerpt',left(assistant_text,1000),
      'excerpt_only',true,'assistant_authority','discovery_only','must_open_source',true) order by observed_at desc nulls last,id),'[]') into hits from selected;
  return jsonb_build_object('schema','ArchiveSearch/vNext1','matches',hits,
    'support_state',case when jsonb_array_length(hits)>0 then 'candidates_only' else 'under_supported' end,
    'retrieval_mode','lexical','lexical_index','bounded_literal_passages_v1','literal_order','newest_first',
    'role_scope',p_role,'semantic_role_scope','contextual_discovery_not_authorship',
    'coverage','representative','exhaustive',false,'status',archive_vnext.status(p_generation));
end $$;

create or replace function archive_vnext.search(p_query text,p_embedding extensions.halfvec(384) default null,
  p_generation text default null,p_role text default 'both',p_from timestamptz default null,p_to timestamptz default null,
  p_limit integer default 8,p_include_inactive boolean default false,p_exact boolean default false)
returns jsonb language plpgsql stable set search_path = '' set statement_timeout = '12s' set plan_cache_mode = 'force_custom_plan' as $$
declare g text; hits jsonb; tsq tsquery; literal_pattern text;
begin
  if p_query is null or length(trim(p_query)) not between 1 and 2000 or p_role not in ('user','assistant','both')
    or p_limit not between 1 and 20 or (p_from is not null and p_to is not null and p_from>p_to) then raise exception 'invalid search parameters'; end if;
  g := coalesce(p_generation,(select active_generation_id from archive_private.corpus_state));
  if not exists(select 1 from archive_vnext.builds where generation_id=g) then raise exception 'generation not indexed'; end if;
  if p_exact and archive_vnext.literal_ready(g) then
    return archive_vnext.search_literal(p_query,g,p_role,p_from,p_to,p_limit,p_include_inactive);
  end if;
  tsq := websearch_to_tsquery('english',p_query);
  literal_pattern := '%'||replace(replace(replace(p_query,E'\\',E'\\\\'),'%',E'\\%'),'_',E'\\_')||'%';
  with lexical as materialized (
    select f.id, row_number() over(order by ts_rank_cd(f.fts,tsq) desc,f.id) rank
    from archive_vnext.frames f where f.generation_id=g
      and (p_include_inactive or f.source_state='active_path')
      and (p_from is null or f.observed_at>=p_from) and (p_to is null or f.observed_at<=p_to)
      and case when p_exact then f.context_text ilike literal_pattern escape E'\\' and position(lower(p_query) in lower(case p_role when 'user' then f.user_text when 'assistant' then f.assistant_text else f.user_text||E'\n'||f.assistant_text end))>0
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

revoke all on function archive_vnext.search(text,extensions.halfvec,text,text,timestamptz,timestamptz,integer,boolean,boolean) from public,anon,authenticated,service_role;

alter table archive_vnext.literal_frames enable row level security;
alter table archive_vnext.literal_passages enable row level security;
revoke all on all tables in schema archive_vnext from public,anon,authenticated,service_role;
revoke all on all functions in schema archive_vnext from public,anon,authenticated,service_role;
commit;
