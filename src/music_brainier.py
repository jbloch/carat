"""
music_brainier - A helper library for MusicBrainz.

Provides a higher level and more pleasant programmatic API than the thin skin over the web API that is musicbrainzngs.
"""

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

__author__ = "Joshua Bloch"
__copyright__ = "Copyright 2026, Joshua Bloch"
__license__ = "MIT"
__version__ = "1.0B3.1"

__all__ = ['MusicBrainzMetadata', 'MusicBrainzTrack', 'fetch_candidate_metadata', 'fetch_metadata',
           'get_best_mb_candidate', 'find_release_group', 'generate_mb_art_urls']

import difflib
import re
import time
from typing import TypedDict

import musicbrainzngs as mb
import requests

import logger
from util import normalize_for_fuzzy_comparison


class MusicBrainzMetadata(TypedDict):
    """ A dictionary of release information returned by MusicBrainz """
    title: str
    artist: str
    year: str
    mbid: str
    medium_index: int
    tracks: list[MusicBrainzTrack]


class MusicBrainzTrack(TypedDict):
    """ Represents a track returned by the MusicBrainz API """
    title: str
    duration_ms: int


def fetch_candidate_metadata(artist: str, album: str) -> list[MusicBrainzMetadata]:
    """
    Returns the metadata for the candidate releases corresponding to the given (inexact) artist and album name.
    All the releases returned will come from the same release group.
    """
    logger.emit("\n[*] === STARTING METADATA FETCH ===")

    rg = find_release_group(artist, album)
    if not rg:
        logger.emit("    [-] No matching release group found. Aborting metadata fetch.")
        return []
    rg_id, rg_artist, rg_title = rg

    releases = find_releases_and_dates_for_release_group(rg_id, rg_title)
    logger.emit(f"    -> Filtered down to {len(releases)} matching release(s).")

    if not releases:
        logger.emit("    [-] No matching releases found. Aborting metadata fetch.")
        return []

    candidates = fetch_metadata_for_releases(releases, rg_id, rg_artist, rg_title)
    return candidates


def get_best_mb_candidate(target_artist: str, target_album: str, chapter_count: int,
                          target_duration_sec: float, candidates: list[MusicBrainzMetadata]) -> MusicBrainzMetadata | None:
    """
    Finds the best MusicBrainz candidate for a given artist, name, target chapter count, and target duration.
    Filters for exact track count matches or +1 preamble matches, prioritizing similar durations, lower MB medium index,
    and finally string similarity to the target artist and album names.
    """
    if not candidates:
        return None

    # Filter for valid matches (exact or +1 preamble)
    matched = [c for c in candidates if 0 <= (chapter_count - len(c['tracks'])) <= 1]
    if not matched:
        return None

    safe_target = normalize_for_fuzzy_comparison(f"{target_artist} {target_album}").replace(" ", "")

    def get_similarity(c: MusicBrainzMetadata) -> float:
        """Calculates a similarity score based on string similarity to the target artist and album."""
        safe_cand = normalize_for_fuzzy_comparison(f"{c['artist']} {c['title']}").replace(" ", "")
        return difflib.SequenceMatcher(None, safe_target, safe_cand).ratio()

    def get_duration_penalty(c: MusicBrainzMetadata) -> int:
        """Calculates a penalty based on total duration difference (in 30-second buckets)."""
        mb_duration_ms = sum(t.get('duration_ms') or 0 for t in c['tracks'])
        if mb_duration_ms == 0 or target_duration_sec == 0:
            return 0
        mb_sec = round(mb_duration_ms / 1000)

        return int(abs(target_duration_sec - mb_sec) // 30)

    def get_medium_penalty(c: MusicBrainzMetadata) -> int:
        """Penalizes bonus discs and non-primary mediums."""
        # Medium 1 gets 0 penalty. Medium 2 gets 1 penalty, etc.
        return max(0, c.get('medium_index', 1) - 1)

    # Sort Hierarchy:
    # 1. Duration Penalty (Filters out radically different tracklists)
    # 2. Medium Position (Prioritizes Disc 1 over Disc 2/3/4)
    # 3. Text Similarity (Fuzzy string match for exact album/artist text)
    matched.sort(key=lambda c: (
        get_duration_penalty(c),
        get_medium_penalty(c),
        -get_similarity(c)
    ))

    for i, c in enumerate(matched):
        bullet = "*" if i == 0 else "-"
        logger.emit(f"    {bullet} {c['title']} ({c.get('year', 'Unknown')}) [MBID {c.get('mbid', 'Unknown')} "
                    f"({c.get('medium_index', 1)})] [Scores: Duration={get_duration_penalty(c)},"
                    f" Medium={get_medium_penalty(c)}, Artist/Title={get_similarity(c) :.2f}]")

    return matched[0]


# The maximum number of releases to search for a good match for the user-supplied album and artist names
MAX_RELEASES_TO_SEARCH: int = 15

def find_release_group(artist: str, album: str) -> tuple[str, str, str] | None:
    """
    Finds the release group corresponding to the given album and artist name (which may be inexact).
    Searches by release rather than release group to bypass strict artist indexing.

    Returns:
        A tuple containing (release_group_id, artist, title) if found, otherwise None.
    """
    for is_strict in [True, False]:
        query = f'artist:"{artist}" AND release:"{album}"' if is_strict else f'"{artist}" "{album}"'
        logger.emit(f"[*] Executing Query: {query}")
        try:
            res = _search_releases(query=query, limit=MAX_RELEASES_TO_SEARCH)
            for r in res.get('release-list', []):
                found_artist = extract_artist_from_musicbrainz_metadata(r)
                found_album = r.get('title', 'Unknown')

                # Evaluate matches for plausibility with "substring leniency"
                artist_match = _is_safe_match(artist, found_artist)
                album_match = _is_safe_match(album, found_album)

                if artist_match and album_match:
                    rg_id = r.get('release-group', {}).get('id')
                    rg_title, rg_artist = found_album, found_artist
                    logger.emit(f"    [+] Match Found: {found_artist} - {found_album} (RG ID: {rg_id})\n")

                    # Ensure rg_id actually exists to satisfy the strict return type!
                    if rg_id:
                        return str(rg_id), rg_artist, rg_title
                else:
                    logger.emit(f"    [-] Rejected Candidate: {found_artist} - {found_album} "
                                f"(Artist Match: {artist_match}, Album Match: {album_match})")
        except mb.WebServiceError as e:
            logger.emit(f"    [!] API Error: {e}")

    return None


def _retry_mb_api(retries: int = 3, delay: float = 2.0):
    """Decorator to retry MusicBrainz API calls on transient web errors."""
    def decorator(func):
        """Internal decorator to retry MusicBrainz API calls."""
        def wrapper(*args, **kwargs):
            """internal decorator that does the actual work"""
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except mb.WebServiceError:
                    attempt += 1
                    if attempt >= retries:
                        raise
                    time.sleep(delay)
        return wrapper
    return decorator


def find_releases_and_dates_for_release_group(rg_id: str, rg_title: str) -> list[tuple[str, str]]:
    """
    Returns release IDs and dates of releases of the given release group corresponding to all possible track-counts.
    Evaluates mediums individually to strictly match physical disc topology.
    """
    logger.emit(f"[*] Fetching all releases and mediums for Release Group: {rg_id} ({rg_title})")

    releases = []
    limit = 100
    offset = 0

    # 1. Fetch all releases by paginating through the browse_releases endpoint
    try:
        while True:
            result = _browse_releases(rg_id, limit, offset)
            batch = result.get('release-list', [])
            releases.extend(batch)

            if len(batch) < limit:
                break  # We've reached the end of the list
            offset += limit

        logger.emit(f"    -> API returned {len(releases)} releases in this group.")
    except mb.WebServiceError as e:
        logger.emit(f"    [!] Error fetching releases: {e}")
        return []

    unique_releases = {}
    seen_counts = set()

    # 2. Map the mediums to find unique track counts
    for r in releases:
        mediums = r.get('medium-list', [])

        if not mediums:
            t_count = int(r.get('medium-track-count', r.get('track-count', 0)))
            if t_count > 0 and t_count not in seen_counts:
                seen_counts.add(t_count)
                unique_releases[r['id']] = r.get('date', '')[:4]
            continue

        for m in mediums:
            m_count = int(m.get('track-count', 0))
            if m_count > 0 and m_count not in seen_counts:
                seen_counts.add(m_count)
                unique_releases[r['id']] = r.get('date', '')[:4]

    logger.emit(f"    -> Identified unique track counts: {sorted(list(seen_counts))}")
    return [(r_id, date) for r_id, date in unique_releases.items()]


def fetch_metadata_for_releases(release_ids_and_dates: list[tuple[str, str]],
                                rg_id: str, rg_artist: str, rg_title: str) -> list[MusicBrainzMetadata]:
    """Fetch the tracklists for the given release ids (and dates), which pertain to the given release group metadata"""
    logger.emit(f"\n[*] Fetching tracklists for {len(release_ids_and_dates)} matching MB releases in release group {rg_id}...")
    candidates: list[MusicBrainzMetadata] = []
    for rel_id, year in release_ids_and_dates:
        try:
            rel_info = _fetch_release_by_id(rel_id, includes=['recordings', 'artist-credits'])

            # Treat EVERY medium as its own independent candidate
            for pos, medium in enumerate(rel_info.get('release', {}).get('medium-list', []), start=1):
                medium_tracks = []
                for track in medium.get('track-list', []):
                    medium_tracks.append({
                        'title': sanitize_track_title(track.get('recording', {}).get('title', 'Unknown Track')),
                        'duration_ms': int(track.get('recording', {}).get('length') or 0)
                    })

                if medium_tracks:
                    candidates.append({
                        'title': rg_title,
                        'artist': rg_artist,
                        'year': year or 'Unknown',
                        'mbid': rel_id,  # Specific release MBID instead of the Release Group ID
                        'medium_index': int(medium.get('position', pos)),
                        'tracks': medium_tracks
                    })
        except mb.WebServiceError:
            continue
    logger.emit(f"[*] Metadata fetch complete. Found {len(candidates)} candidate MB mediums in release group.")
    return candidates


def sanitize_track_title(title: str) -> str:
    """
    Strips Extra Title Information (MusicBrainz ETI) from track titles safely.
    Removes parenthetical blocks ONLY if they contain known technical/mix keywords.
    Preserves structural parentheticals like "(Don't Fear) The Reaper".
    """
    # A blacklist of words that strongly indicate technical metadata rather than a song title
    eti_keywords = r'(mix|remix|remaster|master|version|edit|live|instrumental|demo|take|stereo|mono|surround|atmos|acoustic)'

    # Matches " (" or "(", followed by anything, the keyword (as a whole word), anything, and ")"
    pattern = re.compile(rf'\s*\([^)]*\b{eti_keywords}\b[^)]*\)', re.IGNORECASE)

    # Strip the ETI and clean up any lingering trailing spaces
    return pattern.sub('', title).strip()


def extract_artist_from_musicbrainz_metadata(entity: dict) -> str:
    """
    Reconstructs the full artist credit string from MusicBrainz's parsed list format. MusicBrainz stores
    collaborations as lists of fragments (e.g., [{'name': 'Simon'}, {'joinphrase': ' & '}, {'name': 'Garfunkel'}]).
    """
    # Sometimes older API endpoints just return a flat string
    credit = entity.get('artist-credit', '')
    if isinstance(credit, str):
        return credit

    if isinstance(credit, list):
        full_name = ""
        for fragment in credit:
            if isinstance(fragment, dict):
                # 'name' is the literal text on the jacket; 'artist' is the DB entity
                name = fragment.get('name') or fragment.get('artist', {}).get('name', '')
                join_phrase = fragment.get('joinphrase', '')
                full_name += name + join_phrase
            elif isinstance(fragment, str):
                full_name += fragment
        return full_name.strip() or "Unknown"

    return "Unknown"


def _is_safe_match(expected: str, found: str) -> bool:
    """
    Compares two strings for similarity after stripping all spaces and punctuation.
    Acts as a lenient gatekeeper: allows pure substrings (for truncated titles or
    collaborations) or highly similar strings (for typos/acronyms).
    """
    # Universal substring safety valve for messy artist collaborations or truncated titles
    # (e.g., "Scary Monsters" in "Scary Monsters (and Super Creeps)")
    if expected.lower() in found.lower():
        return True

    safe_expected = normalize_for_fuzzy_comparison(expected).replace(" ", "")
    safe_found = normalize_for_fuzzy_comparison(found).replace(" ", "")

    ratio = difflib.SequenceMatcher(None, safe_expected, safe_found).ratio()
    return ratio > 0.7


def fetch_metadata(mbid: str, medium_idx: int) -> MusicBrainzMetadata:
    """Fetches the metadata for a single medium of a release."""
    logger.emit(f"[*] Fetching metadata for MBID {mbid} (Medium {medium_idx})...")

    rel_info = _fetch_release_by_id(mbid, includes=['recordings', 'artist-credits'])
    medium_info: dict | None = next((m for m in rel_info.get('release', {}).get('medium-list', [])
                                       if int(m.get('position', 1)) == medium_idx), None)
    if medium_info is None:
        raise ValueError(f"Medium {medium_idx} not found in release {mbid}.")

    medium_tracks: list[MusicBrainzTrack] = [{
        'title': sanitize_track_title(track.get('recording', {}).get('title', 'Unknown Track')),
        'duration_ms': int(track.get('recording', {}).get('length') or 0)
    } for track in medium_info.get('track-list', [])]

    metadata: MusicBrainzMetadata = {
        'title': rel_info.get('release', {}).get('title', 'unknown'),
        'artist': extract_artist_from_musicbrainz_metadata(rel_info.get('release', {})),
        'year': rel_info.get('release', {}).get('date', 'Unknown')[:4],
        'mbid': mbid,
        'medium_index': medium_idx,
        'tracks': medium_tracks
    }

    logger.emit(f"[+] Found Release: {metadata['artist']} - {metadata['title']} ({len(metadata['tracks'])} tracks)")
    return metadata


def generate_mb_art_urls(artist: str, album: str, mbid: str | None = None):
    """ Yields candidate CoverArtArchive URLs in priority order. """
    logger.emit(f"[*] Searching MusicBrainz for {artist} - {album}...")
    batches = []

    try:
        if mbid:
            # 1. Exact match (Use a fake future date to ensure it sorts to the top)
            batches.append([{'id': mbid, 'date': '9999'}])

            # 2. Get siblings
            rel_info = _fetch_release_by_id(mbid, includes=["release-groups"])
            rg_id = rel_info['release']['release-group']['id']
        else:
            # 3. No MBID, search by text
            release_groups = _search_release_groups(artist=artist, releasegroup=album)
            if release_groups['release-group-count'] == 0: return
            rg_id = release_groups['release-group-list'][0]['id']

        # Get all releases in the group
        releases = _fetch_release_group_by_id(rg_id, includes=["releases"])['release-group']['release-list']

        # Batch them by priority
        digital = [r for r in releases if r.get('status') == 'Official' and r.get('packaging') is None]
        official = [r for r in releases if r.get('status') == 'Official' and r not in digital]
        batches.extend([digital, official])

        # Yield URLs one by one
        for batch in batches:
            batch.sort(key=lambda x: str(x.get('date', '0000')), reverse=True)
            for r in batch:
                mb_id = r['id']

                # Direct CAA network call (Skips the redundant MB API check!)
                resp = requests.get(f"https://coverartarchive.org/release/{mb_id}", timeout=10)
                if resp.status_code == 200:
                    logger.emit(f"  [+] Found CAA metadata for: {mb_id}")
                    for img_entry in resp.json().get('images', []):
                        if img_entry.get('front'):
                            # YIELD pauses this function and hands the URL back to client, e.g., get_cover_art.py
                            yield img_entry['thumbnails'].get('1200') or img_entry['image']

    except (mb.MusicBrainzError, KeyError, IndexError, requests.RequestException) as e:
        logger.emit(f"  [!] MusicBrainz/CAA API Error: {e}")


@_retry_mb_api()
def _fetch_release_by_id(rel_id: str, includes: list[str] | None = None) -> dict:
    """Fetches a single release by ID with retry logic."""
    return mb.get_release_by_id(rel_id, includes=includes or [])


@_retry_mb_api()
def _fetch_release_group_by_id(rg_id: str, includes: list[str] | None = None) -> dict:
    """Fetches a release group by ID with retry logic."""
    return mb.get_release_group_by_id(rg_id, includes=includes or [])


@_retry_mb_api()
def _search_releases(query: str, limit: int) -> dict:
    """Executes a release search with retry logic."""
    return mb.search_releases(query=query, limit=limit)


@_retry_mb_api()
def _search_release_groups(artist: str, releasegroup: str) -> dict:
    """Searches for release groups with retry logic."""
    return mb.search_release_groups(artist=artist, releasegroup=releasegroup)


@_retry_mb_api()
def _browse_releases(rg_id: str, limit: int, offset: int) -> dict:
    """Fetches a paginated list of releases with retry logic."""
    return mb.browse_releases(release_group=rg_id, includes=['media'], limit=limit, offset=offset)