# canvas_import_um.py
# -----------------------------------------------------------------------------
# 📄 DOCX / Google Doc → GPT (KB) → Canvas
# Panels:
#   1) Pages – parse storyboard, visualize with GPT (uses KB)
#   2) New Quizzes – pick a template quiz, duplicate (clone + poll), then
#      populate the *duplicated* quiz with description + questions
#   3) Discussions – pick a template discussion, duplicate its settings, then
#      populate the new topic with generated (or storyboard) content
#
# Notes:
# - Handles Canvas tenants where /clone returns 202 async; polls the job.
# - If clone truly not available, falls back to “create fresh” + copy common
#   settings from the template (best-effort).
# - “Select elements” checkboxes let you choose to insert description and/or
#   questions (quizzes) or body (discussions).
# - Google Docs inputs are optional. Local .docx works fine.
# - OPENAI_API_KEY can come from st.secrets or sidebar.
# -----------------------------------------------------------------------------

from __future__ import annotations

import json
import re
import time
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

st.set_page_config(page_title="Canvas Import (Pages • New Quizzes • Discussions)", layout="wide")
st.title("Canvas Import (Pages • New Quizzes • Discussions)")

def _init_state():
    defaults = dict(
        pages=[],            # [{index, raw, page_type, page_title, module_name, template_type}]
        gpt={},              # {page_index: {"html": "...", "quiz_json": {...}}}
        vector_store_id=None # OpenAI Vector Store for the template KB
    )
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v
_init_state()

# ======================= Sidebar (Canvas & OpenAI) ===========================

with st.sidebar:
    st.header("Canvas")
    canvas_domain = st.text_input("Domain", placeholder="umich.instructure.com")
    course_id     = st.text_input("Course ID")
    canvas_token  = st.text_input("API Token", type="password")

    st.header("OpenAI")
    openai_api_key = st.text_input(
        "API Key (or use secrets)",
        type="password",
        value=st.secrets.get("OPENAI_API_KEY", "")
    )

    st.header("Template KB (Vector Store)")
    vs_existing = st.text_input("Vector Store ID (optional)", value=st.session_state.get("vector_store_id") or "")
    kb_docx = st.file_uploader("Upload template DOCX (optional)", type=["docx"])
    kb_gdoc = st.text_input("Template Google Doc URL (optional)")
    sa_json_template = st.file_uploader("Service Account JSON (for Template GDoc)", type=["json"])

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Create Vector Store"):
            _cli = OpenAI(api_key=openai_api_key)
            vs = _cli.vector_stores.create(name="umich_canvas_templates")
            st.session_state.vector_store_id = vs.id
            st.success(f"Vector Store: {vs.id}")
    with c2:
        if st.button("Use existing VS"):
            if vs_existing.strip():
                st.session_state.vector_store_id = vs_existing.strip()
                st.success(f"Using VS: {st.session_state.vector_store_id}")
            else:
                st.error("Paste a Vector Store ID first.")

    if st.session_state.vector_store_id and st.button("Upload template to KB"):
        got = None
        if kb_docx:
            got = (BytesIO(kb_docx.getvalue()), kb_docx.name)
        elif kb_gdoc and sa_json_template:
            fid = _gdoc_id_from_url(kb_gdoc)
            if fid:
                try:
                    got = (_fetch_docx_from_gdoc(fid, sa_json_template.read()), "template.docx")
                except Exception as e:
                    st.error(f"Template GDoc fetch failed: {e}")
        if got:
            _cli = OpenAI(api_key=openai_api_key)
            data, fname = got
            f = _cli.files.create(file=(fname, data), purpose="assistants")
            _cli.vector_stores.files.create(vector_store_id=st.session_state.vector_store_id, file_id=f.id)
            st.success("Uploaded to KB.")
        else:
            st.warning("Provide a template .docx OR Template GDoc URL + Service Account JSON.")

# ============================ Helpers ========================================

def _headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

def _require_canvas_ready():
    if not (canvas_domain and course_id and canvas_token):
        st.error("Enter Canvas Domain, Course ID, and API Token in the sidebar first.")
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
            inside, cur = True, [t]
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

# ========================= Modules & Pages (classic) =========================

def get_or_create_module(module_name: str, domain: str, course: str, token: str, cache: Dict) -> Optional[str]:
    if module_name in cache:
        return cache[module_name]
    url = f"https://{domain}/api/v1/courses/{course}/modules"
    r = requests.get(url, headers=_headers(token), timeout=60)
    if r.status_code == 200:
        for m in r.json():
            if m.get("name","").strip().lower() == module_name.strip().lower():
                cache[module_name] = str(m["id"])
                return cache[module_name]
    r2 = requests.post(url, headers=_headers(token),
                       json={"module": {"name": module_name, "published": True}}, timeout=60)
    if r2.status_code in (200, 201):
        cache[module_name] = str(r2.json().get("id"))
        return cache[module_name]
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
    items = data.get("quizzes") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    out = []
    for q in items or []:
        if not isinstance(q, dict): continue
        out.append({
            "title": q.get("title") or q.get("name"),
            "quiz_id": str(q.get("id")) if q.get("id") is not None else None,
            "assignment_id": str(q.get("assignment_id")) if q.get("assignment_id") is not None else None,
        })
    return out

def _get_new_quiz(domain: str, course: str, *, quiz_id: Optional[str], assignment_id: Optional[str], token: str) -> Optional[Dict]:
    h = _headers(token)
    # prefer quiz_id path
    if quiz_id:
        u = f"https://{domain}/api/quiz/v1/courses/{course}/quizzes/{quiz_id}"
        r = requests.get(u, headers=h, timeout=60)
        if r.status_code == 200:
            return r.json()
    if assignment_id:
        u = f"https://{domain}/api/quiz/v1/courses/{course}/quizzes/{assignment_id}"
        r = requests.get(u, headers=h, timeout=60)
        if r.status_code == 200:
            return r.json()
    return None

def clone_new_quiz(domain: str, course: str, *, quiz_id: Optional[str], assignment_id: Optional[str], token: str) -> Optional[str]:
    """
    Try clone by quiz_id and by assignment_id.
    Handle async 202 by polling Location.
    Return NEW assignment_id on success.
    """
    h = _headers(token)

    def _try(path: str) -> Optional[str]:
        r = requests.post(path, headers=h, json={"new_quiz": {}}, timeout=60)
        if r.status_code in (200, 201):
            d = r.json()
            return str(d.get("assignment_id") or d.get("id"))
        if r.status_code == 202:
            # async job — poll Location if provided
            loc = r.headers.get("Location")
            if loc:
                for _ in range(30):  # up to ~30*1s
                    time.sleep(1)
                    rr = requests.get(loc, headers=h, timeout=60)
                    if rr.status_code == 200:
                        d = rr.json()
                        sid = d.get("assignment_id") or d.get("id")
                        if sid: return str(sid)
        # surface diagnostic for the UI (yellow box)
        st.warning(f"Clone failed: {r.status_code} | {r.text[:500]}")
        return None

    # try quiz id
    if quiz_id:
        new_asg = _try(f"https://{domain}/api/quiz/v1/courses/{course}/quizzes/{quiz_id}/clone")
        if new_asg: return new_asg
    # try assignment id
    if assignment_id:
        new_asg = _try(f"https://{domain}/api/quiz/v1/courses/{course}/quizzes/{assignment_id}/clone")
        if new_asg: return new_asg
    return None

def add_new_quiz(domain: str, course: str, title: str, description_html: str, token: str, points_possible: int = 1) -> Optional[str]:
    url = f"https://{domain}/api/quiz/v1/courses/{course}/quizzes"
    r = requests.post(url, headers=_headers(token),
                      json={"quiz": {"title": title, "points_possible": max(points_possible,1),
                                     "instructions": description_html or ""}}, timeout=60)
    if r.status_code in (200, 201):
        d = r.json()
        return str(d.get("assignment_id") or d.get("id"))
    st.error(f"New Quiz create failed: {r.status_code} | {r.text[:500]}")
    return None

def copy_template_settings_to_quiz(domain: str, course: str, template: Dict, new_assignment_id: str, token: str):
    """
    Best-effort: copy a few safe settings fields from template quiz to the new one.
    (Different tenants expose different fields; ignore failures silently.)
    """
    safe = {}
    # examples one might copy if present:
    for k in ("points_possible", "time_limit", "shuffle_answers", "allow_backtracking"):
        if template.get(k) is not None:
            safe[k] = template[k]
    if not safe: return

    try:
        url = f"https://{domain}/api/quiz/v1/courses/{course}/quizzes/{new_assignment_id}"
        requests.patch(url, headers=_headers(token), json={"quiz": safe}, timeout=60)
    except Exception:
        pass

def add_new_quiz_mcq(domain: str, course: str, assignment_id: str, q: Dict, token: str, position: int = 1):
    url = f"https://{domain}/api/quiz/v1/courses/{course}/quizzes/{assignment_id}/items"
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
        "scoring_algorithm": "Equivalence"
    }
    if feedback_block: entry["feedback"] = feedback_block
    if answer_fb:      entry["answer_feedback"] = answer_fb

    payload = {"item": {"entry_type": "Item", "points_possible": 1, "position": position, "entry": entry}}
    r = requests.post(url, headers=_headers(token), json=payload, timeout=60)
    if r.status_code not in (200, 201):
        st.warning(f"Add item failed: {r.status_code} | {r.text[:500]}")

# ============================== Discussions ==================================

def list_discussions(domain: str, course: str, token: str) -> List[Dict]:
    url = f"https://{domain}/api/v1/courses/{course}/discussion_topics"
    r = requests.get(url, headers=_headers(token), timeout=60)
    if r.status_code != 200: return []
    return r.json() if isinstance(r.json(), list) else []

def get_discussion(domain: str, course: str, topic_id: str, token: str) -> Optional[Dict]:
    url = f"https://{domain}/api/v1/courses/{course}/discussion_topics/{topic_id}"
    r = requests.get(url, headers=_headers(token), timeout=60)
    return r.json() if r.status_code == 200 else None

def create_discussion_from_template(domain: str, course: str, template: Dict, title: str, body_html: str, token: str) -> Optional[str]:
    """
    Duplicate-ish: copy common fields from template, then override title/body.
    """
    url = f"https://{domain}/api/v1/courses/{course}/discussion_topics"
    payload = {
        "title": title,
        "message": body_html or template.get("message") or "",
        "published": template.get("published", True),
        "is_announcement": template.get("is_announcement", False),
        "discussion_type": template.get("discussion_type", "side_comment"),
        "delayed_post_at": template.get("delayed_post_at"),
        "require_initial_post": template.get("require_initial_post", False),
        "podcast_has_student_posts": template.get("podcast_has_student_posts", False),
        "pinned": template.get("pinned", False),
        "locked": template.get("locked", False),
        "allow_rating": template.get("allow_rating", False),
        "only_graders_can_rate": template.get("only_graders_can_rate", False),
    }
    r = requests.post(url, headers=_headers(token), json=payload, timeout=60)
    if r.status_code in (200, 201):
        return str(r.json().get("id"))
    st.warning(f"Discussion create failed: {r.status_code} | {r.text[:400]}")
    return None

# ========================== GPT (KB) generation ==============================

SYSTEM = (
    "You are an expert Canvas HTML generator.\n"
    "Use the file_search tool to find the exact uMich template by name/structure.\n"
    "STRICT TEMPLATE RULES:\n"
    "- Reproduce template HTML verbatim (do NOT change/remove classes/attributes/data-*).\n"
    "- Preserve all <img> tags exactly.\n"
    "- Convert any tables to proper <table><tr><td>.\n"
    "- Keep .bluePageHeader, .header, .divisionLineYellow, .landingPageFooter intact.\n\n"
    "QUIZ RULES (when <page_type> is 'quiz'):\n"
    "- Questions between <quiz_start> and </quiz_end>.\n"
    "- <multiple_choice> uses '*' for correct; <shuffle> toggles per-question shuffle.\n"
    "- Optional: <feedback_correct>, <feedback_incorrect>, <feedback_neutral>; per-answer '(feedback: ... )'.\n\n"
    "RETURN:\n"
    "1) Canvas-ready HTML (no code fences)\n"
    "2) If page_type is 'quiz', append a JSON at the very end:\n"
    "{ \"quiz_description\":\"<html>\", \"questions\":[{\"question_name\":\"...\",\"question_text\":\"...\",\n"
    "  \"answers\":[{\"text\":\"A\",\"is_correct\":false,\"feedback\":\"<p>...</p>\"},{\"text\":\"B\",\"is_correct\":true}],\n"
    "  \"shuffle\":true, \"feedback\":{\"correct\":\"<p>...</p>\",\"incorrect\":\"<p>...</p>\",\"neutral\":\"<p>...</p>\"}}]}\n"
    "COVERAGE: include all substantive storyboard content in order. If unmapped, append under an 'Additional Content' block.\n"
)

def generate_for_page(p: Dict, vector_store_id: str) -> Dict:
    cli = ensure_openai()
    user_prompt = f'Use template_type="{p.get("template_type") or "auto"}" if it matches; else best fit.\n\nStoryboard page block:\n{p["raw"]}'
    resp = cli.responses.create(
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

# ================================ UI Tabs ====================================

tab_pages, tab_quizzes, tab_discuss = st.tabs(["Pages", "New Quizzes", "Discussions"])

# -------------------------------- Pages --------------------------------------

with tab_pages:
    st.subheader("1) Pages — parse storyboard, visualize with GPT")
    c1, c2 = st.columns([1,1])
    with c1:
        sb_file = st.file_uploader("Storyboard (.docx)", type=["docx"])
    with c2:
        sb_gdoc = st.text_input("OR: Storyboard Google Doc URL (optional)")
        sa_json_story = st.file_uploader("Service Account JSON (for Storyboard GDoc)", type=["json"])

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
            st.error("Upload a storyboard .docx OR provide a GDoc URL + Service Account JSON.")
            st.stop()

        blocks = extract_canvas_pages(source)
        last_module = None
        for i, b in enumerate(blocks):
            page_type = (extract_tag("page_type", b).lower() or "page").strip()
            page_title = extract_tag("page_title", b) or f"Page {i+1}"
            module_name = extract_tag("module_name", b).strip()
            if not module_name:
                m = re.search(r"<h1>(.*?)</h1>", b, flags=re.I|re.S)
                if m: module_name = m.group(1).strip()
            if not module_name:
                m = re.search(r"\b(Module\s+[A-Za-z0-9 ]+)", page_title, flags=re.I)
                if m: module_name = m.group(1).strip()
            module_name = module_name or last_module or "General"
            last_module = module_name
            st.session_state.pages.append(dict(
                index=i, raw=b, page_type=page_type, page_title=page_title,
                module_name=module_name, template_type=extract_tag("template_type", b)
            ))
        st.success(f"Parsed {len(st.session_state.pages)} page(s).")

    if st.session_state.pages:
        st.caption("Edit page metadata as needed.")
        for p in st.session_state.pages:
            with st.expander(f"{p['index']+1}. {p['page_title']}  [{p['page_type']}] — {p['module_name']}", expanded=False):
                a,b,c,d = st.columns([1.2,.8,1,1])
                p["page_title"]   = a.text_input("Title", value=p["page_title"], key=f"t_{p['index']}")
                p["page_type"]    = b.selectbox("Type", ["page","quiz","discussion"], index=["page","quiz","discussion"].index(p["page_type"]) if p["page_type"] in ["page","quiz","discussion"] else 0, key=f"ty_{p['index']}")
                p["module_name"]  = c.text_input("Module", value=p["module_name"], key=f"m_{p['index']}")
                p["template_type"]= d.text_input("Template (optional)", value=p.get("template_type",""), key=f"tm_{p['index']}")

        st.divider()
        left, right = st.columns([1,1])
        with left:
            if st.button("Visualize ALL with GPT (uses KB)"):
                if not st.session_state.vector_store_id:
                    st.error("Create/Select a Vector Store and upload the template first.")
                    st.stop()
                with st.spinner("Generating..."):
                    for p in st.session_state.pages:
                        st.session_state.gpt[p["index"]] = generate_for_page(p, st.session_state.vector_store_id)
                st.success("Visualization complete.")
        with right:
            quiz_idxs = [p["index"] for p in st.session_state.pages if p["page_type"] == "quiz"]
            if quiz_idxs:
                pick = st.multiselect("Only visualize selected quizzes", quiz_idxs, quiz_idxs)
                if st.button("Visualize selected quizzes"):
                    if not st.session_state.vector_store_id:
                        st.error("Create/Select a Vector Store and upload the template first.")
                        st.stop()
                    with st.spinner("Generating selected quizzes..."):
                        for i in pick:
                            p = next(pp for pp in st.session_state.pages if pp["index"] == i)
                            st.session_state.gpt[p["index"]] = generate_for_page(p, st.session_state.vector_store_id)
                    st.success("Selected quizzes generated.")

# ------------------------------- New Quizzes ---------------------------------

with tab_quizzes:
    st.subheader("2) New Quizzes — duplicate template & insert content")
    _require_canvas_ready()

    # list template quizzes to duplicate
    templates = list_new_quiz_assignments(canvas_domain, course_id, canvas_token)
    label_map = {}
    for t in templates:
        lbl = f"{t['title']} (quiz_id: {t.get('quiz_id') or '—'}, assignment_id: {t.get('assignment_id') or '—'})"
        label_map[lbl] = t
    chosen_label = st.selectbox("Select a New Quiz as template (keeps its settings)", ["(Create fresh each time)"] + list(label_map.keys()))
    chosen_template = label_map.get(chosen_label) if chosen_label != "(Create fresh each time)" else None

    quiz_pages = [p for p in st.session_state.pages if p.get("page_type") == "quiz"]
    if not quiz_pages:
        st.info("No quiz pages in storyboard. Parse/visualize in Pages tab first.", icon="ℹ️")
    else:
        q_idx = [p["index"] for p in quiz_pages]
        q_lab = [f"{p['index']+1}. {p['page_title']}" for p in quiz_pages]
        selected_q = st.multiselect("Select quiz pages to process", q_idx, q_idx, format_func=lambda i: q_lab[q_idx.index(i)])

        # element toggles
        ins_desc = st.checkbox("Insert description HTML", value=True)
        ins_qs   = st.checkbox("Insert questions", value=True)

        if st.button("Duplicate template and populate selected quizzes"):
            if not (ins_desc or ins_qs):
                st.warning("Select at least one element to insert.")
                st.stop()
            with st.spinner("Working..."):
                mod_cache = {}
                for p in quiz_pages:
                    if p["index"] not in selected_q:
                        continue
                    # ensure content exists
                    bundle = st.session_state.gpt.get(p["index"])
                    if not bundle:
                        if not st.session_state.vector_store_id:
                            st.error(f"No generated content for '{p['page_title']}' and KB not ready.")
                            continue
                        bundle = generate_for_page(p, st.session_state.vector_store_id)
                        st.session_state.gpt[p["index"]] = bundle

                    html_desc = bundle.get("html","") if ins_desc else ""
                    qjson = bundle.get("quiz_json",{}) if ins_qs else {}

                    # 1) duplicate template (clone) or create fresh
                    new_asg_id = None
                    template_detail = None
                    if chosen_template:
                        # get full template details (to copy settings if clone unsupported)
                        template_detail = _get_new_quiz(
                            canvas_domain, course_id,
                            quiz_id=chosen_template.get("quiz_id"),
                            assignment_id=chosen_template.get("assignment_id"),
                            token=canvas_token
                        )
                        new_asg_id = clone_new_quiz(
                            canvas_domain, course_id,
                            quiz_id=chosen_template.get("quiz_id"),
                            assignment_id=chosen_template.get("assignment_id"),
                            token=canvas_token
                        )
                        if not new_asg_id:
                            st.info("Template clone not available; creating fresh quiz with copied settings.")
                    if not new_asg_id:
                        new_asg_id = add_new_quiz(canvas_domain, course_id, p["page_title"], html_desc, canvas_token)
                        if new_asg_id and template_detail:
                            copy_template_settings_to_quiz(canvas_domain, course_id, template_detail, new_asg_id, canvas_token)
                    if not new_asg_id:
                        st.error(f"Could not create/clone quiz for '{p['page_title']}'")
                        continue

                    # 2) patch title/description (ensure rename after clone)
                    try:
                        u = f"https://{canvas_domain}/api/quiz/v1/courses/{course_id}/quizzes/{new_asg_id}"
                        patch = {"quiz": {"title": p["page_title"]}}
                        if ins_desc and html_desc:
                            patch["quiz"]["instructions"] = html_desc
                        requests.patch(u, headers=_headers(canvas_token), json=patch, timeout=60)
                    except Exception:
                        pass

                    # 3) insert questions
                    if ins_qs and isinstance(qjson, dict):
                        for pos, q in enumerate(qjson.get("questions", []), start=1):
                            if q.get("answers"):
                                add_new_quiz_mcq(canvas_domain, course_id, new_asg_id, q, canvas_token, position=pos)

                    # 4) add to module
                    mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, mod_cache)
                    if mid and add_to_module(canvas_domain, course_id, mid, "Assignment", new_asg_id, p["page_title"], canvas_token):
                        st.success(f"✅ '{p['page_title']}' duplicated & populated → module '{p['module_name']}'")
                    else:
                        st.warning(f"Uploaded but not added to module (module problem?) for '{p['page_title']}'.")

# -------------------------------- Discussions --------------------------------

with tab_discuss:
    st.subheader("3) Discussions — duplicate template & insert content")
    _require_canvas_ready()

    discussions = list_discussions(canvas_domain, course_id, canvas_token)
    d_label_map = {}
    for d in discussions:
        lbl = f"{d.get('title','(untitled)')} (id: {d.get('id')})"
        d_label_map[lbl] = d
    d_choice = st.selectbox("Select a discussion as template", ["(Create fresh each time)"] + list(d_label_map.keys()))
    d_template = d_label_map.get(d_choice) if d_choice != "(Create fresh each time)" else None

    disc_pages = [p for p in st.session_state.pages if p.get("page_type") == "discussion"]
    if not disc_pages:
        st.info("No discussion pages in storyboard. Update metadata in Pages tab if needed.", icon="ℹ️")
    else:
        d_idx = [p["index"] for p in disc_pages]
        d_lab = [f"{p['index']+1}. {p['page_title']}" for p in disc_pages]
        selected_d = st.multiselect("Select discussion pages to process", d_idx, d_idx, format_func=lambda i: d_lab[d_idx.index(i)])

        insert_body = st.checkbox("Insert body HTML", value=True)

        if st.button("Duplicate & populate selected discussions"):
            with st.spinner("Creating discussions..."):
                mod_cache = {}
                for p in disc_pages:
                    if p["index"] not in selected_d:
                        continue
                    # ensure content (use visualization if exists; else take raw between <canvas_page>)
                    bundle = st.session_state.gpt.get(p["index"])
                    html_body = (bundle.get("html","") if bundle else "") if insert_body else ""
                    if not html_body and st.session_state.vector_store_id:
                        bundle = generate_for_page(p, st.session_state.vector_store_id)
                        st.session_state.gpt[p["index"]] = bundle
                        html_body = bundle.get("html","") if insert_body else ""

                    # create from template
                    new_topic_id = create_discussion_from_template(
                        canvas_domain, course_id, d_template or {}, p["page_title"], html_body, canvas_token
                    )
                    if not new_topic_id:
                        st.error(f"Failed to create discussion for '{p['page_title']}'")
                        continue

                    # add to module
                    mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, mod_cache)
                    if mid and add_to_module(canvas_domain, course_id, mid, "Discussion", new_topic_id, p["page_title"], canvas_token):
                        st.success(f"✅ Discussion '{p['page_title']}' created → module '{p['module_name']}'")
                    else:
                        st.warning(f"Discussion created but not added to module for '{p['page_title']}'.")

