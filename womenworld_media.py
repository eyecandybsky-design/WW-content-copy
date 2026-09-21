import os, json, time, mimetypes, urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from atproto import Client, models

SOURCE = os.getenv('SOURCE_ACCOUNT', 'tullageback.bsky.social').lstrip('@')
TARGET = os.getenv('BSKY_USERNAME', 'womenworld.bsky.social').lstrip('@')
PASSWORD = os.environ['BSKY_PASSWORD']
STATE = Path(os.getenv('STATE_FILE', 'womenworld_state.json'))
MIN_AGE_DAYS = int(os.getenv('MIN_AGE_DAYS', '30'))
MAX_PAGES = int(os.getenv('MAX_PAGES', '30'))
MAX_POSTS = int(os.getenv('MAX_POSTS', '1'))
DRY_RUN = os.getenv('DRY_RUN', 'false').lower() == 'true'

def download(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'WomenWorldMediaTest/1.0'})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return resp.read()

def main():
    client = Client()
    client.login(TARGET, PASSWORD)
    state = json.loads(STATE.read_text()) if STATE.exists() else {'published': {}}
    published = state.setdefault('published', {})
    cutoff = datetime.now(timezone.utc) - timedelta(days=MIN_AGE_DAYS)
    cursor = None
    candidates = []
    for page in range(MAX_PAGES):
        result = client.get_author_feed(actor=SOURCE, limit=100, cursor=cursor, filter='posts_with_media')
        for item in result.feed:
            post = item.post
            record = post.record
            if getattr(item, 'reason', None) is not None or getattr(record, 'reply', None) is not None:
                continue
            if not isinstance(record, models.AppBskyFeedPost.Record):
                continue
            embed = getattr(record, 'embed', None)
            if not isinstance(embed, (models.AppBskyEmbedImages.Main, models.AppBskyEmbedVideo.Main)):
                continue
            created = datetime.fromisoformat(record.created_at.replace('Z', '+00:00'))
            if created > cutoff or post.uri in published:
                continue
            candidates.append((created, post.uri, embed))
        cursor = result.cursor
        if not cursor:
            break
    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        print('Geen geschikte, nog niet gepubliceerde mediapost gevonden. Verhoog MAX_PAGES indien nodig.')
        return
    count = 0
    for created, uri, embed in candidates:
        if count >= MAX_POSTS:
            break
        print(f'Geselecteerd: {uri} ({created.isoformat()})')
        if DRY_RUN:
            continue
        try:
            if isinstance(embed, models.AppBskyEmbedImages.Main):
                images = []
                for image in embed.images:
                    blob = image.image
                    # Retrieve the original blob, not a thumbnail or transcoded CDN image.
                    url = f'https://bsky.social/xrpc/com.atproto.sync.getBlob?did={uri.split("/")[2]}&cid={blob.ref.link}'
                    raw = download(url)
                    if len(raw) > 1_000_000:
                        raise ValueError('Afbeelding groter dan 1 MB; sla deze post over (geen automatische compressie).')
                    uploaded = client.upload_blob(raw).blob
                    images.append(models.AppBskyEmbedImages.Image(alt='', image=uploaded, aspect_ratio=image.aspect_ratio))
                new_embed = models.AppBskyEmbedImages.Main(images=images)
            else:
                blob = embed.video
                url = f'https://bsky.social/xrpc/com.atproto.sync.getBlob?did={uri.split("/")[2]}&cid={blob.ref.link}'
                raw = download(url)
                if len(raw) > 100_000_000:
                    raise ValueError('Video groter dan 100 MB; sla deze post over.')
                uploaded = client.upload_blob(raw).blob
                new_embed = models.AppBskyEmbedVideo.Main(video=uploaded, aspect_ratio=embed.aspect_ratio, alt=None)
            response = client.send_post(text='', embed=new_embed)
            published[uri] = response.uri
            STATE.write_text(json.dumps(state, indent=2))
            count += 1
            print(f'Gepubliceerd: {response.uri}')
        except Exception as exc:
            print(f'Post overgeslagen na fout: {exc}')
    print(f'Gepubliceerd in deze run: {count}')

if __name__ == '__main__':
    main()
