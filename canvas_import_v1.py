# canvas_import_um.py
# -----------------------------------------------------------------------------
# 📄 DOCX/Google Doc → GPT (KB) → Canvas
# Pages / Assignments / Discussions / New Quizzes
# WITH: Per-quiz template picker (assignment duplication during Visualize)
# -----------------------------------------------------------------------------

from io import BytesIO
import uuid
import json
import re
import time
import requests
import streamlit as st
from docx import Document
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ---------------------------- App Setup --------------------------------------
st.set_page_config(page_title="📄 DOCX → GPT (KB) → Canvas", layout="wide")
st.title("📄 Upload DOCX → Convert via GPT (Knowledge Base) → Upload to Canvas")

# ---------------------------- Session State ----------------------------------
def _init_state():
    defaults = {
        "pages": [],                        # list[dict]: parsed storyboard pages
        "gpt_results": {},                  # idx -> {"html":..., "quiz_json":...}
        "visualized": False,
        "vector_store_id": None,
        "assignments_cache": [],            # cached assignments for template pickers
        "assignments_loaded": False,
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

    # Template KB (Vector Store)
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
    canvas_domain = st.text_input("Canvas Base URL", placeholder="canvas.instructure.com")
    course_id = st.text_input("Canvas Course ID")
    canvas_token = st.text_input("Canvas API Token", type="password")
    openai_api_key = st.text_input("OpenAI API Key", type="password")

    use_new_quizzes = st.checkbox("Use New Quizzes (recommended)", value=True)
    dry_run = st.checkbox("🔍 Preview only (Dry Run)", value=False)
    if dry_run:
        st.info("No data will be sent to Canvas. (Duplication will be skipped.)", icon="ℹ️")

# ------------------------ Google Drive Helpers -------------------------------
def _gdoc_id_from_url(url: str):
    if not url:
        return None
    m = re.search(r"/d/([a-zA-Z0-9_-]+)", url)
    if m:
        return m.group(1)
    m = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", url)
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
    """Pull out everything between <canvas_page>...</canvas_page>"""
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

# ------------------------ Canvas helpers -------------------------------------
def _auth_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def list_course_assignments(domain, course_id, token, search_term=None, per_page=100):
    """Fetch up to per_page assignments (single page)."""
    url = f"https://{domain}/api/v1/courses/{course_id}/assignments"
    params = {"per_page": per_page}
    if search_term:
        params["search_term"] = search_term
    resp = requests.get(url, headers=_auth_headers(token), params=params)
    if resp.status_code != 200:
        st.warning(f"Could not list assignments: {resp.status_code}")
        return []
    return resp.json() or []

def ensure_assignments_cache():
    """Load assignments once if creds provided and not loaded yet."""
    if st.session_state.assignments_loaded:
        return
    if not (canvas_domain and course_id and canvas_token):
        return
    st.session_state.assignments_cache = list_course_assignments(canvas_domain, course_id, canvas_token, per_page=100)
    st.session_state.assignments_loaded = True

def find_assignment_id_by_name(domain, course_id, name, token):
    """Fallback: find newest exact name match."""
    assignments = list_course_assignments(domain, course_id, token, search_term=name, per_page=100)
    matches = [a for a in assignments if a.get("name", "").strip() == name.strip()]
    if not matches:
        return None
    def _key(a): return a.get("updated_at") or a.get("created_at") or ""
    return sorted(matches, key=_key, reverse=True)[0].get("id")

def poll_progress_until_done(domain, progress_id, token, timeout_s=180, interval_s=2):
    """Poll /progress for long jobs like assignment copy."""
    url = f"https://{domain}/api/v1/progress/{progress_id}"
    start = time.time()
    while True:
        r = requests.get(url, headers=_auth_headers(token))
        if r.status_code != 200:
            return None
        data = r.json()
        if data.get("workflow_state") in ("completed", "failed"):
            return data
        if time.time() - start > timeout_s:
            return data
        time.sleep(interval_s)

def copy_assignment(domain, course_id, template_assignment_id, new_name, token):
    """
    Try native duplication (keeps *everything*), else fallback to New-Quizzes-aware clone
    that replicates settings through the LTI API and returns a new assignment_id.
    """
    headers = _auth_headers(token)

    # ---- Attempt 1: native copy endpoint (best, if enabled) ----
    url_copy = f"https://{domain}/api/v1/courses/{course_id}/assignments/{template_assignment_id}/copy"
    try:
        resp = requests.post(url_copy, headers=headers, json={"name": new_name})
    except Exception as e:
        st.error(f"❌ Copy request failed to reach Canvas: {e}")
        resp = None

    if resp is not None and resp.status_code in (200, 201):
        p = resp.json()
        progress_id = p.get("id") or p.get("progress", {}).get("id")
        if progress_id:
            done = poll_progress_until_done(domain, progress_id, token)
            if done and done.get("workflow_state") == "completed":
                completion = done.get("completion") or done.get("results") or {}
                if isinstance(completion, dict):
                    return completion.get("assignment_id") or completion.get("id")
        # Some shards return the object directly:
        return p.get("assignment_id") or p.get("id")

    # If we got a hard error (common: 404/403), show a short diagnostic
    code = resp.status_code if resp is not None else "N/A"
    text_preview = (resp.text[:500] if (resp is not None and resp.text) else "").strip()
    st.warning(f"⚠️ Assignment copy not available (status {code}). Falling back to New Quiz clone.\nURL tried: {url_copy}\n{(text_preview or '')}")

    # ---- Attempt 2: Fallback clone for New Quizzes (LTI) ----
    # 2a) Read the template quiz settings
    url_get = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{template_assignment_id}"
    getq = requests.get(url_get, headers=headers)
    if getq.status_code != 200:
        st.error(f"❌ Couldn’t read New Quiz settings for template {template_assignment_id}: {getq.status_code} | {getq.text[:300]}")
        return None
    
    tmpl_id = str(p.get("template_assignment_id") or "").strip()
    if not tmpl_id.isdigit():
        st.error(f"Please choose a valid template assignment from the dropdown for '{p['page_title']}' (need a numeric ID, got: {tmpl_id or 'none'}).")
    else:
        new_asg_id = copy_assignment(canvas_domain, course_id, tmpl_id, p["page_title"], canvas_token)
    

    q = getq.json().get("quiz", getq.json())  # some shards wrap under "quiz"
    # Extract commonly-used settings; add more as your tenant supports them
    settings = {
        "title": new_name,
        "instructions": q.get("instructions") or q.get("description") or "",
        "points_possible": max(1, int(q.get("points_possible") or 1)),
    }
    # Optional settings (only set if present to avoid 400s on locked tenants)
    opt_map = {
        "time_limit": "time_limit",
        "allowed_attempts": "allowed_attempts",
        "one_question_at_a_time": "one_question_at_a_time",
        "show_correct_answers": "show_correct_answers",
        "shuffle_answers": "shuffle_answers",
        "scoring_policy": "scoring_policy",
        "access_code": "access_code",
        "ip_filter": "ip_filter",
        "due_at": "due_at",
        "lock_at": "lock_at",
        "unlock_at": "unlock_at",
    }
    for src, dest in opt_map.items():
        if q.get(src) is not None:
            settings[dest] = q[src]

    # 2b) Create a new New Quiz with those settings
    url_create = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes"
    payload = {"quiz": settings}
    create = requests.post(url_create, headers=headers, json=payload)
    if create.status_code not in (200, 201):
        st.error(f"❌ Fallback New Quiz creation failed: {create.status_code} | {create.text[:300]}")
        return None

    created = create.json()
    new_assignment_id = created.get("assignment_id") or created.get("id")
    if not new_assignment_id:
        # last-ditch—lookup by name
        return find_assignment_id_by_name(domain, course_id, new_name, token)
    return new_assignment_id


# ------------------------ Canvas Classic (Pages/Assign/Disc/Classic Quiz) ----
def get_or_create_module(module_name, domain, course_id, token, module_cache):
    if module_name in module_cache:
        return module_cache[module_name]
    url = f"https://{domain}/api/v1/courses/{course_id}/modules"
    resp = requests.get(url, headers=_auth_headers(token))
    if resp.status_code == 200:
        for m in resp.json():
            if m["name"].strip().lower() == module_name.strip().lower():
                module_cache[module_name] = m["id"]
                return m["id"]
    resp = requests.post(url, headers=_auth_headers(token), json={"module": {"name": module_name, "published": True}})
    if resp.status_code in (200, 201):
        mid = resp.json().get("id")
        module_cache[module_name] = mid
        return mid
    st.error(f"❌ Failed to create/find module: {module_name}")
    st.error(f"📬 Response: {resp.status_code} | {resp.text}")
    return None

def add_page(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/pages"
    payload = {"wiki_page": {"title": title, "body": html_body, "published": True}}
    resp = requests.post(url, headers=_auth_headers(token), json=payload)
    if resp.status_code in (200, 201):
        return resp.json().get("url")
    st.error(f"❌ Page create failed: {resp.text}")
    return None

def add_assignment(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/assignments"
    payload = {"assignment": {"name": title, "description": html_body, "published": True,
                              "submission_types": ["online_text_entry"], "points_possible": 10}}
    resp = requests.post(url, headers=_auth_headers(token), json=payload)
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    st.error(f"❌ Assignment create failed: {resp.text}")
    return None

def add_discussion(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
    payload = {"title": title, "message": html_body, "published": True}
    resp = requests.post(url, headers=_auth_headers(token), json=payload)
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    st.error(f"❌ Discussion create failed: {resp.text}")
    return None

def add_quiz(domain, course_id, title, description_html, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/quizzes"
    payload = {"quiz": {"title": title, "description": description_html or "", "published": True,
                        "quiz_type": "assignment", "scoring_policy": "keep_highest"}}
    resp = requests.post(url, headers=_auth_headers(token), json=payload)
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    st.error(f"❌ Quiz create failed: {resp.text}")
    return None

def add_quiz_question(domain, course_id, quiz_id, q):
    url = f"https://{domain}/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions"
    question_payload = {
        "question": {
            "question_name": q.get("question_name") or "Question",
            "question_text": q.get("question_text") or "",
            "question_type": "multiple_choice_question",
            "points_possible": 1,
            "answers": [{"text": a["text"], "weight": 100 if a.get("is_correct") else 0} for a in q.get("answers", [])]
        }
    }
    requests.post(url, headers=_auth_headers(canvas_token), json=question_payload)

def add_to_module(domain, course_id, module_id, item_type, ref, title, token):
    """item_type: Page | Quiz | Assignment | Discussion"""
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    payload = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = ref
    else:
        payload["module_item"]["content_id"] = ref
    resp = requests.post(url, headers=_auth_headers(token), json=payload)
    return resp.status_code in (200, 201)

# ------------------------ New Quizzes (LTI) APIs -----------------------------
def patch_new_quiz(domain, course_id, assignment_id, token, *, title=None, instructions_html=None, points_possible=None):
    """Update duplicated New Quiz metadata (only sends provided keys)."""
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}"
    body = {"quiz": {}}
    if title is not None:
        body["quiz"]["title"] = title
    if instructions_html is not None:
        body["quiz"]["instructions"] = instructions_html
    if points_possible is not None:
        body["quiz"]["points_possible"] = max(1, int(points_possible))
    if not body["quiz"]:
        return True
    resp = requests.patch(url, headers=_auth_headers(token), json=body)
    return resp.status_code in (200, 201)

def add_new_quiz_mcq(domain, course_id, assignment_id, q, token, position=1):
    """Insert a Choice item with shuffle + question/answer feedback."""
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    headers = _auth_headers(token)

    choices = []
    answer_feedback_map = {}
    correct_choice_id = None
    for idx, ans in enumerate(q.get("answers", []), start=1):
        cid = str(uuid.uuid4())
        ans_text = ans.get("text", "")
        choices.append({"id": cid, "position": idx, "itemBody": f"<p>{ans_text}</p>"})
        if ans.get("is_correct"):
            correct_choice_id = cid
        if ans.get("feedback"):
            answer_feedback_map[cid] = ans["feedback"]

    if not choices:
        st.warning("Skipping MCQ with no answers.")
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
        "scoring_algorithm": "Equivalence"
    }
    if feedback_block:
        entry["feedback"] = feedback_block
    if answer_feedback_map:
        entry["answer_feedback"] = answer_feedback_map

    payload = {"item": {"entry_type": "Item", "points_possible": 1, "position": position, "entry": entry}}
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code not in (200, 201):
        st.warning(f"⚠️ Failed to add item to New Quiz: {resp.status_code} | {resp.text}")

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
kb_cols = st.columns([1, 1, 1])
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

            if not module_name:
                h1 = re.search(r"<h1>(.*?)</h1>", block, flags=re.IGNORECASE | re.DOTALL)
                if h1:
                    module_name = h1.group(1).strip()
            if not module_name:
                m = re.search(r"\b(Module\s+[A-Za-z0-9 ]+)", page_title, flags=re.IGNORECASE)
                if m:
                    module_name = m.group(1).strip()
            if not module_name:
                module_name = last_known_module or "General"
            last_known_module = module_name

            template_type = extract_tag("template_type", block).strip()  # optional

            st.session_state.pages.append({
                "index": idx,
                "raw": block,
                "page_type": page_type,      # "page" | "assignment" | "discussion" | "quiz"
                "page_title": page_title,
                "module_name": module_name,
                "template_type": template_type,
                # quiz-only fields below (populated in edit/visualize)
                "template_assignment_id": None,
                "new_quiz_assignment_id": None,  # duplicated quiz id
                "new_quiz_populated": False      # items already injected
            })

        st.success(f"✅ Parsed {len(st.session_state.pages)} page(s).")

with col2:
    st.write("")

# ------------------------- Editable Page Table (Pre-GPT) ---------------------
if st.session_state.pages:
    st.subheader("2️⃣ Review & adjust page metadata (no GPT yet)")
    ensure_assignments_cache()  # try preloading (if creds present)

    for i, p in enumerate(st.session_state.pages):
        with st.expander(f"Page {i+1}: {p['page_title']} ({p['page_type']}) | Module: {p['module_name']}", expanded=False):
            c1, c2, c3, c4 = st.columns([1.1, 1, 1, 1])

            with c1:
                new_title = st.text_input("Page Title", value=p["page_title"], key=f"title_{i}")
            with c2:
                new_type = st.selectbox(
                    "Page Type", options=["page", "assignment", "discussion", "quiz"],
                    index=["page", "assignment", "discussion", "quiz"].index(p["page_type"]),
                    key=f"type_{i}"
                )
            with c3:
                new_module = st.text_input("Module Name", value=p["module_name"], key=f"module_{i}")
            with c4:
                new_template = st.text_input("Template Type (optional)", value=p["template_type"], key=f"tmpl_{i}")

            # Save basic fields
            p["page_title"] = new_title.strip() or p["page_title"]
            p["page_type"] = new_type
            p["module_name"] = new_module.strip() or p["module_name"]
            p["template_type"] = new_template.strip()

            # If this is a quiz, show the New Quiz template picker HERE
            if p["page_type"] == "quiz" and use_new_quizzes:
                st.markdown("**New Quiz Template to duplicate (keeps all settings):**")
                # Ensure assignments are loaded or provide a button
                if not st.session_state.assignments_cache:
                    if st.button("Load assignments", key=f"load_asg_{i}",
                                 disabled=not (canvas_domain and course_id and canvas_token)):
                        ensure_assignments_cache()

                if st.session_state.assignments_cache:
                    options = [f"{a.get('name','(no name)')}  —  ID:{a.get('id')}" for a in st.session_state.assignments_cache]
                    if options:
                        # If previously chosen, preselect it
                        def _index_for(id_):
                            for idx_opt, a in enumerate(st.session_state.assignments_cache):
                                if str(a.get("id")) == str(id_):
                                    return idx_opt
                            return 0
                        preset_idx = _index_for(p.get("template_assignment_id")) if p.get("template_assignment_id") else 0
                        picked = st.selectbox("Template assignment", options, index=min(preset_idx, len(options)-1), key=f"tmpl_pick_{i}")
                        if picked:
                            m = re.search(r"ID:(\d+)$", picked)
                            if m:
                                p["template_assignment_id"] = m.group(1)

# --------------------- Visualization Trigger (GPT + Duplication) ------------
st.divider()
visualize_clicked = st.button(
    "🔎 Visualize pages with GPT (KB) — duplicates New Quiz templates now",
    type="primary",
    use_container_width=True,
    disabled=not (openai_api_key and st.session_state.get("vector_store_id"))
)

if visualize_clicked:
    client = OpenAI(api_key=openai_api_key)
    st.session_state.gpt_results.clear()

    SYSTEM = (
        "You are an expert Canvas HTML generator.\n"
        "Use the file_search tool to find the exact or closest uMich template by name or structure.\n"
        "STRICT TEMPLATE RULES:\n"
        "- Reproduce template HTML verbatim (do NOT change/remove elements, classes, data-* attributes).\n"
        "- Preserve all <img> tags exactly (src, data-api-endpoint/returntype, width/height).\n"
        "- Only replace inner text/HTML in content areas; if a section has no content, remove just that section.\n"
        "- If a section the storyboard needs doesn't exist, create it with the same template structure.\n"
        "- <element_type> markups indicate template associations; <accordion_title>/<accordion_content> map to details/summary.\n"
        "- Convert any storyboard tables to real <table><tr><td> HTML; keep any <table> already in the storyboard verbatim.\n"
        "- Keep .bluePageHeader, .header, .divisionLineYellow, .landingPageFooter intact.\n\n"
        "QUIZ RULES (when <page_type> is 'quiz'):\n"
        "- Questions appear between <quiz_start> and </quiz_end>.\n"
        "- <multiple_choice> uses '*' prefix to mark correct choices.\n"
        "- <shuffle> inside a question → set \"shuffle\": true.\n"
        "- Question feedback tags (optional): <feedback_correct>, <feedback_incorrect>, <feedback_neutral>.\n"
        "- Per-answer feedback (optional): '(feedback: ...)' after a choice line or <feedback>A: ...</feedback>.\n"
        "RETURN:\n"
        "1) Canvas-ready HTML (no code fences)\n"
        "2) If page_type is 'quiz', append a JSON object at the very END ONLY with:\n"
        "{ \"quiz_description\": \"<html>\", \"questions\": [\n"
        "  {\"question_name\":\"...\",\"question_text\":\"...\",\n"
        "   \"answers\":[{\"text\":\"A\",\"is_correct\":false,\"feedback\":\"<p>...</p>\"},{\"text\":\"B\",\"is_correct\":true}],\n"
        "   \"shuffle\": true,\n"
        "   \"feedback\": {\"correct\":\"<p>...</p>\",\"incorrect\":\"<p>...</p>\",\"neutral\":\"<p>...</p>\"}\n"
        "  }\n"
        "]}\n"
        "COVERAGE (NO-DROP) RULES\n"
        "- Do not omit or summarize substantive content from the storyboard block.\n"
        "- Every sentence/line between <canvas_page>…</canvas_page> must appear in the output HTML.\n"
        "- If something doesn’t clearly map, append it at the end under:\n"
        "  <div class=\"divisionLineYellow\"><h2>Additional Content</h2><div>…unplaced items in order…</div></div>\n"
        "- Preserve content order. Never remove explicit <img>, <table>, <a href>… or HTML already present in the storyboard.\n"
    )

    with st.spinner("Generating HTML and duplicating New Quizzes…"):
        for p in st.session_state.pages:
            idx = p["index"]
            raw_block = p["raw"]

            # Prepare user prompt
            user_prompt = (
                f'Use template_type="{p["template_type"] or "auto"}" if it matches a known template; '
                "otherwise choose best fit.\n\n"
                "Storyboard page block:\n"
                f"{raw_block}"
            )

            # Run GPT with File Search (Vector Store)
            response = client.responses.create(
                model="gpt-4o",
                input=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user_prompt}
                ],
                tools=[{
                    "type": "file_search",
                    "vector_store_ids": [st.session_state["vector_store_id"]]
                }]
            )

            raw_out = (response.output_text or "").strip()
            cleaned = re.sub(r"```(html|json)?", "", raw_out, flags=re.IGNORECASE).strip()

            # Extract trailing JSON for quizzes
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

            # If this is a quiz and a template is selected → duplicate NOW and inject content
            if p["page_type"] == "quiz" and use_new_quizzes and p.get("template_assignment_id") and not dry_run:
                # 1) Duplicate template (keeps settings)
                new_asg_id = copy_assignment(
                    canvas_domain, course_id, p["template_assignment_id"], p["page_title"], canvas_token
                )
                if not new_asg_id:
                    st.error(f"❌ Could not duplicate New Quiz for: {p['page_title']}")
                else:
                    p["new_quiz_assignment_id"] = new_asg_id
                    # 2) Update title/instructions
                    description = html_result
                    if quiz_json and isinstance(quiz_json, dict) and "quiz_description" in quiz_json:
                        description = quiz_json.get("quiz_description") or html_result
                    ok = patch_new_quiz(
                        canvas_domain, course_id, new_asg_id, canvas_token,
                        title=p["page_title"], instructions_html=description
                    )
                    if not ok:
                        st.warning(f"⚠️ Could not update quiz instructions for: {p['page_title']}")

                    # 3) Add items (MCQ)
                    if quiz_json and isinstance(quiz_json, dict):
                        for pos, q in enumerate(quiz_json.get("questions", []), start=1):
                            if q.get("answers"):
                                add_new_quiz_mcq(canvas_domain, course_id, new_asg_id, q, canvas_token, position=pos)
                    p["new_quiz_populated"] = True
                    st.success(f"✅ Duplicated & populated New Quiz: {p['page_title']} (Assignment ID {new_asg_id})")
            elif p["page_type"] == "quiz" and use_new_quizzes and dry_run:
                st.info(f"Dry run: Skipped duplication for quiz '{p['page_title']}'.", icon="⏸️")

    st.session_state.visualized = True
    st.success("✅ Visualization complete. (Quizzes duplicated & populated where selected.)")

# ---------------------------- Preview & Upload -------------------------------
if st.session_state.pages and st.session_state.visualized:
    st.subheader("3️⃣ Previews. Upload to Canvas modules when ready.")

    module_cache = {}
    any_uploaded = False

    colA, colB = st.columns([1, 2])
    with colA:
        upload_all_clicked = st.button(
            "🚀 Add ALL to Modules",
            type="secondary",
            disabled=not (canvas_domain and course_id and canvas_token)
        )
    with colB:
        st.caption("This step only adds items to the selected module; no duplication here.")

    for p in st.session_state.pages:
        idx = p["index"]
        meta = f"{p['page_title']} ({p['page_type']}) | Module: {p['module_name']}"
        with st.expander(f"📄 {meta}", expanded=False):
            html_result = st.session_state.gpt_results.get(idx, {}).get("html", "")
            quiz_json = st.session_state.gpt_results.get(idx, {}).get("quiz_json")
            st.code(html_result or "[No HTML returned]", language="html")

            can_upload = bool(canvas_domain and course_id and canvas_token)
            if st.button(f"Add '{p['page_title']}' to Module", key=f"upl_{idx}", disabled=not can_upload):
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
                    if use_new_quizzes:
                        # If already duplicated during Visualize, just add to module
                        if p.get("new_quiz_assignment_id"):
                            if add_to_module(canvas_domain, course_id, mid, "Assignment", p["new_quiz_assignment_id"], p["page_title"], canvas_token):
                                any_uploaded = True
                                st.success("✅ Existing New Quiz added to module.")
                        else:
                            # Fallback (no template chosen or dry run earlier): create minimal New Quiz now
                            create_url = f"https://{canvas_domain}/api/quiz/v1/courses/{course_id}/quizzes"
                            description = html_result
                            if quiz_json and isinstance(quiz_json, dict) and "quiz_description" in quiz_json:
                                description = quiz_json.get("quiz_description") or html_result
                            cr = requests.post(create_url, headers=_auth_headers(canvas_token),
                                               json={"quiz": {"title": p["page_title"], "instructions": description, "points_possible": 1}})
                            if cr.status_code in (200, 201):
                                new_id = cr.json().get("assignment_id") or cr.json().get("id")
                                if quiz_json and isinstance(quiz_json, dict):
                                    for pos, q in enumerate(quiz_json.get("questions", []), start=1):
                                        if q.get("answers"):
                                            add_new_quiz_mcq(canvas_domain, course_id, new_id, q, canvas_token, position=pos)
                                if add_to_module(canvas_domain, course_id, mid, "Assignment", new_id, p["page_title"], canvas_token):
                                    any_uploaded = True
                                    st.success("✅ New Quiz created & added to module.")
                            else:
                                st.error("❌ New Quiz creation failed.")
                    else:
                        # Classic quiz fallback
                        description = html_result
                        if quiz_json and isinstance(quiz_json, dict) and "quiz_description" in quiz_json:
                            description = quiz_json.get("quiz_description") or html_result
                        qid = add_quiz(canvas_domain, course_id, p["page_title"], description, canvas_token)
                        if qid:
                            if quiz_json and isinstance(quiz_json, dict):
                                for q in quiz_json.get("questions", []):
                                    add_quiz_question(canvas_domain, course_id, qid, q)
                            if add_to_module(canvas_domain, course_id, mid, "Quiz", qid, p["page_title"], canvas_token):
                                any_uploaded = True
                                st.success("✅ Classic Quiz created (with questions) & added to module.")
                        else:
                            st.error("❌ Classic Quiz creation failed.")
                else:
                    st.warning(f"Unsupported page_type: {p['page_type']}")

    if upload_all_clicked and (canvas_domain and course_id and canvas_token):
        for p in st.session_state.pages:
            mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, {})
            if not mid:
                continue
            html_result = st.session_state.gpt_results.get(p["index"], {}).get("html", "")
            quiz_json = st.session_state.gpt_results.get(p["index"], {}).get("quiz_json")

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
                if use_new_quizzes:
                    if p.get("new_quiz_assignment_id"):
                        add_to_module(canvas_domain, course_id, mid, "Assignment", p["new_quiz_assignment_id"], p["page_title"], canvas_token)
                        any_uploaded = True
                        st.toast(f"Added existing New Quiz: {p['page_title']}", icon="✅")
                    else:
                        # Create minimal new quiz now
                        description = html_result
                        if quiz_json and isinstance(quiz_json, dict) and "quiz_description" in quiz_json:
                            description = quiz_json.get("quiz_description") or html_result
                        create_url = f"https://{canvas_domain}/api/quiz/v1/courses/{course_id}/quizzes"
                        cr = requests.post(create_url, headers=_auth_headers(canvas_token),
                                           json={"quiz": {"title": p["page_title"], "instructions": description, "points_possible": 1}})
                        if cr.status_code in (200, 201):
                            new_id = cr.json().get("assignment_id") or cr.json().get("id")
                            if quiz_json and isinstance(quiz_json, dict):
                                for pos, q in enumerate(quiz_json.get("questions", []), start=1):
                                    if q.get("answers"):
                                        add_new_quiz_mcq(canvas_domain, course_id, new_id, q, canvas_token, position=pos)
                            add_to_module(canvas_domain, course_id, mid, "Assignment", new_id, p["page_title"], canvas_token)
                            any_uploaded = True
                            st.toast(f"Uploaded New Quiz: {p['page_title']}", icon="✅")
                else:
                    description = html_result
                    if quiz_json and isinstance(quiz_json, dict) and "quiz_description" in quiz_json:
                        description = quiz_json.get("quiz_description") or html_result
                    qid = add_quiz(canvas_domain, course_id, p["page_title"], description, canvas_token)
                    if qid:
                        if quiz_json and isinstance(quiz_json, dict):
                            for q in quiz_json.get("questions", []):
                                add_quiz_question(canvas_domain, course_id, qid, q)
                        add_to_module(canvas_domain, course_id, mid, "Quiz", qid, p["page_title"], canvas_token)
                        any_uploaded = True
                        st.toast(f"Uploaded Classic Quiz: {p['page_title']}", icon="✅")

        if not any_uploaded:
            st.warning("No items added to modules. Check credentials and try again.")

# ----------------------------- UX Guidance -----------------------------------
has_story = bool(uploaded_file or (gdoc_url and sa_json))
if not has_story:
    st.info("Provide a storyboard (.docx upload or Google Doc URL + SA JSON), then click **Parse storyboard**.", icon="📝")
elif has_story and not st.session_state.pages:
    st.warning("Click **Parse storyboard** to begin (no GPT call yet).", icon="👉")
elif st.session_state.pages and not st.session_state.visualized:
    if not st.session_state.get("vector_store_id"):
        st.warning("Set up the Template Knowledge Base first (Create Vector Store, then upload your template), then click **Visualize**.", icon="📚")
    else:
        st.info("Review & adjust page metadata above, pick a New Quiz template in quiz rows, then click **Visualize**.", icon="🔎")
