begin;
set local statement_timeout='90s';
create function pg_temp.assert(ok boolean,message text) returns void language plpgsql as $$
begin if ok is distinct from true then raise exception 'ASSERT: %',message; end if; end $$;
insert into archive_vnext.builds(generation_id,source_sha256,expected_turns,expected_history)
 values('synthetic-readiness-literal',repeat('a',64),4,1);
insert into archive_vnext.frames(generation_id,source_uri,conversation_id,turn_index,source_state,title,observed_at,raw_messages,user_text,assistant_text,context_text,content_hash)
select 'synthetic-readiness-literal','archive://conversation/literal-qa/turn/'||i,'literal-qa',i,state,'Synthetic literal QA','2025-01-01'::timestamptz+make_interval(days=>i),'[]',u,a,u||E'\n'||a,encode(extensions.digest(u||E'\n'||a,'sha256'),'hex')
from (values
 (0,'active_path',repeat('z',3500)||repeat('🦊',1998)||'XY'||repeat('z',4000),'Boundary end'),
 (1,'active_path','Literal 10%_path\file. ending left','right beginning. violet lighthouse'),
 (2,'inactive_branch','Secret literal otter',''),
 (3,'active_path','Common user witness','Common assistant witness'),
 (4,'active_path','Common user witness newer',''))v(i,state,u,a);
select pg_temp.assert(not archive_vnext.literal_ready('synthetic-readiness-literal'),'incomplete is detected');
select archive_vnext.index_literal_batch('synthetic-readiness-literal',2);
select pg_temp.assert(not archive_vnext.literal_ready('synthetic-readiness-literal'),'partial build never promoted');
select archive_vnext.index_literal_batch('synthetic-readiness-literal',500);
select pg_temp.assert(archive_vnext.literal_ready('synthetic-readiness-literal'),'all frames indexed');
select pg_temp.assert(archive_vnext.index_literal_batch('synthetic-readiness-literal',500)->>'inserted'='0','retry is idempotent');
select pg_temp.assert(jsonb_array_length(archive_vnext.search(repeat('🦊',1998)||'XY',null,'synthetic-readiness-literal','user',null,null,8,false,true)->'matches')=1,'2000 codepoint literal spans a passage edge');
select pg_temp.assert(jsonb_array_length(archive_vnext.search('10%_path\file',null,'synthetic-readiness-literal','user',null,null,8,false,true)->'matches')=1,'wildcards and backslash remain literal');
select pg_temp.assert(jsonb_array_length(archive_vnext.search(E'ending left\nright beginning',null,'synthetic-readiness-literal','both',null,null,8,false,true)->'matches')=1,'both-role newline boundary preserved');
select pg_temp.assert(jsonb_array_length(archive_vnext.search(E'ending left\nright beginning',null,'synthetic-readiness-literal','user',null,null,8,false,true)->'matches')=0,'boundary cannot leak to single role');
select pg_temp.assert(jsonb_array_length(archive_vnext.search('violet lighthouse',null,'synthetic-readiness-literal','user',null,null,8,false,true)->'matches')=0,'assistant text is not user text');
select pg_temp.assert(jsonb_array_length(archive_vnext.search('Secret literal otter',null,'synthetic-readiness-literal','both',null,null,8,false,true)->'matches')=0,'inactive is excluded');
select pg_temp.assert(archive_vnext.search('Secret literal otter',null,'synthetic-readiness-literal','both',null,null,8,true,true)->'matches'->0->>'source_state'='inactive_branch','inactive opt in is labeled');
select pg_temp.assert(archive_vnext.search('Common user witness',null,'synthetic-readiness-literal','user',null,null,1,false,true)->'matches'->0->>'source_uri'='archive://conversation/literal-qa/turn/4','literal ordering is newest first');
select pg_temp.assert(jsonb_array_length(archive_vnext.search('Common user witness',null,'synthetic-readiness-literal','user',null,'2025-01-04T00:00:00Z',8,false,true)->'matches')=1,'date filters apply');
select pg_temp.assert(archive_vnext.search('Common',null,'synthetic-readiness-literal','both',null,null,8,false,true)->>'exhaustive'='false','representative remains explicit');
select pg_temp.assert(not has_table_privilege('anon','archive_vnext.literal_passages','select'),'passages are private');
rollback;
select '15 literal assertions passed; all synthetic writes rolled back' result;
