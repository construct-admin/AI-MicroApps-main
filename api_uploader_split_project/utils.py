# ------------------------------------------------------------------------------
# File: utils.py
# Refactor date: 2025-11-13
# Refactored by: Imaad Fakier
#
# Purpose:
#     Provide ultra-lightweight helper utilities for extracting simple
#     XML-like tags (e.g., <course_id>, <module_name>, <api_key>, etc.)
#     from raw text content used in OES GenAI micro-apps.
#
# Overview:
#     These helpers enable fast lookup of custom tags embedded in source
#     documents (DOCX, text exports, AI prompts). The regex cache ensures
#     repeated lookups remain efficient even with dozens of tags.
#
# Behaviour:
#     - Logic preserved 100% from the original.
#     - Tag matching is case-insensitive and DOTALL-enabled (multi-line).
#     - Missing tags gracefully return a default value.
#
# Example:
#       extract_tag("course_id", "<course_id>1234</course_id>")
#       → "1234"
#
# External dependencies:
#     - Python stdlib only.
# ------------------------------------------------------------------------------

import re


# ==============================================================================
# Internal Regex Cache
# ==============================================================================

TAG_RE_CACHE = {}


def _tag_re(tag: str):
    """
    Return (and cache) a compiled regex for matching:

        <tag>...</tag>

    Parameters:
        tag (str):
            The literal tag name (case-insensitive).

    Returns:
        Pattern:
            Compiled regex with DOTALL + IGNORECASE.

    Notes:
        - Caches compiled regex objects for performance.
        - Preserves original regex behaviour exactly.
    """
    if tag not in TAG_RE_CACHE:
        TAG_RE_CACHE[tag] = re.compile(
            rf"<{tag}>\s*(.*?)\s*</{tag}>", re.IGNORECASE | re.DOTALL
        )
    return TAG_RE_CACHE[tag]


# ==============================================================================
# Public API
# ==============================================================================


def extract_tag(tag: str, text: str, default: str = "") -> str:
    """
    Extract the inner content of a simple <tag>...</tag> block from text.

    Parameters:
        tag (str):
            Case-insensitive XML-like tag name (e.g., "course_id").
        text (str):
            Input text to search.
        default (str):
            Value returned if the tag is not found.

    Returns:
        str:
            The extracted value with surrounding whitespace stripped,
            or `default` if no tag is found.

    Behaviour:
        - Multi-line tag content is supported.
        - Returns empty string or provided default when missing.
        - No attempt is made to validate nested tags.
    """
    if not text:
        return default

    m = _tag_re(tag).search(text)
    return m.group(1).strip() if m else default
