# canvas_import_um.py
# -----------------------------------------------------------------------------
# 📄 DOCX/Google Doc → GPT (with Knowledge Base and/or Course Templates)
#     → Canvas (Pages / Assignments / Discussions / New Quizzes)
#
# Additions in this version:
# - Load course templates from a module named like "Templates" or "Design"
# - Show existing modules and allow picking one per page
# - Read <page_template> from storyboard and pre-select a matching course template
# - Per-page Template Source: "course" (use picked course template page) or "kb"
# - Visualization uses course template HTML when selected; otherwise KB (file_search)
# -----------------------------------------------------------------------------

from io import BytesIO
import uuid
import json
import re
import requests
import streamlit as st
from docx import Document
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ---------------------------- App Setup --------------------------------------
st.set_page_config(page_title="📄 DOCX → GPT (KB/Course Templates) → Canvas", layout="wide")
st.title("📄 Upload DOCX → Convert via GPT (KB / Course Templates) → Upload to Canvas")

# ---------------------------- Session State ----------------------------------
def _init_state():
    defaults = {
        "pages": [],
        "gpt_results": {},      # key: page_idx -> {"html":..., "quiz_json":...}
        "visualized": False,
        "vector_store_id": None,
        "course_templates": {}, # {title: html}
        "course_modules": [],   # [{"id":..., "name":...}, ...]
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

# ------------------------ Sidebar: Credentials & Sources ---------------------
with st.sidebar:
    st.header("Setup")

    # Storyboard sources
    uploaded_file = st.file_uploader("Storyboard (.docx)", type="docx")
    st.subheader("Or pull storyboard from Google Docs")
    gdoc_url = st.text_input("Storyboard Google Doc URL")
    sa_json = st.file_uploader("Service Account JSON (for Drive read)", type=["json"])

    # Template KB (Vector Store) management
    st.subheader("Template Knowledge Base")
    kb_col1, kb_col2 = st.columns(2)
    with kb_col1:
        existing_vs = st.text_input("Vector Store ID (optional)", value=st.session_state.get("vector_store_id") or "")
    with kb_col2:
        st.caption("Paste an existing ID to reuse your KB")
    kb_docx = st.file_uploader("Upload template DOCX (optional)", type=["docx"])
    kb_gdoc_url = st.text_input("Template Google Doc URL (optional)")

    # Canvas + OpenAI
    st.subheader("Canvas & OpenAI")
    canvas_domain = st.text_input("Canvas Base URL", placeholder="youruni.instructure.com")
    course_id = st.text_input("Canvas Course ID")
    canvas_token = st.text_input("Canvas API Token", type="password")
    openai_api_key = st.text_input("OpenAI API Key", type="password")

    use_new_quizzes = st.checkbox("Use New Quizzes (recommended)", value=True)
    dry_run = st.checkbox("🔍 Preview only (Dry Run)", value=False)
    if dry_run:
        st.info("No data will be sent to Canvas. This is a preview only.", icon="ℹ️")

# ------------------------ Google Drive Helpers -------------------------------
def _gdoc_id_from_url(url: str):
    if not url:
        return None
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url) or re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
    return m.group(1) if m else None

def fetch_docx_from_gdoc(file_id: str, sa_json_bytes: bytes) -> BytesIO:
    """Export a Google Doc to DOCX and return as BytesIO."""
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

# ------------------------ DOCX Parsers ---------------------------------------
def extract_canvas_pages(storyboard_docx_file):
    """Pull out everything between <canvas_page>...</canvas_page> (plain-text level). Ignore anything between <ignore>...</ignore>."""
    doc = Document(storyboard_docx_file)
    pages, current_block, inside_block = [], [], False
    for para in doc.paragraphs:
        text = para.text.strip()
        low = text.lower()
        if "<canvas_page>" in low:
            inside_block = True
            current_block = [text]
            continue
        if "</canvas_page>" in low:
            current_block.append(text)
            pages.append("\n".join(current_block))
            inside_block = False
            continue
        if inside_block:
            current_block.append(text)
    return pages

def extract_tag(tag, block):
    m = re.search(fr"<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""

# ------------------------ Canvas API -----------------------------------------
def _BASE(domain): return f"https://{domain}".rstrip("/")
def _H(token): return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def list_modules(domain, course_id, token):
    url = f"{_BASE(domain)}/api/v1/courses/{course_id}/modules"
    r = requests.get(url, headers=_H(token), params={"per_page": 100}, timeout=60)
    r.raise_for_status()
    return r.json()

def list_module_items(domain, course_id, module_id, token):
    url = f"{_BASE(domain)}/api/v1/courses/{course_id}/modules/{module_id}/items"
    r = requests.get(url, headers=_H(token), params={"per_page": 100}, timeout=60)
    r.raise_for_status()
    return r.json()

def get_page_body(domain, course_id, slug, token):
    url = f"{_BASE(domain)}/api/v1/courses/{course_id}/pages/{slug}"
    r = requests.get(url, headers=_H(token), timeout=60)
    r.raise_for_status()
    j = r.json()
    return j.get("body",""), j.get("title","")

def get_or_create_module(module_name, domain, course_id, token, module_cache):
    if module_name in module_cache:
        return module_cache[module_name]
    try:
        mods = list_modules(domain, course_id, token)
    except Exception as e:
        st.error(f"Failed to list modules: {e}")
        return None
    for m in mods:
        if m["name"].strip().lower() == module_name.strip().lower():
            module_cache[module_name] = m["id"]
            return m["id"]
    # create if not found
    url = f"{_BASE(domain)}/api/v1/courses/{course_id}/modules"
    r = requests.post(url, headers=_H(token), json={"module": {"name": module_name, "published": True}}, timeout=60)
    if r.status_code in (200, 201):
        mid = r.json().get("id")
        module_cache[module_name] = mid
        return mid
    st.error(f"❌ Failed to create/find module: {module_name}")
    st.error(f"📬 Response: {r.status_code} | {r.text}")
    return None

def add_page(domain, course_id, title, html_body, token):
    url = f"{_BASE(domain)}/api/v1/courses/{course_id}/pages"
    r = requests.post(url, headers=_H(token), json={"wiki_page": {"title": title, "body": html_body, "published": True}}, timeout=60)
    if r.status_code in (200, 201):
        return r.json().get("url")
    st.error(f"❌ Page create failed: {r.text}")
    return None

def add_assignment(domain, course_id, title, html_body, token):
    url = f"{_BASE(domain)}/api/v1/courses/{course_id}/assignments"
    payload = {"assignment": {"name": title, "description": html_body, "published": True, "submission_types": ["online_text_entry"], "points_possible": 10}}
    r = requests.post(url, headers=_H(token), json=payload, timeout=60)
    if r.status_code in (200, 201):
        return r.json().get("id")
    st.error(f"❌ Assignment create failed: {r.text}")
    return None

def add_discussion(domain, course_id, title, html_body, token):
    url = f"{_BASE(domain)}/api/v1/courses/{course_id}/discussion_topics"
    r = requests.post(url, headers=_H(token), json={"title": title, "message": html_body, "published": True}, timeout=60)
    if r.status_code in (200, 201):
        return r.json().get("id")
    st.error(f"❌ Discussion create failed: {r.text}")
    return None

# Classic quiz fallback
def add_quiz(domain, course_id, title, description_html, token):
    url = f"{_BASE(domain)}/api/v1/courses/{course_id}/quizzes"
    payload = {"quiz": {"title": title, "description": description_html or "", "published": True, "quiz_type": "assignment", "scoring_policy": "keep_highest"}}
    r = requests.post(url, headers=_H(token), json=payload, timeout=60)
    if r.status_code in (200, 201):
        return r.json().get("id")
    st.error(f"❌ Quiz create failed: {r.text}")
    return None

def add_quiz_question(domain, course_id, quiz_id, q, token):
    url = f"{_BASE(domain)}/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions"
    payload = {
        "question": {
            "question_name": q.get("question_name") or "Question",
            "question_text": q.get("question_text") or "",
            "question_type": q.get("question_type", "multiple_choice_question"),
            "points_possible": q.get("points_possible", 1),
            "answers": [{"text": a.get("text",""), "weight": 100 if a.get("is_correct") else 0}
                        for a in q.get("answers", [])]
        }
    }
    r = requests.post(url, headers=_H(token), json=payload, timeout=60)
    if r.status_code not in (200, 201):
        st.warning(f"Classic question failed: {r.status_code} {r.text}")

# New Quizzes (LTI) minimal
def add_new_quiz(domain, course_id, title, description_html, token, points_possible=1):
    url = f"{_BASE(domain)}/api/quiz/v1/courses/{course_id}/quizzes"
    payload = {"quiz": {"title": title, "points_possible": max(points_possible, 1), "instructions": description_html or ""}}
    r = requests.post(url, headers=_H(token), json=payload, timeout=60)
    if r.status_code in (200, 201):
        data = r.json()
        return data.get("assignment_id") or data.get("id")
    st.error(f"❌ New Quiz create failed: {r.status_code} | {r.text}")
    return None

def add_new_quiz_mcq(domain, course_id, assignment_id, q, token, position=1):
    url = f"{_BASE(domain)}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    choices, answer_feedback_map, correct_choice_id = [], {}, None
    for idx, ans in enumerate(q.get("answers", []), start=1):
        cid = str(uuid.uuid4())
        choices.append({"id": cid, "position": idx, "itemBody": f"<p>{ans.get('text','')}</p>"})
        if ans.get("is_correct"):
            correct_choice_id = cid
        if ans.get("feedback"):
            answer_feedback_map[cid] = ans["feedback"]
    if not choices:
        st.warning("Skipping MCQ with no answers.")
        return
    if not correct_choice_id:
        correct_choice_id = choices[0]["id"]
    properties = {"shuffleRules": {"choices": {"toLock": [], "shuffled": bool(q.get("shuffle", False))}},
                  "varyPointsByAnswer": False}
    fb = q.get("feedback") or {}
    feedback_block = {k: v for k, v in fb.items() if v}
    entry = {
        "interaction_type_slug": "choice",
        "title": q.get("question_name") or "Question",
        "item_body": q.get("question_text") or "",
        "calculator_type": "none",
        "interaction_data": {"choices": choices},
        "properties": properties,
        "scoring_data": {"value": correct_choice_id},
        "scoring_algorithm": "Equivalence"
    }
    if feedback_block:
        entry["feedback"] = feedback_block
    if answer_feedback_map:
        entry["answer_feedback"] = answer_feedback_map
    payload = {"item": {"entry_type": "Item", "points_possible": q.get("points_possible", 1), "position": position, "entry": entry}}
    r = requests.post(url, headers=_H(token), json=payload, timeout=60)
    if r.status_code not in (200, 201):
        st.warning(f"⚠️ Failed to add item to New Quiz: {r.status_code} | {r.text}")

def add_to_module(domain, course_id, module_id, item_type, ref, title, token):
    url = f"{_BASE(domain)}/api/v1/courses/{course_id}/modules/{module_id}/items"
    item = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page":
        item["module_item"]["page_url"] = ref
    else:
        item["module_item"]["content_id"] = ref
    r = requests.post(url, headers=_H(token), json=item, timeout=60)
    return r.status_code in (200, 201)

# ------------------------ Course Templates Loader ----------------------------
def load_course_templates_and_modules(domain, course_id, token):
    """Returns (templates_dict, modules_list). templates_dict = {title: html}"""
    templates = {}
    modules = []
    try:
        mods = list_modules(domain, course_id, token)
        modules = [{"id": m["id"], "name": m["name"]} for m in mods]
        tmod = next((m for m in mods if "template" in m["name"].lower() or "design" in m["name"].lower()), None)
        if tmod:
            items = list_module_items(domain, course_id, tmod["id"], token)
            for it in items:
                if it.get("type") == "Page" and it.get("page_url"):
                    body, title = get_page_body(domain, course_id, it["page_url"], token)
                    templates[title] = body or ""
    except Exception as e:
        st.error(f"Failed to load course templates/modules: {e}")
    return templates, modules

# ------------------------ OpenAI KB (Vector Store) ---------------------------
def ensure_client():
    if not openai_api_key:
        st.error("OpenAI API key is required.")
        st.stop()
    return OpenAI(api_key=openai_api_key)

def create_vector_store(client: OpenAI, name="umich_canvas_templates"):
    vs = client.vector_stores.create(name=name)
    return vs.id

def upload_file_to_vs(client: OpenAI, vector_store_id: str, file_like, filename: str):
    f = client.files.create(file=(filename, file_like), purpose="assistants")
    client.vector_stores.files.create(vector_store_id=vector_store_id, file_id=f.id)

def fetch_bytes_for_kb():
    if kb_docx is not None:
        return BytesIO(kb_docx.getvalue()), kb_docx.name
    if kb_gdoc_url and sa_json:
        fid = _gdoc_id_from_url(kb_gdoc_url)
        if fid:
            try:
                data = fetch_docx_from_gdoc(fid, sa_json.read())
                return data, "template_from_gdoc.docx"
            except Exception as e:
                st.error(f"❌ Could not fetch Template Google Doc: {e}")
                return None
    return None

# KB actions
kb_cols = st.columns([1, 1, 1, 1])
with kb_cols[0]:
    if st.button("Create Vector Store", use_container_width=True):
        client = ensure_client()
        vs_id = create_vector_store(client)
        st.session_state.vector_store_id = vs_id
        st.success(f"✅ Created Vector Store: {vs_id}")

with kb_cols[1]:
    if st.button("Upload Template to KB", use_container_width=True, disabled=not (st.session_state.get("vector_store_id") or existing_vs)):
        client = ensure_client()
        vs_id = (st.session_state.get("vector_store_id") or existing_vs).strip()
        got = fetch_bytes_for_kb()
        if not vs_id:
            st.error("Vector Store ID missing.")
        elif not got:
            st.error("Provide a template .docx or Google Doc URL + SA JSON.")
        else:
            data, fname = got
            upload_file_to_vs(client, vs_id, data, fname)
            st.success("✅ Template uploaded to KB.")

with kb_cols[2]:
    if st.button("Use Existing VS ID", use_container_width=True):
        if existing_vs.strip():
            st.session_state.vector_store_id = existing_vs.strip()
            st.success(f"✅ Using Vector Store: {st.session_state.vector_store_id}")
        else:
            st.error("Paste a Vector Store ID first.")

with kb_cols[3]:
    if st.button("Load Course Templates & Modules", use_container_width=True, disabled=not (canvas_domain and course_id and canvas_token)):
        tpls, mods = load_course_templates_and_modules(canvas_domain, course_id, canvas_token)
        st.session_state.course_templates = tpls
        st.session_state.course_modules = mods
        st.success(f"Loaded {len(tpls)} template page(s) and {len(mods)} module(s) from the course.")

# Show modules list (read-only)
if st.session_state.course_modules:
    names = [m["name"] for m in st.session_state.course_modules]
    st.sidebar.caption("Existing modules:")
    st.sidebar.write(", ".join(names))

# ------------------------ Parse Storyboard + Prepare Pages -------------------
col1, col2 = st.columns([1, 2])
with col1:
    has_story = bool(uploaded_file or (gdoc_url and sa_json))
    if st.button("1️⃣ Parse storyboard", type="primary", use_container_width=True, disabled=not has_story):
        st.session_state.pages.clear()
        st.session_state.gpt_results.clear()
        st.session_state.visualized = False

        story_source = uploaded_file
        if not story_source and gdoc_url and sa_json:
            fid = _gdoc_id_from_url(gdoc_url)
            if fid:
                try:
                    story_source = fetch_docx_from_gdoc(fid, sa_json.read())
                except Exception as e:
                    st.error(f"❌ Could not fetch Storyboard Google Doc: {e}")

        if not story_source:
            st.error("Upload a storyboard .docx OR provide a Google Doc URL + SA JSON.")
            st.stop()

        raw_pages = extract_canvas_pages(story_source)

        last_known_module = None
        for idx, block in enumerate(raw_pages):
            page_type = (extract_tag("page_type", block).lower() or "page").strip()
            page_title = extract_tag("page_title", block) or f"Page {idx+1}"
            module_name = extract_tag("module_name", block).strip()
            page_template_name = extract_tag("page_template", block).strip()

            if not module_name:
                m = re.search(r"\b(Module\s+[A-Za-z0-9 ]+)", page_title, flags=re.IGNORECASE)
                if m:
                    module_name = m.group(1).strip()
            if not module_name:
                module_name = last_known_module or "General"
            last_known_module = module_name

            # If storyboard named a page template, try to match to a loaded course template title
            matched_course_template = ""
            if page_template_name and st.session_state.course_templates:
                lower_map = {t.lower(): t for t in st.session_state.course_templates.keys()}
                key = page_template_name.lower()
                if key in lower_map:
                    matched_course_template = lower_map[key]

            st.session_state.pages.append({
                "index": idx,
                "raw": block,
                "page_type": page_type,      # page | assignment | discussion | quiz
                "page_title": page_title,
                "module_name": module_name,
                "page_template_from_doc": page_template_name,
                # default template source preference
                "template_source": "course" if matched_course_template else "kb",
                "course_template_title": matched_course_template,
            })

        st.success(f"✅ Parsed {len(st.session_state.pages)} page(s).")

with col2:
    st.write("")

# ------------------------- Editable Page Table (Pre-GPT) ---------------------
if st.session_state.pages:
    st.subheader("2️⃣ Review & adjust page metadata (no GPT yet)")
    course_titles = list(st.session_state.course_templates.keys()) or ["(no course templates loaded)"]
    module_names = [m["name"] for m in st.session_state.course_modules] or ["(no modules loaded)"]

    for i, p in enumerate(st.session_state.pages):
        header = f"Page {i+1}: {p['page_title']} ({p['page_type']}) | Module: {p['module_name']}"
        with st.expander(header, expanded=False):
            c1, c2, c3 = st.columns([1.15, 1, 1])

            # Left column: title, type
            with c1:
                p["page_title"] = st.text_input("Page Title", value=p["page_title"], key=f"title_{i}")
                p["page_type"] = st.selectbox("Page Type", options=["page","assignment","discussion","quiz"],
                                              index=["page","assignment","discussion","quiz"].index(p["page_type"]),
                                              key=f"type_{i}")

            # Middle column: module (text + pick existing)
            with c2:
                p["module_name"] = st.text_input("Module (from storyboard)", value=p["module_name"], key=f"module_{i}")
                pick = st.selectbox("Or pick existing module", module_names, key=f"modpick_{i}")
                if pick and pick != "(no modules loaded)":
                    p["module_name"] = pick

            # Right column: template source + course template pick
            with c3:
                p["template_source"] = st.selectbox("Template Source", ["course","kb"],
                                                    index=["course","kb"].index(p["template_source"]),
                                                    key=f"ts_{i}")
                if p["template_source"] == "course":
                    default_idx = 0
                    if p.get("course_template_title") and p["course_template_title"] in course_titles:
                        default_idx = course_titles.index(p["course_template_title"])
                    p["course_template_title"] = st.selectbox("Course template page", course_titles, index=default_idx, key=f"ctp_{i}")
                else:
                    st.text_input("Page Template (from doc)", value=p.get("page_template_from_doc",""), key=f"ptdoc_{i}")

            st.caption("Raw storyboard block")
            st.text_area("raw", value=p["raw"], height=170, key=f"raw_{i}")

    st.divider()

    # Selection section
    st.markdown("#### 🔎 Choose pages to visualize")
    sel_cols = st.columns([1, 1])
    with sel_cols[0]:
        if st.button("Select all"):
            for i, _ in enumerate(st.session_state.pages):
                st.session_state[f"viz_sel_{i}"] = True
    with sel_cols[1]:
        if st.button("Select none"):
            for i, _ in enumerate(st.session_state.pages):
                st.session_state[f"viz_sel_{i}"] = False

    # Show per-page checkboxes
    selected_indices = []
    for i, p in enumerate(st.session_state.pages):
        default_checked = st.session_state.get(f"viz_sel_{i}", False)
        checked = st.checkbox(
            f"Page {i+1}: {p['page_title']}  ({p['page_type']}) · Module: {p['module_name']}",
            value=default_checked, key=f"viz_sel_{i}"
        )
        if checked:
            selected_indices.append(i)

    # Visualize button (only selected)
    visualize_selected_clicked = st.button(
        "🔎 Visualize selected pages (no upload yet)",
        type="primary",
        use_container_width=True,
        disabled=not (openai_api_key and selected_indices)
    )

    if visualize_selected_clicked:
        client = OpenAI(api_key=openai_api_key)

        for idx in selected_indices:
            p = st.session_state.pages[idx]
            raw_block = p["raw"]

            # Build prompt depending on template source
            base_rules = (
                "You are an expert Canvas HTML generator.\n"
                "- Preserve ALL <a href> links and any <img> or <table> in the storyboard.\n"
                "- Replace only inner content of template areas; keep structure/classes/attributes intact.\n"
                "  if a section has no content, remove the template section in place; append extra sections at the end.\n"
                "- if a section does not exist in the template, create it with the same structure.\n"
                "- <element_type> tags are used to mark template code associations found within the file_search.\n"
                "- If some content does not map, append it as it appears in the storyboard."
                "- if a section does not exist in the template, create it with the same structure.\n"
                "- <element_type> tags are used to mark template code associations found within the file_search.\n"
                "- <accordion_title> are used for the summary tag in html accordions.\n"
                "- <accordion_content> are used for the content inside the accordion.\n"
                "- table formatting must be converted to HTML tables with <table>, <tr>, <td> tags.\n"
                "- <Table with Row Striping> is a tag and there is template code for it in the template document.\n"
                "- <Table with Column Striping> is a tag and there is template code for it in the template document.\n"
                "- <video> is also a tag with template code in the document. \n" 
                "- There is a possibility of elements within elements. Please add in the code accordingly. \n" 
                "- Keep .bluePageHeader, .header, .divisionLineYellow, .landingPageFooter intact.\n\n"
                "QUIZ RULES (when <page_type> is 'quiz'):\n"
                "- Questions appear between <quiz_start> and </quiz_end>.\n"
                "- <multiple_choice> blocks use '*' prefix to mark correct choices.\n"
                "- If <shuffle> appears inside a question, set \"shuffle\": true; else false.\n"
                "- Question-level feedback tags (optional):\n"
                "  <feedback_correct>...</feedback_correct>, <feedback_incorrect>...</feedback_incorrect>, <feedback_neutral>...</feedback_neutral>\n"
                "- Per-answer feedback (optional): '(feedback: ...)' after a choice line or <feedback>A: ...</feedback>.\n"
                "RETURN:\n"
                "1) Canvas-ready HTML (no code fences) and no other comments\n"
                "2) If page_type is 'quiz', append a JSON object at the very END (no extra text) with:\n"
                "- Support these Canvas-compatible question types:\n"
                "  multiple_choice_question (single correct), multiple_answers_question (checkboxes), true_false_question, "
                "  essay_question, short_answer_question (fill-in-one-blank), fill_in_multiple_blanks_question, "
                "  matching_question, numerical_question.\n"
                "- Include per-answer feedback when available, and overall feedback via a 'feedback' object "
                "(keys: 'correct','incorrect','neutral').\n"
                "JSON SCHEMA EXAMPLES (use only fields relevant to each type; keep it MINIFIED):\n"
                '{"quiz_description":"<p>Intro...</p>","questions":['
                # multiple choice
                '{"question_type":"multiple_choice_question","question_name":"...","question_text":"<p>...</p>",'
                '"answers":[{"text":"A","is_correct":false,"feedback":"<p>...</p>"},{"text":"B","is_correct":true,"feedback":"<p>...</p>"}],'
                '"shuffle":true,"feedback":{"correct":"<p>...</p>","incorrect":"<p>...</p>","neutral":"<p>...</p>"}},'
                # multiple answers (checkboxes)
                '{"question_type":"multiple_answers_question","question_name":"...","question_text":"<p>...</p>",'
                '"answers":[{"text":"A","is_correct":true,"feedback":"<p>...</p>"},{"text":"B","is_correct":true,"feedback":"<p>...</p>"},'
                '{"text":"C","is_correct":false,"feedback":"<p>...</p>"}],'
                '"feedback":{"correct":"<p>...</p>","incorrect":"<p>...</p>"}},'
                # true/false
                '{"question_type":"true_false_question","question_name":"...","question_text":"<p>...</p>",'
                '"answers":[{"text":"True","is_correct":false,"feedback":"<p>...</p>"},{"text":"False","is_correct":true,"feedback":"<p>...</p>"}],'
                '"feedback":{"correct":"<p>...</p>","incorrect":"<p>...</p>"}},'
                # essay
                '{"question_type":"essay_question","question_name":"...","question_text":"<p>...</p>",'
                '"feedback":{"neutral":"<p>Instructor graded.</p>"}},'
                # short answer (single blank; list acceptable strings)
                '{"question_type":"short_answer_question","question_name":"...","question_text":"<p>...</p>",'
                '"answers":[{"text":"chlorophyll"},{"text":"chlorophyl"}],'
                '"feedback":{"correct":"<p>...</p>","incorrect":"<p>...</p>"}},'
                # fill in multiple blanks (use {{blank_id}} in question_text; map answers by blank_id)
                '{"question_type":"fill_in_multiple_blanks_question","question_name":"...","question_text":"<p>H{{b1}}O is {{b2}}.</p>",'
                '"answers":[{"blank_id":"b1","text":"2","feedback":"<p>...</p>"},{"blank_id":"b2","text":"water","feedback":"<p>...</p>"}]},'
                # matching
                '{"question_type":"matching_question","question_name":"...","question_text":"<p>Match:</p>",'
                '"matches":[{"prompt":"H2O","match":"water","feedback":"<p>...</p>"},{"prompt":"NaCl","match":"salt","feedback":"<p>...</p>"}]},'
                # numerical (exact or exact+tolerance)
                '{"question_type":"numerical_question","question_name":"...","question_text":"<p>Speed?</p>",'
                '"numerical_answer":{"exact":12.5,"tolerance":0.5},'
                '"feedback":{"correct":"<p>...</p>","incorrect":"<p>...</p>"}}'
                "]}\n"
                "]}\n"
                "COVERAGE (NO-DROP) RULES\n"
                "- Do not omit or summarize any substantive content from the storyboard block.\n"
                "- Every sentence/line from the storyboard (between <canvas_page>…</canvas_page>) MUST appear in the output HTML.\n"
                "- If a piece of storyboard content doesn’t clearly map to a template section, append it as it appears in the storyboard.\n"
                "- Preserve the original order of content as much as possible.\n"
                "- Never remove <img>, <table>, or any explicit HTML already present in the storyboard; include them verbatim.\n"
            )

            if p["template_source"] == "course":
                # Feed the chosen course template HTML directly
                tmpl_html = st.session_state.course_templates.get(p.get("course_template_title",""), "")
                SYSTEM = base_rules + "\nUse the TEMPLATE HTML verbatim.\nReturn HTML only."
                USER = f"TEMPLATE HTML:\n{tmpl_html}\n\nSTORYBOARD PAGE BLOCK:\n{raw_block}\n"
                tools = None
            else:
                # KB + file_search flow
                SYSTEM = (
                    base_rules +
                    "\nUse file_search to locate the best matching template.\nReturn HTML only.\n"
                )
                USER = f'STORYBOARD PAGE BLOCK (template hint: "{p.get("page_template_from_doc") or "auto"}"):\n{raw_block}\n'
                tools = [{"type": "file_search", "vector_store_ids": [st.session_state["vector_store_id"]]}] if st.session_state.get("vector_store_id") else None

            kwargs = {
                "model": "gpt-4o",
                "input": [{"role":"system","content":SYSTEM}, {"role":"user","content":USER}],
            }
            if tools:
                kwargs["tools"] = tools

            response = client.responses.create(**kwargs)
            raw_out = response.output_text or ""
            cleaned = re.sub(r"```(html|json)?", "", raw_out, flags=re.IGNORECASE).strip()

            # Pull trailing JSON for quizzes if present
            json_match = re.search(r"({[\s\S]+})\s*$", cleaned)
            quiz_json = None
            html_result = cleaned
            if json_match and p["page_type"] == "quiz":
                try:
                    quiz_json = json.loads(json_match.group(1))
                    html_result = cleaned[:json_match.start()].strip()
                except Exception:
                    quiz_json = None

            st.session_state.gpt_results[idx] = {"html": html_result, "quiz_json": quiz_json}

        st.session_state.visualized = True
        st.success("✅ Visualization complete. Preview below.")

# ---------------------------- Preview & Upload -------------------------------
if st.session_state.pages and st.session_state.visualized:
    st.subheader("3️⃣ Previews (post-GPT). Upload to Canvas when ready.")
    module_cache = {}
    any_uploaded = False

    colA, colB = st.columns([1, 2])
    with colA:
        upload_all_clicked = st.button(
            "🚀 Upload ALL to Canvas",
            type="secondary",
            disabled=dry_run or not (canvas_domain and course_id and canvas_token)
        )
    with colB:
        if dry_run:
            st.info("Dry run is ON — uploads are disabled.", icon="⏸️")

    for p in st.session_state.pages:
        idx = p["index"]
        meta = f"{p['page_title']} ({p['page_type']}) | Module: {p['module_name']}"
        with st.expander(f"📄 {meta}", expanded=False):
            html_result = st.session_state.gpt_results.get(idx, {}).get("html", "")
            quiz_json = st.session_state.gpt_results.get(idx, {}).get("quiz_json")
            st.code(html_result or "[No HTML returned]", language="html")

            can_upload = (not dry_run) and canvas_domain and course_id and canvas_token
            if st.button(f"Upload '{p['page_title']}'", key=f"upl_{idx}", disabled=not can_upload):
                mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, module_cache)
                if not mid:
                    st.error("Module creation failed.")
                    st.stop()

                if p["page_type"] == "page":
                    page_url = add_page(canvas_domain, course_id, p["page_title"], html_result, canvas_token)
                    if page_url and add_to_module(canvas_domain, course_id, mid, "Page", page_url, p["page_title"], canvas_token):
                        any_uploaded = True
                        st.success("✅ Page created & added to module.")

                elif p["page_type"] == "assignment":
                    aid = add_assignment(canvas_domain, course_id, p["page_title"], html_result, canvas_token)
                    if aid and add_to_module(canvas_domain, course_id, mid, "Assignment", aid, p["page_title"], canvas_token):
                        any_uploaded = True
                        st.success("✅ Assignment created & added to module.")

                elif p["page_type"] == "discussion":
                    did = add_discussion(canvas_domain, course_id, p["page_title"], html_result, canvas_token)
                    if did and add_to_module(canvas_domain, course_id, mid, "Discussion", did, p["page_title"], canvas_token):
                        any_uploaded = True
                        st.success("✅ Discussion created & added to module.")

                elif p["page_type"] == "quiz":
                    description = html_result
                    if quiz_json and isinstance(quiz_json, dict) and "quiz_description" in quiz_json:
                        description = quiz_json.get("quiz_description") or html_result

                    if use_new_quizzes:
                        assignment_id = add_new_quiz(canvas_domain, course_id, p["page_title"], description, canvas_token)
                        if assignment_id:
                            if quiz_json and isinstance(quiz_json, dict):
                                for pos, q in enumerate(quiz_json.get("questions", []), start=1):
                                    if q.get("answers"):
                                        add_new_quiz_mcq(canvas_domain, course_id, assignment_id, q, canvas_token, position=pos)
                            if add_to_module(canvas_domain, course_id, mid, "Assignment", assignment_id, p["page_title"], canvas_token):
                                any_uploaded = True
                                st.success("✅ New Quiz created (with items) & added to module.")
                        else:
                            st.error("❌ New Quiz creation failed.")
                    else:
                        qid = add_quiz(canvas_domain, course_id, p["page_title"], description, canvas_token)
                        if qid:
                            if quiz_json and isinstance(quiz_json, dict):
                                for q in quiz_json.get("questions", []):
                                    add_quiz_question(canvas_domain, course_id, qid, q, canvas_token)
                            if add_to_module(canvas_domain, course_id, mid, "Quiz", qid, p["page_title"], canvas_token):
                                any_uploaded = True
                                st.success("✅ Classic Quiz created (with questions) & added to module.")
                        else:
                            st.error("❌ Classic Quiz creation failed.")
                else:
                    st.warning(f"Unsupported page_type: {p['page_type']}")

    if upload_all_clicked and (not dry_run):
        for p in st.session_state.pages:
            idx = p["index"]
            html_result = st.session_state.gpt_results.get(idx, {}).get("html", "")
            quiz_json = st.session_state.gpt_results.get(idx, {}).get("quiz_json")
            mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, {})
            if not mid:
                continue

            if p["page_type"] == "page":
                page_url = add_page(canvas_domain, course_id, p["page_title"], html_result, canvas_token)
                if page_url and add_to_module(canvas_domain, course_id, mid, "Page", page_url, p["page_title"], canvas_token):
                    any_uploaded = True
                    st.toast(f"Uploaded page: {p['page_title']}", icon="✅")

            elif p["page_type"] == "assignment":
                aid = add_assignment(canvas_domain, course_id, p["page_title"], html_result, canvas_token)
                if aid and add_to_module(canvas_domain, course_id, mid, "Assignment", aid, p["page_title"], canvas_token):
                    any_uploaded = True
                    st.toast(f"Uploaded assignment: {p['page_title']}", icon="✅")

            elif p["page_type"] == "discussion":
                did = add_discussion(canvas_domain, course_id, p["page_title"], html_result, canvas_token)
                if did and add_to_module(canvas_domain, course_id, mid, "Discussion", did, p["page_title"], canvas_token):
                    any_uploaded = True
                    st.toast(f"Uploaded discussion: {p['page_title']}", icon="✅")

            elif p["page_type"] == "quiz":
                description = html_result
                if quiz_json and isinstance(quiz_json, dict) and "quiz_description" in quiz_json:
                    description = quiz_json.get("quiz_description") or html_result

                if use_new_quizzes:
                    assignment_id = add_new_quiz(canvas_domain, course_id, p["page_title"], description, canvas_token)
                    if assignment_id:
                        if quiz_json and isinstance(quiz_json, dict):
                            for pos, q in enumerate(quiz_json.get("questions", []), start=1):
                                if q.get("answers"):
                                    add_new_quiz_mcq(canvas_domain, course_id, assignment_id, q, canvas_token, position=pos)
                        add_to_module(canvas_domain, course_id, mid, "Assignment", assignment_id, p["page_title"], canvas_token)
                        any_uploaded = True
                        st.toast(f"Uploaded New Quiz: {p['page_title']}", icon="✅")
                else:
                    qid = add_quiz(canvas_domain, course_id, p["page_title"], description, canvas_token)
                    if qid:
                        if quiz_json and isinstance(quiz_json, dict):
                            for q in quiz_json.get("questions", []):
                                add_quiz_question(canvas_domain, course_id, qid, q, canvas_token)
                        add_to_module(canvas_domain, course_id, mid, "Quiz", qid, p["page_title"], canvas_token)
                        any_uploaded = True
                        st.toast(f"Uploaded Classic Quiz: {p['page_title']}", icon="✅")

        if not any_uploaded:
            st.warning("No items uploaded. Check your tokens/IDs and try again.")

# ----------------------------- UX Guidance -----------------------------------
has_story = bool(uploaded_file or (gdoc_url and sa_json))
if not has_story:
    st.info("Provide a storyboard (.docx upload or Google Doc URL + SA JSON), then click **Parse storyboard**.", icon="📝")
elif has_story and not st.session_state.pages:
    st.warning("Click **Parse storyboard** to begin (no GPT call yet).", icon="👉")
elif st.session_state.pages and not st.session_state.visualized:
    if not st.session_state.get("vector_store_id") and not st.session_state.course_templates:
        st.warning("Load course templates or set up the KB (Create Vector Store + Upload template), then click **Visualize**.", icon="📚")
    else:
        st.info("Review & adjust page metadata above, then click **Visualize**.", icon="🔎")
