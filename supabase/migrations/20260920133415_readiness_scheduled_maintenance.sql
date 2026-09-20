-- Schedules are installed separately after development verification.
begin;
create function archive_vnext.dispatch_literal_build() returns jsonb
language plpgsql set search_path='' as $$
declare g text; r jsonb; until_at timestamptz;
begin
  select protected_until into until_at from crowley_v2.operating_policy where singleton and maintenance_enabled;
  if until_at is null or until_at<=now() then return '{"state":"outside_operating_window"}'; end if;
  select active_generation_id into g from archive_private.corpus_state;
  if not exists(select 1 from archive_vnext.builds where generation_id=g) then return '{"state":"generation_not_built"}'; end if;
  r:=archive_vnext.index_literal_batch(g,100);
  if coalesce((r->>'complete')::boolean,false) then
    analyze archive_vnext.literal_passages;
    analyze archive_vnext.literal_frames;
    if exists(select 1 from cron.job where jobname='crowley-vnext-literal-build') then perform cron.unschedule('crowley-vnext-literal-build'); end if;
  end if;
  return r;
end $$;
revoke all on function archive_vnext.dispatch_literal_build() from public,anon,authenticated,service_role;
commit;
