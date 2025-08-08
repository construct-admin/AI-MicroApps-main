# canvas_import_um.py
# -----------------------------------------------------------------------------
# 📄 DOCX → GPT → Canvas (Multi-Page)
#
# What this app does:
# 1) Extracts <canvas_page> blocks from your storyboard .docx (no GPT yet).
# 2) Extracts "template pages" and "components" from uMich_template_code.docx.
# 3) Lets you review & edit page metadata (title/type/module/template).
# 4) Only when you click "Visualize pages with GPT" does it convert to HTML.
# 5) Lets you upload one page at a time OR "Upload ALL" to Canvas.
#
# Supported Canvas content types: Page, Assignment, Discussion, Quiz (MCQ)
# Upload flow: Create/Find Module → Create content → Add module item
#
# Notes:
# - Quiz questions are parsed from a JSON object that GPT appends at the END
#   of the message. It should look like:
#   {
#     "quiz_description": "<html description>",
#     "questions": [
#       {"question_name": "Q1", "question_text": "Text", "answers":[
#         {"text":"A", "is_correct": false}, {"text":"B", "is_correct": true}
#       ]}
#     ]
#   }
# - We look for the last {...} JSON block in the GPT response (safe fallback).
# - We *don't* automatically re-run GPT when you change small things — that only
#   happens when you click "Visualize pages with GPT".
# -----------------------------------------------------------------------------


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
    uploaded_file = st.file_uploader("Storyboard (.docx)", type="docx")
    template_file = st.file_uploader("uMich Template Code (.docx)", type="docx")

    canvas_domain = st.text_input("Canvas Base URL", placeholder="canvas.instructure.com")
    course_id = st.text_input("Canvas Course ID")
    canvas_token = st.text_input("Canvas API Token", type="password")
    openai_api_key = st.text_input("OpenAI API Key", type="password")

    dry_run = st.checkbox("🔍 Preview only (Dry Run)", value=False)
    if dry_run:
        st.info("No data will be sent to Canvas. This is a preview only.", icon="ℹ️")

# ------------------------- Helper: Template Parser ---------------------------
def extract_templates_and_components(template_docx_file):
    """
    Parse uMich_template_code.docx into two dictionaries:
      - template_pages: e.g., {"Module Overview Page": "<div>...</div>", "Video Page": "<div>...</div>", ...}
      - components:     e.g., {"Accordion A": "<div class='umich-accordion-a'>...</div>", ...}

    Heuristics:
    - Sections that start with '#.' are considered page templates (look at your doc).
      We'll use the first <h2> (or line after header) as key if present, else the header text.
    - Sections labeled '[TEMPLATE] Something' or 'TEMPLATE ELEMENT' become components.
    """
    doc = Document(template_docx_file)
    # Join with newlines to keep simple. We’ll split on headings.
    lines = [p.text for p in doc.paragraphs]

    # Collapse multiple blank lines to one (cleaner parsing)
    text = "\n".join([ln for ln in lines])

    # Split roughly by big headers that look like '#.' or '[TEMPLATE]' or 'TEMPLATE ELEMENT'
    # We'll capture headers to know the block type.
    blocks = re.split(r"(?=^#\.\s|\[TEMPLATE\]|\[TEMPLATE ELEMENT\])", text, flags=re.MULTILINE)

    template_pages = {}
    components = {}

    for block in blocks:
        b = block.strip()
        if not b:
            continue

        # Identify header
        header_line = b.splitlines()[0].strip()

        # Determine "type"
        is_page_template = header_line.startswith("#.")
        is_component = ("[TEMPLATE ELEMENT]" in header_line) or header_line.startswith("[TEMPLATE]")

        # Key/name heuristics:
        # For page templates, try to find a friendly name on the first <h2> line or header itself.
        if is_page_template:
            # Find first <h2> content as template name if present
            h2_match = re.search(r"<h2[^>]*>(.*?)<\/h2>", b, flags=re.IGNORECASE | re.DOTALL)
            if h2_match:
                key = re.sub(r"\s+", " ", h2_match.group(1).strip())
            else:
                key = re.sub(r"^#\.\s*", "", header_line).strip()
            # Page HTML = everything after the header line (best effort)
            html = b
            template_pages[key] = html

        elif is_component:
            # Component name is the header line (clean it up)
            key = re.sub(r"^\[TEMPLATE(?:\sELEMENT)?\]\s*", "", header_line).strip()
            html = b
            components[key] = html

        else:
            # Some templates in your doc also start with text like "Post-Course Survey"
            # If it contains a full page structure (<div class="canvasPageCon">...), treat as page template
            if '<div class="canvasPageCon"' in b:
                # Try to extract a name
                h2_match = re.search(r"<h2[^>]*>(.*?)<\/h2>", b, flags=re.IGNORECASE | re.DOTALL)
                if h2_match:
                    key = re.sub(r"\s+", " ", h2_match.group(1).strip())
                else:
                    key = header_line
                template_pages[key] = b
            else:
                # Or treat as component if smaller snippet
                components[header_line] = b

    # Normalize keys – allow simple lookups by a friendlier alias set
    normalized_pages = {}
    for k, v in template_pages.items():
        norm = k.lower()
        normalized_pages[k] = v
        normalized_pages[norm] = v  # convenience

        # Additional aliases you can expand:
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
        # aliases (expand as needed)
        if "accordion" in norm:
            normalized_components["accordion"] = v
        if "call out" in norm or "callout" in norm:
            normalized_components["callout"] = v
        if "table" in norm:
            normalized_components["table"] = v

    return normalized_pages, normalized_components


# -------------------------- Helper: Storyboard Parser ------------------------
def extract_canvas_pages(storyboard_docx_file):
    """
    Pull out everything between <canvas_page>...</canvas_page>
    Returns a list of raw blocks (strings).
    """
    doc = Document(storyboard_docx_file)
    pages = []
    current_block = []
    inside_block = False
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
    """
    Safe text extraction for tags like <page_type> ... </page_type>.
    Case-insensitive. Returns "" if not found.
    """
    m = re.search(fr"<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""


# ------------------------------ Canvas API -----------------------------------
def get_or_create_module(module_name, domain, course_id, token, module_cache):
    if module_name in module_cache:
        return module_cache[module_name]
    url = f"https://{domain}/api/v1/courses/{course_id}/modules"
    headers = {"Authorization": f"Bearer {token}"}

    # Try to find existing
    resp = requests.get(url, headers=headers)
    if resp.status_code == 200:
        for m in resp.json():
            if m["name"].strip().lower() == module_name.strip().lower():
                module_cache[module_name] = m["id"]
                return m["id"]

    # Create if not found
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
        return resp.json().get("url")  # page_url for module item
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
    """
    q format:
    {
      "question_name": "Q1",
      "question_text": "Text",
      "answers": [{"text":"A", "is_correct": false}, ...]
    }
    """
    url = f"https://{domain}/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions"
    headers = {"Authorization": f"Bearer {canvas_token}", "Content-Type": "application/json"}

    # Only MCQ implemented here by design
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
    requests.post(url, headers=headers, json=question_payload)  # No hard fail if a single item errors


def add_to_module(domain, course_id, module_id, item_type, ref, title, token):
    """
    Adds the created item into the module:
      - For Page:     item_type="Page",     ref = page_url
      - For Quiz:     item_type="Quiz",     ref = quiz_id
      - For Assignment:item_type="Assignment", ref = assignment_id
      - For Discussion:item_type="Discussion", ref = discussion_id
    """
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    payload = {"module_item": {"title": title, "type": item_type, "published": True}}

    if item_type == "Page":
        payload["module_item"]["page_url"] = ref
    else:
        payload["module_item"]["content_id"] = ref

    resp = requests.post(url, headers=headers, json=payload)
    return resp.status_code in (200, 201)


# ------------------------- Extraction / Preparation --------------------------
col1, col2 = st.columns([1, 2])
with col1:
    if st.button("1️⃣ Parse storyboard & templates", type="primary", use_container_width=True,
                 disabled=not (uploaded_file and template_file)):
        # Reset prior runs
        st.session_state.pages = []
        st.session_state.gpt_results.clear()
        st.session_state.visualized = False

        # Extract pages first (no GPT yet)
        raw_pages = extract_canvas_pages(uploaded_file)

        # Extract templates and components
        template_pages, components = extract_templates_and_components(template_file)
        st.session_state.templates = {"page": template_pages, "component": components}

        # Convert raw blocks → editable meta rows
        last_known_module = None
        for idx, block in enumerate(raw_pages):
            page_type = (extract_tag("page_type", block).lower() or "page").strip()
            page_title = extract_tag("page_title", block) or f"Page {idx+1}"
            module_name = extract_tag("module_name", block).strip()

            # Fallbacks for module_name
            if not module_name:
                # If storyboard includes <h1> at the top, use that as module name
                h1 = re.search(r"<h1>(.*?)</h1>", block, flags=re.IGNORECASE | re.DOTALL)
                if h1:
                    module_name = h1.group(1).strip()
            if not module_name:
                # Derive from title like "3.0 Module Three Overview"
                m = re.search(r"\b(Module\s+[A-Za-z0-9 ]+)", page_title, flags=re.IGNORECASE)
                if m:
                    module_name = m.group(1).strip()
            if not module_name:
                module_name = last_known_module or "General"
            last_known_module = module_name

            template_type = extract_tag("template_type", block).strip()  # optional tag in storyboard

            st.session_state.pages.append({
                "index": idx,
                "raw": block,                # untouched raw block for GPT
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

                # Build the system prompt from template dictionaries (compact)
                # NOTE: We pass a compactified version to keep tokens low.
                # You can further trim if needed.
                system_prompt = f"""
You are an expert Canvas HTML generator.
Below is a set of uMich Canvas LMS HTML templates and components; match them to the storyboard tags.

You are an expert Canvas HTML generator.
Below is a set of uMich Canvas LMS HTML templates followed by a storyboard page using tags.

Match the tags to the templates and convert the storyboard content to styled HTML for Canvas.

TEMPLATES:
{template_text}

TAGS YOU WILL SEE:
<canvas_page> = start of Canvas page
</canvas_page> = end of Canvas page
<page_type> = Canvas page type
<template_type> = type of template to use for the page
<page_title> = title of the page
<module_name> = name of the module
<quiz_title> = title of the quiz
<question> = question block.
<quiz_start> = start of quiz questions to be imported
<multiple_choice> = multiple choice question
* before a choice = correct answer

Return:
1. HTML content for the page (no ```html tags)
2. If page_type is quiz, also return structured JSON after a blank line, for example:

    {{
      "quiz_description": "<html description>",
      "questions": [
        {{"question_name": "...", "question_text": "...", "answers": [
          {{"text": "...", "is_correct": true}}
        ]}}
      ]
    }}
    
TEMPLATE PAGES (keys → html):
{json.dumps({k: (template_pages[k][:400] + ' ... [truncated]') for k in list(template_pages.keys())[:30]}, ensure_ascii=False)}

COMPONENTS (keys → html):
{json.dumps({k: (components[k][:300] + ' ... [truncated]') for k in list(components.keys())[:30]}, ensure_ascii=False)}

Storyboard tags:
- <canvas_page> boundary mark
- <page_type>, <page_title>, <module_name>, (optional) <template_type>
- <question> blocks (for quizzes). '*' prefix marks correct answers.
- Only return clean HTML for body (no ``` fences).
- If page_type is "quiz", append a JSON object at the very END with quiz metadata:
  {{
    "quiz_description": "<html description>",
    "questions": [
      {{"question_name": "...", "question_text": "...",
        "answers": [{{"text":"...", "is_correct":true}}, ...]
      }}
    ]
  }}
"""

                # User content:
                # We pass the raw block *plus* the resolved template_type so the model can pick the right one.
                user_prompt = f"""
Use template_type="{p['template_type'] or 'auto'}" if it matches a known template page; otherwise choose best fit.

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
                # Strip code fences if any
                cleaned = re.sub(r"```(html|json)?", "", raw, flags=re.IGNORECASE).strip()

                # Pull the LAST {...} JSON block (quiz meta) if present
                json_match = re.search(r"({[\s\S]+})\s*$", cleaned)
                quiz_json = None
                html_result = cleaned
                if json_match and p["page_type"] == "quiz":
                    try:
                        quiz_json = json.loads(json_match.group(1))
                        html_result = cleaned[:json_match.start()].strip()
                    except Exception:
                        # Keep going with just HTML
                        quiz_json = None

                st.session_state.gpt_results[idx] = {
                    "html": html_result,
                    "quiz_json": quiz_json
                }

        st.session_state.visualized = True
        st.success("✅ Visualization complete. Preview below and upload when ready.")

# ---------------------------- Preview & Upload -------------------------------
if st.session_state.pages and st.session_state.visualized:
    st.subheader("3️⃣ Previews (post-GPT). Upload to Canvas when ready.")

    module_cache = {}
    any_uploaded = False

    # Global Upload ALL
    colA, colB = st.columns([1, 2])
    with colA:
        upload_all_clicked = st.button("🚀 Upload ALL to Canvas", type="secondary",
                                       disabled=dry_run or not (canvas_domain and course_id and canvas_token))
    with colB:
        if dry_run:
            st.info("Dry run is ON — uploads are disabled.", icon="⏸️")

    # Iterate pages with per-page upload
    for p in st.session_state.pages:
        idx = p["index"]
        meta = f"{p['page_title']} ({p['page_type']}) | Module: {p['module_name']}"
        with st.expander(f"📄 {meta}", expanded=False):
            html_result = st.session_state.gpt_results.get(idx, {}).get("html", "")
            quiz_json = st.session_state.gpt_results.get(idx, {}).get("quiz_json")

            st.code(html_result or "[No HTML returned]", language="html")

            # Per-page upload button
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
                    # Create quiz, then add questions
                    description = html_result
                    if quiz_json and isinstance(quiz_json, dict) and "quiz_description" in quiz_json:
                        description = quiz_json.get("quiz_description") or html_result

                    qid = add_quiz(canvas_domain, course_id, p["page_title"], description, canvas_token)
                    if qid:
                        # Add questions (if any)
                        if quiz_json and isinstance(quiz_json, dict):
                            for q in quiz_json.get("questions", []):
                                add_quiz_question(canvas_domain, course_id, qid, q)
                        # Add to module
                        if add_to_module(canvas_domain, course_id, mid, "Quiz", qid, p["page_title"], canvas_token):
                            any_uploaded = True
                            st.success("✅ Quiz created (with questions) & added to module.")
                    else:
                        st.error("❌ Quiz creation failed.")

                else:
                    st.warning(f"Unsupported page_type: {p['page_type']}")

    # Handle Upload ALL
    if upload_all_clicked and (not dry_run):
        template_pages = st.session_state.templates["page"]  # not used here, but left for parity
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

                qid = add_quiz(canvas_domain, course_id, p["page_title"], description, canvas_token)
                if qid:
                    # Add questions
                    if quiz_json and isinstance(quiz_json, dict):
                        for q in quiz_json.get("questions", []):
                            add_quiz_question(canvas_domain, course_id, qid, q)
                    add_to_module(canvas_domain, course_id, mid, "Quiz", qid, p["page_title"], canvas_token)
                    any_uploaded = True
                    st.toast(f"Uploaded quiz: {p['page_title']}", icon="✅")

        if not any_uploaded:
            st.warning("No items uploaded. Check your tokens/IDs and try again.")


# ----------------------------- UX Guidance -----------------------------------
if not uploaded_file or not template_file:
    st.info("Upload both the storyboard and template files in the sidebar, then click **Parse storyboard & templates**.", icon="📝")
elif uploaded_file and template_file and not st.session_state.pages:
    st.warning("Click **Parse storyboard & templates** to begin (no GPT call yet).", icon="👉")
elif st.session_state.pages and not st.session_state.visualized:
    st.info("Review & adjust page metadata above, then click **Visualize pages with GPT**.", icon="🔎")
