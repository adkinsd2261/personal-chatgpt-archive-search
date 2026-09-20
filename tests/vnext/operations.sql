-- Rollback-only operating lifecycle checks on isolated development.
begin;
create function pg_temp.assert(ok boolean,message text) returns void language plpgsql as $$
begin if ok is distinct from true then raise exception 'ASSERT: %',message; end if; end $$;
select pg_temp.assert(crowley_v2.maintenance_tick()->>'state'='disabled','maintenance starts disabled');
update crowley_v2.operating_policy set maintenance_enabled=true,protected_until=now()+interval '35 days';
select crowley_v2.operating_alert('qa','warning',true,'{"case":1}');
select crowley_v2.operating_alert('qa','critical',true,'{"case":2}');
select pg_temp.assert((select count(*)=1 and bool_and(severity='critical') from crowley_v2.operating_alerts where key='qa'),'alerts deduplicate and escalate');
select crowley_v2.operating_alert('qa','warning',false);
select pg_temp.assert((select not active and resolved_at is not null from crowley_v2.operating_alerts where key='qa'),'alerts resolve');

insert into crowley_v2.workers(idempotency_key,objective,allowed_tools,source_uris,output_schema,token_budget,call_budget,deadline,state,lease_token,lease_until,attempts)
values ('qa-operating-stale','Synthetic worker','{}','{}','{"type":"object"}',100,2,now()+interval '1 hour','running',gen_random_uuid(),now()-interval '1 minute',1),
('qa-operating-exhausted','Synthetic worker','{}','{}','{"type":"object"}',100,2,now()+interval '1 hour','running',gen_random_uuid(),now()-interval '1 minute',3),
('qa-operating-deadline','Synthetic worker','{}','{}','{"type":"object"}',100,2,now()-interval '1 minute','queued',null,null,0);
select crowley_v2.maintenance_tick();
select pg_temp.assert((select state='queued' and lease_token is null and available_at>now() from crowley_v2.workers where idempotency_key='qa-operating-stale'),'stale worker safely requeues with delay');
select pg_temp.assert((select state='failed' and lease_token is null from crowley_v2.workers where idempotency_key='qa-operating-exhausted'),'retry budget remains enforced');
select pg_temp.assert((select state='expired' and completed_at is not null from crowley_v2.workers where idempotency_key='qa-operating-deadline'),'deadlines expire');
select pg_temp.assert((select active from crowley_v2.operating_alerts where key='worker_failed'),'failed worker visible');
select pg_temp.assert((select count(*)>=1 from crowley_v2.operating_checks),'maintenance is auditable');
select pg_temp.assert(crowley_v2.operating_status()->>'production_route'='v3','maintenance cannot promote retrieval');
select pg_temp.assert(crowley_v2.operating_status()->>'intent_dispatch_enabled'='false','intent dispatch requires explicit enablement');
select pg_temp.assert(not has_function_privilege('anon','crowley_v2.maintenance_tick()','execute'),'anonymous maintenance denied');
select pg_temp.assert(not has_function_privilege('authenticated','crowley_v2.operating_status()','execute'),'operating metadata remains private');
select pg_temp.assert(not has_table_privilege('service_role','crowley_v2.operating_alerts','select'),'API service role cannot read private alerts');
select pg_temp.assert((select relrowsecurity from pg_class where oid='crowley_v2.operating_checks'::regclass),'operating records RLS protected');
select pg_temp.assert((select production_retrieval='v3' and not external_actions_enabled from crowley_v2.settings),'action safety settings preserved');
-- Model the dispatcher circuit without issuing any HTTP request.
insert into archive_vnext.backfill_control(singleton,enabled,endpoint,vault_secret_name,stop_at,request_budget)
values(true,false,'https://aaaaaaaaaaaaaaaaaaaa.supabase.co/functions/v1/crowley-archive-vnext','synthetic-operating-token',now()+interval '1 day',100)
on conflict(singleton) do update set enabled=false,endpoint=excluded.endpoint,vault_secret_name=excluded.vault_secret_name,
 stop_at=excluded.stop_at,request_budget=100,requests_dispatched=0,stop_reason='repeated_failures',retry_after=now()+interval '1 minute';
select vault.create_secret(repeat('x',48),'synthetic-operating-token');
insert into crowley_v2.api_tokens(token_sha256,label,scopes,expires_at)
 values(encode(extensions.digest(repeat('x',48),'sha256'),'hex'),'synthetic-operating-token',array['embedding:write'],now()+interval '2 days');
select crowley_v2.maintenance_tick();
select pg_temp.assert((select not enabled from archive_vnext.backfill_control),'cooldown respected');
update archive_vnext.backfill_control set retry_after=now()-interval '1 second',stop_at=now()-interval '1 second';
select crowley_v2.maintenance_tick();
select pg_temp.assert((select not enabled from archive_vnext.backfill_control),'expired deadline cannot recover');
update archive_vnext.backfill_control set stop_at=now()+interval '1 day',requests_dispatched=request_budget;
select crowley_v2.maintenance_tick();
select pg_temp.assert((select not enabled from archive_vnext.backfill_control),'spent budget cannot recover');
update archive_vnext.backfill_control set requests_dispatched=0,transient_recoveries=12;
select crowley_v2.maintenance_tick();
select pg_temp.assert((select not enabled from archive_vnext.backfill_control),'daily recovery cap enforced');
update archive_vnext.backfill_control set transient_recoveries=0;
update crowley_v2.api_tokens set enabled=false where label='synthetic-operating-token';
select crowley_v2.maintenance_tick();
select pg_temp.assert((select not enabled from archive_vnext.backfill_control),'invalid credential cannot recover');
update crowley_v2.api_tokens set enabled=true where label='synthetic-operating-token';
insert into archive_vnext.backfill_dispatches(request_id,status_code,resolved_at) values(-100,401,now());
select crowley_v2.maintenance_tick();
select pg_temp.assert((select not enabled from archive_vnext.backfill_control),'authorization failure cannot auto-recover');
delete from archive_vnext.backfill_dispatches where request_id=-100;
select crowley_v2.maintenance_tick();
select pg_temp.assert((select enabled and transient_recoveries=1 and last_recovered_at is not null from archive_vnext.backfill_control),'transient circuit safely recovers');
select pg_temp.assert((select count(*)=1 from cron.job where jobname='crowley-vnext-embedding-backfill' and active),'recovery restores one schedule');
update archive_vnext.backfill_control set retry_after=now()+interval '1 minute';
select pg_temp.assert(archive_vnext.dispatch_backfill()->>'state'='cooldown','dispatcher respects cooldown without HTTP');
rollback;
select '24 operating assertions passed; all synthetic writes rolled back' as result;
