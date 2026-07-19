#!/usr/bin/env python3
"""Poll configured RSS feeds and post new items to a Discord webhook."""

from __future__ import annotations

import argparse
import email.utils
import html
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from xml.etree.ElementTree import Element

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
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
FETCH_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
}
DISCORD_RATE_LIMIT_PAUSE_S = 1.0
MAX_SEEN_IDS = 500
FETCH_RETRIES = 3
IMAGE_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+?\.(?:jpg|jpeg|png|webp|gif)(?:\?[^\s\"'<>]*)?",
    re.IGNORECASE,
)
IMG_TAG_RE = re.compile(
    r"<img[^>]+src=[\"'](https?://[^\"']+)[\"']",
    re.IGNORECASE,
)


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


def text_of(el: Element | None) -> str:
    if el is None:
        return ""
    return (el.text or "").strip()


def child(el: Element, *names: str) -> Element | None:
    wanted = set(names)
    for child_el in list(el):
        if local_name(child_el.tag) in wanted:
            return child_el
    return None


def children(el: Element, *names: str) -> list[Element]:
    wanted = set(names)
    return [child_el for child_el in list(el) if local_name(child_el.tag) in wanted]


def is_image_url(url: str) -> bool:
    if not url:
        return False
    lower = url.lower().split("?", 1)[0]
    return lower.endswith((".jpg", ".jpeg", ".png", ".webp", ".gif")) or "image" in lower


def html_body(el: Element) -> str:
    """Return HTML body text, skipping media:* nodes that only carry a url attr."""
    for name in ("encoded", "content", "description", "summary"):
        node = child(el, name)
        if node is None:
            continue
        if node.attrib.get("url") and not (node.text or "").strip():
            continue
        text = text_of(node)
        if text:
            return text
    return ""


def extract_image(el: Element, content_html: str = "") -> str | None:
    # 1) media:content / media:thumbnail
    for media_el in children(el, "content", "thumbnail"):
        url = media_el.attrib.get("url") or media_el.attrib.get("href") or ""
        medium = media_el.attrib.get("medium", "")
        mime = media_el.attrib.get("type", "")
        if url and (medium == "image" or mime.startswith("image/") or is_image_url(url)):
            return url

    # 2) RSS enclosure (Substack cover image)
    for enc in children(el, "enclosure"):
        url = enc.attrib.get("url", "")
        mime = enc.attrib.get("type", "")
        if url and (mime.startswith("image/") or is_image_url(url)):
            return url

    # 3) nested <image><url>
    image_el = child(el, "image")
    if image_el is not None:
        url = text_of(child(image_el, "url")) or image_el.attrib.get("href", "")
        if url:
            return url

    # 4) first <img> / image URL in HTML body
    for match in IMG_TAG_RE.finditer(content_html or ""):
        return match.group(1)
    match = IMAGE_URL_RE.search(content_html or "")
    if match:
        return match.group(0)
    return None


def parse_datetime(value: str) -> datetime | None:
    value = (value or "").strip()
    if not value:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (TypeError, ValueError, IndexError):
        pass
    try:
        # Atom-style timestamps
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
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
        content_html = html_body(item)
        pub = text_of(child(item, "pubDate", "published", "updated"))
        image = extract_image(item, content_html or desc)
        if title and (link or guid):
            items.append(
                {
                    "id": guid,
                    "title": strip_html(title),
                    "link": link,
                    "summary": truncate(strip_html(desc or content_html)),
                    "published": pub,
                    "published_at": parse_datetime(pub),
                    "image": image,
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
        summary = text_of(child(entry, "summary"))
        content_html = html_body(entry)
        pub = text_of(child(entry, "published", "updated"))
        image = extract_image(entry, content_html or summary)
        if title and (link or entry_id):
            items.append(
                {
                    "id": entry_id,
                    "title": strip_html(title),
                    "link": link,
                    "summary": truncate(strip_html(summary or content_html)),
                    "published": pub,
                    "published_at": parse_datetime(pub),
                    "image": image,
                }
            )

    return items


def fetch_url_urllib(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers=FETCH_HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def fetch_url_curl(url: str, timeout: int = 30) -> bytes:
    curl = shutil.which("curl")
    if not curl:
        raise RuntimeError("curl not available")
    cmd = [
        curl,
        "-fsSL",
        "--max-time",
        str(timeout),
        "-A",
        USER_AGENT,
        "-H",
        f"Accept: {FETCH_HEADERS['Accept']}",
        "-H",
        f"Accept-Language: {FETCH_HEADERS['Accept-Language']}",
        "-H",
        f"Referer: {url.rsplit('/feed', 1)[0]}/",
        url,
    ]
    result = subprocess.run(cmd, check=False, capture_output=True)
    if result.returncode != 0:
        err = (result.stderr or b"").decode("utf-8", errors="replace").strip()
        raise RuntimeError(f"curl failed ({result.returncode}): {err or 'unknown error'}")
    return result.stdout


def fetch_url(url: str, timeout: int = 30) -> bytes:
    last_error: Exception | None = None
    for attempt in range(1, FETCH_RETRIES + 1):
        try:
            # curl handles Cloudflare / HTTP2 more reliably on GitHub-hosted runners.
            if shutil.which("curl"):
                return fetch_url_curl(url, timeout=timeout)
            return fetch_url_urllib(url, timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - retry then surface
            last_error = exc
            if attempt < FETCH_RETRIES:
                time.sleep(attempt)
                continue
    assert last_error is not None
    raise last_error


def parse_rss2json(payload: dict) -> list[dict]:
    if payload.get("status") != "ok":
        raise RuntimeError(f"rss2json status={payload.get('status')!r}")
    items: list[dict] = []
    for raw in payload.get("items") or []:
        title = strip_html(raw.get("title") or "")
        link = (raw.get("link") or "").strip()
        guid = (raw.get("guid") or link or title).strip()
        desc = raw.get("description") or ""
        content_html = raw.get("content") or desc
        pub = (raw.get("pubDate") or "").strip()
        image = (raw.get("thumbnail") or "").strip() or None
        if not image:
            for match in IMG_TAG_RE.finditer(content_html or ""):
                image = match.group(1)
                break
            if not image:
                match = IMAGE_URL_RE.search(content_html or "")
                image = match.group(0) if match else None
        if title and (link or guid):
            items.append(
                {
                    "id": guid,
                    "title": title,
                    "link": link,
                    "summary": truncate(strip_html(desc or content_html)),
                    "published": pub,
                    "published_at": parse_datetime(pub),
                    "image": image,
                }
            )
    return items


def fetch_feed_items(url: str) -> list[dict]:
    """Fetch RSS items, falling back to rss2json when the origin blocks datacenter IPs."""
    try:
        return parse_feed(fetch_url(url))
    except Exception as direct_exc:  # noqa: BLE001
        print(f"  direct fetch failed ({direct_exc}); trying rss2json fallback")
        encoded = urllib.parse.quote(url, safe="")
        proxy = f"https://api.rss2json.com/v1/api.json?rss_url={encoded}"
        raw = fetch_url_urllib(proxy)
        payload = json.loads(raw.decode("utf-8"))
        items = parse_rss2json(payload)
        if not items:
            raise RuntimeError(f"rss2json returned no items after direct failure: {direct_exc}") from direct_exc
        return items


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
    embed: dict = {
        "title": item["title"][:256],
        "description": description[:4096],
        "color": 0x5865F2,
        "author": {"name": feed_name},
    }
    if item.get("link"):
        embed["url"] = item["link"]
    if item.get("image"):
        embed["image"] = {"url": item["image"]}
    if item.get("published"):
        # Discord wants ISO8601; fall back to raw string only if parsed.
        published_at = item.get("published_at")
        if isinstance(published_at, datetime):
            embed["timestamp"] = published_at.isoformat().replace("+00:00", "Z")

    payload = {"embeds": [embed]}
    body = json.dumps(payload).encode("utf-8")

    if dry_run:
        image_note = f" image={item['image']}" if item.get("image") else " image=<none>"
        print(f"[dry-run] {feed_name}: {item['title']} -> {item['link']}{image_note}")
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


def within_lookback(item: dict, lookback_hours: int, now: datetime) -> bool:
    if lookback_hours <= 0:
        return True
    published_at = item.get("published_at")
    if not isinstance(published_at, datetime):
        # If a feed omits dates, still allow unseen items through.
        return True
    return published_at >= now - timedelta(hours=lookback_hours)


def process_feed(
    feed: dict,
    webhook_url: str,
    *,
    dry_run: bool,
    backfill: int,
    lookback_hours: int,
) -> int:
    feed_id = feed["id"]
    feed_name = feed.get("name") or feed_id
    url = feed["url"]
    now = datetime.now(timezone.utc)

    print(f"Fetching {feed_name}: {url}")
    items = fetch_feed_items(url)
    if not items:
        print("  no items found")
        return 0

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
            # Mark the rest as seen so we don't flood later.
            seen.update(item["id"] for item in items)
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

    candidates = [item for item in items if item["id"] not in seen]
    # Mark everything currently in the feed as seen eventually, including
    # items outside the lookback window, so old IDs don't linger forever.
    to_post = [
        item
        for item in candidates
        if within_lookback(item, lookback_hours, now)
    ]
    skipped = len(candidates) - len(to_post)
    if skipped:
        print(f"  skipped {skipped} unseen item(s) outside {lookback_hours}h lookback")

    # Post oldest first so channel order matches publish order.
    to_post.reverse()

    if not to_post:
        print("  no new items in lookback window")
        seen.update(item["id"] for item in items)
        state["seen_ids"] = list(seen)
        if not dry_run:
            save_state(feed_id, state)
        return 0

    print(f"  posting {len(to_post)} new item(s)")
    for item in to_post:
        post_discord(webhook_url, feed_name, item, dry_run=dry_run)
        seen.add(item["id"])
        posted += 1

    seen.update(item["id"] for item in items)
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
        "--lookback-hours",
        type=int,
        default=None,
        help="Override feeds.yaml lookback_hours",
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
    lookback_hours = (
        args.lookback_hours
        if args.lookback_hours is not None
        else int(config.get("lookback_hours") or 24)
    )

    feeds = config["feeds"]
    if args.feed_ids:
        wanted = set(args.feed_ids)
        feeds = [f for f in feeds if f.get("id") in wanted]
        missing = wanted - {f.get("id") for f in feeds}
        if missing:
            raise SystemExit(f"Unknown feed id(s): {', '.join(sorted(missing))}")

    total = 0
    failures = 0
    for feed in feeds:
        if not feed.get("id") or not feed.get("url"):
            raise SystemExit(f"Each feed needs id and url: {feed!r}")
        try:
            total += process_feed(
                feed,
                webhook_url,
                dry_run=args.dry_run,
                backfill=args.backfill,
                lookback_hours=lookback_hours,
            )
        except Exception as exc:  # noqa: BLE001 - keep other feeds running
            failures += 1
            print(f"  ERROR processing {feed.get('id')}: {exc}", file=sys.stderr)

    print(f"Done. Posted {total} item(s). Failures: {failures}.")
    # Fail the job only when every feed failed, so successful feeds can still
    # commit seen-state updates.
    return 1 if failures and failures == len(feeds) else 0


if __name__ == "__main__":
    raise SystemExit(main())
