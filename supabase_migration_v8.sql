-- Migration V8: Transfermarkt HTML cache

CREATE TABLE IF NOT EXISTS public.tm_cache (
    url text PRIMARY KEY,
    html text NOT NULL,
    cached_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS tm_cache_cached_at_idx ON public.tm_cache(cached_at);
