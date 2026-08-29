"""
Private utilities for use by Carat.

Every project of a certain size needs a junk drawer.
"""

# Copyright (c) 2026 Joshua Bloch
# SPDX-License-Identifier: MIT

from __future__ import annotations

import re

import unicodedata


def normalize_for_fuzzy_comparison(s: str) -> str:
    """
    Robust normalization for fuzzy matching.
        1. Normalizes Unicode (NFKD) to decompose diacritics and fix full-width chars.
        2. Strips combining diacritics (e.g., the dots on 'ö').
        3. Lowercases and expands '&'.
        4. Keeps ALL alphanumeric characters (including Cyrillic, Kanji, etc.)
        5. Collapses whitespace to single spaces.
    """
    if not s: return ""  #  Shouldn't be necessary, but acceptably defensive under the circumstances

    # 1. Decompose (turn 'ö' into 'o' + '¨', and 'Ｆ' into 'F')
    s = unicodedata.normalize('NFKD', s)

    # 2. Strip diacritics
    s = "".join([c for c in s if not unicodedata.combining(c)])

    s = s.lower().replace("&", "and")

    # 3. Filter: Keep only letters/numbers and spaces. Works for any script, e.g., Greek, Cyrillic, CJK
    s = "".join([c if c.isalnum() else " " for c in s])

    # 4. Collapse multiple spaces down to one
    return re.sub(r'\s+', ' ', s).strip()


def _clean_path_arg(arg: str) -> str:
    """Strips rogue literal quotes caused by Windows shell path escaping."""
    return arg.strip('"')
