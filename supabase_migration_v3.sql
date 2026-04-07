-- Migration V3: Add SofaScore columns to players
ALTER TABLE public.players ADD COLUMN IF NOT EXISTS sofascore_rating numeric;
ALTER TABLE public.players ADD COLUMN IF NOT EXISTS sofascore_id bigint;
