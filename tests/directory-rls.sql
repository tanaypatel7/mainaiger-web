-- Run after supabase/directory-schema.sql with a privileged SQL test connection.
-- Synthetic auth users and directory rows exist only in this transaction.
-- Nothing is committed, no login/email API is called, and no test listing is public.
begin;

create temporary table directory_test_ids(name text primary key, value text) on commit drop;
grant select, insert, update on directory_test_ids to authenticated, anon;
insert into directory_test_ids(name,value)
select name, gen_random_uuid()::text from unnest(array['alice','bob','carol','dana']) name;
insert into auth.users(id,aud,role,email,email_confirmed_at,created_at,updated_at,raw_app_meta_data,raw_user_meta_data)
select value::uuid,'authenticated','authenticated','mainaiger-rls-'||value||'@example.invalid',now(),now(),now(),'{}'::jsonb,'{}'::jsonb
from directory_test_ids;

create function pg_temp.test_id(key text) returns uuid
language sql stable security invoker as $$ select value::uuid from pg_temp.directory_test_ids where name=key $$;
create function pg_temp.assert_true(ok boolean, message text) returns void
language plpgsql security invoker as $$ begin
  if ok is distinct from true then raise exception 'ASSERTION FAILED: %',message; end if;
end $$;
create function pg_temp.expect_error(statement text, codes text[]) returns void
language plpgsql security invoker as $$
declare caught text;
begin
  begin execute statement;
  exception when others then
    get stacked diagnostics caught=returned_sqlstate;
    if not (caught=any(codes)) then raise exception 'Unexpected SQLSTATE % for %',caught,statement; end if;
  end;
  if caught is null then raise exception 'Expected authorization/constraint rejection for %',statement; end if;
end $$;
create function pg_temp.actor(name text, anonymous boolean default false) returns void
language plpgsql security invoker as $$
declare uid uuid := pg_temp.test_id(name);
begin
  perform set_config('request.jwt.claim.sub',uid::text,true);
  perform set_config('request.jwt.claims',json_build_object('sub',uid,'role','authenticated','is_anonymous',anonymous)::text,true);
end $$;
create function pg_temp.make_space(key text, visible boolean default false) returns uuid
language plpgsql security invoker as $$
declare sid uuid;
begin
  insert into public.directory_spaces(owner_id) values(auth.uid()) returning id into sid;
  insert into pg_temp.directory_test_ids(name,value) values(key,sid::text);
  insert into public.directory_listings(space_id,title,published) values(sid,'Temporary policy test '||key,visible);
  return sid;
end $$;
create function pg_temp.zero_changes(statement text, message text) returns void
language plpgsql security invoker as $$
declare changed integer;
begin execute statement; get diagnostics changed=row_count;
  perform pg_temp.assert_true(changed=0,message);
end $$;

do $$ declare temp_schema text;
begin
  select nspname into temp_schema from pg_catalog.pg_namespace where oid=pg_my_temp_schema();
  execute format('grant usage on schema %I to anon, authenticated',temp_schema);
  execute format('grant execute on all functions in schema %I to anon, authenticated',temp_schema);
end $$;

set local role anon;
insert into directory_test_ids(name,value) select 'baseline_spaces',public_spaces::text from public.directory_public_stats();
insert into directory_test_ids(name,value) select 'baseline_devices',reported_devices::text from public.directory_public_stats();
insert into directory_test_ids(name,value) select 'baseline_members',reported_members::text from public.directory_public_stats();
select pg_temp.expect_error('select * from public.directory_spaces',array['42501']);
select pg_temp.expect_error('select * from public.directory_requests',array['42501']);
select pg_temp.expect_error('insert into public.directory_listings(space_id,title) values(gen_random_uuid(),''x'')',array['42501']);

set local role authenticated;
select pg_temp.actor('alice');
select pg_temp.make_space('alice_public',true);
select pg_temp.make_space('alice_private',false);
update public.directory_listings set device_count=2,member_count=4 where space_id=pg_temp.test_id('alice_public');
update public.directory_listings set device_count=9000,member_count=9000 where space_id=pg_temp.test_id('alice_private');
select pg_temp.assert_true((select count(*)=2 from public.directory_spaces),'Alice sees her own ownership records');
select pg_temp.expect_error('insert into public.directory_spaces(owner_id) values(pg_temp.test_id(''bob''))',array['42501']);
select pg_temp.expect_error('update public.directory_spaces set owner_id=pg_temp.test_id(''bob'')',array['42501']);
select pg_temp.expect_error('update public.directory_listings set space_id=gen_random_uuid()',array['42501']);
select pg_temp.expect_error('update public.directory_listings set updated_at=now()-interval ''1 day''',array['42501']);
select pg_temp.expect_error('update public.directory_listings set region=''precise-gps''',array['23514']);
select pg_temp.expect_error('update public.directory_listings set api_types=array[''https://private.example/key'']',array['23514']);
select pg_temp.expect_error('update public.directory_listings set device_types=array[''secret-hostname'']',array['23514']);
select pg_temp.expect_error('update public.directory_listings set description=repeat(''x'',401)',array['23514']);
select pg_temp.expect_error('update public.directory_listings set device_count=-1',array['23514']);
select pg_temp.expect_error('insert into public.directory_requests(space_id,display_name,capabilities) values(pg_temp.test_id(''alice_public''),''Self'',array[''files-read''])',array['42501']);

set local role anon;
select pg_temp.assert_true((select count(*)=1 from public.directory_listings where space_id in (pg_temp.test_id('alice_public'),pg_temp.test_id('alice_private'))),'Anon sees only the published listing');
select pg_temp.assert_true((select public_spaces=(select value::bigint+1 from directory_test_ids where name='baseline_spaces') and reported_devices=(select value::bigint+2 from directory_test_ids where name='baseline_devices') and reported_members=(select value::bigint+4 from directory_test_ids where name='baseline_members') from public.directory_public_stats()),'Public totals exclude all private counts');
select pg_temp.assert_true(not exists(select 1 from information_schema.columns where table_schema='public' and table_name='directory_listings' and column_name in ('owner_id','requester_id','email','ip_address','token','api_key')),'Public listing table contains no account identity or secret columns');

set local role authenticated;
select pg_temp.actor('bob');
select pg_temp.make_space('bob_public',true);
select pg_temp.assert_true((select count(*)=1 from public.directory_spaces),'Bob cannot enumerate Alice ownership');
select pg_temp.assert_true((select count(*)=0 from public.directory_listings where space_id=pg_temp.test_id('alice_private')),'Bob cannot read Alice draft');
select pg_temp.zero_changes('update public.directory_listings set published=false where space_id=pg_temp.test_id(''alice_public'')','Bob cannot withdraw Alice listing');
select pg_temp.zero_changes('delete from public.directory_spaces where id=pg_temp.test_id(''alice_public'')','Bob cannot delete Alice space');
select pg_temp.zero_changes('delete from public.directory_listings where space_id=pg_temp.test_id(''alice_public'')','Bob cannot delete Alice listing');
select pg_temp.expect_error('insert into public.directory_listings(space_id,title) values(pg_temp.test_id(''alice_private''),''Tampered'')',array['42501']);
select pg_temp.expect_error('insert into public.directory_requests(space_id,requester_id,display_name,capabilities) values(pg_temp.test_id(''alice_public''),pg_temp.test_id(''carol''),''Spoof'',array[''files-read''])',array['42501']);
select pg_temp.expect_error('insert into public.directory_requests(space_id,display_name,capabilities) values(pg_temp.test_id(''alice_private''),''Bob'',array[''files-read''])',array['42501']);
select pg_temp.expect_error('insert into public.directory_requests(space_id,display_name,capabilities,status) values(pg_temp.test_id(''alice_public''),''Bob'',array[''files-read''],''accepted_pending_device'')',array['42501']);
select pg_temp.expect_error('insert into public.directory_requests(space_id,display_name,capabilities) values(pg_temp.test_id(''alice_public''),''Bob'',array[''unknown-root-access''])',array['23514']);
select pg_temp.expect_error('insert into public.directory_requests(space_id,display_name,capabilities,message) values(pg_temp.test_id(''alice_public''),''Bob'',array[''files-read''],repeat(''x'',501))',array['23514']);
with made as (insert into public.directory_requests(space_id,display_name,capabilities) values(pg_temp.test_id('alice_public'),'Bob',array['files-read']) returning id)
insert into directory_test_ids(name,value) select 'request_bob',id::text from made;
select pg_temp.assert_true((select requester_id=pg_temp.test_id('bob') and status='pending' from public.directory_requests where id=pg_temp.test_id('request_bob')),'Requester identity and pending state default to caller');
select pg_temp.expect_error('insert into public.directory_requests(space_id,display_name,capabilities) values(pg_temp.test_id(''alice_public''),''Duplicate'',array[''files-read''])',array['23505']);
select pg_temp.expect_error('update public.directory_requests set status=''accepted_pending_device'' where id=pg_temp.test_id(''request_bob'')',array['42501']);
select pg_temp.expect_error('update public.directory_requests set capabilities=array[''terminal-run''] where id=pg_temp.test_id(''request_bob'')',array['42501']);
select pg_temp.expect_error('update public.directory_requests set requester_id=pg_temp.test_id(''carol'')',array['42501']);

select pg_temp.actor('carol');
select pg_temp.assert_true((select count(*)=0 from public.directory_requests where id=pg_temp.test_id('request_bob')),'Unrelated account cannot read request');
select pg_temp.zero_changes('update public.directory_requests set status=''declined'' where id=pg_temp.test_id(''request_bob'')','Unrelated account cannot act on request');

select pg_temp.actor('alice');
select pg_temp.assert_true((select count(*)=1 from public.directory_requests where id=pg_temp.test_id('request_bob')),'Owner can review incoming request');
update public.directory_requests set status='accepted_pending_device' where id=pg_temp.test_id('request_bob');
select pg_temp.assert_true((select status='accepted_pending_device' from public.directory_requests where id=pg_temp.test_id('request_bob')),'Owner may accept only into pending-device workflow');
select pg_temp.expect_error('update public.directory_requests set status=''active'' where id=pg_temp.test_id(''request_bob'')',array['42501','23514']);
update public.directory_listings set published=false where space_id=pg_temp.test_id('alice_public');
select pg_temp.assert_true((select count(*)=1 from public.directory_requests where id=pg_temp.test_id('request_bob')),'Owner retains private inbox after withdrawal');

set local role anon;
select pg_temp.assert_true((select count(*)=0 from public.directory_listings where space_id=pg_temp.test_id('alice_public')),'Withdrawal immediately removes public listing');
select pg_temp.assert_true((select reported_devices=(select value::bigint from directory_test_ids where name='baseline_devices') and reported_members=(select value::bigint from directory_test_ids where name='baseline_members') from public.directory_public_stats()),'Withdrawal removes disclosed counts');
select pg_temp.expect_error('select * from public.directory_requests',array['42501']);

set local role authenticated;
select pg_temp.actor('bob');
update public.directory_requests set status='cancelled' where id=pg_temp.test_id('request_bob');
select pg_temp.assert_true((select status='cancelled' from public.directory_requests where id=pg_temp.test_id('request_bob')),'Requester can cancel accepted coordination request');
select pg_temp.expect_error('update public.directory_requests set status=''pending'' where id=pg_temp.test_id(''request_bob'')',array['42501']);
select pg_temp.actor('alice');
select pg_temp.expect_error('update public.directory_requests set status=''accepted_pending_device'' where id=pg_temp.test_id(''request_bob'')',array['42501']);

select pg_temp.actor('carol',true);
select pg_temp.expect_error('insert into public.directory_spaces(owner_id) values(auth.uid())',array['42501']);
select pg_temp.expect_error('insert into public.directory_requests(space_id,display_name,capabilities) values(pg_temp.test_id(''bob_public''),''Anon account'',array[''files-read''])',array['42501']);

-- Space quota is enforced on the database path, independent of the UI.
select pg_temp.actor('alice');
do $$ begin for i in 1..18 loop perform pg_temp.make_space('alice_extra_'||i,true); end loop; end $$;
select pg_temp.expect_error('insert into public.directory_spaces(owner_id) values(auth.uid())',array['23514']);
select pg_temp.assert_true((select count(*)=20 from public.directory_spaces),'Space quota keeps exactly 20 owned spaces');

-- Build enough independent public spaces to exercise the retained-request cap.
select pg_temp.actor('bob');
do $$ begin for i in 1..19 loop perform pg_temp.make_space('bob_extra_'||i,true); end loop; end $$;
select pg_temp.actor('dana');
do $$ begin for i in 1..14 loop perform pg_temp.make_space('dana_extra_'||i,true); end loop; end $$;
select pg_temp.actor('carol');
do $$ declare target record; counter integer:=0;
begin
  for target in select l.space_id from public.directory_listings l where l.published
    and l.space_id in (select value::uuid from directory_test_ids where name like '%extra_%' or name='bob_public')
    order by l.space_id limit 50
  loop
    insert into public.directory_requests(space_id,display_name,capabilities) values(target.space_id,'Quota test',array['files-read']);
    counter:=counter+1;
  end loop;
  perform pg_temp.assert_true(counter=50,'Quota fixture has 50 distinct request targets');
end $$;
select pg_temp.expect_error('insert into public.directory_requests(space_id,display_name,capabilities) select l.space_id,''Beyond cap'',array[''files-read''] from public.directory_listings l where l.published and l.space_id in (select value::uuid from directory_test_ids where name like ''%extra_%'' or name=''bob_public'') and not exists(select 1 from public.directory_requests r where r.space_id=l.space_id and r.requester_id=auth.uid()) limit 1',array['23514']);
select pg_temp.assert_true((select count(*)=50 from public.directory_requests where requester_id=auth.uid()),'Retained request quota keeps exactly 50 outgoing requests');

-- Deletion cascades only for the owner; participants lose the removed record.
select pg_temp.actor('alice');
delete from public.directory_spaces where id=pg_temp.test_id('alice_public');
select pg_temp.assert_true((select count(*)=0 from public.directory_listings where space_id=pg_temp.test_id('alice_public')),'Owner deletion removes listing');
select pg_temp.actor('bob');
select pg_temp.assert_true((select count(*)=0 from public.directory_requests where id=pg_temp.test_id('request_bob')),'Owner deletion removes associated requests');

reset role;
rollback;
select 'PASS: anonymous privacy, tenant isolation, immutable identity, valid transitions, withdrawal, field bounds, quotas and deletion cascade; all fixtures rolled back' as result;
