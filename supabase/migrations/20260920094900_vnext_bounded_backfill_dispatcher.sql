-- Durable jobs own truth; pg_net is only a wake-up transport, not the queue.
create extension if not exists pg_cron;
create extension if not exists pg_net;
create table archive_vnext.backfill_control (
  singleton boolean primary key default true check(singleton),
  enabled boolean not null default false,
  endpoint text not null check(endpoint ~ '^https://[a-z]{20}\.supabase\.co/functions/v1/crowley-archive-vnext$'),
  vault_secret_name text not null,
  stop_at timestamptz not null,
  request_budget integer not null check(request_budget between 1 and 200000),
  requests_dispatched integer not null default 0,
  stop_reason text,
  created_at timestamptz not null default now()
);
create table archive_vnext.backfill_dispatches (
  request_id bigint primary key,
  created_at timestamptz not null default now(),
  status_code integer,
  resolved_at timestamptz
);
create index backfill_dispatches_pending_idx on archive_vnext.backfill_dispatches(created_at) where resolved_at is null;
alter table archive_vnext.backfill_control enable row level security;
alter table archive_vnext.backfill_dispatches enable row level security;

create function archive_vnext.dispatch_backfill() returns jsonb
language plpgsql set search_path='' set statement_timeout='5s' as $$
declare c archive_vnext.backfill_control%rowtype; token text; request bigint; reason text;
begin
  if not pg_try_advisory_xact_lock(hashtextextended('archive_vnext.dispatch_backfill',0)) then return '{"state":"busy"}'; end if;
  select * into c from archive_vnext.backfill_control where singleton for update;
  if not found or not c.enabled then return '{"state":"disabled"}'; end if;
  update archive_vnext.backfill_dispatches d set status_code=coalesce(r.status_code,599),resolved_at=now()
    from net._http_response r where d.request_id=r.id and d.resolved_at is null;
  update archive_vnext.backfill_dispatches set status_code=598,resolved_at=now() where resolved_at is null and created_at<now()-interval '2 minutes';
  if c.stop_at<=now() then reason:='deadline';
  elsif c.requests_dispatched>=c.request_budget then reason:='request_budget';
  elsif (select count(*)=5 and bool_and(status_code>=400) from (select status_code from archive_vnext.backfill_dispatches where resolved_at is not null order by created_at desc limit 5)d) then reason:='repeated_failures';
  elsif not exists(select 1 from archive_vnext.embedding_jobs where state in ('queued','leased')) then reason:='queue_drained'; end if;
  if reason is not null then
    update archive_vnext.backfill_control set enabled=false,stop_reason=reason where singleton;
    if exists(select 1 from cron.job where jobname='crowley-vnext-embedding-backfill') then perform cron.unschedule('crowley-vnext-embedding-backfill'); end if;
    return jsonb_build_object('state','stopped','reason',reason);
  end if;
  if (select count(*) from archive_vnext.backfill_dispatches where resolved_at is null)>=2 then return '{"state":"in_flight"}'; end if;
  select decrypted_secret into token from vault.decrypted_secrets where name=c.vault_secret_name;
  if token is null or not exists(select 1 from crowley_v2.api_tokens where token_sha256=encode(extensions.digest(token,'sha256'),'hex') and enabled and expires_at>now() and 'embedding:write'=any(scopes)) then
    update archive_vnext.backfill_control set enabled=false,stop_reason='credential_unavailable' where singleton;
    if exists(select 1 from cron.job where jobname='crowley-vnext-embedding-backfill') then perform cron.unschedule('crowley-vnext-embedding-backfill'); end if;
    return '{"state":"stopped","reason":"credential_unavailable"}';
  end if;
  request:=net.http_post(url:=c.endpoint,body:='{"operation":"embed_next"}',
    headers:=jsonb_build_object('Content-Type','application/json','Authorization','Bearer '||token),timeout_milliseconds:=55000);
  insert into archive_vnext.backfill_dispatches(request_id) values(request);
  update archive_vnext.backfill_control set requests_dispatched=requests_dispatched+1 where singleton;
  return jsonb_build_object('state','dispatched','request_id',request);
end $$;
revoke all on all tables in schema archive_vnext from public,anon,authenticated,service_role;
revoke all on function archive_vnext.dispatch_backfill() from public,anon,authenticated,service_role;
-- Explicit operator setup starts a bounded job after the corpus build verifies.
-- No schedule or credential is created by this migration.
