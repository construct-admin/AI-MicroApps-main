# canvas_import_app.py
# -----------------------------------------------------------------------------
# 📄 DOCX/Google Doc → GPT (optional KB) → Canvas (Pages / New Quizzes / Discussions)
#  - Step-by-step tabs to keep token usage small
#  - Optional Vector Store "KB" (OpenAI file_search) for template fidelity
#  - New Quizzes: optional "duplicate template assignment" → insert content + questions → rename
#  - Preserves storyboard links, images, and tables (model is instructed not to drop anything)
# -----------------------------------------------------------------------------

from __future__ import annotations

import re
import time
import json
import uuid
from io import BytesIO
from typing import Any, Dict, List, Optional

import requests
import streamlit as st
from docx import Document
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# If you use OpenAI KB features, install openai>=1.40.0
try:
    from openai import OpenAI
except Exception:
    OpenAI = None  # type: ignore

# --------------------------- Streamlit page setup -----------------------------
st.set_page_config(page_title="Canvas Importer (Pages • New Quizzes • Discussions)", layout="wide")
st.title("Canvas Importer — Pages • New Quizzes • Discussions")

# ----------------------------- Session defaults ------------------------------
if "pages" not in st.session_state:
    st.session_state.pages = []  # [{index, raw, page_type, page_title, module_name, template_type}]
if "gpt_results" not in st.session_state:
    st.session_state.gpt_results = {}  # idx -> {"html":..., "quiz_json":...}
if "visualized" not in st.session_state:
    st.session_state.visualized = False

# ----------------------------- Sidebar inputs --------------------------------
with st.sidebar:
    st.header("Canvas / OpenAI")
    canvas_domain = st.text_input("Canvas Domain", placeholder="umich.instructure.com")
    course_id = st.text_input("Course ID", placeholder="123456")
    canvas_token = st.text_input("Canvas Token", type="password")

    st.divider()
    st.caption("OpenAI (for visualization)")
    openai_api_key = st.text_input("OpenAI API Key", type="password", help="Put in Streamlit secrets if you prefer.")
    vector_store_id = st.text_input("Vector Store ID (optional KB)", help="If provided, GPT will use file_search.")
    st.caption("Tip: upload your template DOCX(s) into this vector store beforehand.")

    st.divider()
    st.caption("Google Docs (optional; for pulling storyboard)")
    gdoc_url = st.text_input("Storyboard Google Doc URL (optional)")
    sa_json = st.file_uploader("Service Account JSON (optional)", type=["json"])

# --------------------------- Utility / API helpers ---------------------------
def require_canvas_ready():
    if not (canvas_domain and course_id and canvas_token):
        st.error("Enter Canvas Domain, Course ID, and Token in the sidebar.")
        st.stop()

def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

def _retry(fn, *args, **kwargs):
    """Tiny retry wrapper for rate limits / flakiness."""
    delays = [0, 1.5, 3.5]
    for i, d in enumerate(delays, start=1):
        try:
            return fn(*args, **kwargs)
        except Exception as e:
            if i == len(delays):
                raise
            time.sleep(d)

# --------------------------- Google Drive helpers ----------------------------
def _gdoc_id_from_url(url: str) -> Optional[str]:
    if not url:
        return None
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
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

# ------------------------------ Storyboard parse -----------------------------
def extract_canvas_pages(storyboard_docx_file) -> List[str]:
    """
    Pull everything between <canvas_page> ... </canvas_page> from a .docx.
    Keeps raw inner text (including any inline HTML the doc might contain).
    """
    if storyboard_docx_file is None:
        return []
    doc = Document(storyboard_docx_file)
    pages, block, inside = [], [], False
    for p in doc.paragraphs:
        text = p.text
        low = text.lower()
        if "<canvas_page>" in low:
            inside, block = True, [text]
            continue
        if "</canvas_page>" in low:
            block.append(text)
            pages.append("\n".join(block))
            inside = False
            continue
        if inside:
            block.append(text)
    return pages

def extract_tag(tag: str, block: str) -> str:
    m = re.search(fr"<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL | re.IGNORECASE)
    return (m.group(1) if m else "").strip()

# ------------------------------- Canvas: modules -----------------------------
def get_or_create_module(module_name: str, domain: str, course_id: str, token: str, cache: Dict[str, int]) -> Optional[int]:
    if module_name in cache:
        return cache[module_name]
    url = f"https://{domain}/api/v1/courses/{course_id}/modules"
    r = requests.get(url, headers=_headers(token), timeout=60)
    if r.status_code == 200:
        for m in r.json():
            if m.get("name", "").strip().lower() == module_name.strip().lower():
                cache[module_name] = m["id"]
                return m["id"]
    r = requests.post(url, headers=_headers(token), json={"module": {"name": module_name, "published": True}}, timeout=60)
    if r.status_code in (200, 201):
        mid = r.json().get("id")
        cache[module_name] = mid
        return mid
    st.error(f"❌ Failed to create/find module '{module_name}': {r.status_code} | {r.text}")
    return None

def add_to_module(domain, course_id, module_id, item_type, ref, title, token) -> bool:
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    payload = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = ref
    else:
        payload["module_item"]["content_id"] = ref
    r = requests.post(url, headers=_headers(token), json=payload, timeout=60)
    return r.status_code in (200, 201)

# ------------------------------- Canvas: pages -------------------------------
def add_page(domain, course_id, title, html_body, token) -> Optional[str]:
    url = f"https://{domain}/api/v1/courses/{course_id}/pages"
    payload = {"wiki_page": {"title": title, "body": html_body, "published": True}}
    r = requests.post(url, headers=_headers(token), json=payload, timeout=60)
    if r.status_code in (200, 201):
        return r.json().get("url")
    st.error(f"❌ Page create failed: {r.status_code} | {r.text}")
    return None

# ---------------------------- Canvas: discussions ----------------------------
def add_discussion(domain, course_id, title, html_body, token) -> Optional[int]:
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
    payload = {"title": title, "message": html_body, "published": True}
    r = requests.post(url, headers=_headers(token), json=payload, timeout=60)
    if r.status_code in (200, 201):
        return r.json().get("id")
    st.error(f"❌ Discussion create failed: {r.status_code} | {r.text}")
    return None

# ------------------------- Canvas: New Quizzes (LTI) -------------------------
def list_new_quiz_assignments(domain: str, course_id: str, token: str) -> List[Dict]:
    """
    Returns a list of New Quiz objects (assignment-aligned) for template selection.
    Normalizes Canvas variants (dict with 'quizzes' vs list).
    """
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes"
    try:
        r = requests.get(url, headers=_headers(token), timeout=60)
    except Exception:
        return []
    if r.status_code != 200:
        return []
    try:
        data = r.json()
    except Exception:
        return []
    items: List[Dict] = []
    if isinstance(data, dict):
        items = data.get("quizzes") or data.get("data") or []
    elif isinstance(data, list):
        items = data
    results = []
    for q in items or []:
        if not isinstance(q, dict):
            continue
        assignment_id = q.get("assignment_id") or q.get("id")
        title = q.get("title") or q.get("name") or f"Quiz {assignment_id}"
        if assignment_id:
            results.append({"id": q.get("id"), "assignment_id": str(assignment_id), "title": title})
    return results

def add_new_quiz(domain, course_id, title, instructions_html, token, points_possible=1) -> Optional[str]:
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes"
    payload = {"quiz": {"title": title, "points_possible": max(points_possible, 1), "instructions": instructions_html or ""}}
    r = requests.post(url, headers=_headers(token), json=payload, timeout=60)
    if r.status_code in (200, 201):
        data = r.json()
        return str(data.get("assignment_id") or data.get("id"))
    st.error(f"❌ New Quiz create failed: {r.status_code} | {r.text}")
    return None

def clone_new_quiz(domain, course_id, src_assignment_id, token) -> Optional[str]:
    """
    Try to clone a New Quiz assignment to preserve settings.
    Attempts LTI clone endpoint; falls back to None on 404.
    """
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{src_assignment_id}/clone"
    r = requests.post(url, headers=_headers(token), json={"new_quiz": {}}, timeout=60)
    if r.status_code in (200, 201):
        data = r.json()
        return str(data.get("assignment_id") or data.get("id"))
    # many tenants return 404; we'll let caller fall back to create
    st.warning(f"Clone failed: {r.status_code} | {r.text[:400]}")
    return None

def add_new_quiz_mcq(domain, course_id, assignment_id, q: Dict, token, position: int = 1):
    """
    Create MCQ item with per-answer and per-question feedback + shuffle.
    """
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    choices = []
    answer_feedback = {}
    correct_choice_id = None
    for idx, ans in enumerate(q.get("answers", []), start=1):
        cid = str(uuid.uuid4())
        choices.append({"id": cid, "position": idx, "itemBody": f"<p>{ans.get('text','')}</p>"})
        if ans.get("is_correct"):
            correct_choice_id = cid
        if ans.get("feedback"):
            answer_feedback[cid] = ans["feedback"]
    if not choices:
        return
    if not correct_choice_id:
        correct_choice_id = choices[0]["id"]

    shuffle = bool(q.get("shuffle", False))
    properties = {"shuffleRules": {"choices": {"toLock": [], "shuffled": shuffle}}, "varyPointsByAnswer": False}
    fb = q.get("feedback") or {}
    feedback_block = {}
    if fb.get("correct"): feedback_block["correct"] = fb["correct"]
    if fb.get("incorrect"): feedback_block["incorrect"] = fb["incorrect"]
    if fb.get("neutral"): feedback_block["neutral"] = fb["neutral"]

    entry = {
        "interaction_type_slug": "choice",
        "title": q.get("question_name") or "Question",
        "item_body": q.get("question_text") or "",
        "calculator_type": "none",
        "interaction_data": {"choices": choices},
        "properties": properties,
        "scoring_data": {"value": correct_choice_id},
        "scoring_algorithm": "Equivalence",
    }
    if feedback_block:
        entry["feedback"] = feedback_block
    if answer_feedback:
        entry["answer_feedback"] = answer_feedback

    payload = {"item": {"entry_type": "Item", "points_possible": 1, "position": position, "entry": entry}}
    r = requests.post(url, headers=_headers(token), json=payload, timeout=60)
    if r.status_code not in (200, 201):
        st.warning(f"⚠️ Add item failed: {r.status_code} | {r.text[:200]}")

# ----------------------------- OpenAI (visualize) ----------------------------
def ensure_openai() -> OpenAI:
    if OpenAI is None:
        st.error("The 'openai' package is not installed. `pip install openai>=1.40.0`")
        st.stop()
    if not openai_api_key:
        st.error("Provide an OpenAI API key in the sidebar.")
        st.stop()
    return OpenAI(api_key=openai_api_key)

SYSTEM_PROMPT = (
    "You are an expert Canvas HTML generator.\n"
    "If file_search is available, use it to find the exact uMich template.\n"
    "STRICT RULES:\n"
    "- Reproduce template HTML verbatim (do NOT remove attributes/classes/data-*).\n"
    "- Preserve all <img> tags exactly.\n"
    "- Replace only inner content (headings, paragraphs, lists). If a section has no content, keep the structure but leave it empty; if extra content exists, append a new section at the end.\n"
    "- If a section does not exist in the template, create it using the same HTML structure and classes used elsewhere.\n"
    "- Convert any table-like content into proper <table><tr><td> markup; keep cell order. Search for table styling in the knowledge base.\n"
    "- Keep .bluePageHeader, .header, .divisionLineYellow, .landingPageFooter intact.\n\n"
    "QUIZ RULES:\n"
    "- Questions are between <quiz_start> and </quiz_end>.\n"
    "- <multiple_choice> uses '*' prefix for correct options.\n"
    "- <shuffle> inside a question means shuffle=true.\n"
    "- Question feedback tags (optional): <feedback_correct>..</feedback_correct>, <feedback_incorrect>..</feedback_incorrect>, <feedback_neutral>..</feedback_neutral>\n"
    "- Per-answer feedback: either '(feedback: ...)' inline or <feedback>A: ...</feedback>.\n\n"
    "RETURN:\n"
    "1) Canvas-ready HTML (no code fences)\n"
    "2) If page_type is 'quiz', append one JSON object at the VERY END:\n"
    "{ \"quiz_description\": \"<html>\", \"questions\": [\n"
    "  {\"question_name\":\"...\",\"question_text\":\"...\",\n"
    "   \"answers\":[{\"text\":\"A\",\"is_correct\":false,\"feedback\":\"<p>...</p>\"}, {\"text\":\"B\",\"is_correct\":true}],\n"
    "   \"shuffle\": true,\n"
    "   \"feedback\": {\"correct\":\"<p>...</p>\",\"incorrect\":\"<p>...</p>\",\"neutral\":\"<p>...</p>\"}\n"
    "  }\n"
    "]}\n\n"
    "COVERAGE (NO-DROP):\n"
    "- Do not omit storyboard content. Preserve order. Keep any explicit <img>, <a>, <table> already present.\n"
)

def run_visualize_for_pages(pages: List[Dict[str, Any]]):
    client = ensure_openai()
    st.session_state.gpt_results.clear()

    for p in pages:
        idx, raw = p["index"], p["raw"]
        user_prompt = (
            f'Use template_type="{p.get("template_type") or "auto"}" when possible; otherwise best-fit.\n\n'
            f"Storyboard page block:\n{raw}"
        )
        # Build request with or without file_search
        if vector_store_id.strip():
            req = {
                "model": "gpt-4o",
                "input": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "tools": [{"type": "file_search", "vector_store_ids": [vector_store_id.strip()]}],
            }
            response = _retry(client.responses.create, **req)
            out = response.output_text or ""
        else:
            # simple chat.completions path (less tokens than stuffing templates)
            req = {
                "model": "gpt-4o",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.2,
            }
            response = _retry(client.chat.completions.create, **req)
            out = response.choices[0].message.content if response and response.choices else ""

        cleaned = re.sub(r"```(html|json)?", "", out or "", flags=re.IGNORECASE).strip()

        quiz_json, html_result = None, cleaned
        if p["page_type"] == "quiz":
            m = re.search(r"({[\s\S]+})\s*$", cleaned)
            if m:
                try:
                    quiz_json = json.loads(m.group(1))
                    html_result = cleaned[: m.start()].strip()
                except Exception:
                    quiz_json = None

        st.session_state.gpt_results[idx] = {"html": html_result, "quiz_json": quiz_json}

# =============================== UI: Tabs ====================================
tab_pages, tab_quizzes, tab_discussions = st.tabs(["Pages", "New Quizzes", "Discussions"])

# ------------------------------- TAB: PAGES ----------------------------------
with tab_pages:
    st.subheader("1) Pages — parse storyboard, visualize with GPT, upload")

    col_upload, col_btn = st.columns([2, 1])
    with col_upload:
        storyboard_file = st.file_uploader("Storyboard (.docx)", type=["docx"], help="Or pull from Google Doc below.")
    with col_btn:
        if st.button("Parse storyboard → pages", use_container_width=True):
            st.session_state.pages.clear()
            st.session_state.gpt_results.clear()
            st.session_state.visualized = False

            source = storyboard_file
            if not source and gdoc_url and sa_json:
                fid = _gdoc_id_from_url(gdoc_url)
                if fid:
                    try:
                        source = fetch_docx_from_gdoc(fid, sa_json.read())
                    except Exception as e:
                        st.error(f"❌ Could not fetch Google Doc: {e}")

            if not source:
                st.error("Upload a .docx or provide a Google Doc + Service Account.")
                st.stop()

            raw_pages = extract_canvas_pages(source)
            last_mod = None
            for i, block in enumerate(raw_pages):
                page_type = (extract_tag("page_type", block) or "page").strip().lower()
                page_title = extract_tag("page_title", block) or f"Page {i+1}"
                module_name = extract_tag("module_name", block) or last_mod or "General"
                tmpl = extract_tag("template_type", block)
                last_mod = module_name
                st.session_state.pages.append({
                    "index": i, "raw": block, "page_type": page_type, "page_title": page_title,
                    "module_name": module_name, "template_type": tmpl,
                })
            st.success(f"✅ Parsed {len(st.session_state.pages)} page(s).")

    if st.session_state.pages:
        st.markdown("**Review pages**")
        for p in st.session_state.pages:
            i = p["index"]
            with st.expander(f"{i+1}. {p['page_title']}  •  type={p['page_type']}  •  module={p['module_name']}", expanded=False):
                a, b, c, d = st.columns([1.2, 0.9, 1, 1])
                with a:
                    p["page_title"] = st.text_input("Title", p["page_title"], key=f"title_{i}")
                with b:
                    p["page_type"] = st.selectbox("Type", ["page", "quiz", "discussion", "assignment"], index=["page","quiz","discussion","assignment"].index(p["page_type"]) if p["page_type"] in ["page","quiz","discussion","assignment"] else 0, key=f"type_{i}")
                with c:
                    p["module_name"] = st.text_input("Module", p["module_name"], key=f"mod_{i}")
                with d:
                    p["template_type"] = st.text_input("Template (optional)", p["template_type"], key=f"tmpl_{i}")

        st.divider()
        col_v1, col_up = st.columns([1, 2])
        with col_v1:
            if st.button("🔎 Visualize (GPT) — builds HTML / quiz JSON", type="primary", use_container_width=True, disabled=not openai_api_key):
                with st.spinner("Generating HTML for selected pages..."):
                    run_visualize_for_pages(st.session_state.pages)
                    st.session_state.visualized = True
                st.success("✅ Visualization complete. See previews below or proceed to New Quizzes / Discussions tabs.")

        if st.session_state.visualized:
            st.divider()
            st.markdown("**Previews** (upload from here only for Page items; Quizzes/Discussions upload in their tabs).")
            require_canvas_ready()
            module_cache = {}
            for p in st.session_state.pages:
                idx = p["index"]
                bundle = st.session_state.gpt_results.get(idx, {})
                html_out = bundle.get("html", "")
                with st.expander(f"Preview: {p['page_title']}  •  {p['page_type']}", expanded=False):
                    st.code(html_out or "[no HTML]", language="html")
                    if p["page_type"] == "page":
                        if st.button(f"Upload page → {p['module_name']}", key=f"upload_page_{idx}"):
                            url = add_page(canvas_domain, course_id, p["page_title"], html_out, canvas_token)
                            if url:
                                mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, module_cache)
                                if mid and add_to_module(canvas_domain, course_id, mid, "Page", url, p["page_title"], canvas_token):
                                    st.success("✅ Page uploaded & added to module.")

# ---------------------------- TAB: NEW QUIZZES --------------------------------
with tab_quizzes:
    st.subheader("2) New Quizzes — duplicate template & insert content")
    require_canvas_ready()

    # Quiz pages parsed
    quiz_pages = [p for p in st.session_state.pages if p.get("page_type") == "quiz"]
    if not quiz_pages:
        st.info("No quiz pages parsed yet. Parse & visualize in the Pages tab first.", icon="ℹ️")
    else:
        templates = list_new_quiz_assignments(canvas_domain, course_id, canvas_token)
        template_map = {f"{t['title']}  (assignment_id: {t['assignment_id']})": t["assignment_id"] for t in templates}

        labels_by_index = {p["index"]: f"{p['index']+1}. {p['page_title']}" for p in quiz_pages}
        selected_q = st.multiselect(
            "Select quiz pages to upload",
            options=[p["index"] for p in quiz_pages],
            default=[p["index"] for p in quiz_pages],
            format_func=lambda i: labels_by_index.get(i, str(i)),
        )

        selected_template = st.selectbox(
            "Optional: select a New Quiz to duplicate (keeps its settings)",
            options=["(Create fresh for each)"] + list(template_map.keys())
        )
        chosen_template_id = template_map.get(selected_template)

        if st.button("Upload selected quizzes", type="primary"):
            with st.spinner("Creating/cloning quizzes and inserting questions..."):
                mod_cache = {}
                for p in quiz_pages:
                    if p["index"] not in selected_q:
                        continue
                    bundle = st.session_state.gpt_results.get(p["index"], {}) or {}
                    html_desc = (bundle.get("quiz_description") or bundle.get("html") or "").strip()
                    qjson = bundle.get("quiz_json") or {}
                    if not html_desc:
                        st.warning(f"Quiz '{p['page_title']}' has no generated HTML. Visualize first.")
                        continue

                    assignment_id: Optional[str] = None
                    if chosen_template_id:
                        assignment_id = clone_new_quiz(canvas_domain, course_id, chosen_template_id, canvas_token)
                        if not assignment_id:
                            st.warning("Template clone not available; creating fresh New Quiz instead.")
                            assignment_id = add_new_quiz(canvas_domain, course_id, p["page_title"], html_desc, canvas_token)
                    else:
                        assignment_id = add_new_quiz(canvas_domain, course_id, p["page_title"], html_desc, canvas_token)

                    if not assignment_id:
                        st.error(f"Could not create/clone New Quiz for '{p['page_title']}'.")
                        continue

                    # Insert questions (MCQ)
                    if isinstance(qjson, dict):
                        for pos, q in enumerate(qjson.get("questions", []), start=1):
                            if q.get("answers"):
                                add_new_quiz_mcq(canvas_domain, course_id, assignment_id, q, canvas_token, position=pos)

                    # Ensure title/instructions (PATCH)
                    try:
                        url = f"https://{canvas_domain}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}"
                        requests.patch(url, headers=_headers(canvas_token),
                                       json={"quiz": {"title": p["page_title"], "instructions": html_desc}}, timeout=60)
                    except Exception:
                        pass

                    # Add to module
                    mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, mod_cache)
                    if mid and add_to_module(canvas_domain, course_id, mid, "Assignment", assignment_id, p["page_title"], canvas_token):
                        st.success(f"✅ '{p['page_title']}' uploaded as New Quiz & added to module.")

# ----------------------------- TAB: DISCUSSIONS -------------------------------
with tab_discussions:
    st.subheader("3) Discussions — create and add to module")
    require_canvas_ready()

    disc_pages = [p for p in st.session_state.pages if p.get("page_type") == "discussion"]
    if not disc_pages:
        st.info("No discussion pages parsed. Parse & visualize in the Pages tab first.", icon="ℹ️")
    else:
        labels_by_index = {p["index"]: f"{p['index']+1}. {p['page_title']}" for p in disc_pages}
        selected_d = st.multiselect(
            "Select discussion pages to upload",
            options=[p["index"] for p in disc_pages],
            default=[p["index"] for p in disc_pages],
            format_func=lambda i: labels_by_index.get(i, str(i)),
        )

        if st.button("Upload selected discussions", type="primary"):
            with st.spinner("Creating discussions..."):
                mod_cache = {}
                for p in disc_pages:
                    if p["index"] not in selected_d:
                        continue
                    bundle = st.session_state.gpt_results.get(p["index"], {}) or {}
                    html_body = (bundle.get("html") or "").strip()
                    if not html_body:
                        st.warning(f"Discussion '{p['page_title']}' has no HTML. Visualize first.")
                        continue
                    did = add_discussion(canvas_domain, course_id, p["page_title"], html_body, canvas_token)
                    if did:
                        mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, mod_cache)
                        if mid and add_to_module(canvas_domain, course_id, mid, "Discussion", did, p["page_title"], canvas_token):
                            st.success(f"✅ Discussion '{p['page_title']}' created & added to module.")
