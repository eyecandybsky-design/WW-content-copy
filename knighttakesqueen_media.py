
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
# INSTELLINGEN
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

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "KnightTakesQueenMedia/1.0"
        }
    )

    with urllib.request.urlopen(
        req,
        timeout=90
    ) as resp:

        return resp.read()


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
            state = {"published": {}}

    else:
        state = {"published": {}}

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

def make_caption(client, original_text):

    original_text = original_text or ""

    if not REDFOX_PATTERN.search(original_text):
        return "", None

    print("RedFox gevonden in originele bronpost.")

    try:
        profile = client.get_profile(
            actor=REDFOX_HANDLE
        )

        redfox_did = profile.did

    except Exception as exc:
        print(f"⚠️ RedFox-DID ophalen mislukt: {exc}")
        return "", None

    caption = "👉🏻 @" + REDFOX_HANDLE

    mention = "@" + REDFOX_HANDLE

    start = caption.encode("utf-8").find(
        mention.encode("utf-8")
    )

    end = start + len(
        mention.encode("utf-8")
    )

    facet = models.AppBskyRichtextFacet.Main(
        index=models.AppBskyRichtextFacet.ByteSlice(
            byte_start=start,
            byte_end=end
        ),
        features=[
            models.AppBskyRichtextFacet.Mention(
                did=redfox_did
            )
        ]
    )

    return caption, [facet]


# ============================================================
# MEDIA OPHALEN EN SELECTEREN
# ============================================================

def collect_candidates(client, published):

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=MIN_AGE_DAYS)
    )

    cursor = None
    candidates = []

    print(f"Media ophalen van: {SOURCE}")

    for page in range(MAX_PAGES):

        print(
            f"Bronpagina {page + 1}/{MAX_PAGES}"
        )

        result = client.get_author_feed(
            actor=SOURCE,
            limit=100,
            cursor=cursor,
            filter="posts_with_media"
        )

        for item in result.feed:

            post = item.post
            record = post.record

            # Alleen berichten van het bronaccount.
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

            # Zelfde mediaherkenning als Scazmo.
            embed = getattr(record, "embed", None)

            if not isinstance(
                embed,
                (
                    models.AppBskyEmbedImages.Main,
                    models.AppBskyEmbedVideo.Main
                )
            ):
                continue

            created = datetime.fromisoformat(
                record.created_at.replace(
                    "Z",
                    "+00:00"
                )
            )

            if created > cutoff:
                continue

            uri = post.uri
            original_text = getattr(
                record,
                "text",
                ""
            ) or ""

            # Foto's afzonderlijk selecteren.
            if isinstance(
                embed,
                models.AppBskyEmbedImages.Main
            ):

                for index, image in enumerate(embed.images):

                    media_key = (
                        f"{uri}#image-{index}"
                    )

                    if media_key in published:
                        continue

                    candidates.append({
                        "created": created,
                        "uri": uri,
                        "key": media_key,
                        "kind": "image",
                        "media": image,
                        "text": original_text
                    })

            # Video als één bericht selecteren.
            elif isinstance(
                embed,
                models.AppBskyEmbedVideo.Main
            ):

                media_key = f"{uri}#video"

                if media_key in published:
                    continue

                candidates.append({
                    "created": created,
                    "uri": uri,
                    "key": media_key,
                    "kind": "video",
                    "media": embed,
                    "text": original_text
                })

        cursor = result.cursor

        if not cursor:
            break

        time.sleep(0.2)

    return candidates


# ============================================================
# FOTO PUBLICEREN
# ============================================================

def publish_image(client, item):

    image = item["media"]
    blob = image.image

    raw = download(
        blob_url(
            item["uri"],
            blob
        )
    )

    if len(raw) > 1_000_000:
        print("⚠️ Foto groter dan 1 MB; overgeslagen.")
        return None

    uploaded = client.upload_blob(raw).blob

    new_image = models.AppBskyEmbedImages.Image(
        alt=getattr(image, "alt", "") or "",
        image=uploaded
    )

    if getattr(image, "aspect_ratio", None):
        new_image.aspect_ratio = image.aspect_ratio

    new_embed = models.AppBskyEmbedImages.Main(
        images=[new_image]
    )

    caption, facets = make_caption(
        client,
        item["text"]
    )

    response = client.send_post(
        text=caption,
        embed=new_embed,
        facets=facets
    )

    return response.uri


# ============================================================
# VIDEO PUBLICEREN
# ============================================================

def publish_video(client, item):

    video_embed = item["media"]
    blob = video_embed.video

    raw = download(
        blob_url(
            item["uri"],
            blob
        )
    )

    if len(raw) > 100_000_000:
        print("⚠️ Video groter dan 100 MB; overgeslagen.")
        return None

    uploaded = client.upload_blob(raw).blob

    video_args = {
        "video": uploaded
    }

    if getattr(video_embed, "aspect_ratio", None):
        video_args["aspect_ratio"] = video_embed.aspect_ratio

    new_embed = models.AppBskyEmbedVideo.Main(
        **video_args
    )

    caption, facets = make_caption(
        client,
        item["text"]
    )

    response = client.send_post(
        text=caption,
        embed=new_embed,
        facets=facets
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

    state = load_state()
    published = state["published"]

    client = Client()

    print("\nVerbinden met Bluesky...")

    client.login(
        TARGET,
        PASSWORD
    )

    print(f"Ingelogd als: {TARGET}")

    candidates = collect_candidates(
        client,
        published
    )

    print(
        f"Geschikte nieuwe media gevonden: "
        f"{len(candidates)}"
    )

    if not candidates:
        print("Geen geschikte, nieuwe media gevonden.")
        return

    random.shuffle(candidates)

    selected = candidates[:MAX_POSTS]

    print(
        f"Willekeurig geselecteerd: {len(selected)}"
    )

    published_count = 0

    for item in selected:

        print(
            f"\nPubliceren: {item['kind']} | "
            f"{item['key']}"
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

            if not response_uri:
                continue

            published[item["key"]] = response_uri

            save_state(state)

            published_count += 1

            print(f"Gepubliceerd: {response_uri}")

            time.sleep(SLEEP_SECONDS)

        except Exception as exc:

            print(
                f"⚠️ Publiceren mislukt: {exc}"
            )

    print(
        f"\nKlaar. Gepubliceerd: "
        f"{published_count}/{MAX_POSTS}"
    )


if __name__ == "__main__":
    main()
