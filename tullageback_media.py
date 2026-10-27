#!/usr/bin/env python3

import io
import os
import json
import time
import random
import urllib.request

from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image, ImageOps
from atproto import Client, models


# ==========================================
# INSTELLINGEN
# ==========================================

SOURCE = os.getenv(
    "SOURCE_ACCOUNT",
    "scazmo.bsky.social"
).lstrip("@")

TARGET = os.getenv(
    "BSKY_USERNAME",
    "snarlup.bsky.social"
).lstrip("@")

# Eigen GitHub Secret voor Snarlup
PASSWORD = os.environ["BSKY_SNARLUP_PASSWORD"]

STATE = Path(
    os.getenv(
        "STATE_FILE",
        "tullageback_media_state.json"
    )
)

MIN_AGE_DAYS = int(os.getenv("MIN_AGE_DAYS", "14"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "30"))
MAX_POSTS = int(os.getenv("MAX_POSTS", "3"))

# Maximale grootte voor afbeeldingen
MAX_IMAGE_BYTES = 1_000_000

# Maximale grootte voor video's
MAX_VIDEO_BYTES = 100_000_000

# RedFox-mention
REDFOX_HANDLE = "redfoxofficial.bsky.social"
REDFOX_PREFIX = "🦊 "
REDFOX_SUFFIX = " 🦊"

# Reboost laatste drie eigen originele posts
OWN_REBOOST_COUNT = 3
REBOOST_DELAY = 2

# Pauze tussen publicaties
POST_DELAY = 2

# False = daadwerkelijk publiceren
DRY_RUN = (
    os.getenv("DRY_RUN", "false").lower() == "true"
)


# ==========================================
# MEDIA DOWNLOADEN
# ==========================================

def download(url):

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "TullagebackMedia/1.0"
        }
    )

    with urllib.request.urlopen(req, timeout=90) as resp:
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

    STATE.parent.mkdir(
        parents=True,
        exist_ok=True
    )

    temporary = STATE.with_suffix(
        STATE.suffix + ".tmp"
    )

    temporary.write_text(
        json.dumps(state, indent=2),
        encoding="utf-8"
    )

    temporary.replace(STATE)


# ==========================================
# REDFOX: ECHTE KLIKBARE MENTION
# ==========================================

def create_redfox_mention(client):

    profile = client.get_profile(
        actor=REDFOX_HANDLE
    )

    handle_text = f"@{REDFOX_HANDLE}"

    text = (
        REDFOX_PREFIX
        + handle_text
        + REDFOX_SUFFIX
    )

    start = len(
        REDFOX_PREFIX.encode("utf-8")
    )

    end = start + len(
        handle_text.encode("utf-8")
    )

    facet = models.AppBskyRichtextFacet.Main(
        index=models.AppBskyRichtextFacet.ByteSlice(
            byte_start=start,
            byte_end=end
        ),
        features=[
            models.AppBskyRichtextFacet.Mention(
                did=profile.did
            )
        ]
    )

    return text, [facet]


# ==========================================
# QUOTE-POSTS HERKENNEN
# ==========================================

def is_quote_post(record):

    embed = getattr(
        record,
        "embed",
        None
    )

    return isinstance(
        embed,
        (
            models.AppBskyEmbedRecord.Main,
            models.AppBskyEmbedRecordWithMedia.Main
        )
    )


# ==========================================
# LAATSTE DRIE EIGEN POSTS REBOOSTEN
# ==========================================

def reboost_own_posts(client):

    print(
        "\n========== EIGEN POSTS REBOOSTEN =========="
    )

    own_did = client.me.did

    result = client.get_author_feed(
        actor=own_did,
        limit=100
    )

    own_posts = []

    for item in result.feed:

        post = item.post
        record = post.record

        # Geen reposts
        if getattr(item, "reason", None) is not None:
            continue

        # Alleen eigen posts
        if post.author.did != own_did:
            continue

        # Alleen originele postrecords
        if not isinstance(
            record,
            models.AppBskyFeedPost.Record
        ):
            continue

        # Geen replies
        if getattr(record, "reply", None) is not None:
            continue

        # Geen quote-posts
        if is_quote_post(record):
            continue

        own_posts.append(post)

        if len(own_posts) >= OWN_REBOOST_COUNT:
            break

    # Oudste van de drie eerst
    own_posts.reverse()

    for post in own_posts:

        try:

            print(
                f"Eigen post: {post.uri}"
            )

            if DRY_RUN:
                print(
                    "DRY_RUN: reboost overgeslagen."
                )
                continue

            viewer = getattr(
                post,
                "viewer",
                None
            )

            repost_uri = (
                getattr(viewer, "repost", None)
                if viewer else None
            )

            # Bestaande repost eerst verwijderen
            if repost_uri:

                client.delete_repost(
                    repost_uri
                )

                print(
                    "Bestaande repost verwijderd."
                )

                time.sleep(
                    REBOOST_DELAY
                )

            # Daarna opnieuw reposten
            client.repost(
                post.uri,
                post.cid
            )

            print(
                "Eigen post opnieuw gerepost."
            )

            time.sleep(
                REBOOST_DELAY
            )

        except Exception as exc:

            print(
                f"Reboost mislukt voor "
                f"{post.uri}: {exc}"
            )

    print(
        "Reboost afgerond."
    )


# ==========================================
# FOTO VERKLEINEN TOT MAXIMAAL 1 MB
# ==========================================

def resize_image(raw):

    if len(raw) <= MAX_IMAGE_BYTES:
        return raw

    print(
        f"Foto is {len(raw) / 1_000_000:.2f} MB. "
        "Automatisch verkleinen..."
    )

    with Image.open(io.BytesIO(raw)) as original:

        image = ImageOps.exif_transpose(
            original
        )

        # Transparante afbeeldingen op witte achtergrond
        if (
            image.mode in ("RGBA", "LA")
            or (
                image.mode == "P"
                and "transparency" in image.info
            )
        ):

            rgba = image.convert("RGBA")

            background = Image.new(
                "RGBA",
                rgba.size,
                (255, 255, 255, 255)
            )

            image = Image.alpha_composite(
                background,
                rgba
            ).convert("RGB")

        else:
            image = image.convert("RGB")

        # Herhaaldelijk comprimeren en verkleinen
        while True:

            for quality in (88, 80, 72, 64, 56, 48, 40):

                output = io.BytesIO()

                image.save(
                    output,
                    format="JPEG",
                    quality=quality,
                    optimize=True
                )

                result = output.getvalue()

                if len(result) <= MAX_IMAGE_BYTES:

                    print(
                        "Foto geoptimaliseerd: "
                        f"{len(result) / 1_000_000:.2f} MB"
                    )

                    return result

            width, height = image.size

            if width <= 320 or height <= 320:

                raise ValueError(
                    "Foto kan niet klein genoeg "
                    "worden gemaakt voor de limiet."
                )

            image = image.resize(
                (
                    max(320, int(width * 0.80)),
                    max(320, int(height * 0.80))
                ),
                Image.Resampling.LANCZOS
            )


# ==========================================
# BRONMEDIA VERZAMELEN
# ==========================================

def collect_candidates(client, published):

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=MIN_AGE_DAYS)
    )

    cursor = None
    candidates = []

    print(
        f"\nBronaccount: {SOURCE}"
    )

    print(
        f"Minimumleeftijd: {MIN_AGE_DAYS} dagen"
    )

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

            # Geen reposts
            if getattr(item, "reason", None) is not None:
                continue

            # Alleen originele postrecords
            if not isinstance(
                record,
                models.AppBskyFeedPost.Record
            ):
                continue

            # Geen replies
            if getattr(record, "reply", None) is not None:
                continue

            # Geen quote-posts
            if is_quote_post(record):
                continue

            embed = getattr(
                record,
                "embed",
                None
            )

            # Alleen foto's en video's
            if not isinstance(
                embed,
                (
                    models.AppBskyEmbedImages.Main,
                    models.AppBskyEmbedVideo.Main
                )
            ):
                continue

            try:

                created = datetime.fromisoformat(
                    record.created_at.replace(
                        "Z",
                        "+00:00"
                    )
                )

            except (ValueError, AttributeError):
                continue

            # Minimaal 14 dagen oud
            if created > cutoff:
                continue

            uri = post.uri

            # Foto's afzonderlijk selecteren
            if isinstance(
                embed,
                models.AppBskyEmbedImages.Main
            ):

                for index, image in enumerate(
                    embed.images
                ):

                    media_key = (
                        f"{uri}#image-{index}"
                    )

                    if media_key in published:
                        continue

                    candidates.append({
                        "created": created,
                        "uri": uri,
                        "cid": post.cid,
                        "media_key": media_key,
                        "type": "image",
                        "media": image
                    })

            # Video afzonderlijk selecteren
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
                    "cid": post.cid,
                    "media_key": media_key,
                    "type": "video",
                    "media": embed
                })

        cursor = result.cursor

        if not cursor:
            break

    # Oudste geschikte media eerst
    candidates.sort(
        key=lambda item: item["created"]
    )

    print(
        f"\nNieuwe geschikte media: {len(candidates)}"
    )

    if candidates:

        print(
            "Oudste media: "
            f"{candidates[0]['created'].isoformat()}"
        )

    return candidates


# ==========================================
# BLOB DOWNLOADEN VAN DE BRONPOST
# ==========================================

def download_blob(uri, blob):

    did = uri.split("/")[2]
    cid = blob.ref.link

    url = (
        "https://bsky.social/xrpc/"
        "com.atproto.sync.getBlob"
        f"?did={did}&cid={cid}"
    )

    return download(url)


# ==========================================
# MEDIA PUBLICEREN MET REDFOX-TAG
# ==========================================

def publish_media(
    client,
    candidate,
    redfox_text,
    redfox_facets
):

    media = candidate["media"]
    media_type = candidate["type"]
    uri = candidate["uri"]

    # ======================================
    # FOTO
    # ======================================

    if media_type == "image":

        raw = download_blob(
            uri,
            media.image
        )

        raw = resize_image(raw)

        upload = client.upload_blob(
            raw
        ).blob

        image_args = {
            "alt": getattr(media, "alt", "") or "",
            "image": upload
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

    # ======================================
    # VIDEO
    # ======================================

    else:

        raw = download_blob(
            uri,
            media.video
        )

        if len(raw) > MAX_VIDEO_BYTES:

            raise ValueError(
                "Video groter dan 100 MB."
            )

        upload = client.upload_blob(
            raw
        ).blob

        video_args = {
            "video": upload
        }

        aspect_ratio = getattr(
            media,
            "aspect_ratio",
            None
        )

        if aspect_ratio is not None:
            video_args["aspect_ratio"] = aspect_ratio

        new_embed = models.AppBskyEmbedVideo.Main(
            **video_args
        )

    # ======================================
    # PUBLICEREN
    # ======================================

    return client.send_post(
        text=redfox_text,
        facets=redfox_facets,
        embed=new_embed
    )


# ==========================================
# HOOFDPROGRAMMA
# ==========================================

def main():

    client = Client()

    client.login(
        TARGET,
        PASSWORD
    )

    print(
        f"Ingelogd als: {TARGET}"
    )

    state = load_state()
    published = state["published"]

    # Eerst de laatste drie eigen posts reboosten
    reboost_own_posts(client)

    # RedFox-DID ophalen voor echte mention
    redfox_text, redfox_facets = (
        create_redfox_mention(client)
    )

    print(
        f"RedFox-tag: {redfox_text}"
    )

    # Geschikte media verzamelen en op datum sorteren
    candidates = collect_candidates(
        client,
        published
    )

    if not candidates:

        print(
            "Geen nieuwe geschikte media gevonden."
        )

        return

    successful = 0

    for candidate in candidates:

        if successful >= MAX_POSTS:
            break

        print(
            f"\nMedia: {candidate['media_key']}"
        )

        print(
            f"Datum: "
            f"{candidate['created'].isoformat()}"
        )

        if DRY_RUN:

            print(
                "DRY_RUN: publicatie overgeslagen."
            )

            continue

        try:

            response = publish_media(
                client,
                candidate,
                redfox_text,
                redfox_facets
            )

            # Alleen na geslaagde publicatie opslaan
            published[candidate["media_key"]] = (
                response.uri
            )

            save_state(state)

            successful += 1

            print(
                f"Gepubliceerd: {response.uri}"
            )

            print(
                "Klikbare RedFox-mention toegevoegd."
            )

            time.sleep(POST_DELAY)

        except Exception as exc:

            print(
                f"Media overgeslagen na fout: {exc}"
            )

    print(
        "\n========== RESULTAAT =========="
    )

    print(
        f"Succesvol gepubliceerd: {successful}"
    )

    print(
        f"Maximaal toegestaan: {MAX_POSTS}"
    )

    print(
        f"Statusbestand: {STATE}"
    )


# ==========================================
# START
# ==========================================

if __name__ == "__main__":
    main()