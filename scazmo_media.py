
import os
import re
import json
import time
import urllib.request

from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

from atproto import Client, models


# ==========================================
# INSTELLINGEN
# ==========================================

SOURCE = os.getenv(
    "SOURCE_ACCOUNT",
    "tullageback.bsky.social"
).lstrip("@")

TARGET = os.getenv(
    "BSKY_USERNAME",
    "scazmo.bsky.social"
).lstrip("@")

PASSWORD = os.environ["BSKY_PASSWORD"]

STATE = Path(
    os.getenv("STATE_FILE", "scazmo_state.json")
)

MIN_AGE_DAYS = int(
    os.getenv("MIN_AGE_DAYS", "30")
)

MAX_PAGES = int(
    os.getenv("MAX_PAGES", "30")
)

MAX_POSTS = int(
    os.getenv("MAX_POSTS", "1")
)

DRY_RUN = (
    os.getenv("DRY_RUN", "false").lower() == "true"
)

# Reboost

OWN_REBOOST_COUNT = 3
REBOOST_DELAY = 2

# RedFox

REDFOX_HANDLE = "redfoxofficial.bsky.social"

REDFOX_TEXT = "❣️ @" + REDFOX_HANDLE

REDFOX_PATTERN = re.compile(
    r"(?<![\w@])@redfoxofficial\.bsky\.social"
    r"(?![\w.-])",
    re.IGNORECASE
)


# ==========================================
# DOWNLOAD
# ==========================================

def download(url):

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "ScazmoMedia/1.0"
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
# REDFOX MENTION
# ==========================================

def make_caption(client, original_text):

    original_text = original_text or ""

    # Alleen de exacte RedFox-vermelding
    if not REDFOX_PATTERN.search(original_text):

        return "", None

    print(
        "RedFox gevonden in originele bronpost."
    )

    # Account omzetten naar DID
    profile = client.get_profile(
        actor=REDFOX_HANDLE
    )

    redfox_did = profile.did

    # Caption
    caption = REDFOX_TEXT

    # Bluesky gebruikt UTF-8 byteposities
    mention = "@" + REDFOX_HANDLE

    start = caption.encode("utf-8").find(
        mention.encode("utf-8")
    )

    end = start + len(
        mention.encode("utf-8")
    )

    # Echte aanklikbare mention
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

    print(
        f"Caption: {caption}"
    )

    return caption, [facet]


# ==========================================
# REBOOST LAATSTE 3 EIGEN POSTS
# ==========================================

def reboost_own_posts(client):

    print(
        "Start reboost laatste 3 eigen posts..."
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
        if getattr(
            item,
            "reason",
            None
        ) is not None:
            continue

        # Alleen eigen posts
        if post.author.did != own_did:
            continue

        if not isinstance(
            record,
            models.AppBskyFeedPost.Record
        ):
            continue

        # Geen replies
        if getattr(
            record,
            "reply",
            None
        ) is not None:
            continue

        # Geen quote-posts
        embed = getattr(
            record,
            "embed",
            None
        )

        if isinstance(
            embed,
            (
                models.AppBskyEmbedRecord.Main,
                models.AppBskyEmbedRecordWithMedia.Main
            )
        ):
            continue

        own_posts.append(post)

        if len(own_posts) >= OWN_REBOOST_COUNT:
            break

    # Oudste eerst
    own_posts.reverse()

    for post in own_posts:

        try:

            uri = post.uri
            cid = post.cid

            print(
                f"Reboost: {uri}"
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

            # Eerst unrepost
            if repost_uri:

                client.delete_repost(
                    repost_uri
                )

                print(
                    "Unrepost uitgevoerd."
                )

                time.sleep(
                    REBOOST_DELAY
                )

            # Daarna repost
            client.repost(
                uri,
                cid
            )

            print(
                "Repost uitgevoerd."
            )

            time.sleep(
                REBOOST_DELAY
            )

        except Exception as exc:

            print(
                f"Reboost mislukt: {exc}"
            )

    print(
        "Reboost afgerond."
    )


# ==========================================
# MEDIA PUBLICEREN
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

    # Eerst eigen posts reboosten
    reboost_own_posts(client)

    # Publicatiegeschiedenis
    state = load_state()

    published = state["published"]

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=MIN_AGE_DAYS)
    )

    cursor = None

    candidates = []

    print(
        f"Bronaccount: {SOURCE}"
    )

    # ======================================
    # BRONACCOUNT DOORZOEKEN
    # ======================================

    for page in range(MAX_PAGES):

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
            if getattr(
                item,
                "reason",
                None
            ) is not None:
                continue

            # Geen replies
            if getattr(
                record,
                "reply",
                None
            ) is not None:
                continue

            embed = getattr(
                record,
                "embed",
                None
            )

            # Alleen originele foto- en videoposts
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

            # Minimaal 30 dagen oud
            if created > cutoff:
                continue

            uri = post.uri

            # Bestaande publicatiegeschiedenis
            if uri in published:
                continue

            # Originele tekst bewaren
            original_text = getattr(
                record,
                "text",
                ""
            ) or ""

            # ==================================
            # FOTO'S AFZONDERLIJK
            # ==================================

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

                    candidates.append(
                        (
                            created,
                            uri,
                            media_key,
                            "image",
                            image,
                            original_text
                        )
                    )

            # ==================================
            # VIDEO
            # ==================================

            elif isinstance(
                embed,
                models.AppBskyEmbedVideo.Main
            ):

                media_key = (
                    f"{uri}#video"
                )

                if media_key in published:
                    continue

                candidates.append(
                    (
                        created,
                        uri,
                        media_key,
                        "video",
                        embed,
                        original_text
                    )
                )

        cursor = result.cursor

        if not cursor:
            break

    # Nieuwste geschikte media eerst
    candidates.sort(
        key=lambda item: item[0],
        reverse=True
    )

    if not candidates:

        print(
            "Geen nieuwe geschikte media gevonden."
        )

        return

    count = 0

    # ======================================
    # MEDIA VERWERKEN
    # ======================================

    for (
        created,
        uri,
        media_key,
        media_type,
        media,
        original_text
    ) in candidates:

        if count >= MAX_POSTS:
            break

        print(
            f"Geselecteerd: {media_key}"
        )

        if DRY_RUN:

            print(
                "DRY_RUN: media niet gepubliceerd."
            )

            count += 1

            continue

        try:

            # ==================================
            # FOTO
            # ==================================

            if media_type == "image":

                blob = media.image

                raw = download(
                    blob_url(uri, blob)
                )

                if len(raw) > 1_000_000:

                    raise ValueError(
                        "Afbeelding groter dan 1 MB."
                    )

                uploaded = client.upload_blob(
                    raw
                ).blob

                new_image = (
                    models.AppBskyEmbedImages.Image(
                        alt="",
                        image=uploaded,
                        aspect_ratio=media.aspect_ratio
                    )
                )

                # Eén foto per post
                new_embed = (
                    models.AppBskyEmbedImages.Main(
                        images=[new_image]
                    )
                )

            # ==================================
            # VIDEO
            # ==================================

            else:

                blob = media.video

                raw = download(
                    blob_url(uri, blob)
                )

                if len(raw) > 100_000_000:

                    raise ValueError(
                        "Video groter dan 100 MB."
                    )

                uploaded = client.upload_blob(
                    raw
                ).blob

                new_embed = (
                    models.AppBskyEmbedVideo.Main(
                        video=uploaded,
                        aspect_ratio=media.aspect_ratio,
                        alt=None
                    )
                )

            # ==================================
            # CAPTION EN REDFOX-MENTION
            # ==================================

            caption, facets = make_caption(
                client,
                original_text
            )

            # ==================================
            # PUBLICEREN
            # ==================================

            response = client.send_post(
                text=caption,
                embed=new_embed,
                facets=facets
            )

            # ==================================
            # PUBLICATIEGESCHIEDENIS
            # ==================================

            published[media_key] = response.uri

            save_state(state)

            count += 1

            print(
                f"Gepubliceerd: {response.uri}"
            )

            print(
                f"Mediatype: {media_type}"
            )

        except Exception as exc:

            print(
                f"Media overgeslagen na fout: {exc}"
            )

    print(
        f"Gepubliceerd in deze run: {count}"
    )


# ==========================================
# START
# ==========================================

if __name__ == "__main__":

    main()
