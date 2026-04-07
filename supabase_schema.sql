-- Run this SQL in the Supabase SQL Editor:
-- https://supabase.com/dashboard/project/lzquvtizhikyjcawazrx/sql/new

-- 1. Enable uuid generation extension (usually enabled by default)
create extension if not exists "uuid-ossp";

-- 2. Whitelist table: tracks users who redeemed an invite code
create table if not exists public.user_profiles (
    user_id uuid primary key references auth.users(id) on delete cascade,
    email text not null unique,
    display_name text,
    budget_m numeric not null default 1000.0,
    created_at timestamptz not null default now()
);

alter table public.user_profiles enable row level security;

-- Users can only see/edit their own profile
drop policy if exists "own_profile_select" on public.user_profiles;
create policy "own_profile_select" on public.user_profiles
    for select using (auth.uid() = user_id);

drop policy if exists "own_profile_upsert" on public.user_profiles;
create policy "own_profile_upsert" on public.user_profiles
    for insert with check (auth.uid() = user_id);

drop policy if exists "own_profile_update" on public.user_profiles;
create policy "own_profile_update" on public.user_profiles
    for update using (auth.uid() = user_id);

-- 3. Players table: one row per favourite player, owned by a user
create table if not exists public.players (
    id bigserial primary key,
    user_id uuid not null references auth.users(id) on delete cascade,
    name text not null,
    club text,
    league text,
    position text,
    nationality text,
    birthplace text,
    photo_url text,
    tm_url text,
    age int,
    height text,
    market_value text,
    foot text,
    dob text,
    on_loan_from text,
    rating int,
    apps int default 0,
    goals int default 0,
    assists int default 0,
    notes text,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists players_user_id_idx on public.players(user_id);

alter table public.players enable row level security;

drop policy if exists "own_players_all" on public.players;
create policy "own_players_all" on public.players
    for all using (auth.uid() = user_id) with check (auth.uid() = user_id);

-- 4. Auto-update updated_at on row change
create or replace function public.set_updated_at()
returns trigger as $$
begin
    new.updated_at = now();
    return new;
end;
$$ language plpgsql;

drop trigger if exists players_set_updated_at on public.players;
create trigger players_set_updated_at
    before update on public.players
    for each row execute function public.set_updated_at();
