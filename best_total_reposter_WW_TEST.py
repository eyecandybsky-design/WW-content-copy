import os
import json
import time
from datetime import datetime, timezone, timedelta
from collections import defaultdict

from atproto import Client

USERNAME = os.getenv("BSKY_USERNAME", "womenworld.bsky.social")
PASSWORD = os.getenv("BSKY_PASSWORD")
CONFIG_FILE = os.getenv("CONFIG_FILE", "best_total_reposter_config.json")
STATE_FILE = os.getenv("STATE_FILE", "best_total_reposter_state.json")

if not PASSWORD:
    raise RuntimeError("BSKY_PASSWORD ontbreekt. Koppel in de workflow alleen het bestaande WomenWorld GitHub Secret.")

def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default

def save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

cfg = load_json(CONFIG_FILE, {})
state = load_json(STATE_FILE, {"reposted": {}, "liked": {}, "reboosted": {}})

MAX_PER_RUN = int(cfg.get("max_per_run", 100))
MAX_PER_USER = int(cfg.get("max_per_user", 3))
LOOKBACK_HOURS = float(cfg.get("lookback_hours", 3))
LIKE_LOOKBACK_HOURS = float(cfg.get("like_lookback_hours", 6))
MAX_LIKES_PER_USER = int(cfg.get("max_likes_per_user", 10))
OWN_POSTS = int(cfg.get("own_posts", 3))
SLEEP_SECONDS = float(cfg.get("sleep_seconds", 2))

client = Client()
client.login(USERNAME, PASSWORD)
me = client.me
cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
like_cutoff = datetime.now(timezone.utc) - timedelta(hours=LIKE_LOOKBACK_HOURS)

def parse_dt(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except Exception:
        return None

def get_record(item):
    return getattr(item, "post", item)

def author_handle(post):
    a = getattr(post, "author", None)
    return getattr(a, "handle", "") if a else ""

def created_at(post):
    rec = getattr(post, "record", None)
    return parse_dt(getattr(rec, "created_at", None))

def is_reply(post):
    rec = getattr(post, "record", None)
    return getattr(rec, "reply", None) is not None

def is_quote(post):
    embed = getattr(post, "embed", None)
    name = type(embed).__name__.lower() if embed else ""
    return "record" in name and "media" not in name

def has_media(post):
    embed = getattr(post, "embed", None)
    if not embed:
        return False
    name = type(embed).__name__.lower()
    return any(x in name for x in ("image", "video", "media"))

def uri(post):
    return getattr(post, "uri", "")

def cid(post):
    return getattr(post, "cid", "")

def safe_repost(post, label):
    u = uri(post)
    if not u:
        return False
    try:
        client.repost(u, cid(post))
        state["reposted"][u] = datetime.now(timezone.utc).isoformat()
        print(f"REPOST [{label}] {author_handle(post)} {u}")
        time.sleep(SLEEP_SECONDS)
        return True
    except Exception as e:
        print(f"SKIP/ERROR repost [{label}] {u}: {e}")
        return False

def safe_like(post, label):
    u = uri(post)
    if not u or u in state["liked"]:
        return False
    try:
        client.like(u, cid(post))
        state["liked"][u] = datetime.now(timezone.utc).isoformat()
        print(f"LIKE [{label}] {author_handle(post)} {u}")
        time.sleep(SLEEP_SECONDS)
        return True
    except Exception as e:
        print(f"SKIP/ERROR like [{label}] {u}: {e}")
        return False

def feed_items(feed_uri, limit=100):
    try:
        r = client.app.bsky.feed.get_feed({"feed": feed_uri, "limit": limit})
        return [get_record(x) for x in r.feed]
    except Exception as e:
        print(f"WARNING feed unavailable: {feed_uri}: {e}")
        return []

def list_feed_items(list_uri, limit=100):
    # Bluesky list feed endpoint. If unavailable, log + skip; never stop the run.
    try:
        r = client.app.bsky.feed.get_list_feed({"list": list_uri, "limit": limit})
        return [get_record(x) for x in r.feed]
    except Exception as e:
        print(f"WARNING list unavailable: {list_uri}: {e}")
        return []

def bsky_url_to_at_uri(url, kind):
    # Resolve bsky.app profile DID URLs directly; supplied WomenWorld URLs use DIDs.
    parts = url.rstrip("/").split("/")
    try:
        did = parts[parts.index("profile") + 1]
        rkey = parts[-1]
        collection = "app.bsky.feed.generator" if kind == "feed" else "app.bsky.graph.list"
        return f"at://{did}/{collection}/{rkey}"
    except Exception:
        return url

candidates = []
likes = []
per_author = defaultdict(int)
like_per_author = defaultdict(int)

def collect(posts, src, replies, repost_on, likes_on):
    for post in posts:
        dt = created_at(post)
        if not dt or not has_media(post) or is_quote(post):
            continue
        if is_reply(post) and not replies:
            continue
        h = author_handle(post)
        u = uri(post)
        if likes_on and dt >= like_cutoff and u not in state["liked"] and like_per_author[h] < MAX_LIKES_PER_USER:
            likes.append((dt, post, src))
            like_per_author[h] += 1
        if repost_on and dt >= cutoff and u not in state["reposted"]:
            candidates.append((dt, post, src))

for source in cfg.get("feeds", []):
    if not source.get("enabled", False):
        continue
    at_uri = bsky_url_to_at_uri(source["url"], "feed")
    collect(feed_items(at_uri), source["name"], source.get("replies", False),
            source.get("repost", False), source.get("likes", False))

for source in cfg.get("lists", []):
    if not source.get("enabled", False):
        continue
    at_uri = bsky_url_to_at_uri(source["url"], "list")
    collect(list_feed_items(at_uri), source["name"], source.get("replies", False),
            source.get("repost", False), source.get("likes", False))

for dt, post, src in sorted(likes, key=lambda x: x[0]):
    safe_like(post, src)

# Old -> new, max 3 selected per author. Reserve Own Posts first.
normal_limit = max(0, MAX_PER_RUN - OWN_POSTS)
done = 0
seen = set()
for dt, post, src in sorted(candidates, key=lambda x: x[0]):
    u = uri(post)
    h = author_handle(post)
    if u in seen or done >= normal_limit:
        continue
    if h != USERNAME and per_author[h] >= MAX_PER_USER:
        continue
    if safe_repost(post, src):
        seen.add(u)
        per_author[h] += 1
        done += 1

# Own Posts always last. Only originals with media; stop after 24h inactivity.
if cfg.get("own_posts_enabled", True):
    try:
        r = client.app.bsky.feed.get_author_feed({"actor": USERNAME, "limit": 50, "filter": "posts_no_replies"})
        own = []
        for item in r.feed:
            post = get_record(item)
            if author_handle(post) != USERNAME or not has_media(post) or is_reply(post) or is_quote(post):
                continue
            own.append(post)
        own = own[:OWN_POSTS]
        newest = created_at(own[0]) if own else None
        if newest and newest >= datetime.now(timezone.utc) - timedelta(hours=24):
            for post in reversed(own):
                u = uri(post)
                try:
                    # If already reposted, delete existing repost first when viewer data exposes it.
                    viewer = getattr(post, "viewer", None)
                    repost_ref = getattr(viewer, "repost", None) if viewer else None
                    if repost_ref:
                        client.delete_repost(repost_ref)
                        time.sleep(SLEEP_SECONDS)
                    client.repost(u, cid(post))
                    print(f"OWN REBOOST {u}")
                    time.sleep(SLEEP_SECONDS)
                except Exception as e:
                    print(f"SKIP/ERROR own reboost {u}: {e}")
        else:
            print("OWN POSTS SKIP: geen nieuwe originele mediapost in de laatste 24 uur.")
    except Exception as e:
        print(f"WARNING Own Posts failed: {e}")

save_json(STATE_FILE, state)
print(f"DONE: {done} normale reposts; state opgeslagen.")
