-- Large derived indexes can outlive a management API request. Install a bounded
-- database-side builder; operators schedule it only when the synchronous build
-- times out. It never changes legacy tables or enables production routing.
create or replace function archive_vnext.build_literal_index_once() returns jsonb
language plpgsql set search_path='' as $$
declare job bigint; valid boolean; failures integer;
begin
  select jobid into job from cron.job where jobname='crowley-vnext-literal-index-once';
  select indisvalid into valid from pg_catalog.pg_index
    where indexrelid=to_regclass('archive_vnext.frames_context_trgm_idx');
  if coalesce(valid,false) then
    if job is not null then perform cron.unschedule(job); end if;
    return '{"state":"ready"}';
  end if;
  select count(*) into failures from cron.job_run_details
    where jobid=job and status='failed';
  if failures>=2 then
    if job is not null then perform cron.unschedule(job); end if;
    return '{"state":"stopped","reason":"retry_budget"}';
  end if;
  create index if not exists frames_context_trgm_idx
    on archive_vnext.frames using gin (context_text extensions.gin_trgm_ops);
  if not exists(select 1 from pg_catalog.pg_index
    where indexrelid=to_regclass('archive_vnext.frames_context_trgm_idx') and indisvalid) then
    raise exception 'literal index is not valid';
  end if;
  analyze archive_vnext.frames;
  if job is not null then perform cron.unschedule(job); end if;
  return '{"state":"ready"}';
end $$;
revoke all on function archive_vnext.build_literal_index_once()
  from public,anon,authenticated,service_role;
-- Optional operator invocation:
-- select cron.schedule('crowley-vnext-literal-index-once','* * * * *',
--   'set statement_timeout=''10min''; set lock_timeout=''5s''; select archive_vnext.build_literal_index_once();');
