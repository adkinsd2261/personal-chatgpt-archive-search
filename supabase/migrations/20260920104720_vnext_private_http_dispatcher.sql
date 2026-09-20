-- pg_net's managed owner prevented reliable revocation of public queue grants.
-- Never put a long-lived credential into that queue. A single bounded cron
-- invocation sends HTTP directly; its token lives only in Vault/backend memory.
-- Durable embedding jobs still own progress and lease fencing.
create extension if not exists http with schema extensions;
create sequence archive_vnext.http_dispatch_request_id start with 1000000000;
revoke all on sequence archive_vnext.http_dispatch_request_id from public,anon,authenticated,service_role;

create or replace function archive_vnext.dispatch_backfill() returns jsonb
language plpgsql set search_path='' as $$
declare c archive_vnext.backfill_control%rowtype; token text; request bigint; reason text; response_status integer; response_body text;
begin
  if not pg_try_advisory_xact_lock(hashtextextended('archive_vnext.dispatch_backfill',0)) then return '{"state":"busy"}'; end if;
  select * into c from archive_vnext.backfill_control where singleton for update;
  if not found or not c.enabled then return '{"state":"disabled"}'; end if;
  -- Retire pending wake-ups from the former transport; new calls resolve inline.
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
-- Uses the platform's default five-second HTTP timeout; no privileged GUC changes.
-- Operator schedule: set statement_timeout='15s'; select archive_vnext.dispatch_backfill();
-- Rotate the former queue credential before enabling this transport.
