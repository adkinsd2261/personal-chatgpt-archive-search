-- Literal searches must use the trigram index before inspecting full role text.
-- Escaping keeps percent, underscore and backslash literal, not SQL wildcards.
-- Only the new immutable frame table is indexed; legacy retrieval is unchanged.
create extension if not exists pg_trgm with schema extensions;
create index if not exists frames_context_trgm_idx on archive_vnext.frames using gin (context_text extensions.gin_trgm_ops);

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
analyze archive_vnext.frames;
