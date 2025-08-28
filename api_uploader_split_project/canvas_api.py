import requests
from typing import Dict, List, Optional, Any, Tuple

def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}

def _url(base: str, path: str) -> str:
    base = base.rstrip("/")
    if base.startswith("http"):
        return f"{base}{path}"
    return f"https://{base}{path}"

# ── Modules & items ──────────────────────────────────────────────────────────
def list_modules(base: str, course_id: str, token: str) -> List[Dict]:
    url = _url(base, f"/api/v1/courses/{course_id}/modules")
    r = requests.get(url, headers=_headers(token))
    r.raise_for_status()
    return r.json()

def list_module_items(base: str, course_id: str, module_id: int, token: str) -> List[Dict]:
    url = _url(base, f"/api/v1/courses/{course_id}/modules/{module_id}/items?per_page=100")
    r = requests.get(url, headers=_headers(token))
    r.raise_for_status()
    items = r.json()
    return items

def get_or_create_module(name: str, base: str, course_id: str, token: str, cache: Dict[str, int]) -> Optional[int]:
    if name in cache:
        return cache[name]
    for m in list_modules(base, course_id, token):
        if m["name"].strip().lower() == name.strip().lower():
            cache[name] = m["id"]
            return m["id"]
    # create
    url = _url(base, f"/api/v1/courses/{course_id}/modules")
    r = requests.post(url, headers=_headers(token), json={"module": {"name": name}})
    r.raise_for_status()
    mid = r.json().get("id")
    if mid:
        cache[name] = mid
    return mid

# ── Pages ────────────────────────────────────────────────────────────────────
def add_page(base: str, course_id: str, title: str, html_body: str, token: str) -> Optional[str]:
    url = _url(base, f"/api/v1/courses/{course_id}/pages")
    payload = {"wiki_page": {"title": title, "body": html_body, "published": True}}
    r = requests.post(url, headers=_headers(token), json=payload)
    r.raise_for_status()
    return r.json().get("url")

def get_page_body(base: str, course_id: str, page_url: str, token: str) -> Tuple[str, Dict]:
    url = _url(base, f"/api/v1/courses/{course_id}/pages/{page_url}")
    r = requests.get(url, headers=_headers(token))
    r.raise_for_status()
    data = r.json()
    return data.get("body") or "", data

# ── Assignments ──────────────────────────────────────────────────────────────
def add_assignment(base: str, course_id: str, title: str, description_html: str, token: str) -> Optional[int]:
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

def get_assignment_description(base: str, course_id: str, assignment_id: int, token: str):
    url = _url(base, f"/api/v1/courses/{course_id}/assignments/{assignment_id}")
    r = requests.get(url, headers=_headers(token))
    r.raise_for_status()
    data = r.json()
    return data.get("description") or "", data

# ── Discussions ──────────────────────────────────────────────────────────────
def add_discussion(base: str, course_id: str, title: str, message_html: str, token: str) -> Optional[int]:
    url = _url(base, f"/api/v1/courses/{course_id}/discussion_topics")
    payload = {"title": title, "message": message_html, "published": True}
    r = requests.post(url, headers=_headers(token), json=payload)
    r.raise_for_status()
    return r.json().get("id")

def get_discussion_body(base: str, course_id: str, discussion_id: int, token: str):
    url = _url(base, f"/api/v1/courses/{course_id}/discussion_topics/{discussion_id}")
    r = requests.get(url, headers=_headers(token))
    r.raise_for_status()
    data = r.json()
    return data.get("message") or "", data

# ── Classic Quizzes helpers ──────────────────────────────────────────────────
def add_to_module(base: str, course_id: str, module_id: int, item_type: str, content_id_or_url, title: str, token: str) -> bool:
    url = _url(base, f"/api/v1/courses/{course_id}/modules/{module_id}/items")
    item = {"type": item_type, "title": title}
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

# ── Classic Quiz accessors ───────────────────────────────────────────────────
def get_quiz_description(base: str, course_id: str, quiz_id: int, token: str):
    url = _url(base, f"/api/v1/courses/{course_id}/quizzes/{quiz_id}")
    r = requests.get(url, headers=_headers(token))
    r.raise_for_status()
    data = r.json()
    return data.get("description") or "", data
