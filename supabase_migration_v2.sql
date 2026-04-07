-- Migration V2: Add transactions table + purchase_price_m column
-- Run in Supabase SQL Editor:
-- https://supabase.com/dashboard/project/lzquvtizhikyjcawazrx/sql/new

-- 1. Add purchase_price_m to players
ALTER TABLE public.players ADD COLUMN IF NOT EXISTS purchase_price_m numeric;

-- 2. Transactions table
CREATE TABLE IF NOT EXISTS public.transactions (
    id bigserial PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    player_id bigint,
    player_name text NOT NULL,
    type text NOT NULL CHECK (type IN ('buy', 'sell')),
    deal_value_m numeric NOT NULL,
    market_value_at_time_m numeric,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS transactions_user_id_idx ON public.transactions(user_id);

ALTER TABLE public.transactions ENABLE ROW LEVEL SECURITY;

DROP POLICY IF EXISTS "own_transactions_all" ON public.transactions;
CREATE POLICY "own_transactions_all" ON public.transactions
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);
