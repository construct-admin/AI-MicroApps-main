# canvas_import_um.py
# -----------------------------------------------------------------------------
# 📄 DOCX/Google Doc → GPT (with Knowledge Base) → Canvas (Pages/Assignments/
# Discussions/New Quizzes)
#
# This build fixes:
# - TypeError from isinstance(Document) by normalizing inputs to python-docx.
# - Tables & paragraphs are read in document order.
# - Tables are converted to HTML for GPT + Canvas and never dropped.
# - Works for uploaded .docx and Google Docs (export → .docx).
# -----------------------------------------------------------------------------

from io import BytesIO
import uuid
import json
import re
import requests
import streamlit as st

from openai import OpenAI

# --- python-docx imports (block walker) ---
from docx import Document as DocxDocument
from docx.document import Document as _DocxDocument
from docx.table import _Cell, Table
from docx.text.paragraph import Paragraph
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P

# --- Google Drive export ---
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# -----------------------------------------------------------------------------
# Streamlit UI
# -----------------------------------------------------------------------------
st.set_page_config(page_title="📄 DOCX → GPT (KB) → Canvas", layout="wide")
st.title("📄 Upload DOCX → Convert via GPT (Knowledge Base) → Upload to Canvas")

def _init_state():
    defaults = {
        "pages": [],
        "gpt_results": {},      # key: page_idx -> {"html":..., "quiz_json":...}
        "visualized": False,
        "vector_store_id": None,
    }
    for k, v in defaults.items():
        if k not in st.session_state:
            st.session_state[k] = v

_init_state()

# -----------------------------------------------------------------------------
# Sidebar
# -----------------------------------------------------------------------------
with st.sidebar:
    st.header("Setup")

    # Storyboard sources
    uploaded_file = st.file_uploader("Storyboard (.docx)", type="docx")
    st.subheader("Or pull storyboard from Google Docs")
    gdoc_url = st.text_input("Storyboard Google Doc URL (shareable to your SA)")
    sa_json = st.file_uploader("Service Account JSON (for Drive read)", type=["json"])

    # Template KB (Vector Store) management
    st.subheader("Template Knowledge Base")
    vs_cols = st.columns(2)
    with vs_cols[0]:
        existing_vs = st.text_input("Vector Store ID (optional)", value=st.session_state.get("vector_store_id") or "")
    with vs_cols[1]:
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
        st.info("No data will be sent to Canvas. This is a preview only.", icon="ℹ️")

# -----------------------------------------------------------------------------
# Google Drive helpers
# -----------------------------------------------------------------------------
def _gdoc_id_from_url(url: str):
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

# -----------------------------------------------------------------------------
# python-docx block walker + HTML helpers
# -----------------------------------------------------------------------------
def _safe_open_docx(doc_source) -> _DocxDocument:
    """
    Accepts Streamlit UploadedFile, BytesIO, bytes, file path, or already-open
    python-docx Document. Returns a python-docx Document.
    """
    if isinstance(doc_source, _DocxDocument):
        return doc_source
    if hasattr(doc_source, "read"):  # Streamlit UploadedFile or file-like
        # Streamlit UploadedFile may be a SpooledTemporaryFile — rebuffer to BytesIO
        data = doc_source.read()
        return DocxDocument(BytesIO(data))
    if isinstance(doc_source, (bytes, bytearray)):
        return DocxDocument(BytesIO(doc_source))
    if isinstance(doc_source, BytesIO):
        return DocxDocument(doc_source)
    # assume path-like
    return DocxDocument(doc_source)

def _iter_block_items(parent):
    """
    Yield paragraphs and tables in document order. Works for Document or _Cell.
    """
    if isinstance(parent, _DocxDocument):
        parent_elm = parent.element.body
    elif isinstance(parent, _Cell):
        parent_elm = parent._tc
    else:
        raise ValueError("Unsupported container for block iteration")

    for child in parent_elm.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)

def _escape_html(text: str) -> str:
    # If the storyboard paragraph already contains angle brackets, assume it's
    # intentional HTML and pass through; otherwise escape minimal.
    if "<" in text and ">" in text:
        return text
    return (
        text.replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
    )

def _paragraph_to_html(p: Paragraph) -> str:
    # Join runs to preserve inline italics/bold is complex; here we use plain text
    # and preserve any explicit HTML the author included.
    txt = p.text or ""
    txt = _escape_html(txt)
    if not txt.strip():
        return ""
    # If user typed a raw tag like <h2>...</h2> keep it; otherwise wrap <p>
    if re.search(r"</?\w+[^>]*>", txt):
        return txt
    return f"<p>{txt}</p>"

def _table_to_html(tbl: Table) -> str:
    rows_html = []
    for r in tbl.rows:
        cells_html = []
        for c in r.cells:
            # Flatten cell content: paragraphs + nested tables (rare)
            parts = []
            for item in _iter_block_items(c):
                if isinstance(item, Paragraph):
                    h = _paragraph_to_html(item)
                    if h:
                        parts.append(h)
                elif isinstance(item, Table):
                    parts.append(_table_to_html(item))
            cells_html.append(f"<td>{''.join(parts) or '&nbsp;'}</td>")
        rows_html.append(f"<tr>{''.join(cells_html)}</tr>")
    return f"<table>{''.join(rows_html)}</table>"

# -----------------------------------------------------------------------------
# Extract storyboard pages (keeps tables!)
# -----------------------------------------------------------------------------
def extract_canvas_pages(storyboard_docx_source):
    """
    Pull out raw text/HTML between <canvas_page> ... </canvas_page>.
    Preserves tables (converted to <table> HTML) and paragraphs in order.
    """
    doc = _safe_open_docx(storyboard_docx_source)

    pages = []
    buf = []
    inside = False

    def _flush():
        nonlocal buf
        if buf:
            pages.append("\n".join(buf))
            buf = []

    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            txt = (block.text or "").strip()
            lo = txt.lower()
            if "<canvas_page>" in lo:
                inside = True
                buf.append(txt)  # keep the open tag line
                continue
            if "</canvas_page>" in lo:
                buf.append(txt)
                _flush()
                inside = False
                continue
            if inside:
                h = _paragraph_to_html(block)
                if h:
                    buf.append(h)

        elif isinstance(block, Table):
            if inside:
                buf.append(_table_to_html(block))

    # In case a page was never properly closed but we reached EOF
    _flush()
    return pages

def extract_tag(tag, block):
    m = re.search(fr"<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""

# -----------------------------------------------------------------------------
# Canvas (classic + new quizzes) — unchanged core behavior
# -----------------------------------------------------------------------------
def get_or_create_module(module_name, domain, course_id, token, module_cache):
    if module_name in module_cache:
        return module_cache[module_name]
    url = f"https://{domain}/api/v1/courses/{course_id}/modules"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        for m in resp.json():
            if m["name"].strip().lower() == module_name.strip().lower():
                module_cache[module_name] = m["id"]
                return m["id"]
    resp = requests.post(url, headers=headers, json={"module": {"name": module_name, "published": True}})
    if resp.status_code in (200, 201):
        mid = resp.json().get("id")
        module_cache[module_name] = mid
        return mid
    else:
        st.error(f"❌ Failed to create/find module: {module_name}")
        st.error(f"📬 Response: {resp.status_code} | {resp.text}")
        return None

def add_page(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/pages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"wiki_page": {"title": title, "body": html_body, "published": True}}
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code in (200, 201):
        return resp.json().get("url")
    st.error(f"❌ Page create failed: {resp.text}")
    return None

def add_assignment(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/assignments"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"assignment": {"name": title, "description": html_body, "published": True,
                              "submission_types": ["online_text_entry"], "points_possible": 10}}
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    st.error(f"❌ Assignment create failed: {resp.text}")
    return None

def add_discussion(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"title": title, "message": html_body, "published": True}
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    st.error(f"❌ Discussion create failed: {resp.text}")
    return None

def add_quiz(domain, course_id, title, description_html, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/quizzes"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"quiz": {"title": title, "description": description_html or "", "published": True,
                        "quiz_type": "assignment", "scoring_policy": "keep_highest"}}
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    st.error(f"❌ Quiz create failed: {resp.text}")
    return None

def add_quiz_question(domain, course_id, quiz_id, q):
    url = f"https://{domain}/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions"
    headers = {"Authorization": f"Bearer {canvas_token}", "Content-Type": "application/json"}
    question_payload = {
        "question": {
            "question_name": q.get("question_name") or "Question",
            "question_text": q.get("question_text") or "",
            "question_type": "multiple_choice_question",
            "points_possible": 1,
            "answers": [{"text": a["text"], "weight": 100 if a.get("is_correct") else 0}
                        for a in q.get("answers", [])]
        }
    }
    requests.post(url, headers=headers, json=question_payload)

def add_to_module(domain, course_id, module_id, item_type, ref, title, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = ref
    else:
        payload["module_item"]["content_id"] = ref
    resp = requests.post(url, headers=headers, json=payload)
    return resp.status_code in (200, 201)

# ------------------------ New Quizzes (LTI) ----------------------------------
def add_new_quiz(domain, course_id, title, description_html, token, points_possible=1):
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"quiz": {"title": title, "points_possible": max(points_possible, 1),
                        "instructions": description_html or ""}}
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code in (200, 201):
        data = resp.json()
        return data.get("assignment_id") or data.get("id")
    st.error(f"❌ New Quiz create failed: {resp.status_code} | {resp.text}")
    return None

def add_new_quiz_mcq(domain, course_id, assignment_id, q, token, position=1):
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Choices with optional per-answer feedback
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

    shuffle = bool(q.get("shuffle", False))
    properties = {"shuffleRules": {"choices": {"toLock": [], "shuffled": shuffle}},
                  "varyPointsByAnswer": False}

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

    item_payload = {"item": {"entry_type": "Item", "points_possible": 1,
                             "position": position, "entry": entry}}
    resp = requests.post(url, headers=headers, json=item_payload)
    if resp.status_code not in (200, 201):
        st.warning(f"⚠️ Failed to add item to New Quiz: {resp.status_code} | {resp.text}")

# -----------------------------------------------------------------------------
# OpenAI Vector Store (KB)
# -----------------------------------------------------------------------------
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

def _kb_fetch_bytes():
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

kb_ctrls = st.columns([1, 1, 1])
with kb_ctrls[0]:
    if st.button("Create Vector Store", use_container_width=True):
        client = ensure_client()
        vs_id = create_vector_store(client)
        st.session_state.vector_store_id = vs_id
        st.success(f"✅ Created Vector Store: {vs_id}")

with kb_ctrls[1]:
    if st.button("Upload Template to KB", use_container_width=True,
                 disabled=not (st.session_state.get("vector_store_id") or existing_vs)):
        client = ensure_client()
        vs_id = (st.session_state.get("vector_store_id") or existing_vs).strip()
        got = _kb_fetch_bytes()
        if not vs_id:
            st.error("Vector Store ID missing.")
        elif not got:
            st.error("Provide a template .docx or Google Doc URL + SA JSON.")
        else:
            data, fname = got
            upload_file_to_vs(client, vs_id, data, fname)
            st.success("✅ Template uploaded to KB.")

with kb_ctrls[2]:
    if st.button("Use Existing VS ID", use_container_width=True):
        if existing_vs.strip():
            st.session_state.vector_store_id = existing_vs.strip()
            st.success(f"✅ Using Vector Store: {st.session_state.vector_store_id}")
        else:
            st.error("Paste a Vector Store ID first.")

# -----------------------------------------------------------------------------
# Parse storyboard → pages
# -----------------------------------------------------------------------------
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

            template_type = extract_tag("template_type", block).strip()

            st.session_state.pages.append({
                "index": idx,
                "raw": block,
                "page_type": page_type,      # "page" | "assignment" | "discussion" | "quiz"
                "page_title": page_title,
                "module_name": module_name,
                "template_type": template_type
            })

        st.success(f"✅ Parsed {len(st.session_state.pages)} page(s).")

with col2:
    st.write("")

# -----------------------------------------------------------------------------
# Pre-GPT metadata edit
# -----------------------------------------------------------------------------
if st.session_state.pages:
    st.subheader("2️⃣ Review & adjust page metadata (no GPT yet)")
    for i, p in enumerate(st.session_state.pages):
        with st.expander(f"Page {i+1}: {p['page_title']} ({p['page_type']}) | Module: {p['module_name']}", expanded=False):
            c1, c2, c3, c4 = st.columns([1.1, 1, 1, 1])
            with c1:
                new_title = st.text_input("Page Title", value=p["page_title"], key=f"title_{i}")
            with c2:
                new_type = st.selectbox("Page Type",
                                        options=["page", "assignment", "discussion", "quiz"],
                                        index=["page", "assignment", "discussion", "quiz"].index(p["page_type"]),
                                        key=f"type_{i}")
            with c3:
                new_module = st.text_input("Module Name", value=p["module_name"], key=f"module_{i}")
            with c4:
                new_template = st.text_input("Template Type (optional)", value=p["template_type"], key=f"tmpl_{i}")

            p["page_title"] = new_title.strip() or p["page_title"]
            p["page_type"] = new_type
            p["module_name"] = new_module.strip() or p["module_name"]
            p["template_type"] = new_template.strip()

    st.divider()
    visualize_clicked = st.button(
        "🔎 Visualize pages with GPT (via Knowledge Base — no upload yet)",
        type="primary", use_container_width=True,
        disabled=not (openai_api_key and st.session_state.get("vector_store_id"))
    )

    if visualize_clicked:
        client = OpenAI(api_key=openai_api_key)
        st.session_state.gpt_results.clear()

        SYSTEM = (
            "You are an expert Canvas HTML generator.\n"
            "Use the file_search tool to find the exact or closest uMich template by name or structure.\n"
            "COVERAGE (NO-DROP) RULES\n"
            "- Do not omit or summarize any substantive content from the storyboard block.\n"
            "- Every sentence/line from the storyboard (between <canvas_page>…</canvas_page>) MUST appear in the output HTML.\n"
            "- If a piece of storyboard content doesn’t clearly map to a template section, append it under a new section at the end:\n"
            "  <div class=\"divisionLineYellow\"><h2>Additional Content</h2><div>…unplaced items in original order…</div></div>\n"
            "- Preserve the original order of content as much as possible.\n"
            "- Never remove <img>, <table>, or any explicit HTML already present in the storyboard; include them verbatim.\n"
            "STRICT TEMPLATE RULES:\n"
            "- Reproduce template HTML verbatim (do NOT change or remove elements, attributes, classes, data-*).\n"
            "- Preserve all <img> tags exactly (src, data-api-endpoint/returntype, width/height).\n"
            "- Only replace inner text/HTML in content areas (headings, paragraphs, lists);\n"
            "  if a section has no content, remove the template section in place; append extra sections at the end.\n"
            "- if a section does not exist in the template, create it with the same structure.\n"
            "- <element_type> tags are used to mark template code associations found within the file_search.\n"
            "- <accordion_title> are used for the summary tag in html accordions.\n"
            "- <accordion_content> are used for the content inside the accordion.\n"
            "- table formatting must be converted to HTML tables with <table>, <tr>, <td> tags.\n"
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
            "{ \"quiz_description\": \"<html>\", \"questions\": [\n"
            "  {\"question_name\":\"...\",\"question_text\":\"...\",\n"
            "   \"answers\":[{\"text\":\"A\",\"is_correct\":false,\"feedback\":\"<p>...</p>\"}, {\"text\":\"B\",\"is_correct\":true}],\n"
            "   \"shuffle\": true,\n"
            "   \"feedback\": {\"correct\":\"<p>...</p>\",\"incorrect\":\"<p>...</p>\",\"neutral\":\"<p>...</p>\"}\n"
            "  }\n"
            "]}\n"
            
        )

        with st.spinner("Generating HTML for all pages via GPT + KB..."):
            for p in st.session_state.pages:
                idx = p["index"]
                raw_block = p["raw"]
                user_prompt = (
                    f'Use template_type="{p["template_type"] or "auto"}" if it matches a known template; '
                    "otherwise choose best fit.\n\n"
                    "Storyboard page block:\n"
                    f"{raw_block}"
                )

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

                raw_out = response.output_text or ""
                cleaned = re.sub(r"```(html|json)?", "", raw_out, flags=re.IGNORECASE).strip()

                # Pull LAST {...} JSON block (quiz meta) if present
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
        st.success("✅ Visualization complete. Preview below and upload when ready.")

# -----------------------------------------------------------------------------
# Preview & Upload
# -----------------------------------------------------------------------------
if st.session_state.pages and st.session_state.visualized:
    st.subheader("3️⃣ Previews (post-GPT). Upload to Canvas when ready.")

    module_cache = {}
    any_uploaded = False

    top_cols = st.columns([1, 2])
    with top_cols[0]:
        upload_all_clicked = st.button(
            "🚀 Upload ALL to Canvas",
            type="secondary",
            disabled=dry_run or not (canvas_domain and course_id and canvas_token)
        )
    with top_cols[1]:
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
                                    add_quiz_question(canvas_domain, course_id, qid, q)
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
                                add_quiz_question(canvas_domain, course_id, qid, q)
                        add_to_module(canvas_domain, course_id, mid, "Quiz", qid, p["page_title"], canvas_token)
                        any_uploaded = True
                        st.toast(f"Uploaded Classic Quiz: {p['page_title']}", icon="✅")

        if not any_uploaded:
            st.warning("No items uploaded. Check your tokens/IDs and try again.")

# -----------------------------------------------------------------------------
# UX Guidance
# -----------------------------------------------------------------------------
has_story = bool(uploaded_file or (gdoc_url and sa_json))
if not has_story:
    st.info("Provide a storyboard (.docx upload or Google Doc URL + SA JSON), then click **Parse storyboard**.", icon="📝")
elif has_story and not st.session_state.pages:
    st.warning("Click **Parse storyboard** to begin (no GPT call yet).", icon="👉")
elif st.session_state.pages and not st.session_state.visualized:
    if not st.session_state.get("vector_store_id"):
        st.warning("Set up the Template Knowledge Base first (Create Vector Store, then upload your template), then click **Visualize**.", icon="📚")
    else:
        st.info("Review & adjust page metadata above, then click **Visualize pages with GPT**.", icon="🔎")
 