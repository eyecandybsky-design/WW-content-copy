
import os
import json
import urllib.request

from datetime import datetime, timedelta, timezone
from pathlib import Path

from atproto import Client, models


# ==========================================
# INSTELLINGEN
# ==========================================

SOURCE = os.getenv(
    'SOURCE_ACCOUNT',
    'tullageback.bsky.social'
).lstrip('@')

TARGET = os.getenv(
    'BSKY_USERNAME',
    'womenworld.bsky.social'
).lstrip('@')

PASSWORD = os.environ['BSKY_PASSWORD']

STATE = Path(
    os.getenv('STATE_FILE', 'womenworld_state.json')
)

MIN_AGE_DAYS = int(
    os.getenv('MIN_AGE_DAYS', '30')
)

MAX_PAGES = int(
    os.getenv('MAX_PAGES', '30')
)

MAX_POSTS = int(
    os.getenv('MAX_POSTS', '1')
)

DRY_RUN = (
    os.getenv('DRY_RUN', 'false').lower() == 'true'
)


# ==========================================
# MEDIA DOWNLOADEN
# ==========================================

def download(url):

    req = urllib.request.Request(
        url,
        headers={
            'User-Agent': 'WomenWorldMediaTest/1.0'
        }
    )

    with urllib.request.urlopen(
        req,
        timeout=90
    ) as resp:

        return resp.read()


# ==========================================
# PUBLICATIEGESCHIEDENIS
# ==========================================

def load_state():

    if STATE.exists():

        state = json.loads(
            STATE.read_text()
        )

    else:

        state = {'published': {}}

    state.setdefault('published', {})

    return state


def save_state(state):

    STATE.write_text(
        json.dumps(
            state,
            indent=2
        )
    )


# ==========================================
# MEDIA OPHALEN
# ==========================================

def main():

    client = Client()

    client.login(
        TARGET,
        PASSWORD
    )

    state = load_state()

    published = state['published']

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=MIN_AGE_DAYS)
    )

    cursor = None

    candidates = []

    # ======================================
    # BRONACCOUNT DOORZOEKEN
    # ======================================

    for page in range(MAX_PAGES):

        result = client.get_author_feed(
            actor=SOURCE,
            limit=100,
            cursor=cursor,
            filter='posts_with_media'
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
                'reason',
                None
            ) is not None:
                continue

            # Geen replies
            if getattr(
                record,
                'reply',
                None
            ) is not None:
                continue

            embed = getattr(
                record,
                'embed',
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

            created = datetime.fromisoformat(
                record.created_at.replace(
                    'Z',
                    '+00:00'
                )
            )

            # Alleen posts van minimaal 30 dagen oud
            if created > cutoff:
                continue

            uri = post.uri

            # ==================================
            # OUDE PUBLICATIEGESCHIEDENIS
            # ==================================

            # Posts die door de vorige versie
            # al volledig zijn gepubliceerd,
            # worden niet opnieuw verwerkt.

            if uri in published:
                continue

            # ==================================
            # FOTO'S AFZONDERLIJK TOEVOEGEN
            # ==================================

            if isinstance(
                embed,
                models.AppBskyEmbedImages.Main
            ):

                for index, image in enumerate(
                    embed.images
                ):

                    # Unieke sleutel per foto
                    media_key = f'{uri}#image-{index}'

                    # Foto al gepubliceerd?
                    if media_key in published:
                        continue

                    candidates.append(
                        (
                            created,
                            uri,
                            media_key,
                            'image',
                            image
                        )
                    )

            # ==================================
            # VIDEO TOEVOEGEN
            # ==================================

            elif isinstance(
                embed,
                models.AppBskyEmbedVideo.Main
            ):

                media_key = f'{uri}#video'

                if media_key in published:
                    continue

                candidates.append(
                    (
                        created,
                        uri,
                        media_key,
                        'video',
                        embed
                    )
                )

        cursor = result.cursor

        if not cursor:
            break

    # ======================================
    # NIEUWSTE GESCHIKTE MEDIA EERST
    # ======================================

    candidates.sort(
        key=lambda item: item[0],
        reverse=True
    )

    if not candidates:

        print(
            'Geen nieuwe geschikte media gevonden.'
        )

        return

    count = 0

    # ======================================
    # MEDIA PUBLICEREN
    # ======================================

    for (
        created,
        uri,
        media_key,
        media_type,
        media
    ) in candidates:

        if count >= MAX_POSTS:
            break

        print(
            f'Geselecteerd: {media_key}'
        )

        if DRY_RUN:

            print(
                'DRY_RUN: media niet gepubliceerd.'
            )

            count += 1

            continue

        try:

            did = uri.split('/')[2]

            # ==================================
            # ÉÉN FOTO PUBLICEREN
            # ==================================

            if media_type == 'image':

                blob = media.image

                url = (
                    'https://bsky.social/xrpc/'
                    'com.atproto.sync.getBlob'
                    f'?did={did}'
                    f'&cid={blob.ref.link}'
                )

                raw = download(url)

                if len(raw) > 1_000_000:

                    raise ValueError(
                        'Afbeelding groter dan 1 MB.'
                    )

                uploaded = client.upload_blob(
                    raw
                ).blob

                # Slechts één afbeelding per post
                new_image = (
                    models.AppBskyEmbedImages.Image(
                        alt='',
                        image=uploaded,
                        aspect_ratio=media.aspect_ratio
                    )
                )

                new_embed = (
                    models.AppBskyEmbedImages.Main(
                        images=[new_image]
                    )
                )

            # ==================================
            # ÉÉN VIDEO PUBLICEREN
            # ==================================

            else:

                blob = media.video

                url = (
                    'https://bsky.social/xrpc/'
                    'com.atproto.sync.getBlob'
                    f'?did={did}'
                    f'&cid={blob.ref.link}'
                )

                raw = download(url)

                if len(raw) > 100_000_000:

                    raise ValueError(
                        'Video groter dan 100 MB.'
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
            # PUBLICEREN ZONDER CAPTION
            # ==================================

            response = client.send_post(
                text='',
                embed=new_embed
            )

            # ==================================
            # MEDIA ALS GEPUBLICEERD OPSLAAN
            # ==================================

            published[media_key] = response.uri

            save_state(state)

            count += 1

            print(
                f'Gepubliceerd: {response.uri}'
            )

            print(
                f'Mediatype: {media_type}'
            )

        except Exception as exc:

            print(
                f'Media overgeslagen na fout: {exc}'
            )

    print(
        f'Gepubliceerd in deze run: {count}'
    )


if __name__ == '__main__':

    main()
