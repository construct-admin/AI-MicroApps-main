# ------------------------------------------------------------------------------
# File: gdoc_utils.py
# Refactor date: 2025-11-13
# Refactored by: Imaad Fakier
#
# Purpose:
#     High-level Google Docs utilities used throughout OES GenAI micro-apps.
#     These functions provide:
#
#         - Google Doc ID extraction from URLs
#         - Parsing of anchors (headings, bookmarks, tab fragments)
#         - Service-Account-based DOCX export from Google Drive
#         - Full heading-outline extraction (flat & hierarchical)
#         - Bookmark, named-range, and heading diagnostics
#         - Section text extraction via:
#               • heading ID
#               • bookmark ID
#               • named range
#               • text match
#               • arbitrary textual markers
#               • combined “anchor” resolver
#
# Behaviour Guarantees:
#     - No logic changes from the original implementation.
#     - No modification to request structures, iteration order, heuristics,
#       or extraction rules.
#     - This file remains a pure helper module with no Streamlit or OpenAI dependencies.
#
# External APIs:
#     - Google Docs API (v1)
#     - Google Drive API (v3)
#
# Requirements:
#     google-api-python-client
#     google-auth
#
# Notes:
#     We intentionally import Google libraries lazily so that importing
#     this module never fails unless the functionality is invoked.
# ------------------------------------------------------------------------------

import io
import re
import json
from typing import List, Dict, Optional, Tuple


# ==============================================================================
# Internal Google Client Builders (Lazy Loaded)
# ==============================================================================


def _ensure_docs(sa_json_bytes: bytes):
    """
    Lazily instantiate a Google Docs API client using a Service Account.

    Parameters:
        sa_json_bytes (bytes): Raw JSON of a Google Service Account.

    Returns:
        googleapiclient.discovery.Resource: Docs API client.

    Raises:
        RuntimeError: Missing dependencies (google-api-python-client / google-auth).
    """
    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
    except Exception as e:
        raise RuntimeError(
            "Google API client libraries are missing. "
            "Add 'google-api-python-client' and 'google-auth' to requirements.txt."
        ) from e

    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json_bytes.decode("utf-8")),
        scopes=[
            "https://www.googleapis.com/auth/drive.readonly",
            "https://www.googleapis.com/auth/documents.readonly",
        ],
    )
    return build("docs", "v1", credentials=creds)


def _ensure_drive(sa_json_bytes: bytes):
    """
    Lazily instantiate a Google Drive API client using a Service Account.
    """
    try:
        from googleapiclient.discovery import build
        from google.oauth2 import service_account
    except Exception as e:
        raise RuntimeError(
            "Google API client libraries are missing. "
            "Add 'google-api-python-client' and 'google-auth' to requirements.txt."
        ) from e

    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json_bytes.decode("utf-8")),
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    return build("drive", "v3", credentials=creds)


def _get_doc(file_id: str, sa_json_bytes: bytes):
    """
    Internal helper: retrieve a full Google Doc document structure.
    """
    docs = _ensure_docs(sa_json_bytes)
    return docs.documents().get(documentId=file_id).execute()


# ==============================================================================
# URL Parsing Utilities
# ==============================================================================


def gdoc_id_from_url(url: str) -> Optional[str]:
    """
    Extract the Google Doc file ID from a given URL.

    Supports URLs such as:
        https://docs.google.com/document/d/<FILEID>/edit
        https://docs.google.com/document/d/<FILEID>/
    """
    if not url:
        return None
    m = re.search(r"/document/d/([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None


def parse_anchor_from_url(url: str) -> Tuple[Optional[str], Optional[str]]:
    """
    Parse heading/bookmark/tab anchors from a Google Doc URL.

    Returns:
        (kind, id):
            - kind: "heading" or "bookmark" or None
            - id: identifier string (headingId, bookmarkId, tab fragment)
    """
    if not url:
        return None, None

    # #heading=<ID>
    if "#heading=" in url:
        return "heading", url.split("#heading=")[1].split("&")[0]

    # #bookmark=<ID>
    if "#bookmark=" in url:
        return "bookmark", url.split("#bookmark=")[1].split("&")[0]

    # tab fragments: ?tab=h.<frag> or ?tab=t.<frag>
    m = re.search(r"[?#&]tab=([ht])\.([A-Za-z0-9_-]+)", url)
    if m:
        kind_code, frag = m.group(1), m.group(2)
        return (
            ("heading", f"h.{frag}") if kind_code == "h" else ("bookmark", f"t.{frag}")
        )

    return None, None


# ==============================================================================
# Export DOCX
# ==============================================================================


def fetch_docx_from_gdoc(file_id: str, sa_json_bytes: bytes) -> io.BytesIO:
    """
    Export a Google Doc as a DOCX file using the Drive API.

    Returns:
        io.BytesIO: In-memory DOCX file content.
    """
    drive = _ensure_drive(sa_json_bytes)
    request = drive.files().export(
        fileId=file_id,
        mimeType="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    buf = io.BytesIO()
    from googleapiclient.http import MediaIoBaseDownload

    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()

    buf.seek(0)
    return buf


# ==============================================================================
# Heading Outline Utilities
# ==============================================================================


def gdoc_outline(file_id: str, sa_json_bytes: bytes) -> List[Dict]:
    """
    Extract a flat list of all heading elements in the Google Doc.

    Returns a list of:
        { "level": int, "text": str, "headingId": str }
    """
    doc = _get_doc(file_id, sa_json_bytes)
    body = doc.get("body", {}).get("content", []) or []

    out = []
    for el in body:
        p = el.get("paragraph")
        if not p:
            continue

        style = p.get("paragraphStyle", {}) or {}
        named = style.get("namedStyleType", "") or ""

        if not named.startswith("HEADING_"):
            continue

        try:
            level = int(named.split("_")[-1])
        except Exception:
            level = 1

        text = "".join(
            r.get("textRun", {}).get("content", "") for r in p.get("elements", [])
        ).strip()
        hid = style.get("headingId")

        if text and hid:
            out.append({"level": level, "text": text, "headingId": hid})

    return out


def gdoc_outline_with_parents(file_id: str, sa_json_bytes: bytes) -> List[Dict]:
    """
    Generate a hierarchical outline with parent relationships included.
    """
    doc = _get_doc(file_id, sa_json_bytes)
    body = doc.get("body", {}).get("content", []) or []

    outline = []
    stack = []

    for el in body:
        p = el.get("paragraph")
        if not p:
            continue

        style = p.get("paragraphStyle", {}) or {}
        named = style.get("namedStyleType", "") or ""
        if not named.startswith("HEADING_"):
            continue

        try:
            level = int(named.split("_")[-1])
        except Exception:
            level = 1

        text = "".join(
            r.get("textRun", {}).get("content", "") for r in p.get("elements", [])
        ).strip()
        hid = style.get("headingId")

        if not (text and hid):
            continue

        # parent resolution
        while stack and stack[-1]["level"] >= level:
            stack.pop()

        parent_id = stack[-1]["headingId"] if stack else None
        outline.append(
            {"level": level, "text": text, "headingId": hid, "parentId": parent_id}
        )

        stack.append({"level": level, "headingId": hid})

    return outline


# ==============================================================================
# Diagnostics
# ==============================================================================


def list_anchors(file_id: str, sa_json_bytes: bytes) -> Dict:
    """
    Retrieve diagnostics on:
        - headings
        - bookmarks
        - named ranges
        - named range IDs
    """
    doc = _get_doc(file_id, sa_json_bytes)
    headings = gdoc_outline(file_id, sa_json_bytes)

    bookmark_ids = sorted(list((doc.get("bookmarks") or {}).keys()))
    named = doc.get("namedRanges") or {}

    named_range_names = sorted(list(named.keys()))
    named_range_ids = sorted(
        list(
            {
                nr.get("namedRangeId")
                for arr in named.values()
                for nr in arr
                if nr.get("namedRangeId")
            }
        )
    )

    return {
        "headings": headings,
        "bookmark_ids": bookmark_ids,
        "named_range_names": named_range_names,
        "named_range_ids": named_range_ids,
    }


# ==============================================================================
# Section Extraction — Headings
# ==============================================================================


def extract_section_text_by_heading(
    file_id: str, sa_json_bytes: bytes, heading_id: str
) -> str:
    """
    Extract text belonging to a heading block until the next heading of the same
    or higher level.
    """
    doc = _get_doc(file_id, sa_json_bytes)
    body = doc.get("body", {}).get("content", []) or []

    capturing = False
    target_level = None
    lines = []

    for el in body:
        p = el.get("paragraph")
        # Capture tables if already inside the section
        if not p:
            if capturing and ("table" in el or "tableOfContents" in el):
                lines.append("[TABLE]")
            continue

        style = p.get("paragraphStyle", {}) or {}
        text = "".join(
            r.get("textRun", {}).get("content", "") for r in p.get("elements", [])
        )
        named = style.get("namedStyleType", "") or ""
        hid = style.get("headingId")

        if hid == heading_id:
            capturing = True
            try:
                target_level = (
                    int(named.split("_")[-1]) if named.startswith("HEADING_") else 1
                )
            except Exception:
                target_level = 1
            continue

        if capturing:
            if named.startswith("HEADING_"):
                try:
                    lvl = int(named.split("_")[-1])
                except Exception:
                    lvl = 1
                if target_level is not None and lvl <= target_level:
                    break
            lines.append(text)

    return "\n".join(lines)


# ==============================================================================
# Bookmark & Named-Range Helpers
# ==============================================================================


def _resolve_bookmark_or_named_range_start_index(
    doc: Dict, anchor_id: str
) -> Optional[int]:
    """
    Resolve the starting offset of a bookmark or named range.
    Supports multiple anchor patterns for robustness.
    """
    variants = [anchor_id]
    frag = anchor_id.split(".", 1)[1] if "." in anchor_id else anchor_id
    variants += [f"id.{frag}", f"t.{frag}", frag]

    # Try bookmarks first
    bookmarks = doc.get("bookmarks", {}) or {}
    for cand in variants:
        if cand in bookmarks:
            return bookmarks[cand].get("position", {}).get("index")

    # Try named ranges
    named = doc.get("namedRanges", {}) or {}
    for name, arr in named.items():
        if name in variants:
            try:
                return arr[0]["ranges"][0].get("startIndex")
            except Exception:
                pass
        for nr in arr:
            nrid = nr.get("namedRangeId")
            if nrid and (nrid in variants or nrid == frag):
                try:
                    return nr["ranges"][0].get("startIndex")
                except Exception:
                    pass

    return None


# ==============================================================================
# Section Extraction — Bookmark / Named Range
# ==============================================================================


def extract_section_text_by_bookmark(
    file_id: str, sa_json_bytes: bytes, bookmark_or_tab_id: str
) -> str:
    """
    Extract text starting at a bookmark or named range until the next H1 or end.
    """
    doc = _get_doc(file_id, sa_json_bytes)
    body = doc.get("body", {}).get("content", []) or []

    start_index = _resolve_bookmark_or_named_range_start_index(doc, bookmark_or_tab_id)
    if start_index is None:
        return ""

    # Determine stop index: next H1
    stop_index = None
    for el in body:
        si = el.get("startIndex")
        if si and si > start_index:
            p = el.get("paragraph")
            if p:
                named = (p.get("paragraphStyle", {}) or {}).get("namedStyleType", "")
                if named == "HEADING_1":
                    stop_index = si
                    break

    lines = []
    for el in body:
        si, ei = el.get("startIndex"), el.get("endIndex")
        if si is None or ei is None or si < start_index:
            continue
        if stop_index is not None and si >= stop_index:
            break

        if "paragraph" in el:
            p = el["paragraph"]
            text = "".join(
                r.get("textRun", {}).get("content", "") for r in p.get("elements", [])
            )
            lines.append(text)
        elif "table" in el or "tableOfContents" in el:
            lines.append("[TABLE]")

    return "\n".join(lines)


# ==============================================================================
# Section Extraction — Text Match
# ==============================================================================


def extract_section_text_by_text_match(
    file_id: str, sa_json_bytes: bytes, needle: str
) -> str:
    """
    Extract section text beginning at a paragraph containing `needle` (case-insensitive),
    stopping at next H1 or end of doc.
    """
    if not needle or not needle.strip():
        return ""

    doc = _get_doc(file_id, sa_json_bytes)
    body = doc.get("body", {}).get("content", []) or []

    # Find start index
    start_index = None
    nlow = needle.lower()

    for el in body:
        p = el.get("paragraph")
        if not p:
            continue
        text = "".join(
            r.get("textRun", {}).get("content", "") for r in p.get("elements", [])
        )
        if nlow in text.lower():
            start_index = el.get("startIndex")
            break

    if start_index is None:
        return ""

    # Determine stop index: next H1
    stop_index = None
    for el in body:
        si = el.get("startIndex")
        if si and si > start_index:
            p = el.get("paragraph")
            if p:
                named = (p.get("paragraphStyle", {}) or {}).get("namedStyleType", "")
                if named == "HEADING_1":
                    stop_index = si
                    break

    # Collect lines
    lines = []
    for el in body:
        si, ei = el.get("startIndex"), el.get("endIndex")
        if si is None or ei is None or si < start_index:
            continue
        if stop_index is not None and si >= stop_index:
            break

        if "paragraph" in el:
            p = el["paragraph"]
            text = "".join(
                r.get("textRun", {}).get("content", "") for r in p.get("elements", [])
            )
            lines.append(text)
        elif "table" in el or "tableOfContents" in el:
            lines.append("[TABLE]")

    return "\n".join(lines)


# ==============================================================================
# Section Extraction — Generic Markers
# ==============================================================================


def extract_text_between_markers(
    file_id: str,
    sa_json_bytes: bytes,
    start_marker: str,
    end_marker: Optional[str] = None,
) -> str:
    """
    Extract text between two textual markers. Case-insensitive.

    If end_marker is not provided, extraction runs until next H1 or EOF.
    """
    doc = _get_doc(file_id, sa_json_bytes)
    body = doc.get("body", {}).get("content", []) or []

    if not start_marker:
        return ""

    sm = start_marker.lower()
    em = end_marker.lower() if end_marker else None

    capturing = False
    lines: List[str] = []

    for el in body:
        if "paragraph" in el:
            p = el["paragraph"]
            text = "".join(
                r.get("textRun", {}).get("content", "") for r in p.get("elements", [])
            )
            low = text.lower()

            if not capturing:
                if sm in low:
                    capturing = True
                continue
            else:
                if em and em in low:
                    break
                lines.append(text)

        elif capturing and ("table" in el or "tableOfContents" in el):
            lines.append("[TABLE]")

    return "\n".join(lines)


# ==============================================================================
# Section Extraction — Combined Anchor Logic
# ==============================================================================


def extract_section_text_by_anchor(
    file_id: str,
    sa_json_bytes: bytes,
    anchor_kind: str,
    anchor_id: str,
    fallback_text: Optional[str] = None,
) -> str:
    """
    Unified extraction helper driven by a URL anchor:
        - For anchor_kind == "heading": use headingId extraction
        - For anchor_kind == "bookmark": resolve bookmark/named range
        - If none resolves and fallback_text is provided -> text-match extraction
    """
    doc = _get_doc(file_id, sa_json_bytes)
    body = doc.get("body", {}).get("content", []) or []

    # Build heading index
    headings = []
    for el in body:
        p = el.get("paragraph")
        if not p:
            continue

        style = p.get("paragraphStyle", {}) or {}
        named = style.get("namedStyleType", "")
        if not named.startswith("HEADING_"):
            continue

        try:
            level = int(named.split("_")[-1])
        except Exception:
            level = 1

        txt = "".join(
            r.get("textRun", {}).get("content", "") for r in p.get("elements", [])
        )
        si = el.get("startIndex")
        hid = style.get("headingId")

        if si is not None:
            headings.append(
                {
                    "startIndex": si,
                    "level": level,
                    "headingId": hid,
                    "text": txt,
                }
            )

    def nearest_heading_level_before(idx: int) -> int:
        lvl = 1
        for h in headings:
            if h["startIndex"] is not None and h["startIndex"] <= idx:
                lvl = h["level"]
        return lvl

    # Determine starting position
    start_index = None
    base_level = 1

    if anchor_kind == "heading":
        for h in headings:
            if h.get("headingId") == anchor_id:
                start_index = h["startIndex"]
                base_level = h["level"]
                break

    elif anchor_kind == "bookmark":
        start_index = _resolve_bookmark_or_named_range_start_index(doc, anchor_id)
        if start_index is not None:
            base_level = nearest_heading_level_before(start_index)

    # Fallback behaviour
    if start_index is None and fallback_text:
        return extract_section_text_by_text_match(file_id, sa_json_bytes, fallback_text)
    if start_index is None:
        return ""

    # Determine stop index: next heading of same or higher level
    stop_index = None
    for el in body:
        si = el.get("startIndex")
        if si and si > start_index:
            p = el.get("paragraph")
            if not p:
                continue

            named = (p.get("paragraphStyle", {}) or {}).get("namedStyleType", "")
            if named.startswith("HEADING_"):
                try:
                    lvl = int(named.split("_")[-1])
                except Exception:
                    lvl = 1

                if lvl <= base_level:
                    stop_index = si
                    break

    # Collect final lines
    lines = []
    for el in body:
        si, ei = el.get("startIndex"), el.get("endIndex")
        if si is None or ei is None:
            continue
        if si < start_index:
            continue
        if stop_index is not None and si >= stop_index:
            break

        if "paragraph" in el:
            p = el["paragraph"]
            text = "".join(
                r.get("textRun", {}).get("content", "") for r in p.get("elements", [])
            )
            lines.append(text)
        elif "table" in el or "tableOfContents" in el:
            lines.append("[TABLE]")

    return "\n".join(lines)
