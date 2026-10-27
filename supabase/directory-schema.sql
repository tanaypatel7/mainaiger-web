-- MainAIger discovery metadata only. This schema never authorizes device execution.
-- Canonical deployment script; apply once using a named Supabase migration.
create schema if not exists mainaiger_private;
revoke all on schema mainaiger_private from public, anon, authenticated;

create table public.directory_spaces (
  id uuid primary key default gen_random_uuid(),
  owner_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  created_at timestamptz not null default now()
);
create index directory_spaces_owner_idx on public.directory_spaces(owner_id);

create table public.directory_listings (
  space_id uuid primary key references public.directory_spaces(id) on delete cascade,
  title text not null check (char_length(btrim(title)) between 1 and 80),
  description text not null default '' check (char_length(description) <= 400),
  region text check (region in ('africa','asia','europe','north-america','south-america','oceania','antarctica')),
  device_types text[] not null default '{}' check (
    device_types <@ array['mac','windows','linux','server','mobile','other']::text[]
    and cardinality(device_types) <= 6 and array_position(device_types,null) is null
    and coalesce(array_ndims(device_types),1)=1),
  device_count integer check (device_count between 0 and 10000),
  member_count integer check (member_count between 0 and 10000),
  cpu_threads integer check (cpu_threads between 0 and 1000000),
  memory_gb integer check (memory_gb between 0 and 100000000),
  gpu_count integer check (gpu_count between 0 and 100000),
  api_types text[] not null default '{}' check (
    api_types <@ array['ollama','openai-compatible','jupyter','mcp','custom']::text[]
    and cardinality(api_types) <= 5 and array_position(api_types,null) is null
    and coalesce(array_ndims(api_types),1)=1),
  published boolean not null default false,
  updated_at timestamptz not null default now()
);
create index directory_listings_public_idx on public.directory_listings(updated_at desc, space_id) where published;

create table public.directory_requests (
  id uuid primary key default gen_random_uuid(),
  space_id uuid not null references public.directory_spaces(id) on delete cascade,
  requester_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  display_name text not null check (char_length(btrim(display_name)) between 1 and 60),
  capabilities text[] not null check (
    capabilities <@ array['files-read','files-write','notebooks-run','terminal-run','models-use','api-use']::text[]
    and cardinality(capabilities) between 1 and 6 and array_position(capabilities,null) is null
    and coalesce(array_ndims(capabilities),1)=1),
  message text not null default '' check (char_length(message) <= 500),
  status text not null default 'pending' check (status in ('pending','accepted_pending_device','declined','cancelled')),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(space_id,requester_id)
);
create index directory_requests_requester_idx on public.directory_requests(requester_id,created_at desc);

alter table public.directory_spaces enable row level security;
alter table public.directory_spaces force row level security;
alter table public.directory_listings enable row level security;
alter table public.directory_listings force row level security;
alter table public.directory_requests enable row level security;
alter table public.directory_requests force row level security;

revoke all on public.directory_spaces, public.directory_listings, public.directory_requests from public, anon, authenticated;
grant usage on schema public to anon, authenticated;
grant select on public.directory_listings to anon;
grant select, delete on public.directory_spaces to authenticated;
grant insert(owner_id) on public.directory_spaces to authenticated;
grant select, delete on public.directory_listings to authenticated;
grant insert(space_id,title,description,region,device_types,device_count,member_count,cpu_threads,memory_gb,gpu_count,api_types,published) on public.directory_listings to authenticated;
grant update(title,description,region,device_types,device_count,member_count,cpu_threads,memory_gb,gpu_count,api_types,published) on public.directory_listings to authenticated;
grant select on public.directory_requests to authenticated;
grant insert(space_id,requester_id,display_name,capabilities,message) on public.directory_requests to authenticated;
grant update(status) on public.directory_requests to authenticated;

create policy spaces_read_own on public.directory_spaces for select to authenticated
using (owner_id = (select auth.uid()));
create policy spaces_create_own on public.directory_spaces for insert to authenticated
with check (owner_id = (select auth.uid()) and coalesce((select auth.jwt()->>'is_anonymous'),'false') <> 'true');
create policy spaces_delete_own on public.directory_spaces for delete to authenticated
using (owner_id = (select auth.uid()));

-- Separate anon policy avoids requiring access to private ownership records.
create policy listings_read_public on public.directory_listings for select to anon using (published);
create policy listings_read_member on public.directory_listings for select to authenticated
using (published or exists (select 1 from public.directory_spaces s where s.id=space_id and s.owner_id=(select auth.uid())));
create policy listings_create_own on public.directory_listings for insert to authenticated
with check (exists (select 1 from public.directory_spaces s where s.id=space_id and s.owner_id=(select auth.uid())));
create policy listings_update_own on public.directory_listings for update to authenticated
using (exists (select 1 from public.directory_spaces s where s.id=space_id and s.owner_id=(select auth.uid())))
with check (exists (select 1 from public.directory_spaces s where s.id=space_id and s.owner_id=(select auth.uid())));
create policy listings_delete_own on public.directory_listings for delete to authenticated
using (exists (select 1 from public.directory_spaces s where s.id=space_id and s.owner_id=(select auth.uid())));

create policy requests_read_participant on public.directory_requests for select to authenticated
using (requester_id=(select auth.uid()) or exists (select 1 from public.directory_spaces s where s.id=space_id and s.owner_id=(select auth.uid())));
create policy requests_create_self on public.directory_requests for insert to authenticated
with check (
  requester_id=(select auth.uid()) and status='pending'
  and coalesce((select auth.jwt()->>'is_anonymous'),'false') <> 'true'
  and exists (select 1 from public.directory_listings l where l.space_id=directory_requests.space_id and l.published)
  and not exists (select 1 from public.directory_spaces s where s.id=space_id and s.owner_id=(select auth.uid()))
);
create policy requests_update_participant on public.directory_requests for update to authenticated
using (requester_id=(select auth.uid()) or exists (select 1 from public.directory_spaces s where s.id=space_id and s.owner_id=(select auth.uid())))
with check (requester_id=(select auth.uid()) or exists (select 1 from public.directory_spaces s where s.id=space_id and s.owner_id=(select auth.uid())));

create function mainaiger_private.directory_space_insert() returns trigger
language plpgsql security invoker set search_path='' as $$
declare actor uuid := auth.uid();
begin
  if actor is null or new.owner_id is distinct from actor then
    raise exception 'Space owner must be the signed-in account' using errcode='42501';
  end if;
  perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('directory-space:'||actor::text,0));
  if (select count(*) from public.directory_spaces where owner_id=actor) >= 20 then
    raise exception 'A maximum of 20 spaces is allowed per account' using errcode='23514';
  end if;
  new.created_at := statement_timestamp();
  return new;
end;
$$;
revoke all on function mainaiger_private.directory_space_insert() from public,anon,authenticated;
create trigger directory_space_insert before insert on public.directory_spaces
for each row execute function mainaiger_private.directory_space_insert();

create function mainaiger_private.directory_listing_stamp() returns trigger
language plpgsql security invoker set search_path='' as $$
begin
  if tg_op='UPDATE' and new.space_id is distinct from old.space_id then
    raise exception 'Space identity is immutable' using errcode='42501';
  end if;
  new.updated_at := statement_timestamp();
  return new;
end;
$$;
revoke all on function mainaiger_private.directory_listing_stamp() from public,anon,authenticated;
create trigger directory_listing_stamp before insert or update on public.directory_listings
for each row execute function mainaiger_private.directory_listing_stamp();

create function mainaiger_private.directory_request_guard() returns trigger
language plpgsql security invoker set search_path='' as $$
declare actor uuid := auth.uid(); is_owner boolean;
begin
  if actor is null then raise exception 'Sign-in required' using errcode='42501'; end if;
  if tg_op='INSERT' then
    if new.requester_id is distinct from actor or new.status <> 'pending' then
      raise exception 'Requests must belong to the signed-in account and start pending' using errcode='42501';
    end if;
    perform pg_catalog.pg_advisory_xact_lock(pg_catalog.hashtextextended('directory-request:'||actor::text,0));
    if (select count(*) from public.directory_requests where requester_id=actor) >= 50 then
      raise exception 'A maximum of 50 retained requests is allowed per account' using errcode='23514';
    end if;
    new.created_at := statement_timestamp();
  else
    if row(new.id,new.space_id,new.requester_id,new.display_name,new.capabilities,new.message,new.created_at)
       is distinct from row(old.id,old.space_id,old.requester_id,old.display_name,old.capabilities,old.message,old.created_at) then
      raise exception 'Request content is immutable' using errcode='42501';
    end if;
    select exists(select 1 from public.directory_spaces where id=old.space_id and owner_id=actor) into is_owner;
    if actor=old.requester_id then
      if new.status <> 'cancelled' or old.status not in ('pending','accepted_pending_device') then
        raise exception 'Requester may only cancel a pending or accepted request' using errcode='42501';
      end if;
    elsif is_owner then
      if not ((old.status='pending' and new.status in ('accepted_pending_device','declined'))
              or (old.status='accepted_pending_device' and new.status='declined')) then
        raise exception 'Owner may accept or decline pending requests, or decline previously accepted requests' using errcode='42501';
      end if;
    else
      raise exception 'Only request participants may change its state' using errcode='42501';
    end if;
  end if;
  new.updated_at := statement_timestamp();
  return new;
end;
$$;
revoke all on function mainaiger_private.directory_request_guard() from public,anon,authenticated;
create trigger directory_request_guard before insert or update on public.directory_requests
for each row execute function mainaiger_private.directory_request_guard();

create function public.directory_public_stats()
returns table(public_spaces bigint, reported_devices bigint, reported_members bigint,
  reported_cpu_threads bigint, reported_memory_gb bigint, reported_gpus bigint)
language sql stable security invoker set search_path='' as $$
  select count(*), coalesce(sum(device_count),0), coalesce(sum(member_count),0),
    coalesce(sum(cpu_threads),0), coalesce(sum(memory_gb),0), coalesce(sum(gpu_count),0)
  from public.directory_listings where published;
$$;
revoke all on function public.directory_public_stats() from public,anon,authenticated;
grant execute on function public.directory_public_stats() to anon,authenticated;

comment on table public.directory_spaces is 'Private ownership registry; never a network/device access grant.';
comment on table public.directory_listings is 'Only owner-chosen directory metadata. Published rows are publicly readable and copyable.';
comment on table public.directory_requests is 'Coordination inbox only. accepted_pending_device is never an executable permission.';
comment on function public.directory_public_stats() is 'Sums voluntarily published owner-reported fields; members are not unique users or account counts.';

notify pgrst, 'reload schema';
