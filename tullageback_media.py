
import json
import os
import random
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import requests
from atproto import Client, models


# ============================================================
# CONFIGURATIE
# ============================================================

SOURCE_ACCOUNT = os.getenv(
    "SOURCE_ACCOUNT",
    "eyescandy.bsky.social"
)

TARGET_ACCOUNT = os.getenv(
    "BSKY_USERNAME",
    "tullageback.bsky.social"
)

PASSWORD = os.getenv("BSKY_PASSWORD")

STATE_FILE = Path(
    os.getenv("STATE_FILE", "tullageback_media_state.json")
)

MIN_AGE_DAYS = int(os.getenv("MIN_AGE_DAYS", "30"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "20"))
MAX_CANDIDATES = int(os.getenv("MAX_CANDIDATES", "500"))

SLEEP_SECONDS = 2
MAX_VIDEO_BYTES = 45 * 1024 * 1024

API_BASE = "https://bsky.social/xrpc"


# ============================================================
# STATE
# ============================================================

def load_state():
    if not STATE_FILE.exists():
        return {"posted_uris": []}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            return {"posted_uris": []}

        state.setdefault("posted_uris", [])
        return state

    except (OSError, json.JSONDecodeError):
        print("⚠️ Statebestand kon niet worden gelezen.")
        raise


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

    temporary_file = STATE_FILE.with_suffix(".tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)

    temporary_file.replace(STATE_FILE)


# ============================================================
# HELPERS
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


def get_blob_cid(blob):
    """Haal de CID uit een AT Protocol blob-object."""

    if blob is None:
        return None

    cid = getattr(blob, "cid", None)
    if cid:
        return str(cid)

    ref = getattr(blob, "ref", None)

    if isinstance(ref, dict):
        return ref.get("$link")

    if ref is not None:
        link = getattr(ref, "link", None)
        if link:
            return str(link)

    if isinstance(blob, dict):
        ref = blob.get("ref", {})
        if isinstance(ref, dict):
            return ref.get("$link")

    return None


def download_image(url):
    response = requests.get(url, timeout=45)
    response.raise_for_status()

    content_type = response.headers.get(
        "Content-Type", ""
    ).split(";")[0].lower()

    if not content_type.startswith("image/"):
        raise ValueError(
            f"Geen geldig afbeeldingsbestand: {content_type}"
        )

    return response.content


def download_video(source_did, video_blob):
    cid = get_blob_cid(video_blob)

    if not cid:
        raise ValueError("Geen videoblob-CID gevonden.")

    url = (
        f"{API_BASE}/com.atproto.sync.getBlob?"
        + urlencode({"did": source_did, "cid": cid})
    )

    response = requests.get(url, timeout=120)
    response.raise_for_status()

    content = response.content

    if not content:
        raise ValueError("Het videobestand is leeg.")

    if len(content) > MAX_VIDEO_BYTES:
        raise ValueError(
            "Video is groter dan de ingestelde limiet van 45 MB."
        )

    content_type = response.headers.get(
        "Content-Type", ""
    ).split(";")[0].lower()

    if content_type and not (
        content_type.startswith("video/")
        or content_type == "application/octet-stream"
    ):
        raise ValueError(
            f"Onverwacht videotype: {content_type}"
        )

    return content


def get_media_type(post_view, record):
    """
    Retourneert image of video.
    Carrousels en posts zonder ondersteunde media worden overgeslagen.
    """

    view_embed = getattr(post_view, "embed", None)
    record_embed = getattr(record, "embed", None)

    if view_embed is None or record_embed is None:
        return None

    # Video
    playlist = getattr(view_embed, "playlist", None)
    raw_video = getattr(record_embed, "video", None)

    if playlist and raw_video is not None:
        return {
            "type": "video",
            "blob": raw_video,
            "alt": getattr(record_embed, "alt", "") or "",
            "aspect_ratio": getattr(
                record_embed, "aspect_ratio", None
            ),
        }

    # Afbeeldingen: alleen exact één afbeelding.
    view_images = getattr(view_embed, "images", None)
    raw_images = getattr(record_embed, "images", None)

    if view_images is not None and raw_images is not None:
        if len(view_images) != 1 or len(raw_images) != 1:
            return None

        image_view = view_images[0]
        image_record = raw_images[0]

        return {
            "type": "image",
            "url": getattr(image_view, "fullsize", None),
            "alt": getattr(image_record, "alt", "") or "",
            "aspect_ratio": getattr(
                image_view, "aspect_ratio", None
            ),
        }

    return None


def make_aspect_ratio(value):
    if value is None:
        return None

    width = getattr(value, "width", None)
    height = getattr(value, "height", None)

    if not width or not height:
        return None

    return models.AppBskyEmbedDefs.AspectRatio(
        width=int(width),
        height=int(height),
    )


def create_media_post(client, source_post, media):
    """Plaats uitsluitend de media, zonder caption of bronposttekst."""

    if media["type"] == "image":
        if not media.get("url"):
            raise ValueError("Geen afbeeldings-URL beschikbaar.")

        image_data = download_image(media["url"])
        uploaded = client.upload_blob(image_data)

        image = models.AppBskyEmbedImages.Image(
            image=uploaded.blob,
            alt=media["alt"],
            aspect_ratio=make_aspect_ratio(
                media.get("aspect_ratio")
            ),
        )

        embed = models.AppBskyEmbedImages.Main(
            images=[image]
        )

        return client.send_post(text="", embed=embed)

    if media["type"] == "video":
        source_did = source_post.author.did

        video_data = download_video(
            source_did,
            media["blob"]
        )

        uploaded = client.upload_blob(video_data)

        embed = models.AppBskyEmbedVideo.Main(
            video=uploaded.blob,
            alt=media["alt"],
            aspect_ratio=make_aspect_ratio(
                media.get("aspect_ratio")
            ),
        )

        return client.send_post(text="", embed=embed)

    raise ValueError("Niet-ondersteund mediatype.")


# ============================================================
# BRONPOSTS OPHALEN
# ============================================================

def find_candidates(client, state):
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=MIN_AGE_DAYS
    )

    already_posted = set(state["posted_uris"])
    candidates = []
    cursor = None

    for page in range(MAX_PAGES):
        params = {
            "actor": SOURCE_ACCOUNT,
            "limit": 100,
            "filter": "posts_no_replies",
        }

        if cursor:
            params["cursor"] = cursor

        response = client.app.bsky.feed.get_author_feed(
            params=params
        )

        feed = response.feed or []

        if not feed:
            break

        reached_cutoff = False

        for item in feed:
            # Alleen originele posts, geen reposts.
            if getattr(item, "reason", None) is not None:
                continue

            post = item.post
            record = post.record

            # Extra controle: geen antwoorden.
            if getattr(record, "reply", None) is not None:
                continue

            created_at = parse_datetime(
                getattr(record, "created_at", None)
            )

            if created_at is None:
                continue

            if created_at > cutoff:
                continue

            reached_cutoff = True

            if post.uri in already_posted:
                continue

            media = get_media_type(post, record)

            if media is None:
                continue

            candidates.append({
                "post": post,
                "media": media,
                "created_at": created_at,
            })

            if len(candidates) >= MAX_CANDIDATES:
                break

        print(
            f"📄 Pagina {page + 1}: "
            f"{len(candidates)} geschikte kandidaten gevonden."
        )

        if len(candidates) >= MAX_CANDIDATES:
            break

        # De feed loopt van nieuw naar oud.
        if reached_cutoff:
            break

        cursor = getattr(response, "cursor", None)

        if not cursor:
            break

    return candidates


# ============================================================
# HOOFDPROGRAMMA
# ============================================================

def main():
    if not PASSWORD:
        raise RuntimeError(
            "BSKY_PASSWORD ontbreekt in GitHub Secrets."
        )

    print("=" * 55)
    print("TULLAGEBACK RANDOM MEDIA AUTOPOST")
    print("=" * 55)
    print(f"Bron: {SOURCE_ACCOUNT}")
    print(f"Doel: {TARGET_ACCOUNT}")
    print(f"Minimale leeftijd: {MIN_AGE_DAYS} dagen")

    state = load_state()

    client = Client()
    client.login(TARGET_ACCOUNT, PASSWORD)

    print("✅ Ingelogd op het doelaccount.")

    candidates = find_candidates(client, state)

    if not candidates:
        print("ℹ️ Geen nieuwe geschikte media gevonden.")
        return

    # Willekeurige keuze uit alle gevonden geschikte kandidaten.
    random.shuffle(candidates)

    for candidate in candidates:
        source_post = candidate["post"]
        source_uri = source_post.uri

        try:
            print(
                "🎲 Gekozen bronpost: "
                f"{source_uri}"
            )
            print(
                "📅 Bronpostdatum: "
                f"{candidate['created_at'].isoformat()}"
            )
            print(
                f"🖼️ Mediatype: {candidate['media']['type']}"
            )

            create_media_post(
                client,
                source_post,
                candidate["media"],
            )

            # Alleen als publiceren is gelukt opslaan als verwerkt.
            state["posted_uris"].append(source_uri)
            state["posted_uris"] = list(
                dict.fromkeys(state["posted_uris"])
            )

            save_state(state)

            print("✅ Media succesvol gepubliceerd.")
            print("🔒 Bronpost geregistreerd tegen duplicaten.")
            return

        except Exception as error:
            print(
                f"⚠️ Deze kandidaat is overgeslagen: {error}"
            )
            time.sleep(SLEEP_SECONDS)

    print("❌ Geen van de geselecteerde kandidaten kon worden geplaatst.")


if __name__ == "__main__":
    main()
