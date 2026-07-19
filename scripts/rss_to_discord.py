#!/usr/bin/env python3
"""Poll configured RSS feeds and post new items to a Discord webhook."""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

try:
    import yaml
except ImportError:  # pragma: no cover
    print("PyYAML is required. Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

try:
    import defusedxml.ElementTree as ET
except ImportError:  # pragma: no cover
    print("defusedxml is required. Run: pip install -r requirements.txt", file=sys.stderr)
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "feeds.yaml"
STATE_DIR = ROOT / "state"
USER_AGENT = "newsfeed-discord/1.0 (+https://github.com/sandjaie/newsfeed-discord)"
DISCORD_RATE_LIMIT_PAUSE_S = 1.0
MAX_SEEN_IDS = 500


def load_config(path: Path) -> dict:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data.get("feeds"), list) or not data["feeds"]:
        raise SystemExit(f"No feeds configured in {path}")
    return data


def strip_html(text: str) -> str:
    text = html.unescape(text or "")
    text = re.sub(r"(?is)<script.*?>.*?</script>", "", text)
    text = re.sub(r"(?is)<style.*?>.*?</style>", "", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def truncate(text: str, limit: int = 280) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def local_name(tag: str) -> str:
    if "}" in tag:
        return tag.rsplit("}", 1)[-1]
    return tag


def text_of(el: ET.Element | None) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def child(el: ET.Element, *names: str) -> ET.Element | None:
    wanted = set(names)
    for child_el in list(el):
        if local_name(child_el.tag) in wanted:
            return child_el
    return None


def parse_feed(xml_bytes: bytes) -> list[dict]:
    root = ET.fromstring(xml_bytes)
    items: list[dict] = []

    # RSS 2.0
    for item in root.iter():
        if local_name(item.tag) != "item":
            continue
        title = text_of(child(item, "title"))
        link = text_of(child(item, "link"))
        guid_el = child(item, "guid")
        guid = text_of(guid_el) or link or title
        desc = text_of(child(item, "description")) or text_of(child(item, "summary"))
        content_el = child(item, "encoded", "content")
        if content_el is not None and (content_el.text or "").strip():
            desc = content_el.text or desc
        pub = text_of(child(item, "pubDate", "published", "updated"))
        if title and (link or guid):
            items.append(
                {
                    "id": guid,
                    "title": strip_html(title),
                    "link": link,
                    "summary": truncate(strip_html(desc)),
                    "published": pub,
                }
            )

    if items:
        return items

    # Atom
    for entry in root.iter():
        if local_name(entry.tag) != "entry":
            continue
        title = text_of(child(entry, "title"))
        link = ""
        for link_el in entry:
            if local_name(link_el.tag) != "link":
                continue
            href = link_el.attrib.get("href", "")
            rel = link_el.attrib.get("rel", "alternate")
            if href and rel in ("alternate", ""):
                link = href
                break
            if href and not link:
                link = href
        entry_id = text_of(child(entry, "id")) or link or title
        summary = text_of(child(entry, "summary")) or text_of(child(entry, "content"))
        pub = text_of(child(entry, "published", "updated"))
        if title and (link or entry_id):
            items.append(
                {
                    "id": entry_id,
                    "title": strip_html(title),
                    "link": link,
                    "summary": truncate(strip_html(summary)),
                    "published": pub,
                }
            )

    return items


def fetch_url(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def load_state(feed_id: str) -> dict:
    path = STATE_DIR / f"{feed_id}.json"
    if not path.exists():
        return {"initialized": False, "seen_ids": []}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {
        "initialized": bool(data.get("initialized")),
        "seen_ids": list(data.get("seen_ids") or []),
    }


def save_state(feed_id: str, state: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    seen = list(dict.fromkeys(state["seen_ids"]))[-MAX_SEEN_IDS:]
    payload = {"initialized": bool(state["initialized"]), "seen_ids": seen}
    path = STATE_DIR / f"{feed_id}.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")


def post_discord(webhook_url: str, feed_name: str, item: dict, dry_run: bool) -> None:
    description = item["summary"] or "New article"
    embed = {
        "title": item["title"][:256],
        "description": description[:4096],
        "url": item["link"] or None,
        "color": 0x5865F2,
        "author": {"name": feed_name},
    }
    if not embed["url"]:
        embed.pop("url")

    payload = {"embeds": [embed]}
    body = json.dumps(payload).encode("utf-8")

    if dry_run:
        print(f"[dry-run] {feed_name}: {item['title']} -> {item['link']}")
        return

    req = urllib.request.Request(
        webhook_url,
        data=body,
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            if resp.status not in (200, 204):
                raise RuntimeError(f"Discord returned HTTP {resp.status}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Discord HTTP {exc.code}: {detail}") from exc

    time.sleep(DISCORD_RATE_LIMIT_PAUSE_S)


def process_feed(
    feed: dict,
    webhook_url: str,
    *,
    dry_run: bool,
    backfill: int,
) -> int:
    feed_id = feed["id"]
    feed_name = feed.get("name") or feed_id
    url = feed["url"]

    print(f"Fetching {feed_name}: {url}")
    items = parse_feed(fetch_url(url))
    if not items:
        print(f"  no items found")
        return 0

    # Feeds are newest-first; keep that order for posting.
    state = load_state(feed_id)
    seen = set(state["seen_ids"])
    posted = 0

    if not state["initialized"]:
        if backfill > 0:
            to_post = list(reversed(items[:backfill]))
            print(f"  first run: backfilling {len(to_post)} item(s)")
            for item in to_post:
                post_discord(webhook_url, feed_name, item, dry_run=dry_run)
                seen.add(item["id"])
                posted += 1
        else:
            print(f"  first run: seeding {len(items)} item(s) without posting")
            seen.update(item["id"] for item in items)

        state["initialized"] = True
        state["seen_ids"] = list(seen)
        if not dry_run:
            save_state(feed_id, state)
        else:
            print("  [dry-run] state not written")
        return posted

    new_items = [item for item in items if item["id"] not in seen]
    # Post oldest first so channel order matches publish order.
    new_items.reverse()

    if not new_items:
        print("  no new items")
        return 0

    print(f"  posting {len(new_items)} new item(s)")
    for item in new_items:
        post_discord(webhook_url, feed_name, item, dry_run=dry_run)
        seen.add(item["id"])
        posted += 1

    state["seen_ids"] = list(seen)
    if not dry_run:
        save_state(feed_id, state)
    else:
        print("  [dry-run] state not written")
    return posted


def resolve_webhook(config: dict) -> str:
    env_name = config.get("discord_webhook_env") or "DISCORD_WEBHOOK"
    url = os.environ.get(env_name, "").strip()
    if not url:
        raise SystemExit(
            f"Missing webhook URL. Set the {env_name} environment variable "
            "(GitHub secret with the same name)."
        )
    return url


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="Path to feeds.yaml",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch feeds and print posts without calling Discord or writing state",
    )
    parser.add_argument(
        "--backfill",
        type=int,
        default=0,
        metavar="N",
        help="On first run for a feed, post the N newest items instead of only seeding",
    )
    parser.add_argument(
        "--feed",
        action="append",
        dest="feed_ids",
        help="Only process this feed id (repeatable)",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    webhook_url = "https://example.invalid/webhook" if args.dry_run else resolve_webhook(config)

    feeds = config["feeds"]
    if args.feed_ids:
        wanted = set(args.feed_ids)
        feeds = [f for f in feeds if f.get("id") in wanted]
        missing = wanted - {f.get("id") for f in feeds}
        if missing:
            raise SystemExit(f"Unknown feed id(s): {', '.join(sorted(missing))}")

    total = 0
    for feed in feeds:
        if not feed.get("id") or not feed.get("url"):
            raise SystemExit(f"Each feed needs id and url: {feed!r}")
        total += process_feed(
            feed,
            webhook_url,
            dry_run=args.dry_run,
            backfill=args.backfill,
        )

    print(f"Done. Posted {total} item(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
