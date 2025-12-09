# ------------------------------------------------------------------------------
# File: canvas_api.py
# Refactor date: 2025-11-13
# Refactored by: Imaad Fakier
# Purpose:
#     Standardise and document all Canvas LMS API helper functions used by the
#     OES GenAI micro-applications (Canvas Import, Umich Bot, VT Generator, etc.)
#
# Overview:
#     This module provides a thin wrapper around the Canvas REST API v1 for:
#         - Managing modules and module items
#         - Creating pages, discussions, assignments
#         - Retrieving and posting classic quizzes
#         - Fetching item bodies (HTML content)
#
#     These helpers are intentionally minimal and synchronous. The app-level code
#     decides how to handle retries, UI display, and GPT transformations.
#
# Behaviour guarantees:
#     - No changes to existing logic
#     - Public signatures preserved exactly
#     - Errors raised via requests.exceptions.HTTPError unless explicitly caught
# ------------------------------------------------------------------------------

import requests
from typing import Dict, List, Optional, Any, Tuple


# ==============================================================================
# Internal helpers
# ==============================================================================


def _headers(token: str) -> Dict[str, str]:
    """
    Construct the required Canvas API headers.

    Parameters:
        token (str): Canvas API token.

    Returns:
        Dict[str, str]: Authorization header.
    """
    return {"Authorization": f"Bearer {token}"}


def _url(base: str, path: str) -> str:
    """
    Build a full Canvas API URL from a base domain and a REST path.

    Notes:
        - If the user enters `canvas.myuni.edu`, we prepend https://
        - If the user already enters https://... we leave it as-is.

    Example:
        _url("canvas.myuni.edu", "/api/v1/courses/123/pages")
        → "https://canvas.myuni.edu/api/v1/courses/123/pages"
    """
    base = base.rstrip("/")
    if base.startswith("http"):
        return f"{base}{path}"
    return f"https://{base}{path}"


# ==============================================================================
# Modules & Module Items
# ==============================================================================


def list_modules(base: str, course_id: str, token: str) -> List[Dict]:
    """
    Retrieve all modules for a Canvas course.

    Returns:
        List[Dict]: Each module dictionary contains fields such as:
            - id
            - name
            - position
            - unlock_at
            - require_sequential_progress (if enabled)
    """
    url = _url(base, f"/api/v1/courses/{course_id}/modules")
    r = requests.get(url, headers=_headers(token))
    r.raise_for_status()
    return r.json()


def list_module_items(
    base: str, course_id: str, module_id: int, token: str
) -> List[Dict]:
    """
    List all items inside a Canvas module.

    Notes:
        - Canvas defaults to pagination; ?per_page=100 ensures larger modules load fully.

    Returns:
        List[Dict]: Items with fields like:
            - id
            - title
            - type ("Page", "Assignment", "Discussion", "Quiz")
            - content_id (for Assignment/Discussion/Quiz)
            - page_url (for Pages)
    """
    url = _url(
        base, f"/api/v1/courses/{course_id}/modules/{module_id}/items?per_page=100"
    )
    r = requests.get(url, headers=_headers(token))
    r.raise_for_status()
    return r.json()


def get_or_create_module(
    name: str,
    base: str,
    course_id: str,
    token: str,
    cache: Dict[str, int],
) -> Optional[int]:
    """
    Retrieve module ID by name, or create the module if it does not exist.

    Parameters:
        name (str): Module name (case-insensitive match).
        cache (dict): Local module-name → id cache.

    Returns:
        Optional[int]: Module ID if found/created, else None.
    """
    # Cached?
    if name in cache:
        return cache[name]

    # Try match existing modules
    for m in list_modules(base, course_id, token):
        if m["name"].strip().lower() == name.strip().lower():
            cache[name] = m["id"]
            return m["id"]

    # Create new
    url = _url(base, f"/api/v1/courses/{course_id}/modules")
    payload = {"module": {"name": name}}
    r = requests.post(url, headers=_headers(token), json=payload)
    r.raise_for_status()

    mid = r.json().get("id")
    if mid:
        cache[name] = mid
    return mid


# ==============================================================================
# Pages (Wiki Pages)
# ==============================================================================


def add_page(
    base: str, course_id: str, title: str, html_body: str, token: str
) -> Optional[str]:
    """
    Create a Canvas Page with HTML content.

    Returns:
        Optional[str]: The page_url slug (Canvas uses this as an identifier), or None
                       if creation failed.

    Notes:
        - "body" must be valid HTML
        - "published": True publishes immediately
    """
    url = _url(base, f"/api/v1/courses/{course_id}/pages")
    payload = {
        "wiki_page": {
            "title": title,
            "body": html_body,
            "published": True,
        }
    }
    r = requests.post(url, headers=_headers(token), json=payload)
    r.raise_for_status()
    return r.json().get("url")


def get_page_body(
    base: str, course_id: str, page_url: str, token: str
) -> Tuple[str, Dict]:
    """
    Retrieve the HTML body of a Canvas Page.

    Returns:
        Tuple:
            - HTML (str)
            - Full Canvas page dictionary
    """
    url = _url(base, f"/api/v1/courses/{course_id}/pages/{page_url}")
    r = requests.get(url, headers=_headers(token))
    r.raise_for_status()

    data = r.json()
    return data.get("body") or "", data


# ==============================================================================
# Assignments
# ==============================================================================


def add_assignment(
    base: str, course_id: str, title: str, description_html: str, token: str
) -> Optional[int]:
    """
    Create a Canvas Assignment that accepts online text entry submissions.

    Returns:
        Optional[int]: Assignment ID.

    Notes:
        - “description” must be HTML
        - Assignments are published immediately
    """
    url = _url(base, f"/api/v1/courses/{course_id}/assignments")
    payload = {
        "assignment": {
            "name": title,
            "submission_types": ["online_text_entry"],
            "published": True,
            "description": description_html,
        }
    }
    r = requests.post(url, headers=_headers(token), json=payload)
    r.raise_for_status()
    return r.json().get("id")


def get_assignment_description(
    base: str, course_id: str, assignment_id: int, token: str
):
    """
    Retrieve assignment HTML description.

    Returns:
        Tuple[str, Dict]:
            - description_html (str)
            - full assignment JSON
    """
    url = _url(base, f"/api/v1/courses/{course_id}/assignments/{assignment_id}")
    r = requests.get(url, headers=_headers(token))
    r.raise_for_status()

    data = r.json()
    return data.get("description") or "", data


# ==============================================================================
# Discussions
# ==============================================================================


def add_discussion(
    base: str, course_id: str, title: str, message_html: str, token: str
) -> Optional[int]:
    """
    Create a Canvas Discussion Topic.

    Returns:
        Optional[int]: Discussion ID.

    Notes:
        - The 'message' field must be HTML
        - 'published': True means visible immediately
    """
    url = _url(base, f"/api/v1/courses/{course_id}/discussion_topics")
    payload = {"title": title, "message": message_html, "published": True}
    r = requests.post(url, headers=_headers(token), json=payload)
    r.raise_for_status()
    return r.json().get("id")


def get_discussion_body(base: str, course_id: str, discussion_id: int, token: str):
    """
    Retrieve the HTML body/message of a Discussion Topic.

    Returns:
        Tuple[str, Dict]:
            - message_html (str)
            - full discussion JSON
    """
    url = _url(base, f"/api/v1/courses/{course_id}/discussion_topics/{discussion_id}")
    r = requests.get(url, headers=_headers(token))
    r.raise_for_status()

    data = r.json()
    return data.get("message") or "", data


# ==============================================================================
# Classic Quiz Integration
# ==============================================================================


def add_to_module(
    base: str,
    course_id: str,
    module_id: int,
    item_type: str,
    content_id_or_url,
    title: str,
    token: str,
) -> bool:
    """
    Add an item (Page / Assignment / Discussion / Quiz) to a module.

    Parameters:
        item_type (str):
            "Page"         → Canvas expects 'page_url'
            Any other type → Canvas expects 'content_id'

    Returns:
        bool: True on success, False otherwise.

    Notes:
        - We intentionally swallow any Canvas errors here and return False,
          because app-level code decides how to display upload failures.
    """
    url = _url(base, f"/api/v1/courses/{course_id}/modules/{module_id}/items")

    item: Dict[str, Any] = {"type": item_type, "title": title}

    if item_type == "Page":
        item["page_url"] = content_id_or_url
    else:
        item["content_id"] = content_id_or_url

    r = requests.post(url, headers=_headers(token), json={"module_item": item})
    try:
        r.raise_for_status()
        return True
    except Exception:
        return False


def get_quiz_description(base: str, course_id: str, quiz_id: int, token: str):
    """
    Retrieve the HTML description of a classic Canvas Quiz.

    Returns:
        Tuple[str, Dict]:
            - description HTML
            - full quiz JSON
    """
    url = _url(base, f"/api/v1/courses/{course_id}/quizzes/{quiz_id}")
    r = requests.get(url, headers=_headers(token))
    r.raise_for_status()

    data = r.json()
    return data.get("description") or "", data
