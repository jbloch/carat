"""
Typed, retrying wrappers for the MusicBrainz API calls used by music_brainier.
"""

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

from __future__ import annotations

__all__ = [
    "init",
    "browse_releases", "fetch_release_by_id", "fetch_release_group_by_id", "search_release_groups", "search_releases",
    "MusicBrainzError", "WebServiceError",
    "BrowseMedium", "BrowseReleasesResponse", "Medium", "Recording",
    "ReleaseByIDResponse", "ReleaseDetails", "ReleaseGroup", "ReleaseGroupByIDResponse",
    "ReleaseGroupSearchResponse", "ReleaseGroupSearchResult", "ReleaseSearchResponse",
    "ReleaseSearchResult", "ReleaseSummary", "Track",
]

import time
from collections.abc import Callable
from functools import wraps
from typing import TYPE_CHECKING, ParamSpec, Protocol, Required, TypedDict, TypeVar, cast

import musicbrainzngs as mb  # pyright: ignore[reportMissingTypeStubs]

# These are the nested response fields that music_brainier reads; other API fields are intentionally omitted.

Recording = TypedDict("Recording", {
        "title": Required[str],
        "length": str,
    },
    total=False,
)
"""MusicBrainz recording fields (`title`, optional `length` in ms as a string)."""

Track = TypedDict("Track", {
        "recording": Required[Recording],
        "artist-credit-phrase": str,
    },
    total=False,
)
"""MusicBrainz track fields (`recording`, optional `artist-credit-phrase`)."""

Medium = TypedDict("Medium", {
        "position": Required[str],
        "track-list": list[Track],
    },
    total=False,
)
"""MusicBrainz medium (disc) fields (`position` as a 1-based string, optional `track-list`)."""

BrowseMedium = TypedDict("BrowseMedium", {"track-count": int})
"""Summary medium fields returned by browse endpoints (`track-count`)."""

ReleaseSummary = TypedDict("ReleaseSummary", {
        "id": Required[str],
        "date": str,
        "status": str,
        "packaging": str,
        "medium-track-count": int,
        "medium-list": list[BrowseMedium],
    },
    total=False,
)
"""Summary release fields returned by browse and release-group endpoints."""

ReleaseGroup = TypedDict("ReleaseGroup", {
        "id": Required[str],
        "primary-type": str,
        "release-list": list[ReleaseSummary],
    },
    total=False,
)
"""MusicBrainz release-group fields (`id`, optional `primary-type` and `release-list`)."""

ReleaseDetails = TypedDict("ReleaseDetails", {
        "title": Required[str],
        "date": str,
        "artist-credit-phrase": str,
        "medium-list": list[Medium],
        "release-group": ReleaseGroup,
    },
    total=False,
)
"""Detailed release fields returned by `fetch_release_by_id`."""

ReleaseByIDResponse = TypedDict("ReleaseByIDResponse", {"release": ReleaseDetails})
"""Top-level response envelope for `fetch_release_by_id`."""

ReleaseGroupByIDResponse = TypedDict("ReleaseGroupByIDResponse", {"release-group": ReleaseGroup})
"""Top-level response envelope for `fetch_release_group_by_id`."""

ReleaseSearchResult = TypedDict("ReleaseSearchResult", {
        "title": str,
        "artist-credit-phrase": str,
        "release-group": ReleaseGroup,
    },
)
"""Single release match returned by `search_releases`."""

ReleaseSearchResponse = TypedDict("ReleaseSearchResponse", {"release-list": list[ReleaseSearchResult]})
"""Top-level response envelope for `search_releases`."""

ReleaseGroupSearchResult = TypedDict("ReleaseGroupSearchResult", {"id": str})
"""Single release-group match returned by `search_release_groups`."""

ReleaseGroupSearchResponse = TypedDict("ReleaseGroupSearchResponse", {
        "release-group-count": int,
        "release-group-list": list[ReleaseGroupSearchResult],
    },
)
"""Top-level response envelope for `search_release_groups`."""

BrowseReleasesResponse = TypedDict("BrowseReleasesResponse", {"release-list": list[ReleaseSummary]})
"""Top-level response envelope for `browse_releases`."""


# This protocol describes only the untyped dependency surface music_brainier calls.
class _MusicBrainzAPI(Protocol):
    def set_useragent(self, app: str, version: str, contact: str | None = None) -> None:
        """Sets the User-Agent header on the underlying musicbrainzngs client."""

    def get_release_by_id(self, rel_id: str, includes: list[str] | None = None) -> ReleaseByIDResponse:
        """Fetches a release by MBID from musicbrainzngs."""

    def get_release_group_by_id(self, rg_id: str, includes: list[str] | None = None) -> ReleaseGroupByIDResponse:
        """Fetches a release group by MBID from musicbrainzngs."""

    def search_releases(self, query: str, limit: int) -> ReleaseSearchResponse:
        """Searches releases via musicbrainzngs."""

    def search_release_groups(self, artist: str, releasegroup: str) -> ReleaseGroupSearchResponse:
        """Searches release groups via musicbrainzngs."""

    def browse_releases(self, release_group: str, includes: list[str], limit: int, offset: int) -> BrowseReleasesResponse:
        """Browses releases in a release group via musicbrainzngs."""


_mb_api = cast(_MusicBrainzAPI, cast(object, mb))

if TYPE_CHECKING:
    class MusicBrainzError(Exception):
        """Base class for all exceptions related to MusicBrainz."""

    class WebServiceError(MusicBrainzError):
        """Error related to MusicBrainz API requests."""
else:
    MusicBrainzError = mb.MusicBrainzError
    WebServiceError = mb.WebServiceError


def init(app_version: str) -> None:
    """Initializes the MusicBrainz API client with Carat's User-Agent.

    Args:
        app_version: Carat version string sent in the User-Agent header.
    """
    _mb_api.set_useragent("carat - concise atmos rip automation tool", app_version, "josh@bloch.us")


# Type parameters for the retry adapter
_P = ParamSpec("_P")
_R = TypeVar("_R")


def _retry_mb_api(retries: int = 3, delay: float = 2.0) -> Callable[[Callable[_P, _R]], Callable[_P, _R]]:
    """Returns a decorator that retries MusicBrainz calls after transient web-service errors.

    Args:
        retries: Maximum number of attempts before re-raising `WebServiceError`.
        delay: Seconds to sleep between failed attempts.
    """
    def decorator(func: Callable[_P, _R]) -> Callable[_P, _R]:
        """Wraps a MusicBrainz API function with retry logic."""
        @wraps(func)
        def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
            """Invokes the wrapped call, retrying on `WebServiceError`."""
            attempt = 0
            while True:
                try:
                    return func(*args, **kwargs)
                except WebServiceError:
                    attempt += 1
                    if attempt >= retries:
                        raise
                    time.sleep(delay)
        return wrapper
    return decorator


#
# The typed, retrying MusicBrainz API calls
#

@_retry_mb_api()
def fetch_release_by_id(rel_id: str, includes: list[str] | None = None) -> ReleaseByIDResponse:
    """Fetches a single MusicBrainz release by MBID, retrying transient web-service errors.

    Args:
        rel_id: The 36-character MusicBrainz Release ID.
        includes: Optional MusicBrainz sub-entities to include (e.g., 'recordings', 'artist-credits').
    """
    return _mb_api.get_release_by_id(rel_id, includes=includes or [])


@_retry_mb_api()
def fetch_release_group_by_id(rg_id: str, includes: list[str] | None = None) -> ReleaseGroupByIDResponse:
    """Fetches a MusicBrainz release group by MBID, retrying transient web-service errors.

    Args:
        rg_id: The 36-character MusicBrainz Release Group ID.
        includes: Optional MusicBrainz sub-entities to include (e.g., 'releases').
    """
    return _mb_api.get_release_group_by_id(rg_id, includes=includes or [])


@_retry_mb_api()
def search_releases(query: str, limit: int) -> ReleaseSearchResponse:
    """Searches MusicBrainz releases using a Lucene query, retrying transient web-service errors.

    Args:
        query: Lucene search query string.
        limit: Maximum number of release results to return.
    """
    return _mb_api.search_releases(query=query, limit=limit)


@_retry_mb_api()
def search_release_groups(artist: str, releasegroup: str) -> ReleaseGroupSearchResponse:
    """Searches MusicBrainz release groups by artist and title, retrying transient web-service errors.

    Args:
        artist: Artist name to search for.
        releasegroup: Release group (album) title to search for.
    """
    return _mb_api.search_release_groups(artist=artist, releasegroup=releasegroup)


@_retry_mb_api()
def browse_releases(rg_id: str, limit: int, offset: int) -> BrowseReleasesResponse:
    """Lists releases in a release group with medium summaries, retrying transient web-service errors.

    Args:
        rg_id: The 36-character MusicBrainz Release Group ID.
        limit: Maximum number of releases to return in this page.
        offset: Zero-based pagination offset into the release list.
    """
    return _mb_api.browse_releases(release_group=rg_id, includes=["media"], limit=limit, offset=offset)
