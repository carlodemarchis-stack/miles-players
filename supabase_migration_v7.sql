-- Migration V7: Saved teams (snapshots)

CREATE TABLE IF NOT EXISTS public.saved_teams (
    id bigserial PRIMARY KEY,
    user_id uuid NOT NULL,
    name text NOT NULL,
    description text,
    snapshot jsonb NOT NULL,
    formation text,
    total_value_m numeric,
    avg_sofascore numeric,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS saved_teams_user_id_idx ON public.saved_teams(user_id);
