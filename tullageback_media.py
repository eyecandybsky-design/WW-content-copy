#!/usr/bin/env python3

import json
import os
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

from atproto import Client, models
from PIL import Image, ImageOps


# ============================================================
# CONFIGURATIE
# ============================================================

SOURCE_ACCOUNT = os.getenv(
    "SOURCE_ACCOUNT", "scazmo.bsky.social"
)

TARGET_ACCOUNT = os.getenv(
    "BSKY_USERNAME", "snarlup.bsky.social"
)

PASSWORD = os.environ["BSKY_SNARLUP_PASSWORD"]

STATE_FILE = os.getenv(
    "STATE_FILE", "tullageback_media_state.json"
)

MIN_AGE_DAYS = int(os.getenv("MIN_AGE_DAYS", "14"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "30"))
MAX_POSTS = int(os.getenv("MAX_POSTS", "3"))

MAX_IMAGE_BYTES = 1_000_000
MAX_VIDEO_BYTES = 100_000_000

POST_DELAY = 2
REBOOST_DELAY = 2
OWN_REBOOST_COUNT = 3

REDFOX_HANDLE = "redfoxofficial.bsky.social"
REDFOX_TEXT = f"🦊 @{REDFOX_HANDLE} 🦊"

DRY_RUN = os.getenv("DRY_RUN", "false").lower() == "true"


# ============================================================
# ALGEMENE FUNCTIES
# ============================================================

def log(message):
    print(f"[Tullageback Media] {message}", flush=True)


def load_state():
    path = Path(STATE_FILE)

    if not path.exists():
        log("Geen bestaand statusbestand. Nieuwe geschiedenis gestart.")
        return {}

    try:
        with path.open("r", encoding="utf-8") as file:
            state = json.load(file)

        if not isinstance(state, dict):
            raise ValueError("Statusbestand bevat geen JSON-object.")

        log(f"Publicatiegeschiedenis geladen: {len(state)} items.")
        return state

    except (json.JSONDecodeError, OSError, ValueError) as exc:
        raise RuntimeError(
            f"Statusbestand kan niet veilig worden gelezen: {exc}"
        ) from exc


def save_state(state):
    path = Path(STATE_FILE)
    temporary_path = path.with_suffix(path.suffix + ".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)

    temporary_path.replace(path)
    log("Publicatiegeschiedenis opgeslagen.")


def parse_datetime(value):
    if not value:
        return None

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))

        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)

        return parsed.astimezone(timezone.utc)

    except (TypeError, ValueError):
        return None


def get_cid(blob):
    ref = getattr(blob, "ref", None)

    if ref is None:
        return None

    return str(ref)


def get_embed(record):
    return getattr(record, "embed", None)


def is_quote_post(record):
    """
    Quotes en quote-posts worden niet opnieuw gepubliceerd.
    """

    embed = get_embed(record)

    if embed is None:
        return False

    class_name = type(embed).__name__.lower()

    if "record" in class_name:
        return True

    if hasattr(embed, "record"):
        return True

    return False


def is_reply(record):
    return getattr(record, "reply", None) is not None


# ============================================================
# REDFOX: ALLEEN TAGGEN ALS DE BRONPOST REDFOX NOEMT
# ============================================================

def source_mentions_redfox(record, redfox_did):
    """
    Herkent RedFox in de tekst of in de bestaande mention-facets
    van de oorspronkelijke Bluesky-post.
    """

    text = getattr(record, "text", "") or ""

    # Controleer de geschreven handle, hoofdletterongevoelig.
    handle_pattern = (
        r"(?<![\w.])@"
        + re.escape(REDFOX_HANDLE)
        + r"(?![\w.-])"
    )

    if re.search(handle_pattern, text, flags=re.IGNORECASE):
        return True

    # Controleer ook echte Bluesky-mentions, zelfs als de tekst
    # of de weergegeven handle anders is opgemaakt.
    for facet in getattr(record, "facets", None) or []:
        for feature in getattr(facet, "features", None) or []:
            feature_type = type(feature).__name__.lower()

            if "mention" not in feature_type:
                continue

            if getattr(feature, "did", None) == redfox_did:
                return True

    return False


def create_redfox_facet(redfox_did):
    """
    Maakt de RedFox-tag klikbaar via een echte Bluesky-mention-facet.
    """

    prefix = "🦊 @"
    suffix = " 🦊"

    text = prefix + REDFOX_HANDLE + suffix

    byte_start = len(prefix.encode("utf-8"))
    byte_end = byte_start + len(REDFOX_HANDLE.encode("utf-8"))

    facet = models.AppBskyRichtextFacet(
        index=models.AppBskyRichtextFacet.ByteSlice(
            byte_start=byte_start,
            byte_end=byte_end,
        ),
        features=[
            models.AppBskyRichtextFacet.Mention(
                did=redfox_did
            )
        ],
    )

    return text, [facet]


# ============================================================
# MEDIA DOWNLOADEN
# ============================================================

def download_blob(source_did, cid, max_bytes):
    if not cid:
        raise ValueError("Media heeft geen geldige CID.")

    query = urllib.parse.urlencode({
        "did": source_did,
        "cid": cid,
    })

    url = (
        "https://bsky.social/xrpc/"
        "com.atproto.sync.getBlob?"
        + query
    )

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "TullagebackMedia/1.0"},
    )

    with urllib.request.urlopen(request, timeout=90) as response:
        content_length = response.headers.get("Content-Length")

        if content_length and int(content_length) > max_bytes:
            raise ValueError(
                f"Bestand is groter dan de limiet van {max_bytes} bytes."
            )

        data = response.read(max_bytes + 1)

    if len(data) > max_bytes:
        raise ValueError(
            f"Bestand overschrijdt de limiet van {max_bytes} bytes."
        )

    return data


# ============================================================
# FOTO'S VERKLEINEN TOT MAXIMAAL 1 MB
# ============================================================

def resize_image(raw_data):
    if len(raw_data) <= MAX_IMAGE_BYTES:
        return raw_data

    log("Foto groter dan 1 MB. Foto wordt verkleind.")

    with Image.open(BytesIO(raw_data)) as original:
        image = ImageOps.exif_transpose(original)

        # JPEG ondersteunt geen transparantie.
        if image.mode in ("RGBA", "LA") or "transparency" in image.info:
            rgba = image.convert("RGBA")
            background = Image.new("RGB", rgba.size, "white")
            background.paste(rgba, mask=rgba.getchannel("A"))
            image = background
        else:
            image = image.convert("RGB")

        quality_levels = [88, 82, 76, 70, 64, 58, 52, 46, 40, 34]

        while True:
            for quality in quality_levels:
                output = BytesIO()

                image.save(
                    output,
                    format="JPEG",
                    quality=quality,
                    optimize=True,
                )

                result = output.getvalue()

                if len(result) <= MAX_IMAGE_BYTES:
                    log(
                        f"Foto verkleind tot "
                        f"{len(result) / 1024:.0f} KB."
                    )
                    return result

            width, height = image.size

            if width <= 320 or height <= 320:
                raise ValueError(
                    "Foto kan niet verder worden verkleind tot 1 MB."
                )

            new_size = (
                max(320, int(width * 0.8)),
                max(320, int(height * 0.8)),
            )

            image = image.resize(new_size, Image.Resampling.LANCZOS)


# ============================================================
# BRONPOSTEN OPHALEN
# ============================================================

def collect_candidates(client, source_did, redfox_did, state):
    candidates = []
    cursor = None
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=MIN_AGE_DAYS
    )

    log(
        f"Zoeken naar originele media ouder dan "
        f"{MIN_AGE_DAYS} dagen."
    )

    for page_number in range(1, MAX_PAGES + 1):
        try:
            response = client.get_author_feed(
                actor=SOURCE_ACCOUNT,
                limit=100,
                cursor=cursor,
                filter="posts_with_media",
            )
        except Exception as exc:
            log(f"Fout bij ophalen bronfeed: {exc}")
            break

        feed = getattr(response, "feed", []) or []

        if not feed:
            break

        for item in feed:
            post = getattr(item, "post", None)

            if post is None:
                continue

            # Reposts uitsluiten.
            if getattr(item, "reason", None) is not None:
                continue

            record = getattr(post, "record", None)

            if record is None:
                continue

            # Alleen eigen originele bronposts; geen replies of quotes.
            if is_reply(record) or is_quote_post(record):
                continue

            author = getattr(post, "author", None)

            if author is None:
                continue

            if getattr(author, "did", None) != source_did:
                continue

            created_at = parse_datetime(
                getattr(record, "created_at", None)
            )

            if created_at is None or created_at > cutoff:
                continue

            embed = get_embed(record)

            if embed is None:
                continue

            source_mentions = source_mentions_redfox(
                record, redfox_did
            )

            # Foto's: elke afbeelding afzonderlijk publiceren.
            images = getattr(embed, "images", None)

            if images:
                for image_index, image_item in enumerate(images):
                    blob = getattr(image_item, "image", None)
                    cid = get_cid(blob)

                    if not cid:
                        continue

                    media_key = (
                        f"{post.uri}#image-{image_index}"
                    )

                    if media_key in state:
                        continue

                    candidates.append({
                        "key": media_key,
                        "uri": post.uri,
                        "cid": post.cid,
                        "source_did": source_did,
                        "created_at": created_at,
                        "kind": "image",
                        "blob_cid": cid,
                        "alt": getattr(image_item, "alt", "") or "",
                        "mentions_redfox": source_mentions,
                    })

                continue

            # Video's: alleen directe video-embeds.
            video_blob = getattr(embed, "video", None)

            if video_blob is not None:
                cid = get_cid(video_blob)

                if not cid:
                    continue

                media_key = f"{post.uri}#video"

                if media_key in state:
                    continue

                candidates.append({
                    "key": media_key,
                    "uri": post.uri,
                    "cid": post.cid,
                    "source_did": source_did,
                    "created_at": created_at,
                    "kind": "video",
                    "blob_cid": cid,
                    "alt": getattr(embed, "alt", "") or "",
                    "mentions_redfox": source_mentions,
                })

        cursor = getattr(response, "cursor", None)

        log(
            f"Pagina {page_number}/{MAX_PAGES}: "
            f"{len(candidates)} geschikte media gevonden."
        )

        if not cursor:
            break

    # Oudste geschikte media eerst.
    candidates.sort(key=lambda candidate: candidate["created_at"])

    return candidates


# ============================================================
# MEDIA PUBLICEREN
# ============================================================

def publish_media(client, candidate, redfox_did):
    kind = candidate["kind"]

    if kind == "image":
        raw_data = download_blob(
            candidate["source_did"],
            candidate["blob_cid"],
            MAX_IMAGE_BYTES * 30,
        )

        raw_data = resize_image(raw_data)
        uploaded = client.upload_blob(raw_data)

        embed = models.AppBskyEmbedImages.Main(
            images=[
                models.AppBskyEmbedImages.Image(
                    alt=candidate["alt"],
                    image=uploaded.blob,
                )
            ]
        )

    elif kind == "video":
        raw_data = download_blob(
            candidate["source_did"],
            candidate["blob_cid"],
            MAX_VIDEO_BYTES,
        )

        uploaded = client.upload_blob(raw_data)

        embed = models.AppBskyEmbedVideo.Main(
            video=uploaded.blob,
            alt=candidate["alt"],
        )

    else:
        raise ValueError(f"Onbekend mediatype: {kind}")

    # RedFox uitsluitend toevoegen als de bronpost RedFox noemt.
    if candidate["mentions_redfox"]:
        text, facets = create_redfox_facet(redfox_did)
        log("RedFox genoemd in bronpost: klikbare tag toevoegen.")
    else:
        text = ""
        facets = []
        log("RedFox niet genoemd in bronpost: geen RedFox-tag.")

    if DRY_RUN:
        log(
            f"DRY RUN: zou {kind} publiceren; "
            f"RedFox-tag={candidate['mentions_redfox']}."
        )
        return f"DRY_RUN:{candidate['key']}"

    response = client.send_post(
        text=text,
        facets=facets,
        embed=embed,
    )

    return response.uri


# ============================================================
# LAATSTE 3 EIGEN POSTS REBOOSTEN
# ============================================================

def reboost_own_posts(client, target_did):
    log("Eigen posts ophalen voor reboost.")

    try:
        response = client.get_author_feed(
            actor=TARGET_ACCOUNT,
            limit=100,
            filter="posts_with_media",
        )
    except Exception as exc:
        log(f"Eigen feed ophalen mislukt: {exc}")
        return

    eligible = []

    for item in getattr(response, "feed", []) or []:
        post = getattr(item, "post", None)

        if post is None:
            continue

        if getattr(item, "reason", None) is not None:
            continue

        author = getattr(post, "author", None)

        if author is None or getattr(author, "did", None) != target_did:
            continue

        record = getattr(post, "record", None)

        if record is None:
            continue

        if is_reply(record) or is_quote_post(record):
            continue

        eligible.append(post)

    # Nieuwste drie originele eigen posts selecteren.
    eligible.sort(
        key=lambda post: (
            parse_datetime(
                getattr(post.record, "created_at", None)
            ) or datetime.min.replace(tzinfo=timezone.utc)
        ),
        reverse=True,
    )

    selected = eligible[:OWN_REBOOST_COUNT]

    # Oudste van de geselecteerde drie eerst.
    selected.reverse()

    for post in selected:
        try:
            if DRY_RUN:
                log(f"DRY RUN: zou eigen post reboosten: {post.uri}")
                continue

            viewer = getattr(post, "viewer", None)
            existing_repost = getattr(viewer, "repost", None)

            if existing_repost:
                client.delete_repost(existing_repost)
                time.sleep(REBOOST_DELAY)

            client.repost(post.uri, post.cid)

            log(f"Eigen post opnieuw gerepost: {post.uri}")
            time.sleep(REBOOST_DELAY)

        except Exception as exc:
            log(f"Reboost mislukt voor {post.uri}: {exc}")


# ============================================================
# HOOFDPROGRAMMA
# ============================================================

def main():
    client = Client()

    log(f"Inloggen als {TARGET_ACCOUNT}.")

    client.login(
        TARGET_ACCOUNT,
        PASSWORD,
    )

    target_profile = client.get_profile(
        actor=TARGET_ACCOUNT
    )

    source_profile = client.get_profile(
        actor=SOURCE_ACCOUNT
    )

    redfox_profile = client.get_profile(
        actor=REDFOX_HANDLE
    )

    target_did = target_profile.did
    source_did = source_profile.did
    redfox_did = redfox_profile.did

    log(f"Bron: {SOURCE_ACCOUNT}")
    log(f"Doel: {TARGET_ACCOUNT}")
    log(f"Bron-DID: {source_did}")
    log(f"RedFox-DID opgehaald.")
    log(f"Dry run: {DRY_RUN}")

    state = load_state()

    reboost_own_posts(
        client,
        target_did,
    )

    candidates = collect_candidates(
        client,
        source_did,
        redfox_did,
        state,
    )

    log(f"{len(candidates)} geschikte media gevonden.")

    published_count = 0

    for candidate in candidates:
        if published_count >= MAX_POSTS:
            break

        try:
            log(
                f"Verwerken: {candidate['kind']} "
                f"uit {candidate['uri']}."
            )

            published_uri = publish_media(
                client,
                candidate,
                redfox_did,
            )

            # Publicatiegeschiedenis na iedere geslaagde publicatie
            # direct opslaan, zodat een volgende run kan hervatten.
            state[candidate["key"]] = published_uri
            save_state(state)

            published_count += 1

            log(
                f"Publicatie {published_count}/{MAX_POSTS} voltooid: "
                f"{published_uri}"
            )

            time.sleep(POST_DELAY)

        except Exception as exc:
            log(
                f"Media overgeslagen wegens fout: {exc}"
            )

    log(
        f"Klaar. Nieuwe publicaties: "
        f"{published_count}/{MAX_POSTS}."
    )


if __name__ == "__main__":
    main()