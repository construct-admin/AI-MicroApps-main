# canvas_import_um.py
# -----------------------------------------------------------------------------
# 📄 DOCX / Google Doc → GPT (KB) → Canvas (Pages / New Quizzes)
# Focus: Quizzes panel duplicates a selected New Quiz template, reads the new
# assignment_id after the duplication, and populates it with storyboard+KB
# content. You can choose which elements to insert (description, questions).
# -----------------------------------------------------------------------------

from __future__ import annotations

import json
import re
import uuid
from io import BytesIO
from typing import Dict, List, Optional

import requests
import streamlit as st
from docx import Document
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from openai import OpenAI

# =========================== App & State =====================================

st.set_page_config(page_title="Canvas Import (Pages + New Quizzes)", layout="wide")
st.title("Canvas Import (Pages + New Quizzes)")

def _init_state():
    defaults = dict(
        pages=[],                 # [{index, raw, page_type, page_title, module_name, template_type}]
        gpt={},                   # {page_index: {"html": "...", "quiz_json": {...}}}
        vector_store_id=None,     # OpenAI Vector Store for template KB
        duplicated_map={},        # {page_index: new_assignment_id}
    )
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

# ======================= Common Inputs (left sidebar) ========================

with st.sidebar:
    st.header("Canvas & OpenAI")

    canvas_domain = st.text_input("Canvas Domain", placeholder="umich.instructure.com")
    course_id     = st.text_input("Course ID")
    canvas_token  = st.text_input("Canvas API Token", type="password")

    openai_api_key = st.text_input("OpenAI API Key (can be in secrets)", type="password",
                                   value=st.secrets.get("OPENAI_API_KEY", ""))

    st.divider()
    st.header("Template Knowledge Base")
    vs_existing = st.text_input("Vector Store ID (optional)", value=st.session_state.get("vector_store_id") or "")
    kb_docx = st.file_uploader("Upload template DOCX (optional)", type=["docx"])
    kb_gdoc = st.text_input("Template Google Doc URL (optional)")
    sa_json_template = st.file_uploader("Service Account JSON (for Template GDoc)", type=["json"])

    col_kb1, col_kb2 = st.columns(2)
    with col_kb1:
        if st.button("Create Vector Store"):
            _client = OpenAI(api_key=openai_api_key)
            vs = _client.vector_stores.create(name="umich_canvas_templates")
            st.session_state.vector_store_id = vs.id
            st.success(f"Vector Store created: {vs.id}")
    with col_kb2:
        if st.button("Use existing VS"):
            if vs_existing.strip():
                st.session_state.vector_store_id = vs_existing.strip()
                st.success(f"Using VS: {st.session_state.vector_store_id}")
            else:
                st.error("Paste a Vector Store ID first.")

    if st.session_state.vector_store_id:
        if st.button("Upload template to KB"):
            got = None
            if kb_docx:
                got = (BytesIO(kb_docx.getvalue()), kb_docx.name)
            elif kb_gdoc and sa_json_template:
                fid = _gdoc_id_from_url(kb_gdoc)
                if fid:
                    try:
                        data = _fetch_docx_from_gdoc(fid, sa_json_template.read())
                        got = (data, "template.docx")
                    except Exception as e:
                        st.error(f"Template GDoc fetch failed: {e}")
            if got:
                _client = OpenAI(api_key=openai_api_key)
                data, fname = got
                f = _client.files.create(file=(fname, data), purpose="assistants")
                _client.vector_stores.files.create(vector_store_id=st.session_state.vector_store_id, file_id=f.id)
                st.success("Uploaded to KB.")
            else:
                st.warning("Provide a template .docx or Template Google Doc URL + SA JSON.")

# ============================ Utilities ======================================

def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json", "Accept": "application/json"}

def _require_canvas_ready():
    if not (canvas_domain and course_id and canvas_token):
        st.error("Enter Canvas Domain, Course ID and Token in the sidebar.")
        st.stop()

def ensure_openai() -> OpenAI:
    if not openai_api_key:
        st.error("OpenAI API key required.")
        st.stop()
    return OpenAI(api_key=openai_api_key)

def _gdoc_id_from_url(url: str) -> Optional[str]:
    if not url: return None
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url) or re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None

def _fetch_docx_from_gdoc(file_id: str, sa_json_bytes: bytes) -> BytesIO:
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

# ===================== Storyboard parsing (DOCX/GDoc) ========================

def extract_canvas_pages(docx_file_like) -> List[str]:
    """Return strings of blocks between <canvas_page>...</canvas_page> (case-insensitive)."""
    doc = Document(docx_file_like)
    pages, cur, inside = [], [], False
    for p in doc.paragraphs:
        t = (p.text or "").strip()
        low = t.lower()
        if "<canvas_page>" in low:
            inside = True
            cur = [t]
            continue
        if "</canvas_page>" in low:
            cur.append(t)
            pages.append("\n".join(cur))
            inside = False
            continue
        if inside:
            cur.append(t)
    return pages

def extract_tag(tag: str, block: str) -> str:
    m = re.search(fr"<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""

# ========================= Canvas: modules & pages ===========================

def get_or_create_module(module_name: str, domain: str, course: str, token: str, cache: Dict) -> Optional[str]:
    if module_name in cache:
        return cache[module_name]
    url = f"https://{domain}/api/v1/courses/{course}/modules"
    r = requests.get(url, headers=_headers(token), timeout=60)
    if r.status_code == 200:
        for m in r.json():
            if m.get("name", "").strip().lower() == module_name.strip().lower():
                cache[module_name] = str(m["id"])
                return cache[module_name]
    r2 = requests.post(url, headers=_headers(token), json={"module": {"name": module_name, "published": True}}, timeout=60)
    if r2.status_code in (200, 201):
        mid = str(r2.json().get("id"))
        cache[module_name] = mid
        return mid
    st.error(f"Module create/find failed: {r2.status_code} | {r2.text[:300]}")
    return None

def add_to_module(domain: str, course: str, module_id: str, item_type: str, ref: str, title: str, token: str) -> bool:
    url = f"https://{domain}/api/v1/courses/{course}/modules/{module_id}/items"
    payload = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = ref
    else:
        payload["module_item"]["content_id"] = ref
    r = requests.post(url, headers=_headers(token), json=payload, timeout=60)
    return r.status_code in (200, 201)

# ============================ New Quizzes (LTI) ==============================

def list_new_quiz_assignments(domain: str, course: str, token: str) -> List[Dict]:
    """
    Returns list of New Quiz entries with BOTH ids:
      {"title", "quiz_id", "assignment_id"}
    """
    url = f"https://{domain}/api/quiz/v1/courses/{course}/quizzes"
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

    items = []
    if isinstance(data, dict):
        items = data.get("quizzes") or data.get("data") or []
    elif isinstance(data, list):
        items = data

    out = []
    for q in items or []:
        if not isinstance(q, dict): continue
        quiz_id = str(q.get("id")) if q.get("id") is not None else None
        asg_id  = str(q.get("assignment_id")) if q.get("assignment_id") is not None else None
        title   = q.get("title") or q.get("name") or f"Quiz {quiz_id or asg_id}"
        if quiz_id or asg_id:
            out.append({"title": title, "quiz_id": quiz_id, "assignment_id": asg_id})
    return out

def clone_new_quiz(domain: str, course: str, *, quiz_id: Optional[str], assignment_id: Optional[str], token: str) -> Optional[str]:
    """Try clone by quiz_id then by assignment_id. Return new assignment_id if successful."""
    h = _headers(token)

    if quiz_id:
        u = f"https://{domain}/api/quiz/v1/courses/{course}/quizzes/{quiz_id}/clone"
        r = requests.post(u, headers=h, json={"new_quiz": {}}, timeout=60)
        if r.status_code in (200, 201):
            d = r.json()
            return str(d.get("assignment_id") or d.get("id"))
        st.warning(f"Clone by quiz_id failed: {r.status_code} | {r.text[:300]}")

    if assignment_id:
        u = f"https://{domain}/api/quiz/v1/courses/{course}/quizzes/{assignment_id}/clone"
        r = requests.post(u, headers=h, json={"new_quiz": {}}, timeout=60)
        if r.status_code in (200, 201):
            d = r.json()
            return str(d.get("assignment_id") or d.get("id"))
        st.warning(f"Clone by assignment_id failed: {r.status_code} | {r.text[:300]}")

    return None

def add_new_quiz(domain: str, course: str, title: str, description_html: str, token: str, points_possible: int = 1) -> Optional[str]:
    """Create a fresh New Quiz; return its assignment_id (or id)."""
    url = f"https://{domain}/api/quiz/v1/courses/{course}/quizzes"
    r = requests.post(url, headers=_headers(token),
                      json={"quiz": {"title": title, "points_possible": max(points_possible, 1), "instructions": description_html or ""}},
                      timeout=60)
    if r.status_code in (200, 201):
        d = r.json()
        return str(d.get("assignment_id") or d.get("id"))
    st.error(f"New Quiz create failed: {r.status_code} | {r.text[:300]}")
    return None

def add_new_quiz_mcq(domain: str, course: str, assignment_id: str, q: Dict, token: str, position: int = 1):
    """Create a choice item with per-answer and per-question feedback and per-question shuffle."""
    url = f"https://{domain}/api/quiz/v1/courses/{course}/quizzes/{assignment_id}/items"

    # choices
    choices, answer_fb, correct_id = [], {}, None
    for i, ans in enumerate(q.get("answers", []), start=1):
        cid = str(uuid.uuid4())
        choices.append({"id": cid, "position": i, "itemBody": f"<p>{ans.get('text','')}</p>"})
        if ans.get("is_correct"): correct_id = cid
        if ans.get("feedback"):   answer_fb[cid] = ans["feedback"]
    if not choices: return
    if not correct_id: correct_id = choices[0]["id"]

    shuffle = bool(q.get("shuffle", False))
    props = {"shuffleRules": {"choices": {"toLock": [], "shuffled": shuffle}}, "varyPointsByAnswer": False}

    qfb = q.get("feedback") or {}
    feedback_block = {}
    if qfb.get("correct"):   feedback_block["correct"] = qfb["correct"]
    if qfb.get("incorrect"): feedback_block["incorrect"] = qfb["incorrect"]
    if qfb.get("neutral"):   feedback_block["neutral"] = qfb["neutral"]

    entry = {
        "interaction_type_slug": "choice",
        "title": q.get("question_name") or "Question",
        "item_body": q.get("question_text") or "",
        "calculator_type": "none",
        "interaction_data": {"choices": choices},
        "properties": props,
        "scoring_data": {"value": correct_id},
        "scoring_algorithm": "Equivalence",
    }
    if feedback_block:     entry["feedback"] = feedback_block
    if answer_fb:          entry["answer_feedback"] = answer_fb

    payload = {"item": {"entry_type": "Item", "points_possible": 1, "position": position, "entry": entry}}
    r = requests.post(url, headers=_headers(token), json=payload, timeout=60)
    if r.status_code not in (200, 201):
        st.warning(f"Add item failed: {r.status_code} | {r.text[:300]}")

# ========================== GPT (KB) HTML/Quiz GEN ===========================

SYSTEM = (
    "You are an expert Canvas HTML generator.\n"
    "Use the file_search tool to find the exact uMich template by name/structure.\n"
    "STRICT TEMPLATE RULES:\n"
    "- Reproduce template HTML verbatim (do NOT change/remove classes/attributes/data-*).\n"
    "- Preserve all <img> tags exactly (including data-api-* attributes, width/height).\n"
    "- Replace text inside content areas; if a section has no storyboard content, you may remove it.\n"
    "- If storyboard has a section not in template, add the content to the page as is, preserving all formatting.\n"
    "- Convert any tables into proper <table><tr><td> markup (no loss of data).\n"
    "- Keep .bluePageHeader, .header, .divisionLineYellow, .landingPageFooter intact.\n\n"
    "QUIZ RULES (when <page_type> is 'quiz'):\n"
    "- Questions are between <quiz_start> and </quiz_end>.\n"
    "- <multiple_choice> uses '*' prefix for correct.\n"
    "- If <shuffle> appears inside a question, set \"shuffle\": true; else false.\n"
    "- Optional question feedback tags: <feedback_correct>, <feedback_incorrect>, <feedback_neutral>.\n"
    "- Optional per-answer feedback: '(feedback: ...)' after a choice line.\n\n"
    "RETURN:\n"
    "1) Canvas-ready HTML (no code fences)\n"
    "2) If page_type is 'quiz', append a JSON object at the very END with:\n"
    "{ \"quiz_description\":\"<html>\", \"questions\":[{\"question_name\":\"...\",\"question_text\":\"...\",\n"
    "  \"answers\":[{\"text\":\"A\",\"is_correct\":false,\"feedback\":\"<p>...</p>\"},{\"text\":\"B\",\"is_correct\":true}],\n"
    "  \"shuffle\":true, \"feedback\":{\"correct\":\"<p>...</p>\",\"incorrect\":\"<p>...</p>\",\"neutral\":\"<p>...</p>\"}}]}\n"
    "COVERAGE (NO-DROP): Include every substantive sentence/line from the storyboard in order; if something can’t be\n"
    "mapped, append under <div class=\"divisionLineYellow\"><h2>Additional Content</h2><div>…</div></div>.\n"
)

def generate_for_page(p: Dict, vector_store_id: str) -> Dict:
    """Return {"html": str, "quiz_json": dict|None}"""
    client = ensure_openai()
    user_prompt = (
        f'Use template_type="{p.get("template_type") or "auto"}" if it matches; else best fit.\n\n'
        "Storyboard page block:\n" + p["raw"]
    )

    resp = client.responses.create(
        model="gpt-4o",
        input=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_prompt}],
        tools=[{"type": "file_search", "vector_store_ids": [vector_store_id]}],
    )
    out = (resp.output_text or "").strip()
    cleaned = re.sub(r"```(html|json)?", "", out, flags=re.IGNORECASE).strip()
    quiz_json, html_part = None, cleaned

    m = re.search(r"({[\s\S]+})\s*$", cleaned)
    if m and p.get("page_type") == "quiz":
        try:
            quiz_json = json.loads(m.group(1))
            html_part = cleaned[:m.start()].strip()
        except Exception:
            quiz_json = None
    return {"html": html_part, "quiz_json": quiz_json}

# ================================ UI: Tabs ===================================

tab_pages, tab_quizzes = st.tabs(["Pages", "New Quizzes"])

# ------------------------------ Pages Tab ------------------------------------

with tab_pages:
    st.subheader("1) Pages — parse storyboard, visualize with GPT")

    colp1, colp2 = st.columns([1, 1])
    with colp1:
        sb_file = st.file_uploader("Storyboard (.docx)", type="docx")
        sb_gdoc = st.text_input("OR: Storyboard Google Doc URL (optional)")
        sa_json_story = st.file_uploader("Service Account JSON (for Storyboard GDoc)", type=["json"])

    with colp2:
        if st.button("Parse storyboard → pages"):
            st.session_state.pages.clear()
            st.session_state.gpt.clear()

            source = None
            if sb_file:
                source = BytesIO(sb_file.getvalue())
            elif sb_gdoc and sa_json_story:
                fid = _gdoc_id_from_url(sb_gdoc)
                if fid:
                    try:
                        source = _fetch_docx_from_gdoc(fid, sa_json_story.read())
                    except Exception as e:
                        st.error(f"Storyboard GDoc fetch failed: {e}")
            if not source:
                st.error("Upload a storyboard .docx OR provide GDoc URL + Service Account JSON.")
                st.stop()

            blocks = extract_canvas_pages(source)
            last_module = None
            for i, b in enumerate(blocks):
                page_type = (extract_tag("page_type", b).lower() or "page").strip()
                page_title = extract_tag("page_title", b) or f"Page {i+1}"
                module_name = extract_tag("module_name", b).strip()
                if not module_name:
                    h1 = re.search(r"<h1>(.*?)</h1>", b, flags=re.I | re.S)
                    if h1: module_name = h1.group(1).strip()
                if not module_name:
                    m = re.search(r"\b(Module\s+[A-Za-z0-9 ]+)", page_title, flags=re.I)
                    if m: module_name = m.group(1).strip()
                if not module_name: module_name = last_module or "General"
                last_module = module_name

                st.session_state.pages.append(dict(
                    index=i, raw=b, page_type=page_type, page_title=page_title,
                    module_name=module_name, template_type=extract_tag("template_type", b)
                ))
            st.success(f"Parsed {len(st.session_state.pages)} page(s).")

    if st.session_state.pages:
        st.caption("Edit titles/types/modules as needed.")
        for p in st.session_state.pages:
            with st.expander(f"{p['index']+1}. {p['page_title']}  [{p['page_type']}]  — Module: {p['module_name']}", expanded=False):
                c1, c2, c3, c4 = st.columns([1.2, .8, 1, 1])
                p["page_title"] = c1.text_input("Page title", value=p["page_title"], key=f"title_{p['index']}")
                p["page_type"]  = c2.selectbox("Type", ["page","quiz"], index=["page","quiz"].index(p["page_type"]),
                                               key=f"type_{p['index']}")
                p["module_name"]= c3.text_input("Module", value=p["module_name"], key=f"mod_{p['index']}")
                p["template_type"] = c4.text_input("Template type (optional)", value=p.get("template_type",""),
                                                   key=f"tmpl_{p['index']}")

        st.divider()
        cols = st.columns([1,1,1])
        with cols[0]:
            if st.button("Visualize ALL pages with GPT (uses KB)"):
                if not st.session_state.vector_store_id:
                    st.error("Create/Select a Vector Store and upload the template first.")
                    st.stop()
                with st.spinner("Generating..."):
                    for p in st.session_state.pages:
                        st.session_state.gpt[p["index"]] = generate_for_page(p, st.session_state.vector_store_id)
                st.success("Visualization complete. Check New Quizzes tab for quiz pages.")
        with cols[1]:
            quiz_idxs = [p["index"] for p in st.session_state.pages if p["page_type"] == "quiz"]
            if quiz_idxs:
                pick = st.multiselect("Only visualize selected quiz pages", quiz_idxs, quiz_idxs)
                if st.button("Visualize selected quizzes"):
                    if not st.session_state.vector_store_id:
                        st.error("Create/Select a Vector Store and upload the template first.")
                        st.stop()
                    with st.spinner("Generating quizzes..."):
                        for i in pick:
                            p = next(pp for pp in st.session_state.pages if pp["index"] == i)
                            st.session_state.gpt[p["index"]] = generate_for_page(p, st.session_state.vector_store_id)
                    st.success("Selected quizzes generated.")

# ----------------------------- New Quizzes Tab -------------------------------

with tab_quizzes:
    st.subheader("2) New Quizzes — duplicate template & insert content")
    _require_canvas_ready()

    # Load available templates (New Quizzes in course)
    templates = list_new_quiz_assignments(canvas_domain, course_id, canvas_token)
    if not templates:
        st.info("No New Quizzes found in this course (or LTI API not available). You can still create fresh quizzes.", icon="ℹ️")

    labels = []
    idx_map = {}
    for t in templates:
        label = f"{t['title']}  (quiz_id: {t.get('quiz_id') or '—'}, assignment_id: {t.get('assignment_id') or '—'})"
        labels.append(label)
        idx_map[label] = t

    chosen_label = st.selectbox("Select a New Quiz to use as the **template** (keeps its settings)",
                                options=["(Create fresh each time)"] + labels)
    chosen_template = idx_map.get(chosen_label) if chosen_label != "(Create fresh each time)" else None

    # Which storyboard pages are quizzes?
    quiz_pages = [p for p in st.session_state.pages if p.get("page_type") == "quiz"]
    if not quiz_pages:
        st.info("No quiz pages parsed. Go to Pages tab first.", icon="ℹ️")
        st.stop()

    q_idx_list = [p["index"] for p in quiz_pages]
    q_labels   = [f"{p['index']+1}. {p['page_title']}" for p in quiz_pages]
    selected_q = st.multiselect("Select quiz pages to process", options=q_idx_list, default=q_idx_list,
                                format_func=lambda i: q_labels[q_idx_list.index(i)])

    ins_desc   = st.checkbox("Insert description HTML", value=True)
    ins_qs     = st.checkbox("Insert questions", value=True)

    st.caption("Tip: Generate content in the Pages tab first (to minimize token usage here).")

    if st.button("Duplicate template and populate selected quizzes"):
        if not (ins_desc or ins_qs):
            st.warning("Select at least one element to insert.")
            st.stop()
        with st.spinner("Duplicating & populating..."):
            mod_cache = {}
            for p in quiz_pages:
                if p["index"] not in selected_q:
                    continue

                # Ensure we have generated content
                bundle = st.session_state.gpt.get(p["index"])
                if not bundle:
                    if not st.session_state.vector_store_id:
                        st.error(f"Quiz '{p['page_title']}' has no generated content and KB not ready.")
                        continue
                    bundle = generate_for_page(p, st.session_state.vector_store_id)
                    st.session_state.gpt[p["index"]] = bundle

                html_desc = bundle.get("html", "") if ins_desc else ""
                qjson     = bundle.get("quiz_json", {}) if ins_qs else {}

                # 1) Duplicate selected template (or create fresh)
                new_assignment_id: Optional[str] = None
                if chosen_template:
                    new_assignment_id = clone_new_quiz(
                        canvas_domain, course_id,
                        quiz_id=chosen_template.get("quiz_id"),
                        assignment_id=chosen_template.get("assignment_id"),
                        token=canvas_token
                    )
                    if not new_assignment_id:
                        st.info("Template clone not available; creating fresh New Quiz instead.")
                if not new_assignment_id:
                    new_assignment_id = add_new_quiz(canvas_domain, course_id, p["page_title"], html_desc, canvas_token)
                if not new_assignment_id:
                    st.error(f"Could not create/clone quiz for '{p['page_title']}'. Skipping.")
                    continue

                # 2) If we cloned but also want to overwrite description, patch it now
                if ins_desc and html_desc:
                    try:
                        url = f"https://{canvas_domain}/api/quiz/v1/courses/{course_id}/quizzes/{new_assignment_id}"
                        requests.patch(url, headers=_headers(canvas_token),
                                       json={"quiz": {"title": p["page_title"], "instructions": html_desc}}, timeout=60)
                    except Exception:
                        pass
                else:
                    # still ensure title matches storyboard
                    try:
                        url = f"https://{canvas_domain}/api/quiz/v1/courses/{course_id}/quizzes/{new_assignment_id}"
                        requests.patch(url, headers=_headers(canvas_token),
                                       json={"quiz": {"title": p["page_title"]}}, timeout=60)
                    except Exception:
                        pass

                # 3) Insert questions
                if ins_qs and isinstance(qjson, dict):
                    for pos, q in enumerate(qjson.get("questions", []), start=1):
                        if q.get("answers"):
                            add_new_quiz_mcq(canvas_domain, course_id, new_assignment_id, q, canvas_token, position=pos)

                # 4) Add to module
                mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, mod_cache)
                if mid and add_to_module(canvas_domain, course_id, mid, "Assignment", new_assignment_id, p["page_title"], canvas_token):
                    st.success(f"✅ '{p['page_title']}' duplicated & populated → module '{p['module_name']}'")
                else:
                    st.warning(f"Uploaded but not added to module (module problem?) for '{p['page_title']}'.")

