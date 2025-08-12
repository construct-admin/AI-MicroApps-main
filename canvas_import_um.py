# canvas_import_um.py
# -----------------------------------------------------------------------------
# 📄 DOCX → GPT → Canvas (Multi-Page)
#
# What this app does:
# 1) Extracts <canvas_page> blocks from your storyboard .docx (or Google Doc).
# 2) Extracts "template pages" and "components" from uMich_template_code.docx (or Google Doc).
# 3) Lets you review & edit page metadata (title/type/module/template).
# 4) Only when you click "Visualize pages with GPT" does it convert to HTML.
# 5) Lets you upload one page at a time OR "Upload ALL" to Canvas.
# 6) Uses Canvas New Quizzes for quizzes and supports per-question shuffle.
# -----------------------------------------------------------------------------

from io import BytesIO
import uuid
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
import streamlit as st
from docx import Document
from openai import OpenAI
import requests
import re
import json

# ---------------------------- UI & State -------------------------------------
st.set_page_config(page_title="📄 DOCX → GPT → Canvas (Multi-Page)", layout="wide")
st.title("📄 Upload DOCX → Convert via GPT → Upload to Canvas")

if "pages" not in st.session_state:
    st.session_state.pages = []          # list[dict] each page's parsed meta + raw block
if "templates" not in st.session_state:
    st.session_state.templates = {"page": {}, "component": {}}  # parsed from uMich template
if "gpt_results" not in st.session_state:
    st.session_state.gpt_results = {}    # key: page_idx, value: {"html":..., "quiz_json":...}
if "visualized" not in st.session_state:
    st.session_state.visualized = False  # did we run GPT yet?

# ------------------------ Inputs / Credentials -------------------------------
with st.sidebar:
    st.header("Setup")

    # Storyboard sources
    uploaded_file = st.file_uploader("Storyboard (.docx)", type="docx")
    st.subheader("Pull Storyboard from Google Docs")
    gdoc_url = st.text_input("Google Doc URL (share with the Service Account)")
    sa_json = st.file_uploader("Service Account JSON (for Drive read)", type=["json"])

    # Template sources
    template_file = st.file_uploader("uMich Template Code (.docx)", type="docx")
    gdoc_url_template = st.text_input("Template Google Doc URL (optional)")

    # Canvas + OpenAI
    canvas_domain = st.text_input("Canvas Base URL", placeholder="canvas.instructure.com")
    course_id = st.text_input("Canvas Course ID")
    canvas_token = st.text_input("Canvas API Token", type="password")
    openai_api_key = st.text_input("OpenAI API Key", type="password")

    # Quiz engine toggle (default New Quizzes ON)
    use_new_quizzes = st.checkbox("Use New Quizzes (recommended)", value=True)

    # Mode
    dry_run = st.checkbox("🔍 Preview only (Dry Run)", value=False)
    if dry_run:
        st.info("No data will be sent to Canvas. This is a preview only.", icon="ℹ️")

# ------------------------- Google Drive Helpers ------------------------------
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

# ------------------------- Helper: Template Parser ---------------------------
def extract_templates_and_components(template_docx_file):
    """
    Parse uMich_template_code.docx into two dictionaries:
      - template_pages: e.g., {"Module Overview Page": "<div>...</div>", ...}
      - components:     e.g., {"Accordion A": "<div class='umich-accordion-a'>...</div>", ...}
    """
    doc = Document(template_docx_file)
    lines = [p.text for p in doc.paragraphs]
    text = "\n".join([ln for ln in lines])

    blocks = re.split(r"(?=^#\.\s|\[TEMPLATE\]|\[TEMPLATE ELEMENT\])", text, flags=re.MULTILINE)

    template_pages = {}
    components = {}

    for block in blocks:
        b = block.strip()
        if not b:
            continue
        header_line = b.splitlines()[0].strip()
        is_page_template = header_line.startswith("#.")
        is_component = ("[TEMPLATE ELEMENT]" in header_line) or header_line.startswith("[TEMPLATE]")

        if is_page_template:
            h2_match = re.search(r"<h2[^>]*>(.*?)</h2>", b, flags=re.IGNORECASE | re.DOTALL)
            if h2_match:
                key = re.sub(r"\s+", " ", h2_match.group(1).strip())
            else:
                key = re.sub(r"^#\.\s*", "", header_line).strip()
            template_pages[key] = b
        elif is_component:
            key = re.sub(r"^\[TEMPLATE(?:\sELEMENT)?\]\s*", "", header_line).strip()
            components[key] = b
        else:
            if '<div class="canvasPageCon"' in b:
                h2_match = re.search(r"<h2[^>]*>(.*?)</h2>", b, flags=re.IGNORECASE | re.DOTALL)
                key = re.sub(r"\s+", " ", h2_match.group(1).strip()) if h2_match else header_line
                template_pages[key] = b
            else:
                components[header_line] = b

    normalized_pages = {}
    for k, v in template_pages.items():
        norm = k.lower()
        normalized_pages[k] = v
        normalized_pages[norm] = v
        if "overview" in norm:
            normalized_pages["module_overview"] = v
        if "video page" in norm:
            normalized_pages["video_page"] = v
        if "two video page" in norm:
            normalized_pages["two_video_page"] = v
        if "three video page" in norm:
            normalized_pages["three_video_page"] = v
        if "reading page" in norm:
            normalized_pages["reading_page"] = v
        if "activity page" in norm:
            normalized_pages["activity_page"] = v
        if "assignment instructions" in norm:
            normalized_pages["assignment_instructions"] = v

    normalized_components = {}
    for k, v in components.items():
        norm = k.lower()
        normalized_components[k] = v
        normalized_components[norm] = v
        if "accordion" in norm:
            normalized_components["accordion"] = v
        if "call out" in norm or "callout" in norm:
            normalized_components["callout"] = v
        if "table" in norm:
            normalized_components["table"] = v

    return normalized_pages, normalized_components

# -------------------------- Helper: Storyboard Parser ------------------------
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
    """Safe text extraction for tags like <page_type>...</page_type> (case-insensitive)."""
    m = re.search(fr"<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""

# ------------------------------ Canvas API (Classic) -------------------------
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
    payload = {
        "assignment": {
            "name": title,
            "description": html_body,
            "published": True,
            "submission_types": ["online_text_entry"],
            "points_possible": 10
        }
    }
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

# Classic quiz (kept as optional fallback)
def add_quiz(domain, course_id, title, description_html, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/quizzes"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "quiz": {
            "title": title,
            "description": description_html or "",
            "published": True,
            "quiz_type": "assignment",
            "scoring_policy": "keep_highest"
        }
    }
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
            "answers": [
                {"text": a["text"], "weight": 100 if a.get("is_correct") else 0}
                for a in q.get("answers", [])
            ]
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

# ------------------------------ Canvas API (New Quizzes) ---------------------
def add_new_quiz(domain, course_id, title, description_html, token, points_possible=1):
    """
    Create a New Quiz (LTI) and return its assignment_id.
    """
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "quiz": {
            "title": title,
            "points_possible": max(points_possible, 1),
            "instructions": description_html or ""
        }
    }
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code in (200, 201):
        data = resp.json()
        return data.get("assignment_id") or data.get("id")
    st.error(f"❌ New Quiz create failed: {resp.status_code} | {resp.text}")
    return None

def add_new_quiz_mcq(domain, course_id, assignment_id, q, token, position=1):
    """
    Create a Multiple Choice (choice) item in a New Quiz.

    q format (extends your quiz_json):
    {
      "question_name": "Q1",
      "question_text": "Text or HTML",
      "answers": [{"text":"A","is_correct":false}, {"text":"B","is_correct":true}],
      "shuffle": true     # optional; default False
    }
    """
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Build choices with UUIDs and 1-based positions
    choices = []
    correct_choice_id = None
    for idx, ans in enumerate(q.get("answers", []), start=1):
        cid = str(uuid.uuid4())
        choices.append({
            "id": cid,
            "position": idx,
            "itemBody": f"<p>{ans.get('text','')}</p>"
        })
        if ans.get("is_correct"):
            correct_choice_id = cid

    if not choices:
        st.warning("Skipping MCQ with no answers.")
        return
    if not correct_choice_id:
        # Fallback: first choice correct if none flagged
        correct_choice_id = choices[0]["id"]

    properties = {
        "shuffleRules": {
            "choices": {
                "toLock": [],  # you could pass indexes to lock if you need
                "shuffled": bool(q.get("shuffle", False))
            }
        },
        "varyPointsByAnswer": False
    }

    item_payload = {
        "item": {
            "entry_type": "Item",
            "points_possible": 1,
            "position": position,
            "entry": {
                "interaction_type_slug": "choice",
                "title": q.get("question_name") or "Question",
                "item_body": q.get("question_text") or "",
                "calculator_type": "none",
                "interaction_data": {
                    "choices": choices
                },
                "properties": properties,
                "scoring_data": {
                    "value": correct_choice_id
                },
                "scoring_algorithm": "Equivalence"
            }
        }
    }

    resp = requests.post(url, headers=headers, json=item_payload)
    if resp.status_code not in (200, 201):
        st.warning(f"⚠️ Failed to add item to New Quiz: {resp.status_code} | {resp.text}")

# ------------------------- Extraction / Preparation --------------------------
col1, col2 = st.columns([1, 2])
with col1:
    has_story = bool(uploaded_file or (gdoc_url and sa_json))
    has_template = bool(template_file or (gdoc_url_template and sa_json))

    if st.button(
        "1️⃣ Parse storyboard & templates",
        type="primary",
        use_container_width=True,
        disabled=not (has_story and has_template)
    ):
        # Reset prior runs
        st.session_state.pages = []
        st.session_state.gpt_results.clear()
        st.session_state.visualized = False

        # ----- Resolve storyboard source (file upload OR Google Doc) -----
        story_source = uploaded_file
        if not story_source and gdoc_url and sa_json:
            fid = _gdoc_id_from_url(gdoc_url)
            if fid:
                try:
                    story_source = fetch_docx_from_gdoc(fid, sa_json.read())
                except Exception as e:
                    st.error(f"❌ Could not fetch Google Doc (storyboard): {e}")

        if not story_source:
            st.error("Please upload a storyboard .docx OR provide a Google Doc URL + Service Account JSON.")
            st.stop()

        raw_pages = extract_canvas_pages(story_source)

        # ----- Resolve template source (file upload OR Google Doc) -----
        tmpl_source = template_file
        if not tmpl_source and gdoc_url_template and sa_json:
            fid_t = _gdoc_id_from_url(gdoc_url_template)
            if fid_t:
                try:
                    tmpl_source = fetch_docx_from_gdoc(fid_t, sa_json.read())
                except Exception as e:
                    st.error(f"❌ Could not fetch Google Doc (template): {e}")

        if not tmpl_source:
            st.error("Please upload the uMich template .docx OR provide its Google Doc URL.")
            st.stop()

        # Extract templates and components
        template_pages, components = extract_templates_and_components(tmpl_source)
        st.session_state.templates = {"page": template_pages, "component": components}

        # Convert raw blocks → editable meta rows
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
                "template_type": template_type
            })

        st.success(f"✅ Parsed {len(st.session_state.pages)} page(s) and loaded templates/components.")

with col2:
    st.write("")

# ------------------------- Editable Page Table (Pre-GPT) ---------------------
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

            # Save back to session
            p["page_title"] = new_title.strip() or p["page_title"]
            p["page_type"] = new_type
            p["module_name"] = new_module.strip() or p["module_name"]
            p["template_type"] = new_template.strip()

    # --------------------- Visualization Trigger (GPT run) -------------------
    st.divider()
    visualize_clicked = st.button(
        "🔎 Visualize pages with GPT (no Canvas upload yet)",
        type="primary",
        use_container_width=True,
        disabled=not openai_api_key
    )

    if visualize_clicked:
        client = OpenAI(api_key=openai_api_key)
        st.session_state.gpt_results.clear()
        template_pages = st.session_state.templates["page"]
        components = st.session_state.templates["component"]

        with st.spinner("Generating HTML for all pages via GPT..."):
            for p in st.session_state.pages:
                idx = p["index"]
                raw_block = p["raw"]

                # --- System prompt (trimmed, with shuffle guidance) ---
                system_prompt = f"""
You are an expert Canvas HTML generator.

Match storyboard tags to the provided uMich Canvas templates/components and output Canvas-ready HTML.
If the page is a quiz, also output a JSON object (see schema) that lists MCQ questions.

VIDEO TEMPLATES:
- If a page has 2 videos, choose the "two video" template; 3 videos -> "three video" template, etc.

QUIZ RULES:
- Questions appear between <quiz_start> and </quiz_end>.
- <multiple_choice> blocks use "*" prefix to mark the correct choice.
- If <shuffle> appears inside a question block, set "shuffle": true for that question in JSON; else false.
- Return JSON ONLY at the very end (after the HTML) and with no code fences.

RETURN:
1) HTML (no code fences)
2) If page_type is "quiz", append JSON like:
{{
  "quiz_description": "<html description>",
  "questions": [
    {{
      "question_name": "Q1",
      "question_text": "Text or HTML",
      "answers": [{{"text":"A","is_correct":false}},{{"text":"B","is_correct":true}}],
      "shuffle": true
    }}
  ]
}}

TEMPLATE PAGES (keys → html, truncated):
{json.dumps({k: (template_pages[k][:400] + ' ... [truncated]') for k in list(template_pages.keys())[:30]}, ensure_ascii=False)}

COMPONENTS (keys → html, truncated):
{json.dumps({k: (components[k][:300] + ' ... [truncated]') for k in list(components.keys())[:30]}, ensure_ascii=False)}
"""

                user_prompt = f"""
Use template_type="{p['template_type'] or 'auto'}" if it matches a known template; otherwise choose best fit.

Storyboard page block:
{raw_block}
"""

                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt}
                    ],
                    temperature=0.2
                )

                raw = response.choices[0].message.content.strip()
                cleaned = re.sub(r"```(html|json)?", "", raw, flags=re.IGNORECASE).strip()

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
                    # --- NEW vs CLASSIC ---
                    description = html_result
                    if quiz_json and isinstance(quiz_json, dict) and "quiz_description" in quiz_json:
                        description = quiz_json.get("quiz_description") or html_result

                    if use_new_quizzes:
                        # New Quiz: create, add MCQs (with shuffle), then add to module as Assignment
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
                        # Classic fallback
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

# ----------------------------- UX Guidance -----------------------------------
has_story = bool(uploaded_file or (gdoc_url and sa_json))
has_template = bool(template_file or (gdoc_url_template and sa_json))

if not has_story or not has_template:
    st.info("Provide a storyboard (.docx or Google Doc) and the template (.docx or Google Doc), then click **Parse storyboard & templates**.", icon="📝")
elif has_story and has_template and not st.session_state.pages:
    st.warning("Click **Parse storyboard & templates** to begin (no GPT call yet).", icon="👉")
elif st.session_state.pages and not st.session_state.visualized:
    st.info("Review & adjust page metadata above, then click **Visualize pages with GPT**.", icon="🔎")
