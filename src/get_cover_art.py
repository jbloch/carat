"""
Cover art retrieval component of Carat.

A freestanding tool and small library to find high-quality album artwork on the web. It currently uses Apple/iTunes
and MusicBrainz as sources, but this does not affect the contract between this module and its users.
"""

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

import sys
from io import BytesIO
from pathlib import Path

import requests
from PIL import Image

__all__ = ['download_cover_art']

import logger
from music_brainier import generate_mb_art_urls

from util import normalize_for_fuzzy_comparison

MAX_APPLE_ALBUM_COVERS_TO_SEARCH = 5  # Maximum number of Apple album covers to examine before switching to CAA
MAX_FILE_SIZE = 15 * 1024 * 1024      # 15 MB Cap on image file size
MIN_DIMENSION = 1000                  # Minimum pixels on a side for 'High Res' image

# We search multiple albums on Apple Music to give us a buffer against Apple's fuzzy search
def download_cover_art(artist: str, album: str, target_dir: Path, mbid: str | None = None) -> None:
    """
    Downloads the "best" available cover art for the specified release (or does nothing if no acceptable art found).
    """
    # Apple's API is still our first choice for high-res art, but MB acts as the precise fallback
    image_url = get_itunes_art_url(artist, album) or get_mb_digital_art_url(artist, album, mbid)

    if image_url:
        logger.emit(f"[*] Downloading: {image_url}")
        img_data = requests.get(image_url, timeout=15).content
        img = Image.open(BytesIO(img_data))

        # Strip transparency layer if present, as it would cause Pillow to crash on jpeg conversion
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # Save as high-quality JPEG
        save_path = target_dir / "cover.jpg"
        img.save(save_path, "JPEG", quality=95)
        logger.emit(f"[+] Success: Saved {img.width}x{img.height} cover to {save_path}")
    else:
        logger.emit("[!] No suitable art found.")

def get_mb_digital_art_url(artist: str, album: str, mbid: str | None = None) -> str | None:
    """ Consumes candidate URLs from MusicBrainz and returns the first valid image. """
    for url in generate_mb_art_urls(artist, album, mbid):
        valid, w, h = is_valid_image(url)
        if valid:
            return url
        else:
            logger.emit(f"  [-] Skipped invalid/undersized CAA art at: {url}")

    return None


def is_valid_image(url: str) -> tuple[bool, int, int]:
    """ Returns true and the image dimensions if the image at the given URL has an appropriate shape for cover art. """
    try:
        # 1. Limit size check to skip fragile HEAD requests for CAA and iTunes CDNs
        is_thumbnail = "coverartarchive.org" in url or "mzstatic.com" in url or "itunes.apple.com" in url

        if not is_thumbnail:
            head = requests.head(url, allow_redirects=True, timeout=5)
            size = int(head.headers.get('Content-Length', 0))
            if size > MAX_FILE_SIZE:
                return False, 0, 0

        # 2. Add custom User-Agent to prevent Archive.org from dropping the connection (Shouldn't be necessary)
        headers = {"User-Agent": "Carat ( josh@bloch.us )"}

        # 3. Load fully into BytesIO. PIL requires seek() for Progressive JPEGs, which resp.raw lacks.
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()

        img = Image.open(BytesIO(resp.content))
        w, h = img.size

        aspect_ratio = w / h
        if 0.95 < aspect_ratio < 1.05 and w >= MIN_DIMENSION:  #Square-ish
            return True, w, h
    except (requests.RequestException, OSError, ValueError):
        pass
    return False, 0, 0


def get_itunes_art_url(artist: str, album: str) -> str | None:
    """ Returns the URL of acceptable album art from Apple/iTunes, or None if not found. """
    url = "https://itunes.apple.com/search"
    params = {"term": f"{artist} {album}", "entity": "album", "limit": MAX_APPLE_ALBUM_COVERS_TO_SEARCH}
    try:
        r = requests.get(url, params=params, timeout=10).json()
        results = r.get('results', [])

        target_album = normalize_for_fuzzy_comparison(album)
        target_artist = normalize_for_fuzzy_comparison(artist)

        for res in results:
            retrieved_album = normalize_for_fuzzy_comparison(res.get('collectionName', ''))
            retrieved_artist = normalize_for_fuzzy_comparison(res.get('artistName', ''))

            # Fuzzy Match: check if the target's normalized string is inside the candidate's normalized string
            # The fuzzy match is necessary because Apple and MusicBrainz can disagree on names
            if target_album in retrieved_album and target_artist in retrieved_artist:
                hq_url = res['artworkUrl100'].replace("100x100bb.jpg", "1200x1200bb.jpg")
                valid, w, h = is_valid_image(hq_url)
                if valid:
                    return hq_url
            else:
                # Log the raw strings so we can see why it failed if it does
                logger.emit(f"[*] Skipping iTunes mismatch: {res.get('artistName')} - {res.get('collectionName')}")
    except (requests.RequestException, KeyError, ValueError):
        pass
    return None


def main():
    """ Simple command line tool to get cover art for the specified release """
    if len(sys.argv) < 4:
        logger.emit('Usage: python get_cover_art.py "Artist" "Album" "/Library/Root"')
        sys.exit(1)

    artist, album, library_root = sys.argv[1], sys.argv[2], sys.argv[3]
    target_dir = Path(library_root) / artist / album
    target_dir.mkdir(parents=True, exist_ok=True)

    download_cover_art(artist, album, target_dir)


if __name__ == "__main__":
    main()
