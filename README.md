# ⚽ Miles's Football Stars

A simple Streamlit app to save Miles's favorite football players with photos, stats, ratings, and notes.

## Run locally

```bash
cd /Users/carlodemarchis/Downloads/miles-players
pip install -r requirements.txt
streamlit run app.py
```

Then open http://localhost:8501 in your browser.

## Deploy (free, for family)

1. Push this folder to a GitHub repo
2. Go to https://share.streamlit.io
3. Sign in with GitHub, click "New app"
4. Pick your repo + `app.py` as the entry point
5. You'll get a public URL like `miles-players.streamlit.app`

## Data

All players are saved in `players.json`. That file is committed to the repo, so when you deploy, the starting data goes with it. (On Streamlit Cloud, new additions via the app may not persist between restarts — for long-term cloud storage, swap the JSON file for Google Sheets or Supabase later.)

## Fields per player

- Name, club, position, nationality, photo URL
- Age, height, market value
- Miles's rating (1–10), personal notes
- Season stats: appearances, goals, assists
