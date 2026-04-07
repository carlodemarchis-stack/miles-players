-- Migration V6: Cross-user player ownership queries
-- These functions run as SECURITY DEFINER (bypass RLS) to see all users' players

CREATE OR REPLACE FUNCTION public.get_player_owners(p_tm_url text)
RETURNS TABLE(user_id uuid, email text, display_name text, team_name text)
LANGUAGE sql SECURITY DEFINER
AS $$
    SELECT p.user_id, up.email, up.display_name, up.team_name
    FROM public.players p
    JOIN public.user_profiles up ON up.user_id = p.user_id
    WHERE p.tm_url = p_tm_url;
$$;

CREATE OR REPLACE FUNCTION public.get_all_owned_players()
RETURNS TABLE(user_id uuid, tm_url text, email text, display_name text, team_name text)
LANGUAGE sql SECURITY DEFINER
AS $$
    SELECT p.user_id, p.tm_url, up.email, up.display_name, up.team_name
    FROM public.players p
    JOIN public.user_profiles up ON up.user_id = p.user_id
    WHERE p.tm_url IS NOT NULL AND p.tm_url != '';
$$;
