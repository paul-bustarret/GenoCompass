-- Atlas schema: assistant conversations + user profiles.
-- Mirrors the shape in src/integrations/supabase/types.ts.

create table if not exists public.profiles (
  id uuid primary key references auth.users (id) on delete cascade,
  display_name text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.atlas_conversations (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users (id) on delete cascade,
  messages jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists atlas_conversations_user_id_updated_at_idx
  on public.atlas_conversations (user_id, updated_at desc);

-- Keep updated_at current: the app updates `messages` without touching it.
create or replace function public.touch_updated_at()
returns trigger
language plpgsql
as $$
begin
  new.updated_at = now();
  return new;
end;
$$;

drop trigger if exists profiles_touch_updated_at on public.profiles;
create trigger profiles_touch_updated_at
  before update on public.profiles
  for each row execute function public.touch_updated_at();

drop trigger if exists atlas_conversations_touch_updated_at on public.atlas_conversations;
create trigger atlas_conversations_touch_updated_at
  before update on public.atlas_conversations
  for each row execute function public.touch_updated_at();

-- RLS. The publishable key is public, so these policies are the only thing
-- standing between a stranger and every user's conversations.
alter table public.profiles enable row level security;
alter table public.atlas_conversations enable row level security;

drop policy if exists "own profile" on public.profiles;
create policy "own profile" on public.profiles
  for all to authenticated
  using (auth.uid() = id)
  with check (auth.uid() = id);

drop policy if exists "own conversations" on public.atlas_conversations;
create policy "own conversations" on public.atlas_conversations
  for all to authenticated
  using (auth.uid() = user_id)
  with check (auth.uid() = user_id);
