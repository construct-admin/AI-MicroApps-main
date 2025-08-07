import streamlit as st
from docx import Document
from openai import OpenAI
import requests
import re
import json
from datetime import datetime

# ==============================
# UI
# ==============================
st.set_page_config(page_title="📄 DOCX → GPT → Canvas (Multi-Page)", layout="wide")
st.title("📄 DOCX → GPT → Canvas (Pages / Assignments / Discussions / Quizzes)")

colA, colB = st.columns(2)
with colA:
    uploaded_file = st.file_uploader("Upload your storyboard (.docx)", type="docx")
    template_file = st.file_uploader("Upload uMich Template Code (.docx)", type="docx")
with colB:
    canvas_domain_input = st.text_input("Canvas Base URL (e.g. canvas.instructure.com)")
    course_id = st.text_input("Canvas Course ID")
    canvas_token = st.text_input("Canvas API Token", type="password")
    openai_api_key = st.text_input("OpenAI API Key", type="password")

dry_run = st.checkbox("🔍 Preview only (Dry Run)", value=False)
if dry_run:
    st.info("No data will be sent to Canvas. This is a preview only.")

st.markdown("---")

# ==============================
# Global Defaults / Settings UI
# ==============================
with st.expander("⚙️ Default Settings for Created Items (applies unless overridden by storyboard)", expanded=False):
    st.markdown("#### Pages")
    default_publish_pages = st.checkbox("Publish Pages", value=True)

    st.markdown("#### Discussions")
    col1, col2, col3 = st.columns(3)
    with col1:
        default_discussion_published = st.checkbox("Publish Discussions", value=True)
        default_discussion_threaded = st.checkbox("Threaded", value=True)
    with col2:
        default_discussion_require_initial_post = st.checkbox("Users must post before seeing replies", value=False)
        default_discussion_allow_rating = st.checkbox("Allow liking", value=True)
    with col3:
        default_discussion_only_graders_can_rate = st.checkbox("Only graders can like", value=False)
        default_discussion_sort_by_rating = st.checkbox("Sort by rating", value=False)

    st.markdown("#### Assignments")
    col4, col5, col6 = st.columns(3)
    with col4:
        default_assignment_points = st.number_input("Points Possible", min_value=0.0, value=10.0, step=1.0)
        default_assignment_published = st.checkbox("Publish Assignments", value=True)
        default_assignment_submission_types_text = st.multiselect("Submission Types", ["online_text_entry","online_url","online_upload","none"], default=["online_text_entry"])
    with col5:
        default_assignment_due_at = st.text_input("Due At (ISO 8601, e.g. 2025-12-31T23:59:00Z)", "")
        default_assignment_peer_reviews = st.checkbox("Peer Reviews", value=False)
        default_assignment_group_category_id = st.text_input("Group Category ID (optional)", "")
    with col6:
        default_assignment_notify_of_update = st.checkbox("Notify of Updates", value=False)
        default_assignment_free_form_criterion_comments = st.checkbox("Free-form rubric comments", value=False)
        default_assignment_omit_from_final_grade = st.checkbox("Omit from final grade", value=False)

    st.markdown("#### Quizzes")
    col7, col8, col9 = st.columns(3)
    with col7:
        default_quiz_published = st.checkbox("Publish Quizzes", value=True)
        default_quiz_time_limit = st.number_input("Time Limit (minutes, 0 = none)", min_value=0, value=0, step=5)
        default_quiz_shuffle_answers = st.checkbox("Shuffle Answers", value=True)
    with col8:
        default_quiz_allowed_attempts = st.number_input("Allowed Attempts (0 = unlimited)", min_value=0, value=1, step=1)
        default_quiz_scoring_policy = st.selectbox("Scoring Policy", options=["keep_highest","keep_latest"], index=0)
        default_quiz_one_question_at_a_time = st.checkbox("One question at a time", value=False)
    with col9:
        default_quiz_require_lockdown_browser = st.checkbox("Require LockDown Browser (placeholder)", value=False)
        default_quiz_show_correct_answers = st.checkbox("Show correct answers after submission", value=True)
        default_quiz_hide_results = st.selectbox("Hide results", options=["none","until_after_last_attempt"], index=0)

st.markdown("---")

# ==============================
# Helpers / Aliases
# ==============================
PAGE_TYPE_ALIASES = {
    "page": "page", "reading": "page", "video": "page", "overview": "page", "activity": "page", "lecture": "page",
    "discussion": "discussion", "discuss": "discussion", "forum": "discussion", "prompt": "discussion",
    "assignment": "assignment", "homework": "assignment", "project": "assignment", "exercise": "assignment", "task": "assignment",
    "quiz": "quiz", "assessment": "quiz", "exam": "quiz", "test": "quiz"
}

def canonical_page_type(value: str) -> str:
    if not value:
        return "page"
    v = value.strip().lower()
    return PAGE_TYPE_ALIASES.get(v, v or "page")

def normalize_base(domain: str) -> str:
    if not domain:
        return ""
    d = domain.strip().replace("http://", "").replace("https://", "").strip("/")
    return f"https://{d}"

def extract_canvas_pages(docx_file):
    """Collect text between <canvas_page> and </canvas_page> across paragraphs."""
    doc = Document(docx_file)
    pages = []
    current_block = []
    inside = False
    for para in doc.paragraphs:
        text = (para.text or "").strip()
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
    st.success(f"✅ Found {len(pages)} <canvas_page> block(s).")
    return pages

def extract_tag(tag, block):
    m = re.search(fr"<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else ""

def load_docx_text(file):
    doc = Document(file)
    return "\n".join([p.text for p in doc.paragraphs if p.text and p.text.strip()])

# ==============================
# Canvas API wrappers
# ==============================
def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}

def get_or_create_module(base, course_id, token, module_name, cache):
    if module_name in cache:
        return cache[module_name]
    url = f"{base}/api/v1/courses/{course_id}/modules"
    r = requests.get(url, headers=headers(token))
    if r.status_code == 200:
        for m in r.json():
            if m.get("name", "").strip().lower() == module_name.strip().lower():
                cache[module_name] = m["id"]
                return m["id"]
    # Create module
    r = requests.post(url, headers=headers(token), json={"module": {"name": module_name, "published": True}})
    if r.status_code in (200, 201):
        mid = r.json().get("id")
        cache[module_name] = mid
        return mid
    st.error(f"❌ Failed to create/find module '{module_name}': {r.status_code} {r.text}")
    return None

def create_page(base, course_id, token, title, html_body, published=True):
    url = f"{base}/api/v1/courses/{course_id}/pages"
    payload = {"wiki_page": {"title": title, "body": html_body, "published": bool(published)}}
    r = requests.post(url, headers=headers(token), json=payload)
    if r.status_code in (200, 201):
        return r.json().get("url")  # slug
    st.error(f"❌ Page '{title}' failed: {r.status_code} {r.text}")
    return None

def create_discussion(base, course_id, token, title, html_body, opts):
    url = f"{base}/api/v1/courses/{course_id}/discussion_topics"
    payload = {
        "title": title,
        "message": html_body,
        "published": opts.get("published", True),
        "discussion_type": "threaded" if opts.get("threaded") else "side_comment",
        "require_initial_post": opts.get("require_initial_post", False),
        "allow_rating": opts.get("allow_rating", False),
        "only_graders_can_rate": opts.get("only_graders_can_rate", False),
        "sort_by_rating": opts.get("sort_by_rating", False),
    }
    r = requests.post(url, headers=headers(token), json=payload)
    if r.status_code in (200, 201):
        return r.json().get("id")
    st.error(f"❌ Discussion '{title}' failed: {r.status_code} {r.text}")
    return None

def create_assignment(base, course_id, token, title, html_body, opts):
    url = f"{base}/api/v1/courses/{course_id}/assignments"
    payload = {
        "assignment": {
            "name": title,
            "description": html_body,
            "published": opts.get("published", True),
            "submission_types": opts.get("submission_types", ["online_text_entry"]),
            "points_possible": opts.get("points_possible", 10),
            "peer_reviews": opts.get("peer_reviews", False),
            "notify_of_update": opts.get("notify_of_update", False),
            "free_form_criterion_comments": opts.get("free_form_criterion_comments", False),
            "omit_from_final_grade": opts.get("omit_from_final_grade", False),
        }
    }
    # optional due date
    due_at = opts.get("due_at")
    if due_at:
        payload["assignment"]["due_at"] = due_at
    # optional group category
    group_cat = opts.get("group_category_id")
    if group_cat:
        try:
            payload["assignment"]["group_category_id"] = int(group_cat)
        except:
            pass

    r = requests.post(url, headers=headers(token), json=payload)
    if r.status_code in (200, 201):
        return r.json().get("id")
    st.error(f"❌ Assignment '{title}' failed: {r.status_code} {r.text}")
    return None

def create_quiz(base, course_id, token, title, description_html, quiz_json, opts):
    # Prefer JSON-provided description if present
    if quiz_json and quiz_json.get("quiz_description"):
        description_html = quiz_json["quiz_description"]

    q_url = f"{base}/api/v1/courses/{course_id}/quizzes"
    q_payload = {
        "quiz": {
            "title": title,
            "description": description_html,
            "published": opts.get("published", True),
            "quiz_type": "assignment",
            "time_limit": opts.get("time_limit", 0) or None,
            "shuffle_answers": opts.get("shuffle_answers", True),
            "allowed_attempts": opts.get("allowed_attempts", 1),
            "scoring_policy": opts.get("scoring_policy", "keep_highest"),
            "one_question_at_a_time": opts.get("one_question_at_a_time", False),
            # hide_results values: 'none' or 'until_after_last_attempt'
        }
    }
    if opts.get("hide_results", "none") != "none":
        q_payload["quiz"]["hide_results"] = opts.get("hide_results")
    q_resp = requests.post(q_url, headers=headers(token), json=q_payload)
    if q_resp.status_code not in (200, 201):
        st.error(f"❌ Quiz '{title}' failed: {q_resp.status_code} {q_resp.text}")
        return None

    quiz_id = q_resp.json().get("id")

    # Add questions if provided
    if quiz_json and quiz_json.get("questions"):
        for q in quiz_json["questions"]:
            answers = q.get("answers", []) or []
            q_type = "multiple_choice_question" if answers else "essay_question"
            ans_payload = [{"text": a["text"], "weight": 100 if a.get("is_correct") else 0} for a in answers] if answers else []
            qq_url = f"{base}/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions"
            qq_payload = {
                "question": {
                    "question_name": q.get("question_name") or "Question",
                    "question_text": q.get("question_text") or "",
                    "question_type": q_type,
                    "points_possible": 1,
                    "answers": ans_payload
                }
            }
            _ = requests.post(qq_url, headers=headers(token), json=qq_payload)

    return quiz_id

def add_to_module(base, course_id, token, module_id, item_type, ref, title):
    """
    item_type: 'Page', 'Discussion', 'Assignment', 'Quiz'
    ref: page_url (slug) for Page; numeric id for others
    """
    url = f"{base}/api/v1/courses/{course_id}/modules/{module_id}/items"
    payload = {"module_item": {"type": item_type, "title": title, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = ref
    else:
        payload["module_item"]["content_id"] = ref
    r = requests.post(url, headers=headers(token), json=payload)
    if r.status_code in (200, 201):
        return True
    st.error(f"❌ Module add failed ({item_type} '{title}'): {r.status_code} {r.text}")
    return False

# ==============================
# Main
# ==============================
if uploaded_file and template_file and canvas_domain_input and course_id and canvas_token and openai_api_key:
    base = normalize_base(canvas_domain_input)

    # Cache GPT results so re-clicking doesn't regenerate
    if "gpt_results" not in st.session_state:
        st.session_state.gpt_results = {}

    pages = extract_canvas_pages(uploaded_file)
    template_text = load_docx_text(template_file)
    client = OpenAI(api_key=openai_api_key)

    module_cache = {}
    last_known_module_name = None

    st.subheader("Detected Pages")

    # Convert per page + show per-page controls
    for i, block in enumerate(pages):
        block = block.strip()

        # Extract metadata
        page_type_raw = (extract_tag("page_type", block) or "page").strip()
        page_type = canonical_page_type(page_type_raw)
        page_title = extract_tag("page_title", block) or f"Page {i+1}"
        module_name = extract_tag("module_name", block)

        # Fallbacks for module name
        if not module_name:
            h1_match = re.search(r"<h1>(.*?)</h1>", block, flags=re.IGNORECASE)
            if h1_match:
                module_name = h1_match.group(1).strip()
                st.info(f"📘 Using <h1> as module name for '{page_title}': '{module_name}'")
        if not module_name:
            title_match = re.search(r"\d+\.\d+\s+(Module\s+[\w\s]+)", page_title, flags=re.IGNORECASE)
            if title_match:
                module_name = title_match.group(1).strip()
                st.info(f"📘 Extracted module name from title for '{page_title}': '{module_name}'")
        if not module_name:
            if last_known_module_name:
                module_name = last_known_module_name
                st.info(f"📘 Using previously found module name for '{page_title}': '{module_name}'")
            else:
                module_name = "General"
                st.warning(f"⚠️ No <module_name> or Heading 1 found for '{page_title}'. Using 'General'.")
        else:
            last_known_module_name = module_name

        cache_key = f"{page_title}-{i}"
        if cache_key not in st.session_state.gpt_results:
            with st.spinner(f"🤖 Converting page {i+1} [{page_title}] via GPT..."):
                system_prompt = f"""
You are an expert Canvas HTML generator. Convert storyboard blocks into **Canvas-ready HTML** using the uMich templates below.

## TEMPLATE LIBRARY (authoritative)
{template_text}

## PAGE TYPE ALIASES (use to normalize <page_type>)
- Page: page, reading, video, overview, activity, lecture
- Discussion: discussion, discuss, forum, prompt
- Assignment: assignment, homework, project, exercise, task
- Quiz: quiz, assessment, exam, test

## SUPPORTED TAGS (storyboard → HTML behavior)
- <canvas_page>...</canvas_page>: wraps a single logical Canvas item
- <page_type>...</page_type>: one of the aliases above (normalize to canonical)
- <page_title>...</page_title>: title string
- <module_name>...</module_name>: module name (may appear only once; reuse for subsequent pages)
- <accordion>Title: ...\nContent: ...</accordion>: render with an accordion component per the template doc
- <callout> ... </callout>: render as a styled callout/blockquote (see template)
- <focus_time> ... </focus_time>: render under "Focus Your Time" section using the template's list structure
- <objectives><li> ... </li> ...</objectives>: render under "Objectives" with ordered list like the template
- <video url=\\"...\\"></video>: place in the "Video Page" template at the ADD VIDEO HERE slot (use 640x360 iframe if YouTube)
- <resource href=\\"URL\\">Label</resource>: list under "Module Resources"
- <question><multiple_choice> ... </multiple_choice></question>: use for quiz parsing; ALSO include JSON described below

## RETURN FORMAT
1) First, return **only the HTML** body for the Canvas item (no triple backticks).
2) If the canonical page type is **quiz**, append a blank line followed by **pure JSON** with this exact structure:
{{
  "quiz_description": "<html description>",
  "questions": [
    {{"question_name": "Q1", "question_text": "...", "answers": [
      {{"text": "Option A", "is_correct": false}},
      {{"text": "Option B", "is_correct": true}}
    ]}}
  ]
}}
- For multiple-choice, set is_correct on the right answers.
- If no answers are present, treat as an essay question.

Now convert the following storyboard block using the templates and tags above.
"""
                response = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": block}
                    ],
                    temperature=0.3
                )
                raw = response.choices[0].message.content.strip()
                cleaned = re.sub(r"```(html|json)?", "", raw, flags=re.IGNORECASE).strip()
                m = re.search(r"({[\s\S]+})\s*$", cleaned)
                if m:
                    html_result = cleaned[:m.start()].strip()
                    try:
                        quiz_json = json.loads(m.group(1))
                    except Exception as e:
                        quiz_json = None
                        st.error(f"❌ Quiz JSON parsing failed for '{page_title}': {e}")
                else:
                    html_result = cleaned
                    quiz_json = None

                st.session_state.gpt_results[cache_key] = {
                    "html": html_result,
                    "quiz_json": quiz_json,
                    "meta": {"page_type": page_type, "page_title": page_title, "module_name": module_name}
                }
        else:
            html_result = st.session_state.gpt_results[cache_key]["html"]
            quiz_json = st.session_state.gpt_results[cache_key]["quiz_json"]

        with st.expander(f"📄 {page_title} ({page_type}) | Module: {module_name}", expanded=True):
            st.code(html_result, language="html")

            # Per-page upload
            if not dry_run and st.button(f"🚀 Upload '{page_title}'", key=f"upload_{i}"):
                mid = get_or_create_module(base, course_id, canvas_token, module_name, module_cache)
                if not mid:
                    st.stop()

                if page_type == "page":
                    slug = create_page(base, course_id, canvas_token, page_title, html_result, published=default_publish_pages)
                    if slug and add_to_module(base, course_id, canvas_token, mid, "Page", slug, page_title):
                        st.success(f"✅ Page '{page_title}' created and added to '{module_name}'")

                elif page_type == "discussion":
                    did = create_discussion(base, course_id, canvas_token, page_title, html_result, {
                        "published": default_discussion_published,
                        "threaded": default_discussion_threaded,
                        "require_initial_post": default_discussion_require_initial_post,
                        "allow_rating": default_discussion_allow_rating,
                        "only_graders_can_rate": default_discussion_only_graders_can_rate,
                        "sort_by_rating": default_discussion_sort_by_rating,
                    })
                    if did and add_to_module(base, course_id, canvas_token, mid, "Discussion", did, page_title):
                        st.success(f"✅ Discussion '{page_title}' created and added to '{module_name}'")

                elif page_type == "assignment":
                    aid = create_assignment(base, course_id, canvas_token, page_title, html_result, {
                        "published": default_assignment_published,
                        "submission_types": default_assignment_submission_types_text,
                        "points_possible": default_assignment_points,
                        "peer_reviews": default_assignment_peer_reviews,
                        "notify_of_update": default_assignment_notify_of_update,
                        "free_form_criterion_comments": default_assignment_free_form_criterion_comments,
                        "omit_from_final_grade": default_assignment_omit_from_final_grade,
                        "due_at": default_assignment_due_at.strip() or None,
                        "group_category_id": default_assignment_group_category_id.strip() or None,
                    })
                    if aid and add_to_module(base, course_id, canvas_token, mid, "Assignment", aid, page_title):
                        st.success(f"✅ Assignment '{page_title}' created and added to '{module_name}'")

                elif page_type == "quiz":
                    if not quiz_json:
                        st.error("❌ This quiz has no parsed questions JSON. Ensure the storyboard block follows the JSON format in the prompt.")
                    else:
                        qid = create_quiz(base, course_id, canvas_token, page_title, html_result, quiz_json, {
                            "published": default_quiz_published,
                            "time_limit": default_quiz_time_limit,
                            "shuffle_answers": default_quiz_shuffle_answers,
                            "allowed_attempts": default_quiz_allowed_attempts,
                            "scoring_policy": default_quiz_scoring_policy,
                            "one_question_at_a_time": default_quiz_one_question_at_a_time,
                            "hide_results": default_quiz_hide_results,
                            "show_correct_answers": default_quiz_show_correct_answers,
                        })
                        if qid and add_to_module(base, course_id, canvas_token, mid, "Quiz", qid, page_title):
                            st.success(f"✅ Quiz '{page_title}' created with questions and added to '{module_name}'")

                else:
                    # Fallback to Page
                    slug = create_page(base, course_id, canvas_token, page_title, html_result, published=default_publish_pages)
                    if slug and add_to_module(base, course_id, canvas_token, mid, "Page", slug, page_title):
                        st.success(f"✅ Page '{page_title}' created and added to '{module_name}' (fallback)")

    # Bulk upload AFTER all GPT conversions (from cache)
    if st.session_state.get("gpt_results"):
        if st.button("🚀 Upload ALL items (cached)", disabled=dry_run):
            if dry_run:
                st.info("Dry run is ON. Skipping uploads.")
            else:
                uploaded = 0
                module_cache = {}  # fresh for bulk
                for i, block in enumerate(pages):
                    title_for_key = extract_tag("page_title", block) or f"Page {i+1}"
                    cache_key = f"{title_for_key}-{i}"
                    item = st.session_state.gpt_results.get(cache_key)
                    if not item:
                        st.error(f"Missing cached GPT result for: {title_for_key}. Convert first.")
                        continue

                    html_result = item["html"]
                    quiz_json   = item.get("quiz_json")
                    meta        = item["meta"]
                    page_type   = meta["page_type"]
                    page_title  = meta["page_title"]
                    module_name = meta["module_name"]

                    mid = get_or_create_module(base, course_id, canvas_token, module_name, module_cache)
                    if not mid:
                        continue

                    if page_type == "page":
                        slug = create_page(base, course_id, canvas_token, page_title, html_result, published=default_publish_pages)
                        ok = slug and add_to_module(base, course_id, canvas_token, mid, "Page", slug, page_title)

                    elif page_type == "discussion":
                        did = create_discussion(base, course_id, canvas_token, page_title, html_result, {
                            "published": default_discussion_published,
                            "threaded": default_discussion_threaded,
                            "require_initial_post": default_discussion_require_initial_post,
                            "allow_rating": default_discussion_allow_rating,
                            "only_graders_can_rate": default_discussion_only_graders_can_rate,
                            "sort_by_rating": default_discussion_sort_by_rating,
                        })
                        ok = did and add_to_module(base, course_id, canvas_token, mid, "Discussion", did, page_title)

                    elif page_type == "assignment":
                        aid = create_assignment(base, course_id, canvas_token, page_title, html_result, {
                            "published": default_assignment_published,
                            "submission_types": default_assignment_submission_types_text,
                            "points_possible": default_assignment_points,
                            "peer_reviews": default_assignment_peer_reviews,
                            "notify_of_update": default_assignment_notify_of_update,
                            "free_form_criterion_comments": default_assignment_free_form_criterion_comments,
                            "omit_from_final_grade": default_assignment_omit_from_final_grade,
                            "due_at": default_assignment_due_at.strip() or None,
                            "group_category_id": default_assignment_group_category_id.strip() or None,
                        })
                        ok = aid and add_to_module(base, course_id, canvas_token, mid, "Assignment", aid, page_title)

                    elif page_type == "quiz":
                        if not quiz_json:
                            st.error(f"❌ (All) Quiz '{page_title}' has no parsed questions JSON.")
                            ok = False
                        else:
                            qid = create_quiz(base, course_id, canvas_token, page_title, html_result, quiz_json, {
                                "published": default_quiz_published,
                                "time_limit": default_quiz_time_limit,
                                "shuffle_answers": default_quiz_shuffle_answers,
                                "allowed_attempts": default_quiz_allowed_attempts,
                                "scoring_policy": default_quiz_scoring_policy,
                                "one_question_at_a_time": default_quiz_one_question_at_a_time,
                                "hide_results": default_quiz_hide_results,
                                "show_correct_answers": default_quiz_show_correct_answers,
                            })
                            ok = qid and add_to_module(base, course_id, canvas_token, mid, "Quiz", qid, page_title)

                    else:
                        slug = create_page(base, course_id, canvas_token, page_title, html_result, published=default_publish_pages)
                        ok = slug and add_to_module(base, course_id, canvas_token, mid, "Page", slug, page_title)

                    if ok:
                        uploaded += 1
                        st.success(f"✅ (All) {page_type.title()} '{page_title}' added to '{module_name}'")

                if uploaded == 0:
                    st.warning("No items were uploaded. Check tokens/IDs and errors above.")
                else:
                    st.success(f"🎉 Uploaded {uploaded} item(s) successfully.")
