-- Synthetic development only. Never run against a production corpus.
begin;
do $$
begin
  if not exists(select 1 from archive_vnext.builds where generation_id='vnext-synthetic-test' and status='indexed')
     or exists(select 1 from archive_vnext.frames where generation_id<>'vnext-synthetic-test') then
    raise exception 'requires isolated synthetic development corpus';
  end if;
end $$;
insert into archive_vnext.frames(generation_id,source_uri,conversation_id,turn_index,source_state,title,observed_at,raw_messages,user_text,assistant_text,context_text,content_hash)
select 'vnext-synthetic-test','archive://conversation/vnext-cursor-fixture/turn/'||n,'vnext-cursor-fixture',n,'active_path','Cursor precision fixture',stamp,
  jsonb_build_array(jsonb_build_object('role','user','position','anchor','authority','primary_user_evidence','turn_index',n,'text',phrase)),
  phrase,'',context,encode(extensions.digest(context,'sha256'),'hex')
from (select n,stamp,phrase,'Title: Cursor precision fixture'||E'\n[user; primary evidence]\n'||phrase as context
 from (values (0,'2025-04-01T00:00:00.123456Z'::timestamptz,'cursor witness alpha'),(1,'2025-04-01T00:00:00.123457Z'::timestamptz,'cursor witness beta'))v(n,stamp,phrase))x
on conflict(generation_id,source_uri) do nothing;
commit;
select count(*) as cursor_fixtures from archive_vnext.frames where conversation_id='vnext-cursor-fixture';
