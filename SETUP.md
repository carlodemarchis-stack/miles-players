# Setup — Supabase + Google auth

## 1. Create the database tables

1. Open **Supabase SQL Editor**: https://supabase.com/dashboard/project/lzquvtizhikyjcawazrx/sql/new
2. Paste the contents of `supabase_schema.sql` and click **Run**.
3. You should see: "Success. No rows returned."

This creates:
- `user_profiles` — one row per user (email, budget, display name)
- `players` — player rows, linked to user via RLS policies

## 2. Enable Google OAuth in Supabase

### 2a. Create Google OAuth credentials
1. Open https://console.cloud.google.com/apis/credentials
2. Create a new project (or pick an existing one)
3. Go to **OAuth consent screen** → set app name "Miles's Players", add your email as support + developer contact → Save
4. Go to **Credentials → Create Credentials → OAuth client ID**
   - Application type: **Web application**
   - Name: "Miles Players"
   - **Authorized JavaScript origins**: add `https://lzquvtizhikyjcawazrx.supabase.co`
   - **Authorized redirect URIs**: add `https://lzquvtizhikyjcawazrx.supabase.co/auth/v1/callback`
5. Click **Create** → copy the **Client ID** and **Client secret**

### 2b. Enable Google provider in Supabase
1. Open https://supabase.com/dashboard/project/lzquvtizhikyjcawazrx/auth/providers
2. Find **Google** → toggle it on
3. Paste the Client ID and Client Secret from step 2a
4. Click **Save**

### 2c. Add your app URLs to the allow list
1. Open https://supabase.com/dashboard/project/lzquvtizhikyjcawazrx/auth/url-configuration
2. Add these **Redirect URLs**:
   - `http://localhost:8502` (local dev)
   - `http://localhost:8502/` (with slash)
   - Your Streamlit Cloud URL once deployed (e.g. `https://miles-players.streamlit.app`)
3. Set **Site URL** to your main URL (localhost for now)

## 3. Run the app

```bash
cd /Users/carlodemarchis/Downloads/miles-players
pip3 install --user -r requirements.txt
python3 -m streamlit run app.py --server.port 8502
```

First login:
1. Click "Sign in with Google"
2. Pick your account
3. Enter the invite code from `.streamlit/secrets.toml` (default: `miles2026`)
4. You're in, with an empty player list.

## 4. Migrate your existing players to Supabase

After your first login (so your user row exists):

```bash
# Get the service role key from:
# https://supabase.com/dashboard/project/lzquvtizhikyjcawazrx/settings/api-keys
# (it's the "service_role" secret key — do NOT share or commit)

export SUPABASE_SERVICE_ROLE_KEY="eyJ...<paste here>..."
python3 migrate_to_supabase.py your-email@example.com
```

This uploads all players from `players.json` to Supabase under your user.

## 5. Deploy to Streamlit Cloud

1. Create a GitHub repo, push this folder (the `.gitignore` already excludes secrets)
2. Go to https://share.streamlit.io → **New app** → select your repo, branch `main`, file `app.py`
3. In the app's **Advanced → Secrets**, paste:

```toml
[supabase]
url = "https://lzquvtizhikyjcawazrx.supabase.co"
anon_key = "eyJ..."

[app]
invite_code = "miles2026"
use_local_json = false
```

4. Deploy. First load takes ~1 min. Add the app URL to Supabase **Auth → URL Configuration → Redirect URLs**.

## Dev mode (no Supabase)

Set `use_local_json = true` in `.streamlit/secrets.toml` → app uses the old `players.json` and skips auth (logs you in as "Miles" automatically).

## Changing the invite code

Edit `invite_code` in `.streamlit/secrets.toml` (and in Streamlit Cloud secrets for prod).
