"""
   A freestanding tool and small library to find high-quality album artowork on the web.
   It currenly uses Apple/iTunes and MusicBrainz as sources, but this does not affect
   the contract between this module and its users.
"""

import sys
import os
import requests
import musicbrainzngs as mb
import logger
from PIL import Image
from io import BytesIO
from pathlib import Path

# Configuration
mb.set_useragent("CoverArtRetrievalTool", "0.1", "josh@bloch.us")

MAX_FILE_SIZE = 15 * 1024 * 1024  # 15 MB Cap
MIN_DIMENSION = 1000              # Minimum pixels for 'High Res'

def is_valid_image(url):
    try:
        # If it's a known CAA thumbnail, we trust the size and skip the HEAD request
        is_thumbnail = "/thumbnails/" in url or "itunes.apple.com" in url
        
        if not is_thumbnail:
            head = requests.head(url, allow_redirects=True, timeout=5)
            size = int(head.headers.get('Content-Length', 0))
            if size > MAX_FILE_SIZE:
                return False, 0, 0

        # We still use stream=True here to check the Aspect Ratio/Dimensions 
        # because we only need the first few KB of the header.
        resp = requests.get(url, stream=True, timeout=10)
        img = Image.open(resp.raw)
        w, h = img.size
        
        # Validation logic...
        aspect_ratio = w / h
        if 0.95 < aspect_ratio < 1.05 and w >= MIN_DIMENSION:
            return True, w, h
    except Exception:
        pass
    return False, 0, 0

def get_mb_digital_art(artist, album, log_callback):
    logger.emit(f"[*] Searching MusicBrainz for {artist} - {album}...", log_callback, log_callback)
    try:
        release_groups = mb.search_release_groups(artist=artist, releasegroup=album)
        if release_groups['release-group-count'] == 0: return None
        
        rgid = release_groups['release-group-list'][0]['id']
        releases = mb.get_release_group_by_id(rgid, includes=["releases"])['release-group']['release-list']
        
        # Prioritize 'Digital' releases (which have digital-native cover art) over other official releases
        digital = [r for r in releases if r.get('status') == 'Official' and r.get('packaging') is None]
        image_url = get_mb_art_from_releases(digital)
        if (not image_url):
            official = [r for r in releases if r.get('status') == 'Official']
            image_url = get_mb_art_from_releases(official, log_callback)
    except: pass
    return image_url

def get_mb_art_from_releases(releases, log_callback):
    # Sort by date desc (ensure date is treated as a string)
    releases.sort(key=lambda x: str(x.get('date', '0000')), reverse=True)
    
    for r in releases:
        mbid = r['id']
        # IMPORTANT: Perform a direct lookup for the release to get fresh CAA status
        # The release data inside a release-group object is often incomplete.
        try:
            full_release = mb.get_release_by_id(mbid)
            status = full_release['release'].get('cover-art-archive', {})
            
            if status.get('artwork') == 'true':
                logger.emit(f"  [+] Found CAA art for: {mbid}", log_callback)
                caa_data = requests.get(f"https://coverartarchive.org/release/{mbid}").json()
                for img_entry in caa_data['images']:
                    if img_entry['front']:
                        url = img_entry['thumbnails'].get('1200') or img_entry['image']
                        valid, w, h = is_valid_image(url)
                        if valid: return url
                        else: logger.emit(f"Invalid CAA art: '{img_entry}'", log_callback);
        except Exception as e:
            logger.emit(f"  [!] Error checking release {mbid}: {e}", log_callback)
            continue
    return None

def get_itunes_art(artist, album, log_callback):
    url = "https://itunes.apple.com/search"
    params = {"term": f"{artist} {album}", "entity": "album", "limit": 1}
    try:
        r = requests.get(url, params=params).json()
        if r.get('resultCount', 0) > 0:
            res = r['results'][0]
            if album.lower() not in res['collectionName'].lower():
                logger.emit(f"Possible mismatch: cover art is for {res['artistName']} - {res['collectionName']}", log_callback)
            else:
                hq_url = r['results'][0]['artworkUrl100'].replace("100x100bb.jpg", "1200x1200bb.jpg")
                valid, w, h = is_valid_image(hq_url)
                if valid: return hq_url
    except: pass
    return None

def download_cover_art(artist, album, target_dir, log_callback=None):
    """
    Downloads the best available cover art. 
    Uses log_callback for thread-safe reporting via logger.py.
    """
    image_url = get_itunes_art(artist, album, log_callback) or get_mb_digital_art(artist, album, log_callback)

    if image_url:
        logger.emit(f"[*] Downloading: {image_url}", log_callback)
        img_data = requests.get(image_url).content
        img = Image.open(BytesIO(img_data))
        
        # Save as high-quality JPEG for Kodi
        save_path = target_dir / "cover.jpg"
        img.save(save_path, "JPEG", quality=95)
        logger.emit(f"[+] Success: Saved {img.width}x{img.height} cover to {save_path}", log_callback, log_callback)
    else:
        logger.emit("[!] No suitable art found.")

def main():
    if len(sys.argv) < 4:
        logger.emit('Usage: python getcoverart.py "Artist" "Album" "/Library/Root"')
        sys.exit(1)

    artist, album, library_root = sys.argv[1], sys.argv[2], sys.argv[3]
    target_dir = Path(library_root) / artist / album
    target_dir.mkdir(parents=True, exist_ok=True)

    download_cover_art(artist, album, target_dir)

if __name__ == "__main__":
    main()
