import os
import json
import time
from collections import defaultdict
from datetime import datetime, timezone, timedelta

from atproto import Client

# ============================================================
# SCAZMO SMALL REPOSTER
# Account: scazmo.bsky.social
#
# - 3 feeds, replies independently ON/OFF
# - 3 lists
# - repost-always accounts
# - 2 hour lookback
# - max 3 selected posts per author
# - max 50 normal reposts
# - last 3 own original posts reboosted last
# - candidates from all sources are mixed by post time
# - no hashtags / no promo
# ============================================================

USERNAME = os.getenv("BSKY_USERNAME", "scazmo.bsky.social")
PASSWORD = os.getenv("BSKY_PASSWORD")

STATE_FILE = os.getenv("STATE_FILE", "scazmo_reposter_state.json")
STATE_KEEP_HOURS = 24

HOURS_BACK = 2
MAX_REPOSTS = 50
MAX_PER_USER = 3
OWN_POSTS = 3
SLEEP_SECONDS = 2

# Accounts to skip. Leave empty if not needed.
SKIP_ACCOUNTS = []

# FEEDS
# You may paste the normal Bluesky web link directly.
# "name" is only a reference for yourself and appears in the Actions log.
#
# Example:
# {"name": "RedFox feed",
#  "url": "https://bsky.app/profile/did:plc:xxxxx/feed/xxxxx",
#  "replies": True}
FEEDS = [
    {"name": "redfox", "url": "https://bsky.app/profile/did:plc:cxrt7ggxkamgzxa47cggtees/feed/aaaoirmgh53zw", "replies": True},
    {"name": "Feed 2", "url": "", "replies": False},
    {"name": "Feed 3", "url": "", "replies": False},
]

# LISTS
# Normal Bluesky list links can also be pasted directly.
# "name" is your own reference and appears in the Actions log.
#
# Example:
# {"name": "Main list",
#  "url": "https://bsky.app/profile/did:plc:xxxxx/lists/xxxxx"}
LISTS = [
    {"name": "List reposters", "url": "https://bsky.app/profile/did:plc:gfyttb6vys3hzp2o2x46uqv3/lists/3mwr2f5sh2f2i"},
    {"name": "List 2", "url": ""},
    {"name": "List 3", "url": ""},
]

# Handles that should also be scanned as direct sources.
# Example: "example.bsky.social"
REPOST_ALWAYS = []



def load_state():
    if not os.path.exists(STATE_FILE):
        return {"reposted": {}}
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            data = {}
        if not isinstance(data.get("reposted"), dict):
            data["reposted"] = {}
        return data
    except Exception as exc:
        print(f"STATE load error: {exc}")
        return {"reposted": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    print(f"STATE saved: {len(state['reposted'])} posts")


def clean_state(state):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=STATE_KEEP_HOURS)
    cleaned = {}
    for uri, timestamp in state.get("reposted", {}).items():
        try:
            dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if dt >= cutoff:
                cleaned[uri] = timestamp
        except Exception:
            pass
    state["reposted"] = cleaned


def already_reposted(state, uri):
    return uri in state.get("reposted", {})


def mark_reposted(state, uri):
    state["reposted"][uri] = datetime.now(timezone.utc).isoformat()
    save_state(state)

def parse_datetime(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None


def get_post_datetime(post):
    record = getattr(post, "record", None)
    if record:
        dt = parse_datetime(getattr(record, "created_at", None))
        if dt:
            return dt
    return parse_datetime(getattr(post, "indexed_at", None))


def get_author_handle(post):
    author = getattr(post, "author", None)
    return getattr(author, "handle", None) if author else None


def is_reply(post):
    record = getattr(post, "record", None)
    return bool(record and getattr(record, "reply", None) is not None)


def has_media(post):
    return getattr(post, "embed", None) is not None


def add_candidate(candidates, post, cutoff, state, allow_replies=False):
    uri = getattr(post, "uri", None)
    handle = get_author_handle(post)
    dt = get_post_datetime(post)

    if not uri or not handle or not dt:
        return
    if uri in candidates:
        return
    if already_reposted(state, uri):
        return
    if handle.lower() in {x.lower() for x in SKIP_ACCOUNTS}:
        return
    if dt < cutoff:
        return
    if is_reply(post) and not allow_replies:
        return
    if not has_media(post):
        return

    candidates[uri] = post



def feed_to_at_uri(value):
    """Accept either an AT URI or a normal bsky.app feed URL."""
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("at://"):
        return value
    prefix = "https://bsky.app/profile/"
    if value.startswith(prefix) and "/feed/" in value:
        rest = value[len(prefix):].split("?", 1)[0].rstrip("/")
        actor, rkey = rest.split("/feed/", 1)
        return f"at://{actor}/app.bsky.feed.generator/{rkey}"
    raise ValueError(f"Unrecognized Bluesky feed link: {value}")


def list_to_at_uri(value):
    """Accept either an AT URI or a normal bsky.app list URL."""
    value = (value or "").strip()
    if not value:
        return ""
    if value.startswith("at://"):
        return value
    prefix = "https://bsky.app/profile/"
    if value.startswith(prefix) and "/lists/" in value:
        rest = value[len(prefix):].split("?", 1)[0].rstrip("/")
        actor, rkey = rest.split("/lists/", 1)
        return f"at://{actor}/app.bsky.graph.list/{rkey}"
    raise ValueError(f"Unrecognized Bluesky list link: {value}")

def collect_feed(client, config, cutoff, candidates, state):
    name = config.get("name", "Unnamed feed")
    raw_url = config.get("url", config.get("uri", "")).strip()
    allow_replies = bool(config.get("replies", False))
    if not raw_url:
        return

    try:
        feed_uri = feed_to_at_uri(raw_url)
    except ValueError as exc:
        print(f"Feed [{name}] link error: {exc}")
        return

    print(f"Feed [{name}]: {raw_url} | replies={'ON' if allow_replies else 'OFF'}")
    cursor = None

    for _ in range(5):
        params = {"feed": feed_uri, "limit": 100}
        if cursor:
            params["cursor"] = cursor

        try:
            response = client.app.bsky.feed.get_feed(params)
        except Exception as exc:
            print(f"Feed error: {exc}")
            return

        if not response.feed:
            return

        reached_old_posts = False

        for item in response.feed:
            post = item.post
            dt = get_post_datetime(post)

            if dt and dt < cutoff:
                reached_old_posts = True
                continue

            # Skip feed items representing reposts.
            if getattr(item, "reason", None) is not None:
                continue

            add_candidate(candidates, post, cutoff, state, allow_replies)

        if reached_old_posts:
            return

        cursor = getattr(response, "cursor", None)
        if not cursor:
            return


def collect_list(client, config, cutoff, candidates, state):
    name = config.get("name", "Unnamed list")
    raw_url = config.get("url", config.get("uri", "")).strip()
    if not raw_url:
        return

    try:
        list_uri = list_to_at_uri(raw_url)
    except ValueError as exc:
        print(f"List [{name}] link error: {exc}")
        return

    print(f"List [{name}]: {raw_url}")
    cursor = None

    for _ in range(5):
        params = {"list": list_uri, "limit": 100}
        if cursor:
            params["cursor"] = cursor

        try:
            response = client.app.bsky.feed.get_list_feed(params)
        except Exception as exc:
            print(f"List error: {exc}")
            return

        if not response.feed:
            return

        reached_old_posts = False

        for item in response.feed:
            post = item.post
            dt = get_post_datetime(post)

            if dt and dt < cutoff:
                reached_old_posts = True
                continue

            # Lists: original posts only, no replies/reposts.
            if getattr(item, "reason", None) is not None:
                continue
            if is_reply(post):
                continue

            add_candidate(candidates, post, cutoff, state, False)

        if reached_old_posts:
            return

        cursor = getattr(response, "cursor", None)
        if not cursor:
            return


def collect_repost_always(client, handle, cutoff, candidates, state):
    handle = handle.strip().lstrip("@").lower()
    if not handle or handle in {x.lower() for x in SKIP_ACCOUNTS}:
        return

    print(f"Repost always: @{handle}")

    try:
        response = client.app.bsky.feed.get_author_feed({
            "actor": handle,
            "limit": 50,
            "filter": "posts_no_replies",
        })
    except Exception as exc:
        print(f"Repost-always error @{handle}: {exc}")
        return

    for item in response.feed:
        if getattr(item, "reason", None) is not None:
            continue

        post = item.post
        dt = get_post_datetime(post)

        if dt and dt < cutoff:
            continue
        if is_reply(post):
            continue

        add_candidate(candidates, post, cutoff, state, False)


def select_posts(candidates):
    # Old -> new. Because reposts are executed in this order, newer source
    # posts are reposted later and therefore appear above older ones.
    posts = sorted(
        candidates.values(),
        key=lambda p: get_post_datetime(p)
        or datetime.min.replace(tzinfo=timezone.utc),
    )

    selected = []
    per_author = defaultdict(int)

    for post in posts:
        handle = get_author_handle(post)
        if not handle:
            continue

        key = handle.lower()
        if per_author[key] >= MAX_PER_USER:
            continue

        selected.append(post)
        per_author[key] += 1

        if len(selected) >= MAX_REPOSTS:
            break

    return selected


def repost_post(client, post, state):
    handle = get_author_handle(post) or "unknown"
    try:
        client.repost(post.uri, post.cid)
        print(f"REPOSTED @{handle} | {get_post_datetime(post)}")
        mark_reposted(state, post.uri)
        return True
    except Exception as exc:
        print(f"SKIP/ERROR @{handle}: {exc}")
        return False


def get_own_posts(client):
    try:
        response = client.app.bsky.feed.get_author_feed({
            "actor": USERNAME,
            "limit": 50,
            "filter": "posts_no_replies",
        })
    except Exception as exc:
        print(f"Own-post lookup error: {exc}")
        return []

    own = []
    for item in response.feed:
        # Ignore reposts appearing in the account feed.
        if getattr(item, "reason", None) is not None:
            continue

        post = item.post
        if is_reply(post):
            continue

        own.append(post)
        if len(own) >= OWN_POSTS:
            break

    # Execute oldest -> newest, leaving the newest own post highest.
    own.reverse()
    return own


def get_existing_repost_uri(client, post_uri):
    try:
        response = client.app.bsky.feed.get_posts({"uris": [post_uri]})
        if not response.posts:
            return None

        viewer = getattr(response.posts[0], "viewer", None)
        return getattr(viewer, "repost", None) if viewer else None
    except Exception as exc:
        print(f"Could not check existing repost: {exc}")
        return None


def reboost_own_posts(client):
    own = get_own_posts(client)
    print(f"Own posts to reboost: {len(own)}")

    done = 0

    for post in own:
        existing = get_existing_repost_uri(client, post.uri)

        if existing:
            try:
                client.delete_repost(existing)
                print(f"Removed old own repost: {post.uri}")
                time.sleep(SLEEP_SECONDS)
            except Exception as exc:
                print(f"Could not remove old own repost: {exc}")

        try:
            client.repost(post.uri, post.cid)
            done += 1
            print(f"OWN REBOOST {done}/{OWN_POSTS}: {post.uri}")
        except Exception as exc:
            print(f"Own reboost error: {exc}")

        time.sleep(SLEEP_SECONDS)

    return done


def main():
    if not PASSWORD:
        raise RuntimeError(
            "Missing BSKY_PASSWORD. Workflow should use "
            "${{ secrets.SCAZMO_PASSWORD }}."
        )

    print("=" * 50)
    print("SCAZMO SMALL REPOSTER")
    print("=" * 50)

    state = load_state()
    clean_state(state)
    save_state(state)
    print(f"STATE loaded: {len(state['reposted'])} previous reposts")

    client = Client()
    client.login(USERNAME, PASSWORD)
    print(f"Logged in as @{USERNAME}")

    cutoff = datetime.now(timezone.utc) - timedelta(hours=HOURS_BACK)
    candidates = {}

    for config in FEEDS:
        collect_feed(client, config, cutoff, candidates, state)

    for list_config in LISTS:
        collect_list(client, list_config, cutoff, candidates, state)

    for handle in REPOST_ALWAYS:
        collect_repost_always(client, handle, cutoff, candidates, state)

    print(f"Unique candidates: {len(candidates)}")

    selected = select_posts(candidates)
    print(f"Selected: {len(selected)}/{MAX_REPOSTS}")

    normal_done = 0
    for post in selected:
        if repost_post(client, post, state):
            normal_done += 1
        time.sleep(SLEEP_SECONDS)

    # Always last, so own boosts finish at the top.
    own_done = reboost_own_posts(client)

    print("=" * 50)
    print(f"Normal reposts successful: {normal_done}")
    print(f"Own reboosts successful:   {own_done}")
    print(f"Successful total:          {normal_done + own_done}")
    save_state(state)
    print("Configured maximum:        53")
    print(f"STATE entries:             {len(state['reposted'])}")
    print("=" * 50)


if __name__ == "__main__":
    main()
