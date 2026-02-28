"""
Cover art retrieval component of Carat.

A freestanding tool and small library to find high-quality album artwork on the web. It currently uses Apple/iTunes
and MusicBrainz as sources, but this does not affect the contract between this module and its users.
"""

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

__author__ = "Joshua Bloch"
__copyright__ = "Copyright 2026, Joshua Bloch"
__license__ = "MIT"
__version__ = "1.0B"

import re
import sys
from io import BytesIO
from pathlib import Path

import musicbrainzngs as mb
import requests
import unicodedata
from PIL import Image

import logger

__all__ = ['download_cover_art']

# We search multiple albums on Apple Music to give us a buffer against Apple's fuzzy search
MAX_APPLE_ALBUM_COVERS_TO_SEARCH = 5

mb.set_useragent("CoverArtRetrievalTool", "0.1", "josh@bloch.us")

MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB Cap
MIN_DIMENSION = 1000  # Minimum pixels for 'High Res'


def is_valid_image(url: str) -> tuple[bool, int, int]:
    """ Returns true and the image dimensions if the image at the given URL has an appropriate shape for cover art. """
    try:
        # If it's a known CAA thumbnail, we trust the size and skip the HEAD request
        is_thumbnail = "/thumbnails/" in url or "itunes.apple.com" in url

        if not is_thumbnail:
            head = requests.head(url, allow_redirects=True, timeout=5)
            size = int(head.headers.get('Content-Length', 0))
            if size > MAX_FILE_SIZE:
                return False, 0, 0

        # We use stream=True to check the Aspect Ratio/Dimensions because we only need the first few KB of the header.
        with requests.get(url, stream=True, timeout=10) as resp:
            img = Image.open(resp.raw)
            w, h = img.size

        # Validation logic...
        aspect_ratio = w / h
        if 0.95 < aspect_ratio < 1.05 and w >= MIN_DIMENSION:  #Square-ish
            return True, w, h
    except (requests.RequestException, OSError, ValueError):
        pass
    return False, 0, 0


def get_mb_digital_art_url(artist: str, album: str) -> str | None:
    """ Returns the URL of acceptable album art from MusicBrainz, or None if not found. """
    logger.emit(f"[*] Searching MusicBrainz for {artist} - {album}...")
    image_url = None
    try:
        release_groups = mb.search_release_groups(artist=artist, releasegroup=album)
        if release_groups['release-group-count'] == 0: return None

        rg_id = release_groups['release-group-list'][0]['id']
        releases = mb.get_release_group_by_id(rg_id, includes=["releases"])['release-group']['release-list']

        # Prioritize 'Digital' releases (which have digital-native cover art) over other official releases
        digital = [r for r in releases if r.get('status') == 'Official' and r.get('packaging') is None]
        image_url = get_mb_art_url_from_releases(digital)
        if not image_url:
            official = [r for r in releases if r.get('status') == 'Official']
            image_url = get_mb_art_url_from_releases(official)
    except (mb.MusicBrainzError, KeyError, IndexError):
        pass
    return image_url


def get_mb_art_url_from_releases(releases)-> str | None:
    """ Returns the URL of the best candidate from the given MB releases, or None if none appear satisfactory. """
    # Sort releases by date desc (ensure date is treated as a string)
    releases.sort(key=lambda x: str(x.get('date', '0000')), reverse=True)
    for r in releases:
        mb_id = r['id']
        # IMPORTANT: Perform a direct lookup for the release to get fresh CAA status
        # The release data inside a release-group object is often incomplete.
        try:
            full_release = mb.get_release_by_id(mb_id)
            status = full_release['release'].get('cover-art-archive', {})

            if status.get('artwork') == 'true':
                logger.emit(f"  [+] Found CAA art for: {mb_id}")
                caa_data = requests.get(f"https://coverartarchive.org/release/{mb_id}").json()
                for img_entry in caa_data['images']:
                    if img_entry['front']:
                        url = img_entry['thumbnails'].get('1200') or img_entry['image']
                        valid, w, h = is_valid_image(url)
                        if valid:
                            return url
                        else:
                            logger.emit(f"Invalid CAA art: '{img_entry}'")
        except Exception as e:
            logger.emit(f"  [!] Error checking release {mb_id}: {e}")
            continue
    return None


def _normalize_for_fuzzy_comparison(s: str) -> str:
    """
    Robust normalization for fuzzy matching.
        1. Normalizes Unicode (NFKD) to decompose diacritics and fix full-width chars.
        2. Strips combining diacritics (e.g., the dots on 'ö').
        3. Lowercases and expands '&'.
        4. Keeps ALL alphanumeric characters (including Cyrillic, Kanji, etc.)
        5. Collapses whitespace.
    """
    if not s: return ""  #  Shouldn't be necessary, but acceptably defensive under the circumstances

    # 1. Decompose (turn 'ö' into 'o' + '¨', and 'Ｆ' into 'F')
    s = unicodedata.normalize('NFKD', s)

    # 2. Strip diacritics
    s = "".join([c for c in s if not unicodedata.combining(c)])

    s = s.lower().replace("&", "and")

    # 3. Filter: Keep only letters/numbers and spaces works for any script, e.g., Greek, Cyrillic, CJK
    s = "".join([c if c.isalnum() else " " for c in s])

    # 4. Collapse multiple spaces down to one
    return re.sub(r'\s+', ' ', s).strip()


def get_itunes_art_url(artist: str, album: str) -> str | None:
    """ Returns the URL of acceptable album art from Apple/iTunes, or None if not found. """
    url = "https://itunes.apple.com/search"
    params = {"term": f"{artist} {album}", "entity": "album", "limit": MAX_APPLE_ALBUM_COVERS_TO_SEARCH}
    try:
        r = requests.get(url, params=params).json()
        results = r.get('results', [])

        target_album = _normalize_for_fuzzy_comparison(album)
        target_artist = _normalize_for_fuzzy_comparison(artist)

        for res in results:
            retrieved_album = _normalize_for_fuzzy_comparison(res.get('collectionName', ''))
            retrieved_artist = _normalize_for_fuzzy_comparison(res.get('artistName', ''))

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


def download_cover_art(artist: str, album: str, target_dir: Path) -> None:
    """
    Downloads the "best" available cover art for the specified release (or does nothing if no acceptable art found).
    """
    image_url = get_itunes_art_url(artist, album) or get_mb_digital_art_url(artist, album)

    if image_url:
        logger.emit(f"[*] Downloading: {image_url}")
        img_data = requests.get(image_url).content
        img = Image.open(BytesIO(img_data))

        # Strip transparency layer if present, as it would cause Pillow to crash on jpeg conversion
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # Save as high-quality JPEG for Kodi
        save_path = target_dir / "cover.jpg"
        img.save(save_path, "JPEG", quality=95)
        logger.emit(f"[+] Success: Saved {img.width}x{img.height} cover to {save_path}")
    else:
        logger.emit("[!] No suitable art found.")


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
