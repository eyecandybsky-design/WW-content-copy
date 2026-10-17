
import os
import json
import time
import random
from pathlib import Path
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from atproto import Client, models


# ============================================================
# KNIGHTTAKESQUEEN BACKUP AUTOPOST
# ============================================================

SOURCE = os.getenv(
    "SOURCE_ACCOUNT",
    "thepriapicknave.bsky.social",
)

TARGET = os.getenv(
    "BSKY_USERNAME",
    "knighttakesqueen.bsky.social",
)

PASSWORD = os.environ["BSKY_PASSWORD"]

STATE_FILE = Path(
    os.getenv(
        "STATE_FILE",
        "knighttakesqueen_state.json",
    )
)

MIN_AGE_DAYS = int(os.getenv("MIN_AGE_DAYS", "30"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "30"))
MAX_POSTS = int(os.getenv("MAX_POSTS", "3"))

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"

SLEEP_SECONDS = 2
REDFOX_HANDLE = "redfoxofficial.bsky.social"


# ============================================================
# STATE
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {
            "published": {},
            "series_progress": {},
        }

    try:
        with STATE_FILE.open("r", encoding="utf-8") as f:
            state = json.load(f)
    except (OSError, json.JSONDecodeError):
        state = {}

    state.setdefault("published", {})
    state.setdefault("series_progress", {})
    return state


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = STATE_FILE.with_suffix(".tmp")

    with temporary.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

    temporary.replace(STATE_FILE)


# ============================================================
# DATES AND BLUESKY HELPERS
# ============================================================

def parse_datetime(value):
    if not value:
        return None

    try:
        result = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if result.tzinfo is None:
            result = result.replace(tzinfo=timezone.utc)

        return result.astimezone(timezone.utc)

    except (ValueError, TypeError):
        return None


def get_record(post):
    return getattr(post, "record", None)


def get_created_at(post):
    record = get_record(post)
    return parse_datetime(
        getattr(record, "created_at", None)
    )


def get_reply_root_uri(post):
    record = get_record(post)
    reply = getattr(record, "reply", None)
    root = getattr(reply, "root", None)

    return getattr(root, "uri", None)


def get_media_type(embed):
    if isinstance(embed, models.AppBskyEmbedImages.Main):
        return "images"

    if isinstance(embed, models.AppBskyEmbedVideo.Main):
        return "video"

    return None


def get_blob_bytes(did, cid):
    url = (
        "https://bsky.social/xrpc/"
        "com.atproto.sync.getBlob"
        f"?did={did}&cid={cid}"
    )

    request = Request(
        url,
        headers={"User-Agent": "KnightTakesQueenBackup/1.0"},
    )

    with urlopen(request, timeout=90) as response:
        return response.read()


def get_redfox_did(client):
    try:
        result = client.com.atproto.identity.resolve_handle(
            {"handle": REDFOX_HANDLE}
        )
        return result.did
    except Exception as error:
        print(f"⚠️ RedFox handle kon niet worden opgelost: {error}")
        return None


def make_redfox_facets(text, redfox_did):
    if not redfox_did:
        return None

    mention = f"@{REDFOX_HANDLE}"
    position = text.find(mention)

    if position < 0:
        return None

    byte_start = len(text[:position].encode("utf-8"))
    byte_end = byte_start + len(mention.encode("utf-8"))

    return [
        models.AppBskyRichtextFacet.Main(
            index=models.AppBskyRichtextFacet.ByteSlice(
                byte_start=byte_start,
                byte_end=byte_end,
            ),
            features=[
                models.AppBskyRichtextFacet.Mention(
                    did=redfox_did,
                )
            ],
        )
    ]


# ============================================================
# IDENTIFY SERIES
# ============================================================

def collect_series_roots(client):
    """
    Zoek mediaberichten van de bron.
    Gebruik de root van een reply om de volledige serie te vinden.
    """

    roots = set()
    cursor = None

    for page in range(1, MAX_PAGES + 1):
        params = {
            "actor": SOURCE,
            "limit": 100,
            "filter": "posts_with_media",
        }

        if cursor:
            params["cursor"] = cursor

        try:
            response = client.app.bsky.feed.get_author_feed(
                params=params
            )
        except Exception as error:
            print(f"⚠️ Feed ophalen mislukt: {error}")
            break

        feed = response.feed or []

        if not feed:
            break

        for item in feed:
            post = item.post

            if post.author.handle.lower() != SOURCE.lower():
                continue

            if getattr(item, "reply", None):
                root_uri = get_reply_root_uri(post)
                if root_uri:
                    roots.add(root_uri)
                else:
                    roots.add(post.uri)
            else:
                roots.add(post.uri)

        cursor = getattr(response, "cursor", None)

        if not cursor:
            break

        time.sleep(0.3)

    return list(roots)


def get_thread_root(client, root_uri):
    """
    Haal de thread op vanaf het gevonden rootbericht.
    """

    response = client.app.bsky.feed.get_post_thread(
        params={
            "uri": root_uri,
            "depth": 100,
            "parent_height": 0,
        }
    )

    return response.thread


def flatten_own_series(thread):
    """
    Behoud de oorspronkelijke volgorde:
    hoofdpost, daarna eigen replies.
    Replies van andere accounts worden genegeerd.
    """

    result = []

    def walk(node, is_root=False):
        post = getattr(node, "post", None)

        if post:
            author = getattr(post.author, "handle", "").lower()

            if author == SOURCE.lower():
                if is_root or not getattr(
                    getattr(post.record, "reply", None),
                    "parent",
                    None,
                ):
                    result.append(post)
                else:
                    # Alleen eigen replies die bij de thread horen.
                    result.append(post)

        replies = getattr(node, "replies", None) or []

        for child in replies:
            child_post = getattr(child, "post", None)

            if not child_post:
                continue

            # Geen inhoud van andere auteurs overnemen.
            if child_post.author.handle.lower() != SOURCE.lower():
                continue

            walk(child)

    walk(thread, is_root=True)
    return result


# ============================================================
# EXTRACT INDIVIDUAL MEDIA
# ============================================================

def extract_series_items(root_uri, posts, cutoff, state):
    """
    Elke foto uit een carousel wordt een eigen item.
    Elke video wordt eveneens een eigen item.
    """

    items = []
    published = state["published"]
    redfox_found = False

    # Als RedFox in enig bericht van de serie voorkomt,
    # markeren we de serie voor een RedFox-tag.
    for post in posts:
        record = get_record(post)
        text = getattr(record, "text", "") or ""

        if REDFOX_HANDLE.lower() in text.lower():
            redfox_found = True

        embed = getattr(post, "embed", None)
        media_type = get_media_type(embed)

        if media_type == "images":
            for index, image in enumerate(embed.images):
                blob_ref = getattr(image.image, "ref", None)
                cid = getattr(blob_ref, "ref", None)

                if not cid:
                    continue

                media_key = (
                    f"{post.uri}|image|{index}|{cid}"
                )

                if media_key in published:
                    continue

                created_at = get_created_at(post)

                if not created_at or created_at > cutoff:
                    continue

                items.append({
                    "key": media_key,
                    "kind": "image",
                    "post_uri": post.uri,
                    "post_cid": post.cid,
                    "image": image,
                    "redfox": False,
                    "created_at": created_at.isoformat(),
                })

        elif media_type == "video":
            video = getattr(embed, "video", None)
            blob_ref = getattr(video, "ref", None) if video else None
            cid = getattr(blob_ref, "ref", None)

            if not cid:
                continue

            media_key = f"{post.uri}|video|{cid}"

            if media_key in published:
                continue

            created_at = get_created_at(post)

            if not created_at or created_at > cutoff:
                continue

            items.append({
                "key": media_key,
                "kind": "video",
                "post_uri": post.uri,
                "post_cid": post.cid,
                "embed": embed,
                "redfox": False,
                "created_at": created_at.isoformat(),
            })

    # Tag alleen wanneer RedFox in de serie is genoemd.
    if redfox_found:
        for item in items:
            item["redfox"] = True

    return items


def collect_all_series(client, state):
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=MIN_AGE_DAYS
    )

    roots = collect_series_roots(client)
    print(f"🔎 Mogelijke series gevonden: {len(roots)}")

    all_series = []

    for root_uri in roots:
        try:
            thread = get_thread_root(client, root_uri)
            posts = flatten_own_series(thread)

            if not posts:
                continue

            items = extract_series_items(
                root_uri,
                posts,
                cutoff,
                state,
            )

            if items:
                all_series.append({
                    "root_uri": root_uri,
                    "items": items,
                })

        except Exception as error:
            print(f"⚠️ Serie overslaan ({root_uri}): {error}")

        time.sleep(0.3)

    return all_series


# ============================================================
# UPLOAD AND PUBLISH
# ============================================================

def publish_image(client, item, redfox_did):
    image = item["image"]
    blob_ref = image.image.ref

    raw_data = get_blob_bytes(
        blob_ref.did,
        blob_ref.ref,
    )

    if len(raw_data) > 1_000_000:
        print("⚠️ Foto groter dan 1 MB; overgeslagen.")
        return False

    uploaded = client.upload_blob(raw_data)

    embed = models.AppBskyEmbedImages.Main(
        images=[
            models.AppBskyEmbedImages.Image(
                alt=getattr(image, "alt", "") or "",
                image=uploaded.blob,
            )
        ]
    )

    text = ""
    facets = None

    if item["redfox"]:
        text = f"👉🏻 @{REDFOX_HANDLE}"
        facets = make_redfox_facets(text, redfox_did)

    client.send_post(
        text=text,
        embed=embed,
        facets=facets,
    )

    return True


def publish_video(client, item, redfox_did):
    video = item["embed"].video
    blob_ref = video.ref

    raw_data = get_blob_bytes(
        blob_ref.did,
        blob_ref.ref,
    )

    if len(raw_data) > 100 * 1024 * 1024:
        print("⚠️ Video groter dan 100 MB; overgeslagen.")
        return False

    uploaded = client.upload_blob(raw_data)

    embed = models.AppBskyEmbedVideo.Main(
        video=uploaded.blob,
        alt=getattr(video, "alt", "") or "",
    )

    text = ""
    facets = None

    if item["redfox"]:
        text = f"👉🏻 @{REDFOX_HANDLE}"
        facets = make_redfox_facets(text, redfox_did)

    client.send_post(
        text=text,
        embed=embed,
        facets=facets,
    )

    return True


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print("KNIGHTTAKESQUEEN BACKUP AUTOPOST")
    print("=" * 60)
    print(f"Bron: {SOURCE}")
    print(f"Doel: {TARGET}")
    print(f"Minimumleeftijd: {MIN_AGE_DAYS} dagen")
    print(f"Maximum per uitvoering: {MAX_POSTS}")
    print(f"Dry run: {DRY_RUN}")

    state = load_state()

    client = Client()
    print("\n🔗 Verbinden met Bluesky...")
    client.login(TARGET, PASSWORD)
    print("✅ Ingelogd als", TARGET)

    redfox_did = get_redfox_did(client)

    series_list = collect_all_series(client, state)

    if not series_list:
        print("ℹ️ Geen geschikte, nieuwe media gevonden.")
        save_state(state)
        return

    # Willekeurige keuze van series, maar de volgorde
    # binnen iedere serie blijft behouden.
    random.shuffle(series_list)

    # Als een serie nog bezig is, krijgt die voorrang
    # bij de volgende uitvoering.
    active_root = state.get("series_progress", {}).get("active_root")

    if active_root:
        series_list.sort(
            key=lambda series: series["root_uri"] != active_root
        )

    queue = []

    for series in series_list:
        for item in series["items"]:
            item["series_root"] = series["root_uri"]
            queue.append(item)

    queue = queue[:MAX_POSTS]

    if DRY_RUN:
        print("\n🧪 DRY RUN — er worden geen posts geplaatst.")

        for item in queue:
            print(
                item["kind"],
                item["key"],
                "RedFox-tag:", item["redfox"],
            )

        return

    published_count = 0

    for item in queue:
        print(f"\n📤 Publiceren: {item['kind']}")

        try:
            if item["kind"] == "image":
                success = publish_image(
                    client,
                    item,
                    redfox_did,
                )
            else:
                success = publish_video(
                    client,
                    item,
                    redfox_did,
                )

            if success:
                state["published"][item["key"]] = {
                    "published_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                    "series_root": item["series_root"],
                    "kind": item["kind"],
                }

                state["series_progress"]["active_root"] = (
                    item["series_root"]
                )

                published_count += 1
                save_state(state)

                print("✅ Gepubliceerd")
                time.sleep(SLEEP_SECONDS)

        except Exception as error:
            print(f"⚠️ Publiceren mislukt: {error}")

    # Controleer of de actieve serie nog niet-gepubliceerde
    # items heeft. Als die klaar is, wordt de actieve serie gewist.
    active_root = state["series_progress"].get("active_root")

    if active_root:
        still_pending = any(
            item["series_root"] == active_root
            and item["key"] not in state["published"]
            for series in series_list
            for item in series["items"]
        )

        if not still_pending:
            state["series_progress"].pop("active_root", None)

    save_state(state)

    print(
        f"\n🏁 Klaar. Nieuwe posts geplaatst: "
        f"{published_count}/{MAX_POSTS}"
    )


if __name__ == "__main__":
    main()
