# canvas_import_steps.py
# -----------------------------------------------------------------------------
# 📄 DOCX/Google Doc → GPT (KB) → Canvas (Pages / New Quizzes / Discussions)
#
# Panels:
#  - Pages: non-quiz Pages/Assignments (select & upload)
#  - New Quizzes: duplicate a template New Quiz, insert items, rename
#  - Discussions: clone settings from a template discussion, insert content
#
# Token minimization:
#  - Parse storyboard once
#  - In each panel: select subset → visualize only selected → upload only selected
#  - Vector Store (file_search) for templates instead of pasting template HTML
# -----------------------------------------------------------------------------

from io import BytesIO
import json
import re
import time
import uuid
import requests
import streamlit as st
from openai import OpenAI
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build
from docx import Document

# ---------------------------- App Setup --------------------------------------
st.set_page_config(page_title="📄 DOCX → GPT (KB) → Canvas (Step-by-step)", layout="wide")
st.title("📄 Upload DOCX → Convert via GPT (KB) → Upload to Canvas — step by step")

# ---------------------------- Session State ----------------------------------
def _init_state():
    defaults = {
        "pages_all": [],              # parsed from storyboard (raw blocks)
        "visualized": {},             # idx -> {"html":..., "quiz_json":...}
        "vector_store_id": None,
        "new_quiz_templates": [],     # list of dicts: {assignment_id, quiz_id, name}
        "discussion_templates": [],   # list of dicts: {id, title}
        "rate_limit_backoff": 1.0,
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
        st.caption("Paste to reuse your KB")
    kb_docx = st.file_uploader("Upload template DOCX (optional)", type=["docx"])
    kb_gdoc_url = st.text_input("Template Google Doc URL (optional)")

    # Canvas + OpenAI
    st.subheader("Canvas & OpenAI")
    canvas_domain = st.text_input("Canvas Base URL", placeholder="umich.instructure.com")
    course_id = st.text_input("Canvas Course ID")
    canvas_token = st.text_input("Canvas API Token", type="password")
    openai_api_key = st.text_input("OpenAI API Key", type="password")

    use_new_quizzes = st.checkbox("Use New Quizzes", value=True)
    dry_run = st.checkbox("🔍 Preview only (no upload)", value=False)

# ------------------------ Helpers: Google Docs & DOCX ------------------------
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

def extract_canvas_pages(storyboard_docx_file):
    """Pull out everything between <canvas_page>...</canvas_page>"""
    doc = Document(storyboard_docx_file)
    pages, current_block, inside = [], [], False
    for para in doc.paragraphs:
        text = para.text
        low = text.lower()
        if "<canvas_page>" in low:
            inside = True
            current_block = [text]
            continue
        if "</canvas_page>" in low:
            current_block.append(text)
            pages.append("\n".join(current_block))
            inside = False
            continue
        if inside:
            current_block.append(text)
    return pages

def extract_tag(tag, block):
    m = re.search(fr"<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL | re.IGNORECASE)
    return (m.group(1).strip() if m else "")

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
    if st.button("Upload Template to KB", use_container_width=True,
                 disabled=not (st.session_state.get("vector_store_id") or existing_vs)):
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

# ------------------------ Canvas REST helpers --------------------------------
def api_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def get_or_create_module(module_name, domain, course_id, token, module_cache):
    if module_name in module_cache:
        return module_cache[module_name]
    url = f"https://{domain}/api/v1/courses/{course_id}/modules"
    resp = requests.get(url, headers=api_headers(token))
    if resp.status_code == 200:
        for m in resp.json():
            if m["name"].strip().lower() == module_name.strip().lower():
                module_cache[module_name] = m["id"]
                return m["id"]
    # create
    resp = requests.post(url, headers=api_headers(token),
                         json={"module": {"name": module_name, "published": True}})
    if resp.status_code in (200, 201):
        mid = resp.json().get("id")
        module_cache[module_name] = mid
        return mid
    st.error(f"❌ Module create/find failed: {resp.status_code} | {resp.text}")
    return None

def add_page(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/pages"
    payload = {"wiki_page": {"title": title, "body": html_body, "published": True}}
    resp = requests.post(url, headers=api_headers(token), json=payload)
    if resp.status_code in (200, 201):
        return resp.json().get("url")
    st.error(f"❌ Page create failed: {resp.text}")
    return None

def add_assignment(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/assignments"
    payload = {"assignment": {"name": title, "description": html_body,
                              "published": True, "submission_types": ["online_text_entry"],
                              "points_possible": 10}}
    resp = requests.post(url, headers=api_headers(token), json=payload)
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    st.error(f"❌ Assignment create failed: {resp.text}")
    return None

def add_discussion(domain, course_id, title, html_body, token, settings=None):
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
    payload = {"title": title, "message": html_body, "published": True}
    if settings:
        payload.update(settings)  # copy template settings
    resp = requests.post(url, headers=api_headers(token), json=payload)
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    st.error(f"❌ Discussion create failed: {resp.text}")
    return None

def add_to_module(domain, course_id, module_id, item_type, ref, title, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    payload = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = ref
    else:
        payload["module_item"]["content_id"] = ref
    resp = requests.post(url, headers=api_headers(token), json=payload)
    return resp.status_code in (200, 201)

# -------- New Quizzes (LTI) — list, clone, add items, rename -----------------
def list_new_quiz_templates(domain, course_id, token):
    """
    Returns list of dicts: {assignment_id, quiz_id, name}
    Uses New Quizzes API list endpoint.
    """
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes"
    resp = requests.get(url, headers=api_headers(token))
    out = []
    if resp.status_code == 200:
        for q in resp.json().get("quizzes", []):
            out.append({
                "assignment_id": q.get("assignment_id"),
                "quiz_id": q.get("id"),
                "name": q.get("title") or f"Quiz {q.get('id')}",
            })
    return out

def clone_new_quiz(domain, course_id, quiz_id, token):
    """Clone a New Quiz by quiz_id; returns dict with new quiz (incl assignment_id)."""
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{quiz_id}/clone"
    resp = requests.post(url, headers=api_headers(token), json={})
    if resp.status_code in (200, 201):
        return resp.json()
    st.error(f"❌ New Quiz clone failed: {resp.status_code} | {resp.text}")
    return None

def rename_assignment(domain, course_id, assignment_id, new_name, token, description_html=None):
    url = f"https://{domain}/api/v1/courses/{course_id}/assignments/{assignment_id}"
    payload = {"assignment": {"name": new_name}}
    if description_html is not None:
        payload["assignment"]["description"] = description_html
    resp = requests.put(url, headers=api_headers(token), json=payload)
    return resp.status_code in (200, 201)

def add_new_quiz_mcq(domain, course_id, assignment_id, q, token, position=1):
    """
    Create a Multiple Choice item in a New Quiz with:
      - per-question shuffle (q['shuffle'])
      - question-level feedback (q['feedback'] -> correct/incorrect/neutral)
      - per-answer feedback (answers[i]['feedback'])
    """
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    headers = api_headers(token)

    # choices & feedback
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
        st.warning(f"⚠️ Failed to add item to New Quiz: {resp.status_code} | {resp.text}")

# -------- Discussion Template helpers (copy settings) ------------------------
def list_discussion_templates(domain, course_id, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics?per_page=100"
    resp = requests.get(url, headers=api_headers(token))
    out = []
    if resp.status_code == 200:
        for d in resp.json():
            out.append({"id": d.get("id"), "title": d.get("title")})
    return out

def get_discussion_settings(domain, course_id, topic_id, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics/{topic_id}"
    resp = requests.get(url, headers=api_headers(token))
    if resp.status_code != 200:
        return {}
    data = resp.json()
    # Copy a handful of common settings if present
    keys = [
        "discussion_type", "require_initial_post", "is_announcement",
        "published", "pinned", "allow_rating", "only_graders_can_rate",
        "sort_by_rating"
    ]
    out = {}
    for k in keys:
        if k in data:
            out[k] = data[k]
    return out

# ------------------------ Parse storyboard -----------------------------------
def parse_storyboard():
    story_source = uploaded_file
    if not story_source and gdoc_url and sa_json:
        fid = _gdoc_id_from_url(gdoc_url)
        if fid:
            story_source = fetch_docx_from_gdoc(fid, sa_json.read())
    if not story_source:
        st.error("Upload a storyboard .docx OR provide a Google Doc URL + SA JSON.")
        return

    raw_pages = extract_canvas_pages(story_source)
    pages = []
    last_module = None
    for idx, block in enumerate(raw_pages):
        page_type = (extract_tag("page_type", block).lower() or "page").strip()
        page_title = extract_tag("page_title", block) or f"Page {idx+1}"
        module_name = extract_tag("module_name", block).strip()

        if not module_name:
            m = re.search(r"\b(Module\s+[A-Za-z0-9 ]+)", page_title, flags=re.IGNORECASE)
            if m: module_name = m.group(1).strip()
        if not module_name:
            module_name = last_module or "General"
        last_module = module_name

        template_type = extract_tag("template_type", block).strip()
        pages.append({
            "index": idx,
            "raw": block,
            "page_type": page_type,
            "page_title": page_title,
            "module_name": module_name,
            "template_type": template_type,
            # optional user choice fields:
            "selected": False,
            "template_assignment_id": "",   # for quizzes
            "template_discussion_id": "",   # for discussions
        })
    st.session_state.pages_all = pages
    st.session_state.visualized = {}
    st.success(f"✅ Parsed {len(pages)} storyboard page(s).")

# ------------------------ GPT Prompt + Call (with backoff) -------------------
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

def gpt_generate_for_block(client, vs_id, page):
    """
    Returns (html, quiz_json|None) for a single storyboard block.
    With exponential backoff on 429 token-rate errors.
    """
    user_prompt = (
        f'Use template_type="{page.get("template_type") or "auto"}" if it matches; otherwise choose best fit.\n\n'
        "Storyboard page block:\n"
        f"{page['raw']}"
    )

    backoff = st.session_state.get("rate_limit_backoff", 1.0)
    for attempt in range(6):
        try:
            resp = client.responses.create(
                model="gpt-4o",
                input=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user", "content": user_prompt}
                ],
                tools=[{"type": "file_search", "vector_store_ids": [vs_id]}]
            )
            raw_out = (resp.output_text or "").strip()
            cleaned = re.sub(r"```(html|json)?", "", raw_out, flags=re.IGNORECASE).strip()

            # extract JSON tail for quizzes
            quiz_json = None
            if page["page_type"] == "quiz":
                m = re.search(r"({[\s\S]+})\s*$", cleaned)
                if m:
                    try:
                        quiz_json = json.loads(m.group(1))
                        cleaned = cleaned[:m.start()].strip()
                    except Exception:
                        quiz_json = None
            st.session_state["rate_limit_backoff"] = max(1.0, backoff / 2)
            return cleaned, quiz_json

        except Exception as e:
            msg = str(e)
            if "rate_limit_exceeded" in msg or "429" in msg:
                time.sleep(backoff)
                backoff = min(backoff * 2, 16)
                st.session_state["rate_limit_backoff"] = backoff
                continue
            raise
    st.error("❌ GPT call failed repeatedly due to rate limits.")
    return "", None

# ------------------------ UI: Parse storyboard -------------------------------
parse_col = st.columns([1,1,1])
with parse_col[0]:
    if st.button("1️⃣ Parse storyboard (.docx / Google Doc)"):
        parse_storyboard()

with parse_col[1]:
    if st.button("Refresh New Quiz templates", disabled=not (canvas_domain and course_id and canvas_token)):
        st.session_state.new_quiz_templates = list_new_quiz_templates(canvas_domain, course_id, canvas_token)
        st.success(f"Found {len(st.session_state.new_quiz_templates)} New Quiz templates.")

with parse_col[2]:
    if st.button("Refresh Discussion templates", disabled=not (canvas_domain and course_id and canvas_token)):
        st.session_state.discussion_templates = list_discussion_templates(canvas_domain, course_id, canvas_token)
        st.success(f"Found {len(st.session_state.discussion_templates)} discussion templates.")

# ------------------------ Tabs: Pages / New Quizzes / Discussions ------------
tabs = st.tabs(["Pages", "New Quizzes", "Discussions"])

# ======= TAB 1: PAGES (non-quiz) ============================================
with tabs[0]:
    st.subheader("Pages & Assignments (non-quiz)")
    if not st.session_state.pages_all:
        st.info("Parse a storyboard first.", icon="📝")
    else:
        page_items = [p for p in st.session_state.pages_all if p["page_type"] in ("page","assignment")]
        if not page_items:
            st.info("No non-quiz pages detected.")
        else:
            st.caption("Select which items you want to process (visualize/upload).")
            for p in page_items:
                p["selected"] = st.checkbox(
                    f"[{p['page_type']}] {p['page_title']}  |  Module: {p['module_name']}",
                    key=f"sel_pg_{p['index']}", value=p.get("selected", False)
                )

            btn_cols = st.columns([1,1])
            with btn_cols[0]:
                if st.button("🔎 Visualize Selected (Pages/Assignments)", type="primary",
                             disabled=not (openai_api_key and st.session_state.get("vector_store_id"))):
                    client = ensure_client()
                    vs_id = st.session_state["vector_store_id"]
                    for p in page_items:
                        if not p["selected"]:
                            continue
                        html, _ = gpt_generate_for_block(client, vs_id, p)
                        st.session_state.visualized[p["index"]] = {"html": html, "quiz_json": None}
                    st.success("Visualization complete for selected items.")

            with btn_cols[1]:
                if st.button("🚀 Upload Selected (Pages/Assignments)", disabled=dry_run or not (canvas_domain and course_id and canvas_token)):
                    module_cache = {}
                    anyup = False
                    for p in page_items:
                        if not p["selected"]:
                            continue
                        vis = st.session_state.visualized.get(p["index"], {})
                        html = vis.get("html","")
                        if not html:
                            st.warning(f"No HTML for: {p['page_title']}. Visualize first.")
                            continue
                        mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, module_cache)
                        if not mid:
                            continue
                        if p["page_type"] == "page":
                            page_url = add_page(canvas_domain, course_id, p["page_title"], html, canvas_token)
                            if page_url and add_to_module(canvas_domain, course_id, mid, "Page", page_url, p["page_title"], canvas_token):
                                anyup = True
                                st.success(f"✅ Uploaded Page: {p['page_title']}")
                        else:
                            aid = add_assignment(canvas_domain, course_id, p["page_title"], html, canvas_token)
                            if aid and add_to_module(canvas_domain, course_id, mid, "Assignment", aid, p["page_title"], canvas_token):
                                anyup = True
                                st.success(f"✅ Uploaded Assignment: {p['page_title']}")
                    if not anyup:
                        st.warning("Nothing uploaded.")

            # previews
            st.divider()
            for p in page_items:
                if p["selected"]:
                    vis = st.session_state.visualized.get(p["index"], {})
                    html = vis.get("html","")
                    with st.expander(f"Preview: {p['page_title']}", expanded=False):
                        st.code(html or "[No HTML]", language="html")

# ======= TAB 2: NEW QUIZZES =================================================
with tabs[1]:
    st.subheader("New Quizzes (duplicate template → insert items)")

    quiz_items = [p for p in st.session_state.pages_all if p["page_type"] == "quiz"]
    if not quiz_items:
        st.info("No quiz pages detected in storyboard.")
    else:
        if not st.session_state.new_quiz_templates:
            st.warning("Click **Refresh New Quiz templates** in the header to load templates from this course.")
        tmpl_map = {str(t["assignment_id"]): t for t in st.session_state.new_quiz_templates}

        for p in quiz_items:
            left, right = st.columns([0.7, 0.3])
            with left:
                p["selected"] = st.checkbox(f"[quiz] {p['page_title']}  |  Module: {p['module_name']}",
                                            key=f"sel_quiz_{p['index']}", value=p.get("selected", False))
            with right:
                tmpl_choices = [""] + [f"{t['name']}  (asg:{t['assignment_id']})" for t in st.session_state.new_quiz_templates]
                current_label = ""
                if p.get("template_assignment_id"):
                    t = tmpl_map.get(str(p["template_assignment_id"]))
                    if t:
                        current_label = f"{t['name']}  (asg:{t['assignment_id']})"
                sel = st.selectbox("Template New Quiz", tmpl_choices, index=(tmpl_choices.index(current_label) if current_label in tmpl_choices else 0), key=f"tmpl_quiz_{p['index']}")
                if sel:
                    # extract assignment_id from label tail
                    m = re.search(r"\(asg:(\d+)\)$", sel)
                    if m:
                        p["template_assignment_id"] = m.group(1)

        btn_cols = st.columns([1,1,1])
        with btn_cols[0]:
            if st.button("🔎 Visualize Selected (Quizzes)", type="primary",
                         disabled=not (openai_api_key and st.session_state.get("vector_store_id"))):
                client = ensure_client()
                vs_id = st.session_state["vector_store_id"]
                for p in quiz_items:
                    if not p["selected"]:
                        continue
                    html, quiz_json = gpt_generate_for_block(client, vs_id, p)
                    st.session_state.visualized[p["index"]] = {"html": html, "quiz_json": quiz_json}
                st.success("Visualization complete for selected quiz items.")

        with btn_cols[1]:
            if st.button("🚀 Duplicate Template & Upload Selected (New Quizzes)",
                         disabled=dry_run or not (canvas_domain and course_id and canvas_token)):
                module_cache = {}
                anyup = False
                # Pre-map assignment_id→quiz_id for chosen templates
                asg_to_quiz = {str(t["assignment_id"]): t["quiz_id"] for t in st.session_state.new_quiz_templates}
                for p in quiz_items:
                    if not p["selected"]:
                        continue
                    if not p.get("template_assignment_id"):
                        st.warning(f"No template selected for quiz: {p['page_title']}")
                        continue
                    vis = st.session_state.visualized.get(p["index"], {})
                    html = vis.get("html","")
                    qjson = vis.get("quiz_json")
                    if not html:
                        st.warning(f"No HTML for: {p['page_title']}. Visualize first.")
                        continue

                    # 1) Clone template quiz by quiz_id
                    tmpl_asg = str(p["template_assignment_id"])
                    tmpl_quiz_id = asg_to_quiz.get(tmpl_asg)
                    if not tmpl_quiz_id:
                        st.error(f"Cannot resolve quiz_id for template assignment {tmpl_asg}")
                        continue
                    cloned = clone_new_quiz(canvas_domain, course_id, tmpl_quiz_id, canvas_token)
                    if not cloned:
                        continue
                    new_assignment_id = cloned.get("assignment_id") or cloned.get("id")

                    # 2) Rename cloned assignment and set instructions/description to HTML
                    rename_assignment(canvas_domain, course_id, new_assignment_id, p["page_title"], canvas_token, description_html=html)

                    # 3) Add items
                    if qjson and isinstance(qjson, dict):
                        for pos, q in enumerate(qjson.get("questions", []), start=1):
                            if q.get("answers"):
                                add_new_quiz_mcq(canvas_domain, course_id, new_assignment_id, q, canvas_token, position=pos)

                    # 4) Add to module
                    mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, module_cache)
                    if mid and add_to_module(canvas_domain, course_id, mid, "Assignment", new_assignment_id, p["page_title"], canvas_token):
                        anyup = True
                        st.success(f"✅ Cloned & uploaded New Quiz: {p['page_title']}")
                if not anyup:
                    st.warning("Nothing uploaded.")

        with btn_cols[2]:
            # previews
            if st.button("Show Previews (Selected)", type="secondary"):
                for p in quiz_items:
                    if not p["selected"]:
                        continue
                    vis = st.session_state.visualized.get(p["index"], {})
                    html = vis.get("html","")
                    with st.expander(f"Preview HTML: {p['page_title']}", expanded=False):
                        st.code(html or "[No HTML]", language="html")
                    if vis.get("quiz_json"):
                        with st.expander(f"Preview JSON: {p['page_title']}", expanded=False):
                            st.code(json.dumps(vis["quiz_json"], indent=2))

# ======= TAB 3: DISCUSSIONS ==================================================
with tabs[2]:
    st.subheader("Discussions (clone settings from template → insert content)")

    disc_items = [p for p in st.session_state.pages_all if p["page_type"] == "discussion"]
    if not disc_items:
        st.info("No discussion pages detected.")
    else:
        if not st.session_state.discussion_templates:
            st.warning("Click **Refresh Discussion templates** in the header to load templates from this course.")
        tmpl_disc_map = {str(t["id"]): t for t in st.session_state.discussion_templates}

        for p in disc_items:
            left, right = st.columns([0.7, 0.3])
            with left:
                p["selected"] = st.checkbox(f"[discussion] {p['page_title']}  |  Module: {p['module_name']}",
                                            key=f"sel_disc_{p['index']}", value=p.get("selected", False))
            with right:
                tmpl_choices = [""] + [f"{t['title']}  (id:{t['id']})" for t in st.session_state.discussion_templates]
                current_label = ""
                if p.get("template_discussion_id"):
                    t = tmpl_disc_map.get(str(p["template_discussion_id"]))
                    if t:
                        current_label = f"{t['title']}  (id:{t['id']})"
                sel = st.selectbox("Template Discussion", tmpl_choices, index=(tmpl_choices.index(current_label) if current_label in tmpl_choices else 0), key=f"tmpl_disc_{p['index']}")
                if sel:
                    m = re.search(r"\(id:(\d+)\)$", sel)
                    if m:
                        p["template_discussion_id"] = m.group(1)

        btn_cols = st.columns([1,1])
        with btn_cols[0]:
            if st.button("🔎 Visualize Selected (Discussions)", type="primary",
                         disabled=not (openai_api_key and st.session_state.get("vector_store_id"))):
                client = ensure_client()
                vs_id = st.session_state["vector_store_id"]
                for p in disc_items:
                    if not p["selected"]:
                        continue
                    html, _ = gpt_generate_for_block(client, vs_id, p)
                    st.session_state.visualized[p["index"]] = {"html": html, "quiz_json": None}
                st.success("Visualization complete for selected discussions.")

        with btn_cols[1]:
            if st.button("🚀 Upload Selected (Discussions)", disabled=dry_run or not (canvas_domain and course_id and canvas_token)):
                module_cache = {}
                anyup = False
                for p in disc_items:
                    if not p["selected"]:
                        continue
                    vis = st.session_state.visualized.get(p["index"], {})
                    html = vis.get("html","")
                    if not html:
                        st.warning(f"No HTML for: {p['page_title']}. Visualize first.")
                        continue

                    settings = {}
                    if p.get("template_discussion_id"):
                        settings = get_discussion_settings(canvas_domain, course_id, p["template_discussion_id"], canvas_token)

                    did = add_discussion(canvas_domain, course_id, p["page_title"], html, canvas_token, settings=settings)
                    mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, module_cache)
                    if did and mid and add_to_module(canvas_domain, course_id, mid, "Discussion", did, p["page_title"], canvas_token):
                        anyup = True
                        st.success(f"✅ Uploaded Discussion: {p['page_title']}")
                if not anyup:
                    st.warning("Nothing uploaded.")
