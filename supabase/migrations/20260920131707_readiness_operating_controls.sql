-- Private operating controls. Configuration is explicit; no route promotion.
begin;
set local lock_timeout='3s';

create table if not exists crowley_v2.operating_policy (
  singleton boolean primary key default true check(singleton),
  protected_until timestamptz,
  maintenance_enabled boolean not null default false,
  intent_dispatch_enabled boolean not null default false,
  retention_days integer not null default 45 check(retention_days between 35 and 180),
  max_transient_recoveries integer not null default 12 check(max_transient_recoveries between 0 and 24),
  updated_at timestamptz not null default now()
);
insert into crowley_v2.operating_policy values(true,null,false,false,45,12,now()) on conflict(singleton) do nothing;

create table if not exists crowley_v2.operating_checks (
  id bigint generated always as identity primary key,
  checked_at timestamptz not null default now(),
  result jsonb not null check(jsonb_typeof(result)='object')
);
create index if not exists operating_checks_time_idx on crowley_v2.operating_checks(checked_at);
create table if not exists crowley_v2.operating_alerts (
  key text primary key,
  severity text not null check(severity in ('warning','critical')),
  active boolean not null default true,
  first_seen timestamptz not null default now(),
  last_seen timestamptz not null default now(),
  resolved_at timestamptz,
  details jsonb not null default '{}',
  last_notified_at timestamptz
);

alter table archive_vnext.backfill_control
  add column if not exists retry_after timestamptz,
  add column if not exists last_recovered_at timestamptz,
  add column if not exists transient_recoveries integer not null default 0,
  add column if not exists recovery_window_start timestamptz not null default now();

create or replace function crowley_v2.operating_alert(p_key text,p_severity text,p_active boolean,p_details jsonb default '{}')
returns void language plpgsql set search_path='' as $$
begin
  if p_active then
    insert into crowley_v2.operating_alerts(key,severity,details) values(p_key,p_severity,p_details)
    on conflict(key) do update set severity=excluded.severity,active=true,last_seen=now(),
      first_seen=case when crowley_v2.operating_alerts.active then crowley_v2.operating_alerts.first_seen else now() end,
      resolved_at=null,details=excluded.details;
  else
    update crowley_v2.operating_alerts set active=false,resolved_at=now(),last_seen=now() where key=p_key and active;
  end if;
end $$;

create or replace function crowley_v2.recovery_status() returns jsonb
language plpgsql stable set search_path='' as $$
declare r jsonb;
begin
  if to_regprocedure('archive_private.archive_recovery_readiness_v1()') is null then
    return '{"release_blocker":true,"restore_drill_proven":false,"release_blocker_reason":"recovery adapter unavailable"}';
  end if;
  execute 'select archive_private.archive_recovery_readiness_v1()' into r;
  return r;
end $$;

create or replace function crowley_v2.operating_status() returns jsonb
language plpgsql stable set search_path='' as $$
declare p crowley_v2.operating_policy%rowtype; c archive_vnext.backfill_control%rowtype;
  s jsonb; credential_until timestamptz; last_ok timestamptz; pending bigint;
begin
  select * into p from crowley_v2.operating_policy where singleton;
  select * into c from archive_vnext.backfill_control where singleton;
  s:=archive_vnext.status(null);
  select max(t.expires_at) into credential_until from crowley_v2.api_tokens t join vault.decrypted_secrets v
    on t.token_sha256=encode(extensions.digest(v.decrypted_secret,'sha256'),'hex')
    where v.name=c.vault_secret_name and t.enabled and 'embedding:write'=any(t.scopes);
  select max(created_at) into last_ok from archive_vnext.backfill_dispatches where status_code=200;
  select count(*) into pending from archive_vnext.embedding_jobs where state in ('queued','leased');
  return jsonb_build_object('schema','CrowleyOperatingStatus/1','checked_at',now(),'production_route','v3',
    'protected_until',p.protected_until,'maintenance_enabled',p.maintenance_enabled,
    'maintenance_last_at',(select max(checked_at) from crowley_v2.operating_checks),
    'intent_dispatch_enabled',p.intent_dispatch_enabled,'archive',s,
    'backfill',jsonb_build_object('enabled',c.enabled,'stop_reason',c.stop_reason,'stop_at',c.stop_at,
      'pending_frames',pending,'requests_used',c.requests_dispatched,'request_budget',c.request_budget,
      'credential_valid_until',credential_until,'last_success_at',last_ok,'retry_after',c.retry_after,
      'transient_recoveries',c.transient_recoveries),
    'runtime',jsonb_build_object('host','ChatGPT/Codex','record_count',(select count(*) from crowley_v2.records),
      'active_intents',(select count(*) from crowley_v2.intents where status='active'),
      'pending_workers',(select count(*) from crowley_v2.workers where state in ('queued','running')),
      'failed_workers',(select count(*) from crowley_v2.workers where state='failed')),
    'recovery',crowley_v2.recovery_status(),
    'alerts',coalesce((select jsonb_agg(jsonb_build_object('key',key,'severity',severity,'first_seen',first_seen,
      'last_seen',last_seen,'details',details) order by key) from crowley_v2.operating_alerts where active),'[]'),
    'promotion','requires_separate_release_gate');
end $$;

create or replace function crowley_v2.maintenance_tick() returns jsonb
language plpgsql set search_path='' as $$
declare p crowley_v2.operating_policy%rowtype; c archive_vnext.backfill_control%rowtype;
  n integer; s jsonb; alerts integer:=0; last_ok timestamptz; token_ok boolean;
begin
  if not pg_try_advisory_xact_lock(hashtextextended('crowley_v2.maintenance',0)) then return '{"state":"busy"}'; end if;
  select * into p from crowley_v2.operating_policy where singleton;
  if not p.maintenance_enabled then return '{"state":"disabled"}'; end if;
  update crowley_v2.workers set state='expired',lease_token=null,lease_until=null,completed_at=now()
    where state in ('queued','running') and deadline<=now();
  update crowley_v2.workers set state='failed',lease_token=null,lease_until=null,completed_at=now(),error_code='retry_budget_exhausted'
    where state='running' and lease_until<now() and attempts>=max_attempts;
  update crowley_v2.workers set state='queued',lease_token=null,lease_until=null,available_at=now()+interval '1 minute',error_code='expired_lease_requeued'
    where state='running' and lease_until<now() and attempts<max_attempts and deadline>now();
  update archive_vnext.embedding_jobs set state='dead',lease_token=null,lease_until=null,last_error='retry_budget_exhausted'
    where state='leased' and lease_until<now() and attempts>=5;
  update crowley_v2.intents set status='expired',version=version+1,updated_at=now()
    where status in ('draft','active','paused') and expires_at<=now();
  update crowley_v2.action_proposals set status='expired' where status in ('proposed','approved') and expires_at<=now();
  if p.intent_dispatch_enabled then perform crowley_v2.tick_intents(20); end if;

  select * into c from archive_vnext.backfill_control where singleton for update;
  if c.recovery_window_start<now()-interval '1 day' then
    update archive_vnext.backfill_control set transient_recoveries=0,recovery_window_start=now() where singleton;
    c.transient_recoveries:=0;
  end if;
  select exists(select 1 from vault.decrypted_secrets v join crowley_v2.api_tokens t
    on t.token_sha256=encode(extensions.digest(v.decrypted_secret,'sha256'),'hex')
    where v.name=c.vault_secret_name and t.enabled and t.expires_at>now() and 'embedding:write'=any(t.scopes)) into token_ok;
  -- Recover only a transient circuit break. Never override a deadline, budget,
  -- credential failure, deliberate disable, exhausted retry budget, or cutover gate.
  if not c.enabled and c.stop_reason='repeated_failures' and c.stop_at>now()
    and c.requests_dispatched<c.request_budget and token_ok and c.transient_recoveries<p.max_transient_recoveries
    and coalesce(c.retry_after,'-infinity'::timestamptz)<=now()
    and not exists(select 1 from (select status_code from archive_vnext.backfill_dispatches order by created_at desc limit 5)d where status_code in (401,403)) then
    update archive_vnext.backfill_control set enabled=true,stop_reason=null,transient_recoveries=transient_recoveries+1,
      retry_after=null,last_recovered_at=now() where singleton;
    if not exists(select 1 from cron.job where jobname='crowley-vnext-embedding-backfill' and active) then
      perform cron.schedule('crowley-vnext-embedding-backfill','2 seconds','set statement_timeout=''30s''; select archive_vnext.dispatch_backfill();');
    end if;
  end if;

  select max(created_at) into last_ok from archive_vnext.backfill_dispatches where status_code=200;
  perform crowley_v2.operating_alert('embedding_stalled','critical',
    exists(select 1 from archive_vnext.embedding_jobs where state in ('queued','leased'))
      and (last_ok is null or last_ok<now()-interval '20 minutes'),jsonb_build_object('last_success_at',last_ok));
  perform crowley_v2.operating_alert('embedding_dead_jobs','critical',exists(select 1 from archive_vnext.embedding_jobs where state='dead'));
  perform crowley_v2.operating_alert('worker_failed','warning',exists(select 1 from crowley_v2.workers where state='failed'));
  perform crowley_v2.operating_alert('worker_credential_expiring','critical',
    exists(select 1 from archive_vnext.embedding_jobs where state in ('queued','leased')) and not exists(
      select 1 from crowley_v2.api_tokens t join vault.decrypted_secrets v
        on t.token_sha256=encode(extensions.digest(v.decrypted_secret,'sha256'),'hex')
        where v.name=c.vault_secret_name and t.enabled and 'embedding:write'=any(t.scopes)
          and t.expires_at>coalesce(p.protected_until,now()+interval '30 days')));
  perform crowley_v2.operating_alert('operating_window_ending','warning',p.protected_until is null or p.protected_until<now()+interval '7 days');
  perform crowley_v2.operating_alert('restore_unproven','critical',coalesce((crowley_v2.recovery_status()->>'release_blocker')::boolean,true));

  -- Operational counters are disposable; source data, events, versions and QA
  -- evidence are never retention-deleted by maintenance.
  delete from crowley_v2.api_usage where window_start<now()-interval '2 days';
  delete from crowley_v2.operating_checks where checked_at<now()-make_interval(days=>p.retention_days);
  delete from archive_vnext.backfill_dispatches where created_at<now()-make_interval(days=>p.retention_days) and resolved_at is not null;
  s:=crowley_v2.operating_status();
  insert into crowley_v2.operating_checks(result) values(s);
  return s;
end $$;

create or replace function archive_vnext.dispatch_backfill() returns jsonb
language plpgsql set search_path='' as $$
declare c archive_vnext.backfill_control%rowtype; token text; request bigint; reason text; response_status integer; response_body text;
begin
  if not pg_try_advisory_xact_lock(hashtextextended('archive_vnext.dispatch_backfill',0)) then return '{"state":"busy"}'; end if;
  select * into c from archive_vnext.backfill_control where singleton for update;
  if not found or not c.enabled then return '{"state":"disabled"}'; end if;
  if c.retry_after>now() then return '{"state":"cooldown"}'; end if;
  -- Retire pending wake-ups from the former transport; new calls resolve inline.
  update archive_vnext.backfill_dispatches set status_code=598,resolved_at=now() where resolved_at is null and created_at<now()-interval '2 minutes';
  if c.stop_at<=now() then reason:='deadline';
  elsif c.requests_dispatched>=c.request_budget then reason:='request_budget';
  elsif (select count(*)=5 and bool_and(status_code>=400) from (select status_code from archive_vnext.backfill_dispatches where resolved_at is not null and created_at>coalesce(c.last_recovered_at,'-infinity'::timestamptz) order by created_at desc limit 5)d) then reason:='repeated_failures';
  elsif not exists(select 1 from archive_vnext.embedding_jobs where state in ('queued','leased')) then reason:='queue_drained'; end if;
  if reason is not null then
    update archive_vnext.backfill_control set enabled=false,stop_reason=reason,
      retry_after=case when reason='repeated_failures' then now()+interval '5 minutes' end where singleton;
    if exists(select 1 from cron.job where jobname='crowley-vnext-embedding-backfill') then perform cron.unschedule('crowley-vnext-embedding-backfill'); end if;
    return jsonb_build_object('state','stopped','reason',reason);
  end if;
  select decrypted_secret into token from vault.decrypted_secrets where name=c.vault_secret_name;
  if token is null or not exists(select 1 from crowley_v2.api_tokens where token_sha256=encode(extensions.digest(token,'sha256'),'hex') and enabled and expires_at>now() and 'embedding:write'=any(scopes)) then
    update archive_vnext.backfill_control set enabled=false,stop_reason='credential_unavailable' where singleton;
    if exists(select 1 from cron.job where jobname='crowley-vnext-embedding-backfill') then perform cron.unschedule('crowley-vnext-embedding-backfill'); end if;
    return '{"state":"stopped","reason":"credential_unavailable"}';
  end if;
  request:=nextval('archive_vnext.http_dispatch_request_id'::regclass);
  begin
    select h.status,h.content into response_status,response_body
      from extensions.http(('POST',c.endpoint,
        array[extensions.http_header('Authorization','Bearer '||token)],
        'application/json','{"operation":"embed_next"}')::extensions.http_request) h;
    if response_status=200 and coalesce(response_body::jsonb->>'ok','false')<>'true' then
      response_status:=502;
    end if;
  exception when others then response_status:=599;
  end;
  insert into archive_vnext.backfill_dispatches(request_id,status_code,resolved_at)
    values(request,coalesce(response_status,599),now());
  update archive_vnext.backfill_control set requests_dispatched=requests_dispatched+1 where singleton;
  return jsonb_build_object('state','dispatched','request_id',request,'http_status',response_status);
end $$;

revoke all on function archive_vnext.dispatch_backfill() from public,anon,authenticated,service_role;

alter table crowley_v2.operating_policy enable row level security;
alter table crowley_v2.operating_checks enable row level security;
alter table crowley_v2.operating_alerts enable row level security;
revoke all on all tables in schema crowley_v2 from public,anon,authenticated,service_role;
revoke all on all sequences in schema crowley_v2 from public,anon,authenticated,service_role;
revoke all on all functions in schema crowley_v2 from public,anon,authenticated,service_role;
commit;
