-- Isolated fixture verifies literal metacharacters after trigram optimization.
begin;
set local statement_timeout='30s';
insert into archive_private.generations(generation_id,status,source_database_sha256,source_schema_version,corpus_cutoff,expected_turns,expected_user_chunks,expected_history_messages)
  values('vnext-literal-test','ready',repeat('d',64),2,now(),2,0,0);
insert into archive_private.turns(generation_id,conversation_id,turn_index,create_time,title,user_text,assistant_text) values
 ('vnext-literal-test','literal-fixture',0,now(),'Literal fixture',E'Literal 100%_quote\\path is exact.','A distinct assistant phrase.'),
 ('vnext-literal-test','literal-fixture',1,now(),'Literal fixture','Literal 100anythingXquoteXpath is different.','Acknowledged.');
do $$
declare x jsonb; i integer;
begin
  perform archive_vnext.begin_build('vnext-literal-test');
  perform archive_vnext.index_turn_batch('vnext-literal-test');
  perform archive_vnext.finish_build('vnext-literal-test');
  for i in 1..8 loop
    x:=archive_vnext.search(E'100%_quote\\path',null,'vnext-literal-test','user',null,null,8,false,true);
    assert jsonb_array_length(x->'matches')=1,'literal metacharacters must not become wildcards or disappear';
    assert x->'matches'->0->>'source_uri'='archive://conversation/literal-fixture/turn/0','exact source';
  end loop;
  x:=archive_vnext.search(E'100%_quote\\path',null,'vnext-literal-test','assistant',null,null,8,false,true);
  assert jsonb_array_length(x->'matches')=0,'literal authorship';
end $$;
select 'PASS: exact percent, underscore, backslash and role scope' result;
rollback;
