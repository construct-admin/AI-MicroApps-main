# canvas_import_app.py
# -----------------------------------------------------------------------------
# 📄 DOCX/Google Doc → GPT (KB-aware, token-lean) → Canvas (Pages / New Quizzes / Discussions)
#
# Design notes
# - Inputs for Canvas: domain, token, course ID (in the sidebar).
# - Optional Google Docs sources (service account JSON) — fully optional.
# - Optional KB from a GitHub repo branch; you can list one or more files to pull.
# - Three token-lean tabs: Pages, Quizzes, Discussions. Each has its own
#   "Select" filters and "Run/Upload" buttons so you never process everything at once.
# - Tables in DOCX/Google Docs are preserved (converted to <table> HTML).
# - New Quizzes:
#     * You can select a template assignment (New Quiz) to duplicate.
#     * If Canvas rejects "copy" (404), we fetch the template's settings (if available),
#       create a new quiz with those settings, then insert generated MCQs (shuffle+feedback).
# - "No-drop" content policy: we instruct GPT to preserve all storyboard text in order.
# -----------------------------------------------------------------------------

from io import BytesIO
import json
import re
import uuid
import html
from typing import Iterable, List, Tuple, Optional, Dict

import requests
import streamlit as st
from openai import OpenAI

# python-docx bits
from docx import Document as DocxDocument
from docx.document import Document as _Document
from docx.oxml.table import CT_Tbl
from docx.oxml.text.paragraph import CT_P
from docx.table import Table
from docx.text.paragraph import Paragraph

# Google APIs (optional, used only if user supplies Service Account JSON)
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build


# ---------------------------- App Setup --------------------------------------
st.set_page_config(page_title="Canvas Builder (Pages • New Quizzes • Discussions)", layout="wide")
st.title("Canvas Builder — Pages • New Quizzes • Discussions")

# ---------------------------- Secrets / API Keys -----------------------------
OPENAI_KEY = st.secrets.get("OPENAI_API_KEY", None)  # Prefer secrets if available

# ---------------------------- Sidebar: Inputs --------------------------------
with st.sidebar:
    st.header("Canvas & OpenAI")
    canvas_domain = st.text_input("Canvas Domain", placeholder="canvas.instructure.com")
    canvas_token = st.text_input("Canvas API Token", type="password")
    course_id = st.text_input("Canvas Course ID")

    # OpenAI key (use secrets if present; allow manual override)
    openai_api_key = st.text_input("OpenAI API Key (optional if in secrets)", type="password")
    if not openai_api_key and OPENAI_KEY:
        openai_api_key = OPENAI_KEY

    st.divider()
    st.subheader("Optional: Google Docs")
    use_gdocs = st.checkbox("Use Google Docs as source(s)?", value=False)
    gdoc_story_url = st.text_input("Storyboard Google Doc URL") if use_gdocs else ""
    gdoc_template_url = st.text_input("Template Google Doc URL") if use_gdocs else ""
    sa_json_file = st.file_uploader("Service Account JSON", type=["json"]) if use_gdocs else None

    st.divider()
    st.subheader("Optional: KB from GitHub branch")
    use_repo_kb = st.checkbox("Load KB from GitHub branch?", value=False)
    repo_owner = st.text_input("Repo Owner", value="", disabled=not use_repo_kb)
    repo_name = st.text_input("Repo Name", value="", disabled=not use_repo_kb)
    repo_branch = st.text_input("Branch", value="knowledge-base", disabled=not use_repo_kb)
    repo_kb_paths = st.text_area(
        "KB file paths (one per line, relative to repo root)",
        value="",
        disabled=not use_repo_kb,
        height=100,
        placeholder="templates/umich_overview.html\ncomponents/accordion.html"
    )
    st.caption("These will be fetched via raw.githubusercontent.com")

    st.divider()
    st.subheader("General Options")
    use_new_quizzes = st.checkbox("Use New Quizzes for quiz pages", value=True)
    dry_run = st.checkbox("Preview only (no upload)", value=False)

# ---------------------------- Guards -----------------------------------------
def require_canvas_ready():
    if not canvas_domain or not canvas_token or not course_id:
        st.error("Please provide Canvas domain, token, and course ID in the sidebar.")
        st.stop()

def ensure_openai():
    if not openai_api_key:
        st.error("OpenAI API key is required (sidebar).")
        st.stop()
    return OpenAI(api_key=openai_api_key)

# ---------------------------- Google Drive Helpers ---------------------------
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

# ---------------------------- KB from GitHub ---------------------------------
def fetch_repo_text(owner: str, repo: str, branch: str, path: str) -> Optional[str]:
    try:
        url = f"https://raw.githubusercontent.com/{owner}/{repo}/{branch}/{path}"
        r = requests.get(url, timeout=30)
        if r.status_code == 200:
            return r.text
        else:
            st.warning(f"KB fetch failed ({r.status_code}) for {path}")
            return None
    except Exception as e:
        st.warning(f"KB fetch error for {path}: {e}")
        return None

def load_kb_snippets() -> List[str]:
    if not use_repo_kb or not repo_owner or not repo_name or not repo_branch:
        return []
    paths = [p.strip() for p in repo_kb_paths.splitlines() if p.strip()]
    snippets = []
    for p in paths:
        txt = fetch_repo_text(repo_owner, repo_name, repo_branch, p)
        if txt:
            # Keep snippets modest (truncate very large files to reduce prompt size)
            if len(txt) > 8000:
                snippets.append(txt[:8000] + "\n<!-- [truncated] -->")
            else:
                snippets.append(txt)
    return snippets

# ---------------------------- DOCX Parsing (with tables) ---------------------
def _iter_block_items(parent: _Document) -> Iterable:
    """
    Yield paragraphs and tables in the order they appear in the document.
    Works on a `docx.document.Document` object.
    """
    for child in parent.element.body.iterchildren():
        if isinstance(child, CT_P):
            yield Paragraph(child, parent)
        elif isinstance(child, CT_Tbl):
            yield Table(child, parent)

def _table_to_html(tbl: Table) -> str:
    rows_html = []
    for row in tbl.rows:
        tds = []
        for cell in row.cells:
            # Merge duplicates in python-docx; use cell.text as a basic representation
            tds.append(f"<td>{html.escape(cell.text)}</td>")
        rows_html.append("<tr>" + "".join(tds) + "</tr>")
    return "<table>" + "".join(rows_html) + "</table>"

def read_docx_with_tables(file_like) -> str:
    """
    Returns a text-ish representation preserving tables as inline <table> HTML.
    We keep paragraph text as-is. This is used for extracting <canvas_page> blocks.
    """
    doc = DocxDocument(file_like)
    parts = []
    for block in _iter_block_items(doc):
        if isinstance(block, Paragraph):
            parts.append(block.text)
        elif isinstance(block, Table):
            parts.append(_table_to_html(block))
    return "\n".join(parts)

def extract_canvas_pages_from_docx(file_like) -> List[str]:
    """
    Reads a .docx, preserving tables as HTML, then extracts everything between
    <canvas_page> and </canvas_page> (case-insensitive).
    """
    text = read_docx_with_tables(file_like)
    blocks = re.findall(r"(?is)<canvas_page>(.*?)</canvas_page>", text, flags=re.DOTALL | re.IGNORECASE)
    return ["<canvas_page>\n" + b.strip() + "\n</canvas_page>" for b in blocks]

def extract_tag(tag: str, block: str) -> str:
    m = re.search(fr"(?is)<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""

# ---------------------------- Canvas API: helpers ----------------------------
def _auth_headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def get_or_create_module(module_name: str, domain: str, course_id: str, token: str, cache: Dict[str, int]) -> Optional[int]:
    if module_name in cache:
        return cache[module_name]
    url = f"https://{domain}/api/v1/courses/{course_id}/modules"
    resp = requests.get(url, headers=_auth_headers(token), timeout=60)
    if resp.status_code == 200:
        for m in resp.json():
            if m.get("name", "").strip().lower() == module_name.strip().lower():
                cache[module_name] = m["id"]
                return m["id"]
    # Create
    resp = requests.post(url, headers=_auth_headers(token), json={"module": {"name": module_name, "published": True}}, timeout=60)
    if resp.status_code in (200, 201):
        mid = resp.json().get("id")
        cache[module_name] = mid
        return mid
    else:
        st.error(f"❌ Could not create/find module '{module_name}': {resp.status_code} | {resp.text}")
        return None

def add_page(domain: str, course_id: str, title: str, html_body: str, token: str) -> Optional[str]:
    url = f"https://{domain}/api/v1/courses/{course_id}/pages"
    payload = {"wiki_page": {"title": title, "body": html_body, "published": True}}
    resp = requests.post(url, headers=_auth_headers(token), json=payload, timeout=120)
    if resp.status_code in (200, 201):
        return resp.json().get("url")
    st.error(f"❌ Page create failed: {resp.status_code} | {resp.text}")
    return None

def add_discussion(domain: str, course_id: str, title: str, html_body: str, token: str) -> Optional[int]:
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
    payload = {"title": title, "message": html_body, "published": True}
    resp = requests.post(url, headers=_auth_headers(token), json=payload, timeout=120)
    if resp.status_code in (200, 201):
        return resp.json().get("id")
    st.error(f"❌ Discussion create failed: {resp.status_code} | {resp.text}")
    return None

def add_to_module(domain: str, course_id: str, module_id: int, item_type: str, ref, title: str, token: str) -> bool:
    """
    item_type: "Page" (page_url), "Assignment" (assignment_id), "Discussion" (discussion_id), "Quiz" (classic)
    """
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    payload = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = ref
    else:
        payload["module_item"]["content_id"] = ref
    resp = requests.post(url, headers=_auth_headers(token), json=payload, timeout=120)
    return resp.status_code in (200, 201)

# ---------------------------- Canvas: New Quizzes ----------------------------
def list_new_quiz_assignments(domain: str, course_id: str, token: str) -> List[Dict]:
    """
    Returns a list of New Quiz "assignments" (LTI) if available.
    Fallback: returns empty list if endpoint not accessible.
    """
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes"
    resp = requests.get(url, headers=_auth_headers(token), timeout=60)
    if resp.status_code != 200:
        return []
    data = resp.json()
    # Normalized list with key fields
    results = []
    for q in data.get("quizzes", data if isinstance(data, list) else []):
        # Assignment id is often exposed as "assignment_id" or just "id"
        results.append({
            "id": q.get("id"),
            "assignment_id": q.get("assignment_id") or q.get("id"),
            "title": q.get("title") or q.get("name") or f"Quiz {q.get('id')}",
        })
    return results

def clone_new_quiz(domain: str, course_id: str, template_assignment_id: str, token: str) -> Optional[int]:
    """
    Attempts a true clone of a New Quiz.
    Some Canvas instances may not expose an official "clone" endpoint; return None on failure.
    """
    # Common (but not guaranteed) clone endpoint:
    url_try = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{template_assignment_id}/clone"
    resp = requests.post(url_try, headers=_auth_headers(token), json={}, timeout=120)
    if resp.status_code in (200, 201):
        new_id = resp.json().get("assignment_id") or resp.json().get("id")
        return new_id
    return None

def create_new_quiz(domain: str, course_id: str, title: str, description_html: str, token: str, points_possible: int = 1) -> Optional[int]:
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes"
    payload = {"quiz": {"title": title, "points_possible": max(points_possible, 1), "instructions": description_html or ""}}
    resp = requests.post(url, headers=_auth_headers(token), json=payload, timeout=120)
    if resp.status_code in (200, 201):
        data = resp.json()
        return data.get("assignment_id") or data.get("id")
    st.error(f"❌ New Quiz create failed: {resp.status_code} | {resp.text}")
    return None

def add_new_quiz_mcq(domain: str, course_id: str, assignment_id: str, q: Dict, token: str, position: int = 1):
    """
    Creates an MCQ item with shuffle + feedback support.
    """
    url = f"https://{domain}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    headers = _auth_headers(token)

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
    r = requests.post(url, headers=headers, json=payload, timeout=120)
    if r.status_code not in (200, 201):
        st.warning(f"⚠️ New Quiz MCQ add failed: {r.status_code} | {r.text}")

# ---------------------------- GPT: Prompts -----------------------------------
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

def make_user_prompt(page_meta: Dict, block: str, kb_snippets: List[str]) -> str:
    kb_part = ""
    if kb_snippets:
        # Keep KB small; include up to ~4 snippets
        top = kb_snippets[:4]
        kb_part = "\n\n--- KB SNIPPETS (for structure only; DO NOT paste verbatim) ---\n" + \
                  "\n\n---SNIPPET---\n".join(top)

    return (
        f'Use template_type="{page_meta.get("template_type") or "auto"}" if it matches a known template; '
        f'otherwise choose the closest layout.\n'
        f'{kb_part}\n\n'
        "Storyboard page block:\n"
        f"{block}"
    )

# ---------------------------- App State --------------------------------------
if "pages" not in st.session_state:
    st.session_state.pages = []           # [{index, raw, page_type, page_title, module_name, template_type}]
if "gpt" not in st.session_state:
    st.session_state.gpt = {}             # idx -> {"html":..., "quiz_json":...}

# ---------------------------- Tabs -------------------------------------------
tab_pages, tab_quizzes, tab_discussions = st.tabs(["Pages", "New Quizzes", "Discussions"])

# =========================== TAB: PAGES ======================================
with tab_pages:
    st.subheader("1) Pages — parse storyboard, visualize with GPT, upload")

    # Source: DOCX upload or Google Doc (optional)
    col_src = st.columns([1, 1, 1])
    with col_src[0]:
        story_docx = st.file_uploader("Storyboard (.docx)", type=["docx"], key="story_docx_pages")
    with col_src[1]:
        parse_btn = st.button("Parse storyboard → pages")
    with col_src[2]:
        st.write("")

    if parse_btn:
        st.session_state.pages.clear()
        st.session_state.gpt.clear()

        file_like = None
        if story_docx is not None:
            file_like = BytesIO(story_docx.getvalue())
        elif use_gdocs and gdoc_story_url and sa_json_file is not None:
            fid = _gdoc_id_from_url(gdoc_story_url)
            if fid:
                try:
                    file_like = fetch_docx_from_gdoc(fid, sa_json_file.read())
                except Exception as e:
                    st.error(f"❌ Google Doc fetch failed: {e}")
                    st.stop()
        else:
            st.error("Provide a .docx or Google Doc URL + Service Account JSON.")
            st.stop()

        blocks = extract_canvas_pages_from_docx(file_like)
        last_module = None
        for i, block in enumerate(blocks):
            ptype = (extract_tag("page_type", block).lower() or "page").strip()
            title = extract_tag("page_title", block) or f"Page {i+1}"
            module = extract_tag("module_name", block).strip()
            if not module:
                module = last_module or "General"
            last_module = module
            template = extract_tag("template_type", block).strip()
            st.session_state.pages.append({
                "index": i,
                "raw": block,
                "page_type": ptype,          # page | assignment | discussion | quiz
                "page_title": title,
                "module_name": module,
                "template_type": template or ""
            })
        st.success(f"Parsed {len(st.session_state.pages)} page block(s).")

    if st.session_state.pages:
        # selection
        indices = [p["index"] for p in st.session_state.pages]
        labels = [f'{p["index"]+1}. {p["page_title"]} [{p["page_type"]}]' for p in st.session_state.pages]
        selected = st.multiselect("Select pages to visualize/upload", options=indices, default=indices, format_func=lambda i: labels[i])

        kb_snippets = load_kb_snippets() if use_repo_kb else []

        if st.button("Visualize selected with GPT (no upload yet)"):
            client = ensure_openai()
            with st.spinner("Generating HTML via GPT (per selected page)…"):
                for p in st.session_state.pages:
                    if p["index"] not in selected:
                        continue
                    user_prompt = make_user_prompt(p, p["raw"], kb_snippets)
                    try:
                        resp = client.chat.completions.create(
                            model="gpt-4o",
                            messages=[{"role": "system", "content": SYSTEM_STRICT},
                                      {"role": "user", "content": user_prompt}],
                            temperature=0.2,
                        )
                        out = (resp.choices[0].message.content or "").strip()
                    except Exception as e:
                        st.error(f"OpenAI error: {e}")
                        continue

                    cleaned = re.sub(r"```(html|json)?", "", out, flags=re.IGNORECASE).strip()
                    # If quiz: split trailing JSON
                    json_match = re.search(r"({[\s\S]+})\s*$", cleaned)
                    qjson, html_out = None, cleaned
                    if json_match and p["page_type"] == "quiz":
                        try:
                            qjson = json.loads(json_match.group(1))
                            html_out = cleaned[:json_match.start()].strip()
                        except Exception:
                            qjson = None
                    st.session_state.gpt[p["index"]] = {"html": html_out, "quiz_json": qjson}

        # Previews + upload
        if st.session_state.gpt:
            require_canvas_ready()
            mod_cache = {}
            for p in st.session_state.pages:
                if p["index"] not in selected:
                    continue
                g = st.session_state.gpt.get(p["index"], {})
                with st.expander(f'Preview — {p["page_title"]} [{p["page_type"]}] (Module: {p["module_name"]})', expanded=False):
                    st.code(g.get("html", "") or "[no HTML]", language="html")
                    if p["page_type"] == "quiz" and g.get("quiz_json"):
                        st.json(g["quiz_json"])

                    if st.button(f"Upload: {p['page_title']}", key=f"up_{p['index']}", disabled=dry_run):
                        mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, mod_cache)
                        if not mid:
                            st.error("Module not available.")
                            st.stop()

                        if p["page_type"] == "page":
                            page_url = add_page(canvas_domain, course_id, p["page_title"], g.get("html", ""), canvas_token)
                            if page_url and add_to_module(canvas_domain, course_id, mid, "Page", page_url, p["page_title"], canvas_token):
                                st.success("✅ Page created & added to module.")

                        elif p["page_type"] == "discussion":
                            did = add_discussion(canvas_domain, course_id, p["page_title"], g.get("html", ""), canvas_token)
                            if did and add_to_module(canvas_domain, course_id, mid, "Discussion", did, p["page_title"], canvas_token):
                                st.success("✅ Discussion created & added to module.")

                        elif p["page_type"] == "quiz":
                            # Handled in Quizzes tab to keep token budget separate
                            st.info("Quiz uploads are handled in the New Quizzes tab.", icon="ℹ️")
                        else:
                            st.warning(f"Unsupported page_type: {p['page_type']}")

# =========================== TAB: NEW QUIZZES ================================
with tab_quizzes:
    st.subheader("2) New Quizzes — duplicate template & insert content")

    require_canvas_ready()

    # Pull list of new quiz templates (assignments)
    templates = list_new_quiz_assignments(canvas_domain, course_id, canvas_token)
    template_map = {f"{q['title']} (id:{q['assignment_id']})": q["assignment_id"] for q in templates} if templates else {}
    if not templates:
        st.info("No New Quizzes found via LTI API listing; you can still create fresh New Quizzes without duplication.", icon="ℹ️")

    # Which parsed storyboard pages are quizzes?
    quiz_pages = [p for p in st.session_state.pages if p.get("page_type") == "quiz"]
    if not quiz_pages:
        st.info("No quiz pages parsed yet. Parse in the Pages tab first.", icon="ℹ️")
    else:
        # Choose which quiz pages to process
        q_indices = [p["index"] for p in quiz_pages]
        q_labels = [f'{p["index"]+1}. {p["page_title"]}' for p in quiz_pages]
        selected_q = st.multiselect("Select quiz pages to upload", options=q_indices, default=q_indices, format_func=lambda i: q_labels[q_indices.index(i)])

        selected_template_label = st.selectbox(
            "Optional: choose a New Quiz template to duplicate",
            options=["(Create each from scratch)"] + list(template_map.keys())
        )
        chosen_template_id = template_map.get(selected_template_label)

        if st.button("Upload selected quiz pages"):
            client = ensure_openai()  # just to ensure key present, content already generated in Pages
            with st.spinner("Uploading quizzes…"):
                mod_cache = {}
                for p in quiz_pages:
                    if p["index"] not in selected_q:
                        continue
                    bundle = st.session_state.gpt.get(p["index"], {})
                    html_desc = bundle.get("html", "")
                    qjson = bundle.get("quiz_json", {}) or {}
                    if not html_desc:
                        st.warning(f"Quiz '{p['page_title']}' has no generated HTML. Visualize first in Pages tab.")
                        continue

                    # Duplicate or create
                    assignment_id = None
                    if chosen_template_id:
                        # Try true clone
                        new_id = clone_new_quiz(canvas_domain, course_id, str(chosen_template_id), canvas_token)
                        if new_id:
                            assignment_id = new_id
                        else:
                            st.warning("Template clone not supported on this instance; creating a fresh New Quiz instead.")
                            assignment_id = create_new_quiz(canvas_domain, course_id, p["page_title"], html_desc, canvas_token)
                    else:
                        assignment_id = create_new_quiz(canvas_domain, course_id, p["page_title"], html_desc, canvas_token)

                    if not assignment_id:
                        st.error(f"Could not create/clone quiz for '{p['page_title']}'.")
                        continue

                    # Insert questions
                    if isinstance(qjson, dict):
                        for pos, q in enumerate(qjson.get("questions", []), start=1):
                            if q.get("answers"):
                                add_new_quiz_mcq(canvas_domain, course_id, str(assignment_id), q, canvas_token, position=pos)

                    # Rename (if cloned) to match page_title
                    # Many clones already carry the title; but call PATCH anyway to be safe
                    try:
                        url = f"https://{canvas_domain}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}"
                        requests.patch(url, headers=_auth_headers(canvas_token),
                                       json={"quiz": {"title": p["page_title"], "instructions": html_desc}}, timeout=60)
                    except Exception:
                        pass

                    # Add to module
                    mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, mod_cache)
                    if mid and add_to_module(canvas_domain, course_id, mid, "Assignment", assignment_id, p["page_title"], canvas_token):
                        st.success(f"✅ '{p['page_title']}' uploaded as New Quiz & added to module.")

# =========================== TAB: DISCUSSIONS ================================
with tab_discussions:
    st.subheader("3) Discussions — create (or duplicate) & insert content")

    require_canvas_ready()

    # Filter discussion pages parsed earlier
    d_pages = [p for p in st.session_state.pages if p.get("page_type") == "discussion"]
    if not d_pages:
        st.info("No discussion pages parsed yet. Parse in the Pages tab first.", icon="ℹ️")
    else:
        d_indices = [p["index"] for p in d_pages]
        d_labels = [f'{p["index"]+1}. {p["page_title"]}' for p in d_pages]
        selected_d = st.multiselect("Select discussions to upload", options=d_indices, default=d_indices, format_func=lambda i: d_labels[d_indices.index(i)])

        # Allow (optional) duplication from an existing discussion topic by ID
        dup_disc_id = st.text_input("Optional: duplicate from existing Discussion ID (leave blank to create new)")
        if st.button("Upload selected discussions"):
            mod_cache = {}
            for p in d_pages:
                if p["index"] not in selected_d:
                    continue
                g = st.session_state.gpt.get(p["index"], {})  # you can also create discussion HTML directly in Pages tab
                html_body = g.get("html", "")
                if not html_body:
                    st.warning(f"Discussion '{p['page_title']}' has no generated HTML. Visualize first in Pages tab.")
                    continue

                new_disc_id: Optional[int] = None
                if dup_disc_id:
                    # Try to clone by fetching & re-posting (best-effort)
                    try:
                        src_url = f"https://{canvas_domain}/api/v1/courses/{course_id}/discussion_topics/{dup_disc_id}"
                        r = requests.get(src_url, headers=_auth_headers(canvas_token), timeout=60)
                        if r.status_code == 200:
                            # Use some of the settings from the source
                            settings = r.json()
                            payload = {
                                "title": p["page_title"],
                                "message": html_body,
                                "published": True,
                                "delayed_post_at": settings.get("delayed_post_at"),
                                "lock_at": settings.get("lock_at"),
                                "require_initial_post": settings.get("require_initial_post", False),
                                "podcast_enabled": settings.get("podcast_enabled", False),
                                "discussion_type": settings.get("discussion_type") or "threaded"
                            }
                            new_disc_id = add_discussion(canvas_domain, course_id, p["page_title"], html_body, canvas_token)
                        else:
                            st.warning(f"Could not read source discussion; creating new. ({r.status_code})")
                            new_disc_id = add_discussion(canvas_domain, course_id, p["page_title"], html_body, canvas_token)
                    except Exception as e:
                        st.warning(f"Clone discussion failed, creating new. ({e})")
                        new_disc_id = add_discussion(canvas_domain, course_id, p["page_title"], html_body, canvas_token)
                else:
                    new_disc_id = add_discussion(canvas_domain, course_id, p["page_title"], html_body, canvas_token)

                mid = get_or_create_module(p["module_name"], canvas_domain, course_id, canvas_token, mod_cache)
                if mid and new_disc_id and add_to_module(canvas_domain, course_id, mid, "Discussion", new_disc_id, p["page_title"], canvas_token):
                    st.success(f"✅ Discussion '{p['page_title']}' uploaded & added to module.")
