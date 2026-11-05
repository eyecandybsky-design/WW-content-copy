
import os
import json
import random
import urllib.request

from datetime import datetime, timedelta, timezone
from pathlib import Path

from atproto import Client, models


# ==========================================
# INSTELLINGEN
# ==========================================

SOURCE = os.getenv(
    "SOURCE_ACCOUNT",
    "leonie04.bsky.social"
).lstrip("@")

TARGET = os.getenv(
    "LESBEGIN_BSKY_USERNAME",
    "sexybynature.bsky.social"
).lstrip("@")

PASSWORD = os.environ["LESBEGIN_BSKY_PASSWORD"]

STATE = Path(
    os.getenv(
        "STATE_FILE",
        "lesbegin_state.json"
    )
)

MIN_AGE_DAYS = int(os.getenv("MIN_AGE_DAYS", "30"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "30"))
MAX_POSTS = int(os.getenv("MAX_POSTS", "3"))

# Bluesky uploadlimieten
MAX_IMAGE_SIZE = 2_000_000
MAX_VIDEO_SIZE = 300_000_000


# ==========================================
# MEDIA DOWNLOADEN
# ==========================================

def download(url):

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "LesbeginMedia/1.0"}
    )

    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


# ==========================================
# PUBLICATIEGESCHIEDENIS
# ==========================================

def load_state():

    if STATE.exists():
        state = json.loads(
            STATE.read_text(encoding="utf-8")
        )
    else:
        state = {"published": {}}

    state.setdefault("published", {})
    return state


def save_state(state):

    STATE.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8"
    )


# ==========================================
# MEDIA OPHALEN
# ==========================================

def collect_candidates(client, published):

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=MIN_AGE_DAYS)
    )

    cursor = None
    candidates = []

    totals = {
        "feed_items": 0,
        "reposts": 0,
        "replies": 0,
        "non_media": 0,
        "media_posts": 0,
        "too_new": 0,
        "old_media_posts": 0,
        "already_published": 0,
        "new_images": 0,
        "new_videos": 0
    }

    print(f"Bronaccount: {SOURCE}")
    print(f"Doelaccount: {TARGET}")
    print(f"Zoeklimiet: {MAX_PAGES} pagina's")
    print(f"Minimumleeftijd: {MIN_AGE_DAYS} dagen")
    print(f"Maximum publicaties: {MAX_POSTS}")

    for page in range(MAX_PAGES):

        print(
            f"Bronpagina {page + 1}/{MAX_PAGES} bekijken..."
        )

        result = client.get_author_feed(
            actor=SOURCE,
            limit=100,
            cursor=cursor,
            filter="posts_with_media"
        )

        items = result.feed or []
        totals["feed_items"] += len(items)

        for item in items:

            post = item.post
            record = post.record

            # Geen reposts
            if getattr(item, "reason", None) is not None:
                totals["reposts"] += 1
                continue

            if not isinstance(
                record,
                models.AppBskyFeedPost.Record
            ):
                continue

            # Geen replies
            if getattr(record, "reply", None) is not None:
                totals["replies"] += 1
                continue

            embed = getattr(record, "embed", None)

            # Alleen foto's en video's
            if not isinstance(
                embed,
                (
                    models.AppBskyEmbedImages.Main,
                    models.AppBskyEmbedVideo.Main
                )
            ):
                totals["non_media"] += 1
                continue

            totals["media_posts"] += 1

            try:
                created = datetime.fromisoformat(
                    record.created_at.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                continue

            # Minimaal 30 dagen oud
            if created > cutoff:
                totals["too_new"] += 1
                continue

            totals["old_media_posts"] += 1

            uri = post.uri

            # FOTO'S
            if isinstance(
                embed,
                models.AppBskyEmbedImages.Main
            ):

                for index, image in enumerate(embed.images):

                    media_key = f"{uri}#image-{index}"

                    if media_key in published:
                        totals["already_published"] += 1
                        continue

                    candidates.append({
                        "created": created,
                        "uri": uri,
                        "media_key": media_key,
                        "type": "image",
                        "media": image
                    })

                    totals["new_images"] += 1

            # VIDEO'S
            elif isinstance(
                embed,
                models.AppBskyEmbedVideo.Main
            ):

                media_key = f"{uri}#video"

                if media_key in published:
                    totals["already_published"] += 1
                    continue

                candidates.append({
                    "created": created,
                    "uri": uri,
                    "media_key": media_key,
                    "type": "video",
                    "media": embed
                })

                totals["new_videos"] += 1

        cursor = result.cursor

        if not cursor:
            print("Einde van de bronfeed bereikt.")
            break

    print("\n========== ZOEKSAMENVATTING ==========")

    for key, value in totals.items():
        print(f"{key}: {value}")

    print(f"Nieuwe mediakandidaten: {len(candidates)}")
    print("======================================\n")

    return candidates


# ==========================================
# MEDIA PUBLICEREN
# ==========================================

def publish_media(client, candidate):

    uri = candidate["uri"]
    media = candidate["media"]
    media_type = candidate["type"]

    did = uri.split("/")[2]

    if media_type == "image":

        blob = media.image

        url = (
            "https://bsky.social/xrpc/"
            "com.atproto.sync.getBlob"
            f"?did={did}&cid={blob.ref.link}"
        )

        raw = download(url)

        if len(raw) > MAX_IMAGE_SIZE:
            raise ValueError(
                "Afbeelding groter dan 2 MB."
            )

        uploaded = client.upload_blob(raw).blob

        image_args = {
            "alt": getattr(media, "alt", "") or "",
            "image": uploaded
        }

        aspect_ratio = getattr(
            media,
            "aspect_ratio",
            None
        )

        if aspect_ratio is not None:
            image_args["aspect_ratio"] = aspect_ratio

        new_image = models.AppBskyEmbedImages.Image(
            **image_args
        )

        new_embed = models.AppBskyEmbedImages.Main(
            images=[new_image]
        )

    else:

        blob = media.video

        url = (
            "https://bsky.social/xrpc/"
            "com.atproto.sync.getBlob"
            f"?did={did}&cid={blob.ref.link}"
        )

        raw = download(url)

        if len(raw) > MAX_VIDEO_SIZE:
            raise ValueError(
                "Video groter dan 300 MB."
            )

        uploaded = client.upload_blob(raw).blob

        video_args = {
            "video": uploaded
        }

        aspect_ratio = getattr(
            media,
            "aspect_ratio",
            None
        )

        if aspect_ratio is not None:
            video_args["aspect_ratio"] = aspect_ratio

        alt = getattr(media, "alt", None)

        if alt:
            video_args["alt"] = alt

        new_embed = models.AppBskyEmbedVideo.Main(
            **video_args
        )

    # GEEN CAPTION OF TEKST
    return client.send_post(
        text="",
        embed=new_embed
    )


# ==========================================
# HOOFDPROGRAMMA
# ==========================================

def main():

    client = Client()
    client.login(TARGET, PASSWORD)

    print(f"Ingelogd als: {TARGET}")

    state = load_state()
    published = state["published"]

    candidates = collect_candidates(
        client,
        published
    )

    if not candidates:
        print("Geen nieuwe geschikte media gevonden.")
        return

    random.shuffle(candidates)

    print(
        f"Geschikte media gevonden: {len(candidates)}"
    )

    successful = 0

    for candidate in candidates:

        if successful >= MAX_POSTS:
            break

        print(
            f"Publicatiepoging {successful + 1}/{MAX_POSTS}: "
            f"{candidate['media_key']}"
        )

        try:

            response = publish_media(
                client,
                candidate
            )

            published[candidate["media_key"]] = response.uri
            save_state(state)

            successful += 1

            print(f"Gepubliceerd: {response.uri}")

        except Exception as exc:

            print(f"Media overgeslagen na fout: {exc}")

    print("\n========== RESULTAAT ==========")
    print(f"Succesvol gepubliceerd: {successful}")
    print(f"Maximum per run: {MAX_POSTS}")
    print("===============================\n")


# ==========================================
# START
# ==========================================

if __name__ == "__main__":
    main()
