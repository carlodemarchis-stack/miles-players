#!/bin/bash
cd /Users/carlodemarchis/Downloads/miles-players

# Check for changes
if [ -z "$(git status --porcelain)" ]; then
    echo "No changes to deploy."
    exit 0
fi

# Show what changed
echo "📦 Changes to deploy:"
git status --short
echo ""

# Commit and push
read -p "Commit message (or Enter for auto): " msg
if [ -z "$msg" ]; then
    msg="Update $(date '+%Y-%m-%d %H:%M')"
fi

git add -A
git commit -m "$msg"
git push

echo ""
echo "✅ Deployed! Live in ~1 min at:"
echo "https://miles-players-kwqq3k62ggod3qzgwdrzwj.streamlit.app"
