
import streamlit as st
from docx import Document
from openai import OpenAI
import requests
import re
import json

st.set_page_config(page_title="📄 DOCX → GPT → Canvas (All Types)", layout="centered")
st.title("📄 Upload DOCX → Convert via GPT → Upload to Canvas (All Content Types)")

# --- Inputs ---
uploaded_file = st.file_uploader("Upload your storyboard (.docx)", type="docx")
template_file = st.file_uploader("Upload uMich Template Code (.docx)", type="docx")
canvas_domain = st.text_input("Canvas Base URL (e.g. canvas.instructure.com)")
course_id = st.text_input("Canvas Course ID")
canvas_token = st.text_input("Canvas API Token", type="password")
openai_api_key = st.text_input("OpenAI API Key", type="password")
dry_run = st.checkbox("🔍 Preview only (Dry Run)")
bulk_upload = st.checkbox("📄 Upload all pages automatically (no buttons)", value=False)
if dry_run:
    st.info("No data will be sent to Canvas. This is a preview only.")

# --- Helper Functions ---
def extract_canvas_pages(docx_file):
    doc = Document(docx_file)
    pages = []
    current_block = []
    inside_block = False

    for para in doc.paragraphs:
        text = para.text.strip()
        if "<canvas_page>" in text.lower():
            inside_block = True
            current_block = [text]
            continue
        if "</canvas_page>" in text.lower():
            current_block.append(text)
            pages.append("\n".join(current_block))
            inside_block = False
            continue
        if inside_block:
            current_block.append(text)

    st.success(f"✅ Found {len(pages)} <canvas_page> block(s).")
    return pages

def extract_tag(tag, block):
    match = re.search(fr"<{tag}>(.*?)</{tag}>", block, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else ""

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

# Canvas creation functions
def create_page(domain, course_id, title, html_body, token):
    if len(title) > 255:
        title = title[:252] + "..."
    url = f"https://{domain}/api/v1/courses/{course_id}/pages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"wiki_page": {"title": title, "body": html_body, "published": True}}
    response = requests.post(url, headers=headers, json=payload)
    return response.json().get("url") if response.status_code in (200, 201) else None

def create_assignment(domain, course_id, title, description, token):
    if len(title) > 255:
        title = title[:252] + "..."
    url = f"https://{domain}/api/v1/courses/{course_id}/assignments"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"assignment": {"name": title, "description": description, "published": True, "submission_types": ["online_upload"], "points_possible": 100}}
    r = requests.post(url, headers=headers, json=payload)
    return r.json().get("id") if r.status_code in (200, 201) else None

def create_discussion(domain, course_id, title, message, token):
    if len(title) > 255:
        title = title[:252] + "..."
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"title": title, "message": message, "published": True}
    r = requests.post(url, headers=headers, json=payload)
    return r.json().get("id") if r.status_code in (200, 201) else None

def create_quiz(domain, course_id, title, description, token):
    if len(title) > 255:
        title = title[:252] + "..."
    url = f"https://{domain}/api/v1/courses/{course_id}/quizzes"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"quiz": {"title": title, "description": description, "published": True, "quiz_type": "assignment"}}
    r = requests.post(url, headers=headers, json=payload)
    return r.json().get("id") if r.status_code in (200, 201) else None

def add_quiz_question(domain, course_id, quiz_id, question_data, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"question": question_data}
    requests.post(url, headers=headers, json=payload)

def add_to_module(domain, course_id, module_id, item_type, item_id_or_url, title, token):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    payload = {"module_item": {"type": item_type, "title": title, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = item_id_or_url
    elif item_type in ["Assignment", "Discussion", "Quiz"]:
        payload["module_item"]["content_id"] = item_id_or_url
    requests.post(url, headers=headers, json=payload)

def load_docx_text(file):
    doc = Document(file)
    return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])

# --- Main Logic ---
if uploaded_file and template_file and canvas_domain and course_id and canvas_token and openai_api_key:
    if "gpt_results" not in st.session_state:
        st.session_state.gpt_results = {}

    pages = extract_canvas_pages(uploaded_file)
    template_text = load_docx_text(template_file)
    client = OpenAI(api_key=openai_api_key)
    module_cache = {}
    last_known_module_name = None

    st.subheader("Detected Pages")
    for i, block in enumerate(pages):
        page_type = extract_tag("page_type", block).lower() or "page"
        page_title = extract_tag("page_title", block) or f"Page {i+1}"
        module_name = extract_tag("module_name", block)

        if not module_name and last_known_module_name:
            module_name = last_known_module_name
        elif module_name:
            last_known_module_name = module_name
        else:
            module_name = "General"

        cache_key = f"{page_title}-{i}"
        if cache_key not in st.session_state.gpt_results:
            with st.spinner(f"🤖 Converting page {i+1} via GPT..."):
                system_prompt = f"""
You are an expert Canvas HTML generator.
Below is a set of uMich Canvas LMS HTML templates followed by a storyboard page using tags.

TEMPLATES:
{template_text}
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
                match = re.search(r"({[\s\S]+})$", cleaned)
                if match:
                    html_result = cleaned[:match.start()].strip()
                    try:
                        quiz_json = json.loads(match.group(1))
                    except:
                        quiz_json = None
                else:
                    html_result = cleaned
                    quiz_json = None

                st.session_state.gpt_results[cache_key] = {"html": html_result, "quiz_json": quiz_json}
        else:
            html_result = st.session_state.gpt_results[cache_key]["html"]
            quiz_json = st.session_state.gpt_results[cache_key]["quiz_json"]

        with st.expander(f"{page_title} ({page_type}) | Module: {module_name}", expanded=True):
            st.code(html_result, language="html")

            if bulk_upload or dry_run:
                if bulk_upload and not dry_run:
                    mid = get_or_create_module(module_name, canvas_domain, course_id, canvas_token, module_cache)
                    if mid:
                        if page_type == "page":
                            page_url = create_page(canvas_domain, course_id, page_title, html_result, canvas_token)
                            if page_url:
                                add_to_module(canvas_domain, course_id, mid, "Page", page_url, page_title, canvas_token)
                        elif page_type == "assignment":
                            aid = create_assignment(canvas_domain, course_id, page_title, html_result, canvas_token)
                            if aid:
                                add_to_module(canvas_domain, course_id, mid, "Assignment", aid, page_title, canvas_token)
                        elif page_type == "discussion":
                            did = create_discussion(canvas_domain, course_id, page_title, html_result, canvas_token)
                            if did:
                                add_to_module(canvas_domain, course_id, mid, "Discussion", did, page_title, canvas_token)
                        elif page_type == "quiz":
                            qid = create_quiz(canvas_domain, course_id, page_title, html_result, canvas_token)
                            if qid and quiz_json:
                                for q in quiz_json.get("questions", []):
                                    add_quiz_question(canvas_domain, course_id, qid, q, canvas_token)
                                add_to_module(canvas_domain, course_id, mid, "Quiz", qid, page_title, canvas_token)
            else:
                if st.button(f"🚀 Upload '{page_title}'", key=f"upload_{i}"):
                    mid = get_or_create_module(module_name, canvas_domain, course_id, canvas_token, module_cache)
                    if mid:
                        if page_type == "page":
                            page_url = create_page(canvas_domain, course_id, page_title, html_result, canvas_token)
                            if page_url:
                                add_to_module(canvas_domain, course_id, mid, "Page", page_url, page_title, canvas_token)
                        elif page_type == "assignment":
                            aid = create_assignment(canvas_domain, course_id, page_title, html_result, canvas_token)
                            if aid:
                                add_to_module(canvas_domain, course_id, mid, "Assignment", aid, page_title, canvas_token)
                        elif page_type == "discussion":
                            did = create_discussion(canvas_domain, course_id, page_title, html_result, canvas_token)
                            if did:
                                add_to_module(canvas_domain, course_id, mid, "Discussion", did, page_title, canvas_token)
                        elif page_type == "quiz":
                            qid = create_quiz(canvas_domain, course_id, page_title, html_result, canvas_token)
                            if qid and quiz_json:
                                for q in quiz_json.get("questions", []):
                                    add_quiz_question(canvas_domain, course_id, qid, q, canvas_token)
                                add_to_module(canvas_domain, course_id, mid, "Quiz", qid, page_title, canvas_token)
