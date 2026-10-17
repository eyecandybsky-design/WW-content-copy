
import os
import re
import json
import time
import random
import urllib.request

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from atproto import Client, models


# ============================================================
# KNIGHTTAKESQUEEN MEDIA AUTOPOST
# Gebaseerd op het werkende Scazmo-script
# ============================================================

SOURCE = os.getenv(
    "SOURCE_ACCOUNT",
    "thepriapicknave.bsky.social"
).lstrip("@")

TARGET = os.getenv(
    "BSKY_USERNAME",
    "knighttakesqueen.bsky.social"
).lstrip("@")

PASSWORD = os.environ["BSKY_PASSWORD"]

STATE = Path(
    os.getenv(
        "STATE_FILE",
        "knighttakesqueen_state.json"
    )
)

MIN_AGE_DAYS = int(
    os.getenv("MIN_AGE_DAYS", "30")
)

MAX_PAGES = int(
    os.getenv("MAX_PAGES", "30")
)

MAX_POSTS = int(
    os.getenv("MAX_POSTS", "3")
)

DRY_RUN = (
    os.getenv("DRY_RUN", "true").lower()
    == "true"
)

SLEEP_SECONDS = 2

REDFOX_HANDLE = "redfoxofficial.bsky.social"

REDFOX_PATTERN = re.compile(
    r"(?<![\w@])@redfoxofficial\.bsky\.social"
    r"(?![\w.-])",
    re.IGNORECASE
)


# ============================================================
# MEDIA DOWNLOADEN
# ============================================================

def download(url):

    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "KnightTakesQueenMedia/1.0"
        }
    )

    with urllib.request.urlopen(
        request,
        timeout=90
    ) as response:

        return response.read()


def blob_url(uri, blob):

    did = uri.split("/")[2]

    query = urlencode({
        "did": did,
        "cid": blob.ref.link
    })

    return (
        "https://bsky.social/xrpc/"
        "com.atproto.sync.getBlob?"
        + query
    )


# ============================================================
# PUBLICATIEGESCHIEDENIS
# ============================================================

def load_state():

    if STATE.exists():

        try:
            state = json.loads(
                STATE.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            print("⚠️ Statebestand ongeldig; nieuwe geschiedenis gestart.")
            state = {}

    else:
        state = {}

    state.setdefault("published", {})

    return state


def save_state(state):

    STATE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = STATE.with_suffix(".tmp")

    temporary.write_text(
        json.dumps(
            state,
            indent=2,
            ensure_ascii=False
        ),
        encoding="utf-8"
    )

    temporary.replace(STATE)


# ============================================================
# REDFOX MENTION
# ============================================================

def get_redfox_did(client):

    try:

        profile = client.get_profile(
            actor=REDFOX_HANDLE
        )

        return profile.did

    except Exception as error:

        print(
            f"⚠️ RedFox-account niet gevonden: {error}"
        )

        return None


def make_redfox_facets(text, redfox_did):

    if not redfox_did:
        return None

    mention = "@" + REDFOX_HANDLE

    position = text.encode("utf-8").find(
        mention.encode("utf-8")
    )

    if position < 0:
        return None

    byte_start = position

    byte_end = (
        byte_start
        + len(mention.encode("utf-8"))
    )

    facet = models.AppBskyRichtextFacet.Main(
        index=models.AppBskyRichtextFacet.ByteSlice(
            byte_start=byte_start,
            byte_end=byte_end
        ),
        features=[
            models.AppBskyRichtextFacet.Mention(
                did=redfox_did
            )
        ]
    )

    return [facet]


def make_caption(client, original_text):

    original_text = original_text or ""

    if not REDFOX_PATTERN.search(original_text):

        return "", None

    print(
        "❣️ RedFox gevonden in de originele bronpost."
    )

    redfox_did = get_redfox_did(client)

    caption = "👉🏻 @" + REDFOX_HANDLE

    facets = make_redfox_facets(
        caption,
        redfox_did
    )

    return caption, facets


# ============================================================
# DATUM EN MEDIA HELPERS
# ============================================================

def parse_created_at(value):

    if not value:
        return None

    try:

        result = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if result.tzinfo is None:
            result = result.replace(
                tzinfo=timezone.utc
            )

        return result.astimezone(timezone.utc)

    except (ValueError, TypeError):

        return None


def get_media_blob_cid(blob):

    try:
        return blob.ref.link
    except (AttributeError, TypeError):
        return None


# ============================================================
# BRONACCOUNT DOORZOEKEN
# Alleen originele posts, geen replies of reposts
# ============================================================

def collect_candidates(client, published):

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=MIN_AGE_DAYS)
    )

    cursor = None
    candidates = []
    seen_keys = set()

    print(
        f"🔎 Media ophalen van: {SOURCE}"
    )

    for page in range(1, MAX_PAGES + 1):

        print(
            f"📄 Bronpagina {page}/{MAX_PAGES}..."
        )

        try:

            result = client.get_author_feed(
                actor=SOURCE,
                limit=100,
                cursor=cursor,
                filter="posts_with_media"
            )

        except Exception as error:

            print(
                f"⚠️ Feed ophalen mislukt: {error}"
            )

            break

        feed = result.feed or []

        if not feed:
            break

        for item in feed:

            post = item.post
            record = post.record

            # Alleen originele posts van de bron.
            if post.author.handle.lower() != SOURCE.lower():
                continue

            # Geen reposts.
            if getattr(item, "reason", None) is not None:
                continue

            if not isinstance(
                record,
                models.AppBskyFeedPost.Record
            ):
                continue

            # Geen replies.
            if getattr(record, "reply", None) is not None:
                continue

            created = parse_created_at(
                getattr(record, "created_at", None)
            )

            if not created or created > cutoff:
                continue

            embed = getattr(record, "embed", None)

            if isinstance(
                embed,
                models.AppBskyEmbedImages.Main
            ):

                # Iedere foto uit een carousel is een apart item.
                for index, image in enumerate(embed.images):

                    cid = get_media_blob_cid(
                        image.image
                    )

                    if not cid:
                        continue

                    media_key = (
                        f"{post.uri}#image-{index}-{cid}"
                    )

                    if media_key in published:
                        continue

                    if media_key in seen_keys:
                        continue

                    seen_keys.add(media_key)

                    original_text = (
                        getattr(record, "text", "")
                        or ""
                    )

                    candidates.append({
                        "key": media_key,
                        "uri": post.uri,
                        "kind": "image",
                        "blob": image.image,
                        "alt": getattr(image, "alt", "") or "",
                        "aspect_ratio": getattr(
                            image,
                            "aspect_ratio",
                            None
                        ),
                        "original_text": original_text,
                        "created_at": created,
                    })

            elif isinstance(
                embed,
                models.AppBskyEmbedVideo.Main
            ):

                cid = get_media_blob_cid(
                    embed.video
                )

                if not cid:
                    continue

                media_key = (
                    f"{post.uri}#video-{cid}"
                )

                if media_key in published:
                    continue

                if media_key in seen_keys:
                    continue

                seen_keys.add(media_key)

                original_text = (
                    getattr(record, "text", "")
                    or ""
                )

                candidates.append({
                    "key": media_key,
                    "uri": post.uri,
                    "kind": "video",
                    "blob": embed.video,
                    "alt": getattr(embed, "alt", "") or "",
                    "aspect_ratio": getattr(
                        embed,
                        "aspect_ratio",
                        None
                    ),
                    "original_text": original_text,
                    "created_at": created,
                })

        cursor = getattr(result, "cursor", None)

        if not cursor:
            break

        time.sleep(0.2)

    return candidates


# ============================================================
# FOTO PUBLICEREN
# ============================================================

def publish_image(client, item):

    raw = download(
        blob_url(
            item["uri"],
            item["blob"]
        )
    )

    # Bluesky-fotolimiet: maximaal 1 MB per afbeelding.
    if len(raw) > 1_000_000:

        print(
            "⚠️ Foto groter dan 1 MB; overgeslagen."
        )

        return False

    uploaded = client.upload_blob(raw).blob

    new_image = models.AppBskyEmbedImages.Image(
        alt=item["alt"],
        image=uploaded
    )

    if item["aspect_ratio"]:

        new_image.aspect_ratio = (
            item["aspect_ratio"]
        )

    new_embed = models.AppBskyEmbedImages.Main(
        images=[new_image]
    )

    caption, facets = make_caption(
        client,
        item["original_text"]
    )

    response = client.send_post(
        text=caption,
        embed=new_embed,
        facets=facets
    )

    print(
        f"✅ Foto gepubliceerd: {response.uri}"
    )

    return response.uri


# ============================================================
# VIDEO PUBLICEREN
# ============================================================

def publish_video(client, item):

    raw = download(
        blob_url(
            item["uri"],
            item["blob"]
        )
    )

    if len(raw) > 100 * 1024 * 1024:

        print(
            "⚠️ Video groter dan 100 MB; overgeslagen."
        )

        return False

    uploaded = client.upload_blob(raw).blob

    video_args = {
        "video": uploaded
    }

    if item["aspect_ratio"]:
        video_args["aspect_ratio"] = (
            item["aspect_ratio"]
        )

    new_embed = models.AppBskyEmbedVideo.Main(
        **video_args
    )

    caption, facets = make_caption(
        client,
        item["original_text"]
    )

    response = client.send_post(
        text=caption,
        embed=new_embed,
        facets=facets
    )

    print(
        f"✅ Video gepubliceerd: {response.uri}"
    )

    return response.uri


# ============================================================
# MAIN
# ============================================================

def main():

    print("=" * 60)
    print("KNIGHTTAKESQUEEN MEDIA AUTOPOST")
    print("=" * 60)

    print(f"Bron: {SOURCE}")
    print(f"Doel: {TARGET}")
    print(f"Minimumleeftijd: {MIN_AGE_DAYS} dagen")
    print(f"Maximum per uitvoering: {MAX_POSTS}")
    print(f"Dry run: {DRY_RUN}")

    state = load_state()
    published = state["published"]

    client = Client()

    print("\n🔗 Verbinden met Bluesky...")

    client.login(
        TARGET,
        PASSWORD
    )

    print(
        f"✅ Ingelogd als {TARGET}"
    )

    candidates = collect_candidates(
        client,
        published
    )

    print(
        f"\n📊 Geschikte nieuwe media gevonden: "
        f"{len(candidates)}"
    )

    if not candidates:

        print(
            "ℹ️ Geen geschikte, nieuwe media gevonden."
        )

        return

    # Willekeurige volgorde, zoals in Scazmo.
    random.shuffle(candidates)

    selected = candidates[:MAX_POSTS]

    print(
        f"🎲 Willekeurig geselecteerd: {len(selected)}"
    )

    if DRY_RUN:

        print(
            "\n🧪 DRY RUN: er worden geen berichten gepubliceerd."
        )

        for item in selected:

            print(
                f"- {item['kind']} | "
                f"{item['key']} | "
                f"Bron: {item['uri']}"
            )

        return

    published_count = 0

    for item in selected:

        print(
            f"\n📤 Publiceren: {item['kind']}"
        )

        try:

            if item["kind"] == "image":

                response_uri = publish_image(
                    client,
                    item
                )

            else:

                response_uri = publish_video(
                    client,
                    item
                )

            if response_uri:

                published[item["key"]] = {
                    "published_at": datetime.now(
                        timezone.utc
                    ).isoformat(),
                    "source_uri": item["uri"],
                    "target_uri": response_uri,
                    "kind": item["kind"],
                }

                save_state(state)

                published_count += 1

                time.sleep(SLEEP_SECONDS)

        except Exception as error:

            print(
                f"⚠️ Publiceren mislukt: {error}"
            )

    print(
        f"\n🏁 Klaar. Nieuwe berichten geplaatst: "
        f"{published_count}/{MAX_POSTS}"
    )


if __name__ == "__main__":
    main()
