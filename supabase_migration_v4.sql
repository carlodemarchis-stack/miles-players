-- Migration V4: User profiles + App settings
-- Run in Supabase SQL Editor

-- 1. Add profile fields to user_profiles
ALTER TABLE public.user_profiles ADD COLUMN IF NOT EXISTS first_name text;
ALTER TABLE public.user_profiles ADD COLUMN IF NOT EXISTS last_name text;
ALTER TABLE public.user_profiles ADD COLUMN IF NOT EXISTS nickname text;
ALTER TABLE public.user_profiles ADD COLUMN IF NOT EXISTS is_admin boolean DEFAULT false;

-- 2. Set carlodemarchis@gmail.com as admin
UPDATE public.user_profiles SET is_admin = true WHERE email = 'carlodemarchis@gmail.com';

-- 3. App-wide settings table (single row)
CREATE TABLE IF NOT EXISTS public.app_settings (
    id int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    team_name text NOT NULL DEFAULT 'Miles''s Football Stars',
    budget_m numeric NOT NULL DEFAULT 1000.0,
    max_squad_size int NOT NULL DEFAULT 22,
    min_players_for_analysis int NOT NULL DEFAULT 11,
    analysis_prompt text,
    updated_at timestamptz NOT NULL DEFAULT now()
);

-- Insert default row if empty
INSERT INTO public.app_settings (id) VALUES (1) ON CONFLICT DO NOTHING;

-- Anyone can read app_settings, only service role / admin writes (no RLS needed — public read)
ALTER TABLE public.app_settings ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "anyone_can_read_settings" ON public.app_settings;
CREATE POLICY "anyone_can_read_settings" ON public.app_settings
    FOR SELECT USING (true);

DROP POLICY IF EXISTS "admin_can_update_settings" ON public.app_settings;
CREATE POLICY "admin_can_update_settings" ON public.app_settings
    FOR UPDATE USING (
        EXISTS (SELECT 1 FROM public.user_profiles WHERE user_id = auth.uid() AND is_admin = true)
    );
