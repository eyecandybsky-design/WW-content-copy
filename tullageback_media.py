import os
import json
import time
import random
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from atproto import Client, models


SOURCE = os.getenv("SOURCE_ACCOUNT", "scazmo.bsky.social").lstrip("@")
TARGET = os.getenv("BSKY_USERNAME", "snarlup.bsky.social").lstrip("@")
PASSWORD = os.environ["BSKY_SNARLUP_PASSWORD"]

STATE = Path(os.getenv("STATE_FILE", "tullageback_media_state.json"))
MIN_AGE_DAYS = int(os.getenv("MIN_AGE_DAYS", "14"))
MAX_PAGES = int(os.getenv("MAX_PAGES", "30"))
MAX_POSTS = int(os.getenv("MAX_POSTS", "3"))

REDFOX_HANDLE = "redfoxofficial.bsky.social"
REDFOX_TEXT = f"🦊 @{REDFOX_HANDLE} 🦊"


def download(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "KnightTakesQueenMedia/1.0"}
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()


def get_redfox_text(client, record):
    original_text = getattr(record, "text", "") or ""
    original_facets = getattr(record, "facets", None) or []

    text_has_handle = REDFOX_HANDLE.lower() in original_text.lower()

    # Controleer ook of de originele post een klikbare mention bevat.
    has_mention_facets = any(
        any(
            "mention" in type(feature).__name__.lower()
            or "mention" in getattr(feature, "py_type", "").lower()
            for feature in getattr(facet, "features", [])
        )
        for facet in original_facets
    )

    if not text_has_handle and not has_mention_facets:
        return "", None

    try:
        profile = client.get_profile(actor=REDFOX_HANDLE)
        redfox_did = profile.did
    except Exception as exc:
        print(f"RedFox-profiel kon niet worden opgehaald: {exc}")
        return "", None

    mentioned_in_facets = any(
        getattr(feature, "did", None) == redfox_did
        for facet in original_facets
        for feature in getattr(facet, "features", [])
    )

    if not text_has_handle and not mentioned_in_facets:
        return "", None

    text = REDFOX_TEXT
    start = len("🦊 ".encode("utf-8"))
    end = start + len(f"@{REDFOX_HANDLE}".encode("utf-8"))

    facets = [
        models.AppBskyRichtextFacet.Main(
            index=models.AppBskyRichtextFacet.ByteSlice(
                byte_start=start,
                byte_end=end,
            ),
            features=[
                models.AppBskyRichtextFacet.Mention(did=redfox_did)
            ],
        )
    ]

    return text, facets


def collect_candidates(client, published):
    cutoff = datetime.now(timezone.utc) - timedelta(days=MIN_AGE_DAYS)
    candidates = {}
    cursor = None

    for _ in range(MAX_PAGES):
        response = client.app.bsky.feed.get_author_feed(
            {
                "actor": SOURCE,
                "limit": 100,
                "filter": "posts_with_media",
                "cursor": cursor,
            }
        )

        if not response.feed:
            break

        for item in response.feed:
            post = item.post
            record = post.record
            uri = post.uri

            if getattr(item, "reason", None):
                continue

            if not isinstance(record, models.AppBskyFeedPost.Record):
                continue

            if getattr(record, "reply", None):
                continue

            embed = getattr(record, "embed", None)

            if isinstance(embed, models.AppBskyEmbedImages.Main):
                media_items = [
                    ("image", index, image)
                    for index, image in enumerate(embed.images)
                ]
            elif isinstance(embed, models.AppBskyEmbedVideo.Main):
                media_items = [("video", 0, embed)]
            else:
                continue

            created_at = getattr(record, "created_at", None)
            if not created_at:
                continue

            try:
                post_date = datetime.fromisoformat(
                    created_at.replace("Z", "+00:00")
                )
            except (ValueError, AttributeError):
                continue

            if post_date > cutoff:
                continue

            for media_type, index, media in media_items:
                key = (
                    f"{uri}#image-{index}"
                    if media_type == "image"
                    else f"{uri}#video"
                )

                if key in published:
                    continue

                candidates[key] = {
                    "uri": uri,
                    "media": media,
                    "media_type": media_type,
                    "record": record,
                    "created_at": created_at,
                    "key": key,
                }

        cursor = getattr(response, "cursor", None)
        if not cursor:
            break

    return list(candidates.values())


def publish_media(client, candidate):
    uri = candidate["uri"]
    media = candidate["media"]
    media_type = candidate["media_type"]
    record = candidate["record"]

    did = uri.split("/")[2]
    text, facets = get_redfox_text(client, record)

    if media_type == "image":
        blob = media.image
        url = (
            "https://bsky.social/xrpc/com.atproto.sync.getBlob"
            f"?did={did}&cid={blob.ref.link}"
        )
        raw = download(url)

        if len(raw) > 1_000_000:
            raise ValueError("Afbeelding groter dan 1 MB.")

        uploaded = client.upload_blob(raw)

        new_embed = models.AppBskyEmbedImages.Main(
            images=[
                models.AppBskyEmbedImages.Image(
                    alt=getattr(media, "alt", "") or "",
                    image=uploaded.blob,
                )
            ]
        )

    elif media_type == "video":
        blob = media.video
        url = (
            "https://bsky.social/xrpc/com.atproto.sync.getBlob"
            f"?did={did}&cid={blob.ref.link}"
        )
        raw = download(url)

        if len(raw) > 100_000_000:
            raise ValueError("Video groter dan 100 MB.")

        uploaded = client.upload_blob(raw)

        new_embed = models.AppBskyEmbedVideo.Main(
            video=uploaded.blob,
            alt=getattr(media, "alt", "") or "",
        )

    else:
        raise ValueError(f"Onbekend mediatype: {media_type}")

    post_args = {
        "text": text,
        "embed": new_embed,
    }

    if facets:
        post_args["facets"] = facets

    return client.send_post(**post_args)


def main():
    print(f"Bronaccount: {SOURCE}")
    print(f"Doelaccount: {TARGET}")
    print(f"Minimum leeftijd: {MIN_AGE_DAYS} dagen")
    print(f"Maximum posts deze run: {MAX_POSTS}")

    client = Client()
    client.login(TARGET, PASSWORD)

    if STATE.exists():
        with STATE.open("r", encoding="utf-8") as file:
            state = json.load(file)
    else:
        state = {}

    published = state.get("published", {})

    candidates = collect_candidates(client, published)
    random.shuffle(candidates)

    print(f"Geschikte media gevonden: {len(candidates)}")

    successful = 0

    for candidate in candidates:
        if successful >= MAX_POSTS:
            break

        try:
            response = publish_media(client, candidate)

            published[candidate["key"]] = {
                "published_at": datetime.now(timezone.utc).isoformat(),
                "uri": getattr(response, "uri", ""),
            }

            successful += 1
            print(
                f"Media gepubliceerd: {candidate['key']} "
                f"({successful}/{MAX_POSTS})"
            )

            time.sleep(2)

        except Exception as exc:
            print(f"Media overgeslagen: {candidate['key']} — {exc}")

    state["published"] = published
    state["last_run"] = datetime.now(timezone.utc).isoformat()

    STATE.parent.mkdir(parents=True, exist_ok=True)

    with STATE.open("w", encoding="utf-8") as file:
        json.dump(state, file, indent=2, ensure_ascii=False)

    print(f"Klaar. Aantal gepubliceerd: {successful}")


if __name__ == "__main__":
    main()