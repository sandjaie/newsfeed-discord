# newsfeed-discord

Post new RSS / Substack articles to a Discord channel via webhook.

## How it works

1. Feeds are listed in [`feeds.yaml`](feeds.yaml).
2. A GitHub Action runs **once daily at 05:00 UTC (09:00 Gulf / UTC+4)** and on demand.
3. Unseen articles from the last `lookback_hours` (default 24) are posted to one Discord webhook.
4. Posts include title, summary, link, and the article cover image when the feed provides one.
5. Seen article IDs are stored under [`state/`](state/) so posts are not repeated.

## Add a feed later

Append a block to `feeds.yaml`:

```yaml
feeds:
  - id: bytebytego
    name: ByteByteGo
    url: https://blog.bytebytego.com/feed

  - id: some-other-blog
    name: Some Other Blog
    url: https://example.com/feed
```

Commit and push. No code changes needed.

## One-time setup

1. In Discord: Channel → Integrations → Webhooks → create/copy webhook URL.
   If this URL was ever pasted into chat, regenerate it first.
2. In the GitHub repo: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `DISCORD_WEBHOOK`
   - Value: the webhook URL
3. Push this repo to `main`, then run **Actions → RSS to Discord → Run workflow**.

First run seeds current articles **without** posting (avoids flooding the channel).  
To post the latest article once while testing, use Run workflow with `backfill = 1`.

## Local dry run

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python scripts/rss_to_discord.py --dry-run
```

Live local post:

```bash
export DISCORD_WEBHOOK='https://discord.com/api/webhooks/...'
python scripts/rss_to_discord.py --backfill 1   # only on a fresh feed state
```

## Manual workflow options

| Input | Meaning |
| --- | --- |
| `backfill` | On first run of a feed, post N newest items instead of seeding silently |
| `dry_run` | Fetch and log only; no Discord posts, no state commits |
