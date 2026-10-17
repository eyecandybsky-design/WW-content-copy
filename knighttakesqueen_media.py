
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

# Maximaal 30 pagina's van elk maximaal 100 berichten
MAX_PAGES = int(
    os.getenv("MAX_PAGES", "30")
)

# Maximaal 3 mediaposts per run
MAX_POSTS = int(
    os.getenv("MAX_POSTS", "3")
)

REDFOX_HANDLE = "redfoxofficial.bsky.social"


# ==========================================
# MEDIA DOWNLOADEN
# ==========================================

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


def download_blob(did, blob):

    url = (
        "https://bsky.social/xrpc/"
        "com.atproto.sync.getBlob"
        f"?did={did}"
        f"&cid={blob.ref.link}"
    )

    return download(url)


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
        json.dumps(
            state,
            indent=2
        ),
        encoding="utf-8"
    )


# ==========================================
# REDFOX-MENTION
# Alleen toevoegen als de bronpost
# @redfoxofficial.bsky.social vermeldt
# ==========================================

def get_redfox_text(client, record):

    original_text = getattr(
        record,
        "text",
        ""
    ) or ""

    if REDFOX_HANDLE.lower() not in original_text.lower():
        return "", None

    try:

        profile = client.get_profile(
            actor=REDFOX_HANDLE
        )

        text = f"👉🏻 @{REDFOX_HANDLE}"

        prefix = "👉🏻 "
        byte_start = len(prefix.encode("utf-8"))
        byte_end = len(text.encode("utf-8"))

        facet = models.AppBskyRichtextFacet.Main(
            index=models.AppBskyRichtextFacet.ByteSlice(
                byte_start=byte_start,
                byte_end=byte_end
            ),
            features=[
                models.AppBskyRichtextFacet.Mention(
                    did=profile.did
                )
            ]
        )

        return text, [facet]

    except Exception as exc:

        print(
            f"RedFox-mention kon niet worden gemaakt: {exc}"
        )

        return "", None


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

    print(f"Bronaccount: {SOURCE}")
    print(f"Zoeklimiet: {MAX_PAGES} pagina's")
    print(f"Minimumleeftijd: {MIN_AGE_DAYS} dagen")

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

        for item in result.feed:

            post = item.post
            record = post.record

            if not isinstance(
                record,
                models.AppBskyFeedPost.Record
            ):
                continue

            # Geen reposts
            if getattr(item, "reason", None) is not None:
                continue

            # Geen replies
            if getattr(record, "reply", None) is not None:
                continue

            embed = getattr(record, "embed", None)

            # Alleen originele foto's en video's
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

            # Alleen media van minimaal 30 dagen oud
            if created > cutoff:
                continue

            uri = post.uri

            if isinstance(
                embed,
                models.AppBskyEmbedImages.Main
            ):

                # Elke foto uit een carrousel apart
                for index, image in enumerate(embed.images):

                    media_key = f"{uri}#image-{index}"

                    if media_key in published:
                        continue

                    candidates.append({
                        "created": created,
                        "uri": uri,
                        "media_key": media_key,
                        "type": "image",
                        "media": image,
                        "record": record
                    })

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
                    "media_key": media_key,
                    "type": "video",
                    "media": embed,
                    "record": record
                })

        cursor = result.cursor

        if not cursor:
            print("Einde van de beschikbare bronfeed bereikt.")
            break

    return candidates


# ==========================================
# MEDIA PUBLICEREN
# ==========================================

def publish_media(client, candidate):

    uri = candidate["uri"]
    media_key = candidate["media_key"]
    media_type = candidate["type"]
    media = candidate["media"]
    record = candidate["record"]

    did = uri.split("/")[2]

    print(f"Geselecteerd: {media_key}")
    print(f"Mediatype: {media_type}")

    # RedFox-tag uitsluitend als de bronpost
    # @redfoxofficial.bsky.social bevat.
    text, facets = get_redfox_text(
        client,
        record
    )

    if media_type == "image":

        blob = media.image
        raw = download_blob(did, blob)

        if len(raw) > 1_000_000:
            raise ValueError(
                "Afbeelding groter dan 1 MB."
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
        raw = download_blob(did, blob)

        if len(raw) > 100_000_000:
            raise ValueError(
                "Video groter dan 100 MB."
            )

        uploaded = client.upload_blob(raw).blob

        video_args = {
            "video": uploaded,
            "alt": getattr(media, "alt", None)
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

    # Normaal geen caption.
    # Alleen de klikbare RedFox-tag wanneer vereist.
    post_args = {
        "text": text,
        "embed": new_embed
    }

    if facets:
        post_args["facets"] = facets

    response = client.send_post(**post_args)

    print(f"Gepubliceerd: {response.uri}")

    return response.uri


# ==========================================
# HOOFDPROGRAMMA
# ==========================================

def main():

    client = Client()

    client.login(
        TARGET,
        PASSWORD
    )

    print(f"Ingelogd als: {TARGET}")

    state = load_state()
    published = state["published"]

    candidates = collect_candidates(
        client,
        published
    )

    if not candidates:

        print(
            "Geen nieuwe geschikte media gevonden."
        )

        return

    # Willekeurig kiezen uit alle gevonden media
    random.shuffle(candidates)

    print(
        f"Totaal geschikte media gevonden: {len(candidates)}"
    )

    successful = 0
    attempted = 0

    for candidate in candidates:

        if successful >= MAX_POSTS:
            break

        # Bij fouten proberen we een volgende kandidaat.
        attempted += 1

        try:

            response_uri = publish_media(
                client,
                candidate
            )

            published[candidate["media_key"]] = response_uri
            save_state(state)

            successful += 1

        except Exception as exc:

            print(
                f"Media overgeslagen na fout: {exc}"
            )

    print(f"Pogingen: {attempted}")
    print(f"Succesvol gepubliceerd: {successful}")
    print("KnightTakesQueen-run afgerond.")


# ==========================================
# START
# ==========================================

if __name__ == "__main__":
    main()
