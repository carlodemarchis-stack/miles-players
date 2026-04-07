-- Migration V5: Tactics support
-- Run in Supabase SQL Editor

-- App settings: formations JSON column
ALTER TABLE public.app_settings ADD COLUMN IF NOT EXISTS formations text;

-- User profiles: selected formation + manual overrides
ALTER TABLE public.user_profiles ADD COLUMN IF NOT EXISTS selected_formation text;
ALTER TABLE public.user_profiles ADD COLUMN IF NOT EXISTS formation_overrides text;
