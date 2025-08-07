import streamlit as st
from docx import Document
from openai import OpenAI
import requests
import re

# --- UI Setup ---
st.set_page_config(page_title="📄 DOCX → GPT → Canvas", layout="centered")
st.title("📄 Upload DOCX → Convert via GPT → Upload to Canvas")

# --- Inputs ---
uploaded_file = st.file_uploader("Upload your storyboard (.docx)", type="docx")
template_file = st.file_uploader("Upload uMich Template Code (.docx)", type="docx")
canvas_domain = st.text_input("Canvas Base URL (e.g. canvas.instructure.com)")
course_id = st.text_input("Canvas Course ID")
canvas_token = st.text_input("Canvas API Token", type="password")
openai_api_key = st.text_input("OpenAI API Key", type="password")

# --- Helpers ---
def load_docx_text(file):
    doc = Document(file)
    return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])

def extract_canvas_pages(docx_file):
    doc = Document(docx_file)
    full_text = '\n'.join([para.text for para in doc.paragraphs])
    return re.findall(r"<canvas_page>(.*?)</canvas_page>", full_text, re.DOTALL)

def extract_tag(tag, text):
    match = re.search(fr"<{tag}>(.*?)</{tag}>", text)
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
        st.error(f"Failed to create/find module: {module_name}")
        return None

def create_canvas_item(canvas_type, domain, course_id, title, html_body, token, questions=None):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if canvas_type == "page":
        url = f"https://{domain}/api/v1/courses/{course_id}/pages"
        payload = {"wiki_page": {"title": title, "body": html_body, "published": True}}
    elif canvas_type == "assignment":
        url = f"https://{domain}/api/v1/courses/{course_id}/assignments"
        payload = {"assignment": {"name": title, "description": html_body, "published": True, "submission_types": ["online_text_entry"], "points_possible": 10}}
    elif canvas_type == "discussion":
        url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
        payload = {"title": title, "message": html_body}
    elif canvas_type == "quiz":
        url = f"https://{domain}/api/v1/courses/{course_id}/quizzes"
        payload = {"quiz": {"title": title, "description": html_body, "published": True, "quiz_type": "assignment"}}
    else:
        return None, None

    resp = requests.post(url, headers=headers, json=payload)
    if resp.status_code not in (200, 201):
        st.error(f"❌ Failed to create {canvas_type}: {resp.text}")
        return None, None

    result = resp.json()
    item_id = result.get("id") or result.get("url")

    # Handle quiz questions if needed
    if canvas_type == "quiz" and questions:
        for q in questions:
            q_type = "multiple_choice_question" if q['type'] == "multiple choice" else "essay_question"
            q_payload = {
                "question": {
                    "question_name": "Q",
                    "question_text": q['text'],
                    "question_type": q_type,
                    "points_possible": 1,
                    "answers": [
                        {"text": a['text'], "weight": 100 if a['correct'] else 0}
                        for a in q.get('answers', [])
                    ] if q_type == "multiple_choice_question" else []
                }
            }
            q_url = f"https://{domain}/api/v1/courses/{course_id}/quizzes/{item_id}/questions"
            requests.post(q_url, headers=headers, json=q_payload)

    return result.get("url"), item_id

def add_to_module(domain, course_id, module_id, item_type, item_ref, title, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = item_ref
    else:
        payload["module_item"]["content_id"] = item_ref

    resp = requests.post(url, headers=headers, json=payload)
    return resp.status_code in (200, 201)

def parse_quiz_questions(raw):
    questions = []
    blocks = re.findall(r"<question><(.*?)>\s*(.*?)</question>", raw, re.DOTALL)
    for qtype, qbody in blocks:
        lines = qbody.strip().split("\n")
        qtext = ""
        answers = []
        for line in lines:
            if not qtext:
                qtext = line.strip()
            elif re.match(r"\*?[A-E][.:]", line.strip()):
                correct = line.strip().startswith("*")
                text = re.sub(r"^\*?([A-E][.:])", r"\1", line.strip()).strip()
                answers.append({"text": text, "correct": correct})
        if qtext:
            questions.append({"type": qtype.strip().lower(), "text": qtext, "answers": answers})
    return questions

# --- Main Logic ---
if uploaded_file and template_file and canvas_domain and course_id and canvas_token and openai_api_key:
    with st.spinner("📖 Reading documents..."):
        template_text = load_docx_text(template_file)
        canvas_blocks = extract_canvas_pages(uploaded_file)

    client = OpenAI(api_key=openai_api_key)
    module_cache = {}

    for i, block_text in enumerate(canvas_blocks):
        page_type = extract_tag("page_type", block_text).lower() or "page"
        page_title = extract_tag("page_name", block_text) or f"Page {i+1}"
        module_name = extract_tag("module_name", block_text) or "General"

        st.markdown(f"### ✨ {page_type.title()}: {page_title} in {module_name}")

        # Generate HTML with GPT
        with st.spinner("🤖 Sending content to GPT..."):
            system_prompt = f"""
You are an expert Canvas HTML generator.
Below is a set of uMich Canvas LMS HTML templates followed by a storyboard page using tags.

Match the tags to the templates and convert the storyboard content to styled HTML for Canvas.

TEMPLATES:
{template_text}

TAGS YOU WILL SEE:
<canvas_page>, <page_type>, <page_title>, <module_name>, <quiz_title>, <question>, <multiple_choice>
* before a choice = correct answer
"""
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": block_text}
                ],
                temperature=0.3
            )
            html_result = response.choices[0].message.content
            st.code(html_result, language='html')

        # Canvas integration
        mid = get_or_create_module(module_name, canvas_domain, course_id, canvas_token, module_cache)
        if not mid:
            continue

        quiz_questions = parse_quiz_questions(block_text) if page_type == "quiz" else None
        item_url, item_id = create_canvas_item(page_type, canvas_domain, course_id, page_title, html_result, canvas_token, quiz_questions)

        item_type = {
            "page": "Page",
            "assignment": "Assignment",
            "discussion": "Discussion",
            "quiz": "Quiz"
        }.get(page_type, "Page")

        if item_url and add_to_module(canvas_domain, course_id, mid, item_type, item_id, page_title, canvas_token):
            st.success(f"✅ {item_type} '{page_title}' created and added to module '{module_name}'")
        else:
            st.error(f"❌ Failed to add {page_type} '{page_title}' to module")
