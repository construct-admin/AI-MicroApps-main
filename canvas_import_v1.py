# canvas_import_v2.py
# -----------------------------------------------------------------------------
# 📄 DOCX/Google Doc → GPT (with optional KB) → Canvas
# Split workflow to reduce tokens/timeouts:
#   1) Pages   2) New Quizzes (duplicate template assignment)   3) Discussions
#
# - New Quizzes: list template assignments (LTI), duplicate, inject items,
#   keep settings, then rename to storyboard <page_title>.
# - Coverage: do not drop storyboard content; preserve links, images, tables.
# -----------------------------------------------------------------------------

from io import BytesIO
import uuid
import json
import re
import html
import requests
import streamlit as st
from docx import Document
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

# ---------------------------- App Setup --------------------------------------
st.set_page_config(page_title="📄 DOCX → GPT → Canvas (Split Steps)", layout="wide")
st.title("📄 DOCX/Google Doc → GPT → Canvas (Split Steps)")

# ---------------------------- Session State ----------------------------------
def _init_state():
    defaults = {
        "pages": [],                        # parsed storyboard pages (dicts)
        "gpt_results": {},                  # page_idx -> {"html":..., "quiz_json":...}
        "vector_store_id": None,            # OpenAI file_search KB (optional)
        "new_quiz_templates": [],           # list of assignments that are New Quizzes (LTI)
        "selected_for_pages": set(),        # selected page idx for Pages step
        "selected_for_quizzes": set(),      # selected page idx for New Quizzes step
        "selected_for_discussions": set(),  # selected page idx for Discussions step
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

    # Optional Template KB (OpenAI Vector Store)
    st.subheader("Template Knowledge Base (optional)")
    existing_vs = st.text_input("Vector Store ID (optional)", value=st.session_state.get("vector_store_id") or "")
    kb_docx = st.file_uploader("Upload template DOCX to KB (optional)", type=["docx"])
    kb_gdoc_url = st.text_input("Template Google Doc URL to KB (optional)")

    # Canvas + OpenAI
    st.subheader("Canvas & OpenAI")
    canvas_domain = st.text_input("Canvas Base URL", placeholder="yourdomain.instructure.com")
    course_id = st.text_input("Canvas Course ID")
    canvas_token = st.text_input("Canvas API Token", type="password")
    openai_api_key = st.text_input("OpenAI API Key", type="password")

    dry_run = st.checkbox("🔍 Preview only (Dry Run)", value=False)
    if dry_run:
        st.info("No data will be sent to Canvas in Dry Run.", icon="ℹ️")

# ------------------------ Helpers: Google Drive & Storyboard -----------------
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

def extract_canvas_pages(storyboard_docx_file):
    """Pull out everything between <canvas_page>...</canvas_page>."""
    doc = Document(storyboard_docx_file)
    pages, current_block, inside_block = [], [], False
    for para in doc.paragraphs:
        text = para.text
        low = text.lower().strip()
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
    return (m.group(1).strip() if m else "")

# ------------------------ OpenAI Client / KB ---------------------------------
def ensure_client():
    if not openai_api_key:
        st.error("OpenAI API key is required.")
        st.stop()
    return OpenAI(api_key=openai_api_key)

def kb_upload_if_provided():
    """Optionally create/upload a DOCX into a Vector Store for file_search."""
    if not openai_api_key:
        return
    client = ensure_client()
    vs_id = (st.session_state.get("vector_store_id") or existing_vs).strip()
    if not vs_id:
        return
    got = None
    if kb_docx is not None:
        got = (BytesIO(kb_docx.getvalue()), kb_docx.name)
    elif kb_gdoc_url and sa_json:
        fid = _gdoc_id_from_url(kb_gdoc_url)
        if fid:
            try:
                got = (fetch_docx_from_gdoc(fid, sa_json.read()), "template_from_gdoc.docx")
            except Exception as e:
                st.error(f"❌ Could not fetch Template Google Doc: {e}")
    if not got:
        return
    data, fname = got
    f = client.files.create(file=(fname, data), purpose="assistants")
    client.vector_stores.files.create(vector_store_id=vs_id, file_id=f.id)
    st.success("✅ Uploaded template DOCX into Vector Store.")

# ------------------------ Canvas API: Modules/Pages/Discussions --------------
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
        st.error(f"{resp.status_code} | {resp.text}")
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

def add_discussion(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"title": title, "message": html_body, "published": True}
    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    st.error(f"❌ Discussion create failed: {resp.text}")
    return None

def add_to_module(domain, course_id, module_id, item_type, ref, title, token):
    """
    item_type: "Page" → ref page_url
               "Discussion" → ref discussion_id
               "Assignment" → ref assignment_id (for New Quizzes)
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

# ------------------------ Canvas API: New Quizzes (LTI) ----------------------
def list_new_quiz_templates(canvas_domain, course_id, token):
    """
    List Assignments that are New Quizzes (external tool). Returns:
    [{id, name, url}] where url is the new-quiz launch URL.
    """
    url = f"https://{canvas_domain}/api/v1/courses/{course_id}/assignments"
    headers = {"Authorization": f"Bearer {token}"}
    params = {"per_page": 100}
    templates = []

    while url:
        resp = requests.get(url, headers=headers, params=params)
        if resp.status_code != 200:
            st.error(f"Error fetching assignments: {resp.status_code} {resp.text}")
            return []
        for a in resp.json():
            ext = a.get("external_tool_tag_attributes") or {}
            ext_url = ext.get("url", "")
            # Heuristic: New Quiz LTI URLs contain "quizzes" or "quiz-lti"
            if ext_url and ("quizzes" in ext_url or "quiz" in ext_url):
                templates.append({"id": a["id"], "name": a["name"], "url": ext_url})
        url = resp.links.get('next', {}).get('url')
        params = None  # after first call links drive paging

    return templates

def copy_assignment(canvas_domain, course_id, template_assignment_id, new_name, token):
    """
    Try to copy an assignment (works for classic/ext tool in many instances).
    If 404, we’ll fall back to 'clone' the New Quiz via quiz LTI API.
    Returns new assignment_id or None.
    """
    url = f"https://{canvas_domain}/api/v1/courses/{course_id}/assignments/{template_assignment_id}/copy"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    body = {"name": new_name, "publish": False}
    resp = requests.post(url, headers=headers, json=body)
    if resp.status_code in (200, 201):
        return resp.json().get("id")

    st.warning(
        f"Assignment copy not available (status {resp.status_code}). Falling back to New Quiz clone. URL tried:\n{url}\n{resp.text[:500]}"
    )
    # Fallback: New Quiz clone (if the assignment is a New Quiz)
    # Clone endpoint (empirical): /api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/clone
    clone_url = f"https://{canvas_domain}/api/quiz/v1/courses/{course_id}/quizzes/{template_assignment_id}/clone"
    resp2 = requests.post(clone_url, headers=headers, json={"new_title": new_name})
    if resp2.status_code in (200, 201):
        data = resp2.json()
        # Canvas returns new quiz data; assignment_id is typically "assignment_id"
        return data.get("assignment_id") or data.get("id")

    st.error(f"❌ Clone failed: {resp2.status_code} | {resp2.text[:600]}")
    return None

def rename_assignment(canvas_domain, course_id, assignment_id, new_name, token):
    url = f"https://{canvas_domain}/api/v1/courses/{course_id}/assignments/{assignment_id}"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    resp = requests.put(url, headers=headers, json={"assignment": {"name": new_name}})
    return resp.status_code in (200, 201)

def add_new_quiz_mcq(canvas_domain, course_id, assignment_id, q, token, position=1):
    """
    Create an MCQ item in New Quiz with shuffle + feedback.
    q must be like:
      {
        "question_name": "...",
        "question_text": "<p>...</p>",
        "answers": [
          {"text":"...", "is_correct":true, "feedback":"<p>...</p>"},
          ...
        ],
        "shuffle": true,
        "feedback": {"correct":"<p>...</p>", "incorrect":"<p>...</p>", "neutral":"<p>...</p>"}
      }
    """
    url = f"https://{canvas_domain}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

    # Build choices with stable IDs
    choices = []
    answer_feedback_map = {}
    correct_choice_id = None
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

    item_payload = {"item": {"entry_type": "Item", "points_possible": 1, "position": position, "entry": entry}}
    resp = requests.post(url, headers=headers, json=item_payload)
    if resp.status_code not in (200, 201):
        st.warning(f"⚠️ Failed to add item: {resp.status_code} | {resp.text[:600]}")

# ------------------------ GPT prompts (coverage, preserve) -------------------
SYSTEM = (
            "You are an expert Canvas HTML generator.\n"
            "Use the file_search tool to find the exact or closest uMich template by name or structure.\n"
            "COVERAGE (NO-DROP) RULES\n"
            "- Do not omit or summarize any substantive content from the storyboard block.\n"
            "- Every sentence/line from the storyboard (between <canvas_page>…</canvas_page>) MUST appear in the output HTML.\n"
            "- If a piece of storyboard content doesn’t clearly map to a template section, add it to the page as it appears in the storyboard:\n"
            "- Preserve the original order of content as much as possible.\n"
            "- Never remove <img>, <table>, or any explicit HTML already present in the storyboard; include them verbatim.\n"
            "STRICT TEMPLATE RULES:\n"
            "- Reproduce template HTML verbatim (do NOT change or remove elements, attributes, classes, data-*).\n"
            "- Preserve all <img> tags exactly (src, data-api-endpoint/returntype, width/height).\n"
            "- Preserve all links in the content.\n"
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

def run_gpt_for_block(client: OpenAI, vector_store_id: str | None, raw_block: str, template_hint: str):
    user_prompt = (
        f'Use template_type="{template_hint or "auto"}" if it matches a known template; '
        "otherwise choose best fit.\n\nStoryboard page block:\n" + raw_block
    )

    if vector_store_id:
        resp = client.responses.create(
            model="gpt-4o",
            input=[
                {"role": "system", "content": SYSTEM},
                {"role": "user", "content": user_prompt}
            ],
            tools=[{"type": "file_search", "vector_store_ids": [vector_store_id]}]
        )
    else:
        resp = client.responses.create(
            model="gpt-4o",
            input=[{"role": "system", "content": SYSTEM}, {"role": "user", "content": user_prompt}]
        )

    raw_out = (resp.output_text or "").strip()
    cleaned = re.sub(r"```(html|json)?", "", raw_out, flags=re.IGNORECASE).strip()

    # Pull LAST JSON block for quizzes
    json_match = re.search(r"({[\s\S]+})\s*$", cleaned)
    quiz_json = None
    html_result = cleaned
    if json_match:
        try:
            quiz_json = json.loads(json_match.group(1))
            html_result = cleaned[:json_match.start()].strip()
        except Exception:
            quiz_json = None
    return html_result, quiz_json

# ------------------------ STEP 0: Parse storyboard ---------------------------
st.subheader("Step 0 — Parse storyboard")
col0a, col0b = st.columns([1, 2])
with col0a:
    if st.button("Parse storyboard", type="primary", use_container_width=True,
                 disabled=not (uploaded_file or (gdoc_url and sa_json))):
        st.session_state.pages.clear()
        st.session_state.gpt_results.clear()
        st.session_state.selected_for_pages.clear()
        st.session_state.selected_for_quizzes.clear()
        st.session_state.selected_for_discussions.clear()

        story_source = uploaded_file
        if not story_source and gdoc_url and sa_json:
            fid = _gdoc_id_from_url(gdoc_url)
            if fid:
                try:
                    story_source = fetch_docx_from_gdoc(fid, sa_json.read())
                except Exception as e:
                    st.error(f"❌ Could not fetch Storyboard Google Doc: {e}")

        if not story_source:
            st.error("Upload a storyboard .docx OR provide Google Doc URL + SA JSON.")
            st.stop()

        raw_pages = extract_canvas_pages(story_source)
        last_known_module = None
        for idx, block in enumerate(raw_pages):
            page_type = (extract_tag("page_type", block).lower() or "page").strip()
            page_title = extract_tag("page_title", block) or f"Page {idx+1}"
            module_name = extract_tag("module_name", block).strip() or last_known_module or "General"
            last_known_module = module_name
            template_type = extract_tag("template_type", block).strip()
            st.session_state.pages.append({
                "index": idx,
                "raw": block,
                "page_type": page_type,               # page | assignment | discussion | quiz
                "page_title": page_title,
                "module_name": module_name,
                "template_type": template_type,
                "template_assignment_id": None,       # used for quiz duplication
            })
        st.success(f"✅ Parsed {len(st.session_state.pages)} page(s).")

with col0b:
    if st.button("(Optional) Upload template DOCX to KB", use_container_width=True,
                 disabled=not (openai_api_key and (existing_vs or st.session_state.get("vector_store_id") or kb_docx or (kb_gdoc_url and sa_json)))):
        if existing_vs.strip():
            st.session_state.vector_store_id = existing_vs.strip()
        if not st.session_state.get("vector_store_id"):
            # create a new Vector Store
            client = ensure_client()
            vs = client.vector_stores.create(name="umich_canvas_templates")
            st.session_state.vector_store_id = vs.id
            st.info(f"Created Vector Store: {vs.id}")
        kb_upload_if_provided()

# ------------------------ STEP 1: Pages --------------------------------------
st.subheader("Step 1 — Pages")
if st.session_state.pages:
    client = OpenAI(api_key=openai_api_key) if openai_api_key else None
    # Select only page-type items
    page_rows = [p for p in st.session_state.pages if p["page_type"] == "page"]
    if not page_rows:
        st.info("No 'page' items found in storyboard.")
    else:
        for p in page_rows:
            i = p["index"]
            with st.expander(f"Page {i+1}: {p['page_title']} | Module: {p['module_name']}", expanded=False):
                sel = st.checkbox("Include in Pages step", value=(i in st.session_state.selected_for_pages), key=f"sel_page_{i}")
                if sel: st.session_state.selected_for_pages.add(i)
                else: st.session_state.selected_for_pages.discard(i)

                if st.button("Visualize (this page only)", key=f"viz_page_{i}",
                             disabled=not openai_api_key):
                    html_out, quiz_json = run_gpt_for_block(
                        client, st.session_state.get("vector_store_id"),
                        p["raw"], p["template_type"]
                    )
                    st.session_state.gpt_results[i] = {"html": html_out, "quiz_json": None}
                    st.code(html_out or "[No HTML]", language="html")

                if i in st.session_state.gpt_results:
                    st.code(st.session_state.gpt_results[i]["html"], language="html")
                    can_upload = (not dry_run) and canvas_domain and course_id and canvas_token
                    if st.button("Upload Page to Canvas", key=f"upload_page_{i}", disabled=not can_upload):
                        mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, {})
                        if not mid:
                            st.error("Module creation failed.")
                        else:
                            page_url = add_page(canvas_domain, course_id, p["page_title"], st.session_state.gpt_results[i]["html"], canvas_token)
                            if page_url and add_to_module(canvas_domain, course_id, mid, "Page", page_url, p["page_title"], canvas_token):
                                st.success("✅ Page created & added to module.")

# ------------------------ STEP 2: New Quizzes --------------------------------
st.subheader("Step 2 — New Quizzes (duplicate template assignment)")
if st.session_state.pages:
    quiz_rows = [p for p in st.session_state.pages if p["page_type"] == "quiz"]
    if not quiz_rows:
        st.info("No 'quiz' items found in storyboard.")
    else:
        # Load template list once
        colT1, colT2 = st.columns([1, 2])
        with colT1:
            if st.button("Refresh New-Quiz Templates", use_container_width=True,
                         disabled=not (canvas_domain and course_id and canvas_token)):
                st.session_state.new_quiz_templates = list_new_quiz_templates(canvas_domain, course_id, canvas_token)
                st.success(f"Found {len(st.session_state.new_quiz_templates)} New Quiz template assignment(s).")

        tmpl_options = {f"{t['name']} (#{t['id']})": t["id"] for t in st.session_state.new_quiz_templates}

        client = OpenAI(api_key=openai_api_key) if openai_api_key else None

        for p in quiz_rows:
            i = p["index"]
            with st.expander(f"Quiz {i+1}: {p['page_title']} | Module: {p['module_name']}", expanded=False):
                sel = st.checkbox("Include in New Quizzes step", value=(i in st.session_state.selected_for_quizzes), key=f"sel_quiz_{i}")
                if sel: st.session_state.selected_for_quizzes.add(i)
                else: st.session_state.selected_for_quizzes.discard(i)

                # Choose a template assignment to duplicate
                chosen = st.selectbox(
                    "Template New Quiz (assignment) to duplicate",
                    options=["— Select —"] + list(tmpl_options.keys()),
                    key=f"tmpl_select_{i}",
                )
                if chosen != "— Select —":
                    p["template_assignment_id"] = tmpl_options[chosen]

                # Visualize (this quiz only)
                if st.button("Visualize Quiz (this only)", key=f"viz_quiz_{i}",
                             disabled=not openai_api_key):
                    html_out, quiz_json = run_gpt_for_block(
                        client, st.session_state.get("vector_store_id"),
                        p["raw"], p["template_type"]
                    )
                    st.session_state.gpt_results[i] = {"html": html_out, "quiz_json": quiz_json}
                    st.code(html_out or "[No HTML]", language="html")
                    if quiz_json:
                        st.write("Parsed questions JSON:")
                        st.json(quiz_json)

                # Duplicate template, inject items, rename, add to module
                can_run = (not dry_run) and canvas_domain and course_id and canvas_token and (i in st.session_state.gpt_results) and p.get("template_assignment_id")
                if st.button("Duplicate template & Upload to Canvas", key=f"upload_quiz_{i}", disabled=not can_run):
                    mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, {})
                    if not mid:
                        st.error("Module creation failed.")
                    else:
                        new_asg_id = copy_assignment(canvas_domain, course_id, p["template_assignment_id"], p["page_title"], canvas_token)
                        if not new_asg_id:
                            st.error("❌ Could not duplicate assignment.")
                        else:
                            # Inject description & items
                            quiz_json = st.session_state.gpt_results[i]["quiz_json"] or {}
                            # Update instructions/description via new-quiz API:
                            put_url = f"https://{canvas_domain}/api/quiz/v1/courses/{course_id}/quizzes/{new_asg_id}"
                            headers = {"Authorization": f"Bearer {canvas_token}", "Content-Type": "application/json"}
                            desc_html = quiz_json.get("quiz_description") or st.session_state.gpt_results[i]["html"] or ""
                            requests.put(put_url, headers=headers, json={"quiz": {"instructions": desc_html}})
                            # Add MCQ items
                            for pos, q in enumerate(quiz_json.get("questions", []), start=1):
                                if q.get("answers"):
                                    add_new_quiz_mcq(canvas_domain, course_id, new_asg_id, q, canvas_token, position=pos)
                            # Ensure final name matches storyboard title
                            rename_assignment(canvas_domain, course_id, new_asg_id, p["page_title"], canvas_token)
                            # Add to module
                            if add_to_module(canvas_domain, course_id, mid, "Assignment", new_asg_id, p["page_title"], canvas_token):
                                st.success("✅ Duplicated, updated, & added New Quiz to module.")

# ------------------------ STEP 3: Discussions --------------------------------
st.subheader("Step 3 — Discussions")
if st.session_state.pages:
    disc_rows = [p for p in st.session_state.pages if p["page_type"] == "discussion"]
    if not disc_rows:
        st.info("No 'discussion' items found in storyboard.")
    else:
        client = OpenAI(api_key=openai_api_key) if openai_api_key else None
        for p in disc_rows:
            i = p["index"]
            with st.expander(f"Discussion {i+1}: {p['page_title']} | Module: {p['module_name']}", expanded=False):
                sel = st.checkbox("Include in Discussions step", value=(i in st.session_state.selected_for_discussions), key=f"sel_disc_{i}")
                if sel: st.session_state.selected_for_discussions.add(i)
                else: st.session_state.selected_for_discussions.discard(i)

                if st.button("Visualize (this discussion only)", key=f"viz_disc_{i}",
                             disabled=not openai_api_key):
                    html_out, _ = run_gpt_for_block(
                        client, st.session_state.get("vector_store_id"),
                        p["raw"], p["template_type"]
                    )
                    st.session_state.gpt_results[i] = {"html": html_out, "quiz_json": None}
                    st.code(html_out or "[No HTML]", language="html")

                can_upload = (not dry_run) and canvas_domain and course_id and canvas_token and (i in st.session_state.gpt_results)
                if st.button("Upload Discussion to Canvas", key=f"upload_disc_{i}", disabled=not can_upload):
                    mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, {})
                    if not mid:
                        st.error("Module creation failed.")
                    else:
                        did = add_discussion(canvas_domain, course_id, p["page_title"], st.session_state.gpt_results[i]["html"], canvas_token)
                        if did and add_to_module(canvas_domain, course_id, mid, "Discussion", did, p["page_title"], canvas_token):
                            st.success("✅ Discussion created & added to module.")
