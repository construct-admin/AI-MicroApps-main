# canvas_import_um.py
# -----------------------------------------------------------------------------
# 📄 DOCX/Google Doc → GPT (optional KB) → Canvas
# Tabs to reduce token usage:
#   1) Pages     — parse storyboard, visualize only selected pages, upload Pages/Assignments
#   2) New Quizzes — select a template, duplicate (or copy-settings), insert description + questions
#   3) Discussions — select a template, duplicate (copy-settings), insert description
#
# Notes:
# - Google Doc inputs are optional; you can just upload .docx.
# - Vector Store (OpenAI File Search) is optional; leave blank to run without KB.
# - New Quizzes: if clone endpoint is 404, we "clone by copy": create new quiz with the
#   template’s most important settings, then insert description/questions.
# - Questions JSON schema (at END of GPT output for quiz pages):
#   {
#     "quiz_description":"<html>",
#     "questions":[
#       {"question_name":"Q1","question_text":"<p>…</p>",
#        "shuffle": true,
#        "feedback":{"correct":"<p>…</p>","incorrect":"<p>…</p>","neutral":"<p>…</p>"},
#        "answers":[{"text":"A","is_correct":false,"feedback":"<p>…</p>"}, ...]
#       }
#     ]
#   }
# -----------------------------------------------------------------------------

from __future__ import annotations
from io import BytesIO
import json
import re
import uuid
from typing import Any

import requests
import streamlit as st
from docx import Document
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# Optional OpenAI (visualize pages)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

# =============================== Streamlit UI =================================

st.set_page_config(page_title="DOCX → GPT → Canvas", layout="wide")
st.title("📄 DOCX → GPT → Canvas")

# ---------------------------- Session State -----------------------------------
def _init_state():
    ss = st.session_state
    ss.setdefault("pages", [])                 # list[dict]
    ss.setdefault("gpt_results", {})           # idx -> {"html":..., "quiz_json":...}
    ss.setdefault("visualized", False)
    ss.setdefault("vector_store_id", "")
    ss.setdefault("new_quiz_templates", [])
    ss.setdefault("discussion_templates", [])

_init_state()

# =============================== Sidebar ======================================

with st.sidebar:
    st.header("Canvas & Keys")
    canvas_domain = st.text_input("Canvas base domain", placeholder="yourcollege.instructure.com")
    course_id = st.text_input("Course ID")
    canvas_token = st.text_input("Canvas API token", type="password")

    st.divider()
    st.header("OpenAI (for visualization)")
    openai_api_key = st.text_input("OpenAI API Key", type="password")
    vector_store_id = st.text_input("OpenAI Vector Store ID (optional)", value=st.session_state.get("vector_store_id", ""))

    st.divider()
    st.header("Storyboard sources")
    storyboard_docx = st.file_uploader("Storyboard (.docx)", type=["docx"])
    st.caption("— OR —")
    gdoc_url = st.text_input("Storyboard Google Doc URL (optional)")
    sa_json = st.file_uploader("Service Account JSON (optional)", type=["json"])

    st.divider()
    dry_run = st.checkbox("🔍 Dry run (no upload)", value=False)

def require_canvas_ready():
    if not (canvas_domain and course_id and canvas_token):
        st.error("Please fill Canvas domain, course ID, and token in the sidebar.")
        st.stop()

# =========================== Utilities & Parsers ===============================

def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}

def _gdoc_id_from_url(url: str) -> str | None:
    if not url: return None
    m = re.search(r"/d/([A-Za-z0-9_-]+)", url)
    if m: return m.group(1)
    m = re.search(r"[?&]id=([A-Za-z0-9_-]+)", url)
    return m.group(1) if m else None

def fetch_docx_from_gdoc(file_id: str, sa_json_bytes: bytes) -> BytesIO:
    creds = Credentials.from_service_account_info(
        json.loads(sa_json_bytes.decode("utf-8")),
        scopes=["https://www.googleapis.com/auth/drive.readonly"],
    )
    service = build("drive", "v3", credentials=creds)
    data = service.files().export(
        fileId=file_id,
        mimeType="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ).execute()
    return BytesIO(data)

def extract_canvas_pages(docx_like) -> list[str]:
    """
    Pull everything between <canvas_page>...</canvas_page>, preserving any inline
    HTML, headings, lists, and raw text. Also captures any docx tables found
    while we're inside a canvas_page block (serialized as <table>…</table>).
    """
    doc = Document(docx_like)
    pages: list[str] = []
    buf: list[str] = []
    inside = False

    def flush():
        nonlocal buf
        if buf:
            pages.append("\n".join(buf))
            buf = []

    for block in doc.element.body:
        tag = block.tag.lower()
        # Paragraph
        if tag.endswith("p"):
            tx = "".join(run.text for run in Document(docx_like).paragraphs if False)  # no-op to keep import happy
        # Use the higher-level API to iterate mixed content robustly:
    # We’ll just iterate using python-docx objects
    for para in doc.paragraphs:
        t = para.text.strip()
        low = t.lower()
        if "<canvas_page>" in low:
            inside = True
            buf = [t] if t else ["<canvas_page>"]
            continue
        if "</canvas_page>" in low:
            if t and t != "</canvas_page>":
                buf.append(t)
            buf.append("</canvas_page>")
            flush()
            inside = False
            continue
        if inside:
            buf.append(t)

    # Also sweep tables; python-docx keeps tables separate from paragraphs order,
    # so we conservatively append them to the last open page if present.
    if pages:
        # serialize all tables into one block and append to the last page if not already present
        tbl_htmls = []
        for tbl in doc.tables:
            rows = []
            for r in tbl.rows:
                cells = []
                for c in r.cells:
                    cells.append(f"<td>{c.text.strip()}</td>")
                rows.append(f"<tr>{''.join(cells)}</tr>")
            tbl_htmls.append(f"<table>{''.join(rows)}</table>")
        if tbl_htmls and "</canvas_page>" in pages[-1]:
            pages[-1] = pages[-1].replace("</canvas_page>", "\n".join(tbl_htmls) + "\n</canvas_page>")

    return pages

def extract_tag(tag: str, block: str) -> str:
    m = re.search(fr"<{tag}>(.*?)</{tag}>", block, flags=re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""

# ============================== Canvas (Core) =================================

def get_or_create_module(module_name: str, domain: str, course_id: str, token: str, cache: dict) -> int | None:
    if module_name in cache:
        return cache[module_name]
    url = f"https://{domain}/api/v1/courses/{course_id}/modules"
    r = requests.get(url, headers=_auth_headers(token), timeout=60)
    if r.status_code == 200:
        for m in r.json():
            if m.get("name", "").strip().lower() == module_name.strip().lower():
                cache[module_name] = m["id"]
                return m["id"]
    r = requests.post(url, headers=_auth_headers(token), json={"module": {"name": module_name, "published": True}}, timeout=60)
    if r.status_code in (200, 201):
        mid = r.json().get("id")
        cache[module_name] = mid
        return mid
    st.error(f"❌ Failed to create/find module '{module_name}': {r.status_code} | {r.text}")
    return None

def add_to_module(domain: str, course_id: str, module_id: int, item_type: str, ref: Any, title: str, token: str) -> bool:
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    payload = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = ref
    else:
        payload["module_item"]["content_id"] = ref
    r = requests.post(url, headers=_auth_headers(token), json=payload, timeout=60)
    return r.status_code in (200, 201)

def add_page(domain: str, course_id: str, title: str, html_body: str, token: str) -> str | None:
    url = f"https://{domain}/api/v1/courses/{course_id}/pages"
    r = requests.post(url, headers=_auth_headers(token), json={"wiki_page": {"title": title, "body": html_body, "published": True}}, timeout=60)
    if r.status_code in (200, 201):
        return r.json().get("url")
    st.error(f"❌ Page create failed: {r.status_code} | {r.text}")
    return None

def add_assignment(domain: str, course_id: str, title: str, html_body: str, token: str) -> int | None:
    url = f"https://{domain}/api/v1/courses/{course_id}/assignments"
    payload = {"assignment": {"name": title, "description": html_body, "published": True, "submission_types": ["online_text_entry"], "points_possible": 10}}
    r = requests.post(url, headers=_auth_headers(token), json=payload, timeout=60)
    if r.status_code in (200, 201):
        return r.json().get("id")
    st.error(f"❌ Assignment create failed: {r.status_code} | {r.text}")
    return None

# ============================== New Quizzes (LTI) =============================

def list_new_quiz_assignments(domain: str, course_id: str, token: str) -> list[dict]:
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes"
    try:
        r = requests.get(url, headers=_auth_headers(token), timeout=60)
        if r.status_code != 200: return []
        data = r.json()
    except Exception:
        return []
    raw = data.get("quizzes") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    items: list[dict] = []
    for q in raw:
        if not isinstance(q, dict): continue
        items.append({"id": q.get("id"), "assignment_id": q.get("assignment_id"), "title": q.get("title") or q.get("name") or f"Quiz {q.get('id')}", "settings": q})
    return items

def get_new_quiz(domain: str, course_id: str, quiz_id: str, token: str) -> dict | None:
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{quiz_id}"
    r = requests.get(url, headers=_auth_headers(token), timeout=60)
    if r.status_code != 200: return None
    try: return r.json()
    except Exception: return None

def clone_new_quiz_if_supported(domain: str, course_id: str, quiz_id: str, token: str) -> str | None:
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{quiz_id}/clone"
    try:
        r = requests.post(url, headers=_auth_headers(token), json={}, timeout=60)
        if r.status_code in (200, 201):
            return str((r.json() or {}).get("id") or "")
    except Exception:
        pass
    return None

def create_new_quiz_from_template(domain: str, course_id: str, token: str, template_quiz: dict, title: str, instructions_html: str | None) -> str | None:
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes"
    base = template_quiz or {}
    quiz_payload = {
        "title": title or base.get("title") or "New Quiz",
        "points_possible": base.get("points_possible") or 1,
        "shuffle_answers": base.get("shuffle_answers", False),
        "time_limit": base.get("time_limit"),
        "one_question_at_a_time": base.get("one_question_at_a_time", False),
        "require_lockdown_browser": base.get("require_lockdown_browser", False),
        "instructions": instructions_html or base.get("instructions") or "",
    }
    r = requests.post(url, headers=_auth_headers(token), json={"quiz": quiz_payload}, timeout=60)
    if r.status_code not in (200, 201): return None
    try:
        data = r.json()
        return str(data.get("id") or data.get("assignment_id") or "")
    except Exception:
        return None

def patch_new_quiz(domain: str, course_id: str, quiz_id: str, token: str, title: str | None = None, instructions_html: str | None = None) -> None:
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{quiz_id}"
    body = {"quiz": {}}
    if title is not None: body["quiz"]["title"] = title
    if instructions_html is not None: body["quiz"]["instructions"] = instructions_html
    try: requests.patch(url, headers=_auth_headers(token), json=body, timeout=60)
    except Exception: pass

def add_new_quiz_mcq(domain: str, course_id: str, quiz_id: str, token: str, q: dict, position: int = 1) -> bool:
    choices = []
    correct = None
    ans_fb = {}
    for idx, ans in enumerate(q.get("answers", []), start=1):
        cid = str(uuid.uuid4())
        choices.append({"id": cid, "position": idx, "text": str(ans.get("text", ""))})
        if ans.get("is_correct"): correct = cid
        if ans.get("feedback"): ans_fb[cid] = ans["feedback"]
    if not choices: return False
    if not correct: correct = choices[0]["id"]
    shuffle = bool(q.get("shuffle", False))
    title = q.get("question_name") or "Question"
    body = q.get("question_text") or ""

    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{quiz_id}/items"
    # Shape A
    a = {
        "item": {
            "entry_type": "Item", "points_possible": 1, "position": position,
            "entry": {
                "title": title, "interaction_type_slug": "choice", "item_body": body,
                "interaction_data": {"choices": choices},
                "properties": {"shuffleRules": {"choices": {"toLock": [], "shuffled": shuffle}}},
                "scoring_algorithm": "Equivalence", "scoring_data": {"value": correct}
            }
        }
    }
    if q.get("feedback"):
        fb = {}
        if q["feedback"].get("correct"): fb["correct"] = q["feedback"]["correct"]
        if q["feedback"].get("incorrect"): fb["incorrect"] = q["feedback"]["incorrect"]
        if q["feedback"].get("neutral"): fb["neutral"] = q["feedback"]["neutral"]
        if fb: a["item"]["entry"]["feedback"] = fb
    if ans_fb: a["item"]["entry"]["answer_feedback"] = ans_fb

    r = requests.post(url, headers=_auth_headers(token), json=a, timeout=60)
    if r.status_code in (200, 201): return True

    # Shape B
    b = {
        "item": {
            "entry_type": "Item", "points_possible": 1, "position": position,
            "entry": {
                "title": title, "interaction_type_slug": "choice", "stem": body,
                "interaction_data": {"choices": [{"id": c["id"], "position": c["position"], "itemBody": f"<p>{c['text']}</p>"} for c in choices]},
                "properties": {"shuffleRules": {"choices": {"toLock": [], "shuffled": shuffle}}},
                "scoring_algorithm": "Equivalence", "scoring_data": {"value": correct}
            }
        }
    }
    if q.get("feedback"):
        fb = {}
        if q["feedback"].get("correct"): fb["correct"] = q["feedback"]["correct"]
        if q["feedback"].get("incorrect"): fb["incorrect"] = q["feedback"]["incorrect"]
        if q["feedback"].get("neutral"): fb["neutral"] = q["feedback"]["neutral"]
        if fb: b["item"]["entry"]["feedback"] = fb
    if ans_fb: b["item"]["entry"]["answer_feedback"] = ans_fb

    r2 = requests.post(url, headers=_auth_headers(token), json=b, timeout=60)
    return r2.status_code in (200, 201)

def add_items_from_quiz_json(domain: str, course_id: str, quiz_id: str, token: str, quiz_json: dict) -> None:
    qs = quiz_json.get("questions", []) if isinstance(quiz_json, dict) else []
    pos = 1
    for q in qs:
        try:
            if q.get("answers"):
                ok = add_new_quiz_mcq(domain, course_id, quiz_id, token, q, position=pos)
                pos += 1 if ok else 0
        except Exception:
            pass

# =============================== Discussions ==================================

def list_discussions(domain: str, course_id: str, token: str) -> list[dict]:
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
    r = requests.get(url, headers=_auth_headers(token), timeout=60)
    if r.status_code != 200: return []
    items = []
    for d in r.json():
        items.append({"id": d.get("id"), "title": d.get("title"), "settings": d})
    return items

def create_discussion_from_template(domain: str, course_id: str, token: str, template: dict, title: str, message_html: str) -> int | None:
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
    base = template or {}
    payload = {
        "title": title or base.get("title") or "Discussion",
        "message": message_html or base.get("message") or "",
        "published": True,
        # copy some common knobs if present
        "delayed_post_at": base.get("delayed_post_at"),
        "lock_at": base.get("lock_at"),
        "is_announcement": base.get("is_announcement", False),
        "require_initial_post": base.get("require_initial_post", False),
        "podcast_enabled": base.get("podcast_enabled", False),
        "discussion_type": base.get("discussion_type", "side_comment"),
    }
    r = requests.post(url, headers=_auth_headers(token), json=payload, timeout=60)
    if r.status_code in (200, 201):
        return r.json().get("id")
    return None

# =============================== OpenAI (viz) =================================

def ensure_openai() -> OpenAI:
    if not openai_api_key or OpenAI is None:
        st.error("OpenAI key missing (or openai package not installed).")
        st.stop()
    return OpenAI(api_key=openai_api_key)

SYSTEM_PROMPT = (
    "You are an expert Canvas HTML generator.\n"
    "If a uMich template is in the KB, reproduce its HTML verbatim (do NOT remove classes/attrs).\n"
    "Preserve all <img> tags exactly; keep .bluePageHeader, .header, .divisionLineYellow, .landingPageFooter intact.\n"
    "Only replace inner text/HTML in content regions. If a section doesn't exist in the template, create it with the same structure.\n"
    "Convert any table-like content into proper <table><tr><td> HTML.\n\n"
    "QUIZ RULES:\n"
    "- Questions appear between <quiz_start> and </quiz_end>.\n"
    "- <multiple_choice> uses '*' to mark correct choices.\n"
    "- Optional <shuffle> per-question => set \"shuffle\": true.\n"
    "- Optional feedback tags: <feedback_correct>, <feedback_incorrect>, <feedback_neutral>.\n"
    "- Optional per-answer feedback '(feedback: ...)' after a choice line.\n\n"
    "RETURN:\n"
    "1) Canvas-ready HTML (no code fences)\n"
    "2) If page_type is 'quiz', append a JSON object at the very END with keys:\n"
    "{\"quiz_description\":\"<html>\",\"questions\":[{\"question_name\":\"...\",\"question_text\":\"...\",\n"
    "\"answers\":[{\"text\":\"A\",\"is_correct\":false,\"feedback\":\"<p>..</p>\"}],\"shuffle\":true,\n"
    "\"feedback\":{\"correct\":\"<p>..</p>\",\"incorrect\":\"<p>..</p>\",\"neutral\":\"<p>..</p>\"}}]}\n"
    "COVERAGE RULES:\n"
    "- Do not omit content from <canvas_page>…</canvas_page>.\n"
    "- Preserve original order; if content doesn't map, append under:\n"
    "<div class=\"divisionLineYellow\"><h2>Additional Content</h2><div>…</div></div>\n"
    "- Never remove <img>, <table>, or explicit HTML already present in the storyboard.\n"
)

# ================================ Tabs ========================================

tab_pages, tab_quizzes, tab_discussions = st.tabs(["Pages", "New Quizzes", "Discussions"])

# -------------------------------- Pages ---------------------------------------
with tab_pages:
    st.header("1) Pages — parse storyboard, visualize with GPT, upload")

    colA, colB = st.columns([1, 2])
    with colA:
        if st.button("Parse storyboard → pages", type="primary", use_container_width=True, disabled=not (storyboard_docx or (gdoc_url and sa_json))):
            st.session_state.pages.clear()
            st.session_state.gpt_results.clear()
            st.session_state.visualized = False

            source = storyboard_docx
            if not source and gdoc_url and sa_json:
                fid = _gdoc_id_from_url(gdoc_url)
                if fid:
                    try:
                        source = fetch_docx_from_gdoc(fid, sa_json.read())
                    except Exception as e:
                        st.error(f"❌ Google Doc fetch failed: {e}")

            if not source:
                st.error("Provide a .docx or a Google Doc URL + Service Account JSON")
                st.stop()

            raw_pages = extract_canvas_pages(source)
            last_mod = None
            for idx, block in enumerate(raw_pages):
                page_type = (extract_tag("page_type", block).lower() or "page").strip()
                page_title = extract_tag("page_title", block) or f"Page {idx+1}"
                module_name = extract_tag("module_name", block).strip() or last_mod or "General"
                last_mod = module_name
                st.session_state.pages.append({
                    "index": idx, "raw": block, "page_type": page_type,
                    "page_title": page_title, "module_name": module_name
                })

            st.success(f"✅ Parsed {len(st.session_state.pages)} page(s).")

    if st.session_state.pages:
        st.subheader("Select pages to visualize (to save tokens)")
        idxs = [p["index"] for p in st.session_state.pages]
        labels = [f'{p["index"]+1}. {p["page_title"]} ({p["page_type"]})' for p in st.session_state.pages]
        sel = st.multiselect("Pages to visualize", options=idxs, default=idxs, format_func=lambda i: labels[idxs.index(i)])

        if st.button("🔎 Visualize selected pages with GPT", type="primary", disabled=not openai_api_key):
            client = ensure_openai()
            st.session_state.gpt_results.clear()
            use_vs = vector_store_id.strip()
            if use_vs:
                st.session_state.vector_store_id = use_vs

            with st.spinner("Calling GPT…"):
                for p in st.session_state.pages:
                    if p["index"] not in sel:
                        continue
                    user_prompt = f"Storyboard page block:\n{p['raw']}"
                    if vector_store_id.strip():
                        resp = client.responses.create(
                            model="gpt-4o",
                            input=[{"role": "system", "content": SYSTEM_PROMPT},
                                   {"role": "user", "content": user_prompt}],
                            tools=[{"type": "file_search", "vector_store_ids": [vector_store_id.strip()]}],
                        )
                        out = (resp.output_text or "").strip()
                    else:
                        chat = client.chat.completions.create(
                            model="gpt-4o",
                            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                                      {"role": "user", "content": user_prompt}],
                            temperature=0.1,
                        )
                        out = (chat.choices[0].message.content or "").strip()

                    cleaned = re.sub(r"```(?:html|json)?", "", out, flags=re.IGNORECASE).strip()
                    json_match = re.search(r"({[\s\S]+})\s*$", cleaned)
                    quiz_json = None
                    html_result = cleaned
                    if json_match and p["page_type"] == "quiz":
                        try:
                            quiz_json = json.loads(json_match.group(1))
                            html_result = cleaned[:json_match.start()].strip()
                        except Exception:
                            quiz_json = None
                    st.session_state.gpt_results[p["index"]] = {"html": html_result, "quiz_json": quiz_json}

            st.session_state.visualized = True
            st.success("✅ Visualization done. Use the other tabs to upload quizzes/discussions.")

        # Quick page/assignment upload (optional)
        require_canvas_ready()
        mod_cache = {}
        st.divider()
        st.subheader("Upload visualized Pages/Assignments (optional)")
        up_sel = st.multiselect("Select items to upload (non-quiz/discussion)", options=idxs, default=[], format_func=lambda i: labels[idxs.index(i)])
        if st.button("Upload selected Pages/Assignments"):
            for p in st.session_state.pages:
                if p["index"] not in up_sel:
                    continue
                if p["page_type"] not in ("page", "assignment"):
                    continue
                bundle = st.session_state.gpt_results.get(p["index"], {})
                html_result = bundle.get("html", "")
                if not html_result:
                    st.warning(f"No HTML for '{p['page_title']}'. Visualize first.")
                    continue
                mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, mod_cache)
                if not mid:
                    st.error("Module create/find failed.")
                    continue
                if p["page_type"] == "page":
                    page_url = add_page(canvas_domain, course_id, p["page_title"], html_result, canvas_token)
                    if page_url and add_to_module(canvas_domain, course_id, mid, "Page", page_url, p["page_title"], canvas_token):
                        st.success(f"✅ Page '{p['page_title']}' uploaded.")
                else:
                    aid = add_assignment(canvas_domain, course_id, p["page_title"], html_result, canvas_token)
                    if aid and add_to_module(canvas_domain, course_id, mid, "Assignment", aid, p["page_title"], canvas_token):
                        st.success(f"✅ Assignment '{p['page_title']}' uploaded.")

# ------------------------------ New Quizzes -----------------------------------
with tab_quizzes:
    st.header("2) New Quizzes — duplicate template & insert content")
    require_canvas_ready()

    if not st.session_state.new_quiz_templates:
        st.session_state.new_quiz_templates = list_new_quiz_assignments(canvas_domain, course_id, canvas_token)

    tmpl_map = {f"[TEMPLATE] {q['title']} (quiz_id: {q['id']}, assignment_id: {q.get('assignment_id') or '—'})": q for q in st.session_state.new_quiz_templates}
    tmpl_label = st.selectbox("Select a New Quiz as template (keeps its settings)", options=list(tmpl_map.keys()) or ["— none found —"])
    template_choice = tmpl_map.get(tmpl_label)

    quiz_pages = [p for p in st.session_state.pages if p.get("page_type") == "quiz"]
    if not quiz_pages:
        st.info("No quiz pages parsed yet (Pages tab).")
    else:
        idxs = [p["index"] for p in quiz_pages]
        labels = [f'{p["index"]+1}. {p["page_title"]}' for p in quiz_pages]
        sel = st.multiselect("Select quiz pages to process", options=idxs, default=idxs, format_func=lambda i: labels[idxs.index(i)])

        c1, c2 = st.columns(2)
        with c1:
            ins_desc = st.checkbox("Insert description HTML", value=True)
        with c2:
            ins_qs = st.checkbox("Insert questions", value=True)

        if st.button("Duplicate template and populate selected quizzes", type="primary", use_container_width=True, disabled=not template_choice):
            mod_cache = {}
            # Load more complete settings for template
            def _tmpl_settings(t: dict) -> dict:
                info = get_new_quiz(canvas_domain, course_id, str(t["id"]), canvas_token)
                return info or t.get("settings") or {}

            base_settings = _tmpl_settings(template_choice) if template_choice else {}

            for p in quiz_pages:
                if p["index"] not in sel: continue
                bundle = st.session_state.gpt_results.get(p["index"], {})
                html_desc = (bundle.get("quiz_json", {}) or {}).get("quiz_description") or bundle.get("html", "") or ""
                quiz_json = bundle.get("quiz_json") or {}

                # 1) Try true clone
                new_qid = clone_new_quiz_if_supported(canvas_domain, course_id, str(template_choice["id"]), canvas_token) if template_choice else None
                if not new_qid:
                    new_qid = create_new_quiz_from_template(canvas_domain, course_id, canvas_token, base_settings, p["page_title"], (html_desc if ins_desc else base_settings.get("instructions") or ""))
                    if not new_qid:
                        st.error(f"❌ Could not create quiz for '{p['page_title']}'.")
                        continue
                else:
                    # Patch clone’s title/instructions
                    if ins_desc:
                        patch_new_quiz(canvas_domain, course_id, new_qid, canvas_token, title=p["page_title"], instructions_html=html_desc)
                    else:
                        patch_new_quiz(canvas_domain, course_id, new_qid, canvas_token, title=p["page_title"])

                # 2) Insert questions
                if ins_qs and isinstance(quiz_json, dict) and quiz_json.get("questions"):
                    add_items_from_quiz_json(canvas_domain, course_id, str(new_qid), canvas_token, quiz_json)

                # 3) Add to module
                mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, mod_cache)
                if mid and add_to_module(canvas_domain, course_id, mid, "Assignment", new_qid, p["page_title"], canvas_token):
                    st.success(f"✅ '{p['page_title']}' duplicated & populated → module '{p['module_name']}'")
                else:
                    st.success(f"✅ '{p['page_title']}' duplicated & populated")

# ------------------------------- Discussions ----------------------------------
with tab_discussions:
    st.header("3) Discussions — duplicate template & insert content")
    require_canvas_ready()

    if not st.session_state.discussion_templates:
        st.session_state.discussion_templates = list_discussions(canvas_domain, course_id, canvas_token)

    dmap = {f"[TEMPLATE] {d['title']} (id: {d['id']})": d for d in st.session_state.discussion_templates}
    dlabel = st.selectbox("Select a Discussion as template (optional)", options=["(Create fresh)"] + list(dmap.keys()))
    dtemplate = dmap.get(dlabel)

    disc_pages = [p for p in st.session_state.pages if p.get("page_type") == "discussion"]
    if not disc_pages:
        st.info("No discussion pages parsed yet (Pages tab).")
    else:
        idxs = [p["index"] for p in disc_pages]
        labels = [f'{p["index"]+1}. {p["page_title"]}' for p in disc_pages]
        sel = st.multiselect("Select discussion pages to upload", options=idxs, default=idxs, format_func=lambda i: labels[idxs.index(i)])

        if st.button("Upload selected discussions", type="primary"):
            mod_cache = {}
            for p in disc_pages:
                if p["index"] not in sel: continue
                bundle = st.session_state.gpt_results.get(p["index"], {})
                html_msg = bundle.get("html", "")
                if not html_msg:
                    st.warning(f"No HTML for '{p['page_title']}'. Visualize first.")
                    continue
                did = create_discussion_from_template(canvas_domain, course_id, canvas_token, (dtemplate or {}).get("settings", dtemplate), p["page_title"], html_msg)
                if not did:
                    st.error(f"❌ Discussion create failed for '{p['page_title']}'.")
                    continue
                mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, mod_cache)
                if mid and add_to_module(canvas_domain, course_id, mid, "Discussion", did, p["page_title"], canvas_token):
                    st.success(f"✅ Discussion '{p['page_title']}' uploaded & added to module.")
                else:
                    st.success(f"✅ Discussion '{p['page_title']}' uploaded.")
