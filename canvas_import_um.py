import streamlit as st
from docx import Document
from openai import OpenAI
import requests
import re
import json

st.set_page_config(page_title="📄 DOCX → GPT → Canvas (Multi-Page)", layout="centered")
st.title("📄 Upload DOCX → Convert via GPT → Upload to Canvas")

# --- Inputs ---
uploaded_file = st.file_uploader("Upload your storyboard (.docx)", type="docx")
template_file = st.file_uploader("Upload uMich Template Code (.docx)", type="docx")
canvas_domain = st.text_input("Canvas Base URL (e.g. canvas.instructure.com)")
course_id = st.text_input("Canvas Course ID")
canvas_token = st.text_input("Canvas API Token", type="password")
openai_api_key = st.text_input("OpenAI API Key", type="password")
dry_run = st.checkbox("🔍 Preview only (Dry Run)")
bulk_upload = st.checkbox("📤 Upload all pages automatically (no buttons)", value=False)
if dry_run:
    st.info("No data will be sent to Canvas. This is a preview only.")

# --- Helper Functions ---
def extract_canvas_pages(docx_file):
    doc = Document(docx_file)
    full_text = '\n'.join([para.text for para in doc.paragraphs])
    matches = re.findall(r"<canvas_page>(.*?)</canvas_page>", full_text, re.DOTALL | re.IGNORECASE)
    st.info(f"✅ Found {len(matches)} <canvas_page> block(s).")
    return matches

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
        st.error(f"Failed to create/find module: {module_name}")
        return None

def create_page(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/pages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"wiki_page": {"title": title, "body": html_body, "published": True}}
    response = requests.post(url, headers=headers, json=payload)
    return response.json().get("url") if response.status_code in (200, 201) else None

def create_assignment(domain, course_id, title, html_body, token):
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
    response = requests.post(url, headers=headers, json=payload)
    return response.json().get("id") if response.status_code in (200, 201) else None

def add_to_module(domain, course_id, module_id, item_type, item_ref, title, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = item_ref
    else:
        payload["module_item"]["content_id"] = item_ref
    return requests.post(url, headers=headers, json=payload).status_code in (200, 201)

def load_docx_text(file):
    doc = Document(file)
    return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])

# --- Main Logic ---
if uploaded_file and template_file and canvas_domain and course_id and canvas_token and openai_api_key:
    pages = extract_canvas_pages(uploaded_file)
    module_cache = {}
    template_text = load_docx_text(template_file)
    client = OpenAI(api_key=openai_api_key)

    st.subheader("Detected Pages")
    for i, block in enumerate(pages):
        block = block.strip()
        page_type = extract_tag("page_type", block).lower() or "page"
        page_title = extract_tag("page_title", block) or f"Page {i+1}"
        module_name = extract_tag("module_name", block) or "General"

        system_prompt = f"""
You are an expert Canvas HTML generator.
Below is a set of uMich Canvas LMS HTML templates followed by a storyboard page using tags.

Match the tags to the templates and convert the storyboard content to styled HTML for Canvas.

TEMPLATES:
{template_text}

TAGS YOU WILL SEE:
<canvas_page> = start of Canvas page
</canvas_page> = end of Canvas page
<page_type> = Canvas page type
<page_title> = title of the page
<module_name> = name of the module
<quiz_title> = title of the quiz
<question> = question block.
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
"""
        user_prompt = block

        with st.spinner(f"🤖 Converting page {i+1} [{page_title}] via GPT..."):
            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
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
                except Exception as e:
                    quiz_json = None
                    st.error(f"❌ Quiz JSON parsing failed: {e}")
            else:
                html_result = cleaned
                quiz_json = None

        st.markdown(f"### 📄 {page_title} ({page_type}) in module: {module_name}")
        st.code(html_result, language="html")

        if bulk_upload:
            submit = True
        else:
            with st.form(key=f"form_{i}"):
                submit = st.form_submit_button(f"🚀 Upload '{page_title}'")

        if submit:
            mid = get_or_create_module(module_name, canvas_domain, course_id, canvas_token, module_cache)
            if not mid:
                continue
            if dry_run:
                st.info(f"[Dry Run] Skipped upload of '{page_title}'")
                continue

            if page_type == "assignment":
                aid = create_assignment(canvas_domain, course_id, page_title, html_result, canvas_token)
                if aid and add_to_module(canvas_domain, course_id, mid, "Assignment", aid, page_title, canvas_token):
                    st.success(f"✅ Assignment '{page_title}' created and added to '{module_name}'")

            elif page_type == "discussion":
                url = f"https://{canvas_domain}/api/v1/courses/{course_id}/discussion_topics"
                headers = {"Authorization": f"Bearer {canvas_token}", "Content-Type": "application/json"}
                payload = {"title": page_title, "message": html_result, "published": True}
                resp = requests.post(url, headers=headers, json=payload)
                if resp.status_code in (200, 201):
                    did = resp.json().get("id")
                    if add_to_module(canvas_domain, course_id, mid, "Discussion", did, page_title, canvas_token):
                        st.success(f"✅ Discussion '{page_title}' created and added to '{module_name}'")

            elif page_type == "quiz":
                try:
                    if not quiz_json:
                        raise ValueError("No quiz JSON detected")
                    description = quiz_json.get("quiz_description", "")
                    url = f"https://{canvas_domain}/api/v1/courses/{course_id}/quizzes"
                    headers = {"Authorization": f"Bearer {canvas_token}", "Content-Type": "application/json"}
                    payload = {"quiz": {"title": page_title, "description": description, "published": True, "quiz_type": "assignment"}}
                    resp = requests.post(url, headers=headers, json=payload)
                    if resp.status_code in (200, 201):
                        qid = resp.json().get("id")
                        for q in quiz_json.get("questions", []):
                            q_url = f"https://{canvas_domain}/api/v1/courses/{course_id}/quizzes/{qid}/questions"
                            q_payload = {
                                "question": {
                                    "question_name": q.get("question_name", "Q"),
                                    "question_text": q.get("question_text", ""),
                                    "question_type": "multiple_choice_question",
                                    "points_possible": 1,
                                    "answers": [
                                        {"text": ans.get("text"), "weight": 100 if ans.get("is_correct") else 0}
                                        for ans in q.get("answers", [])
                                    ]
                                }
                            }
                            requests.post(q_url, headers=headers, json=q_payload)
                        if add_to_module(canvas_domain, course_id, mid, "Quiz", qid, page_title, canvas_token):
                            st.success(f"✅ Quiz '{page_title}' with questions created and added to '{module_name}'")
                except Exception as e:
                    st.error(f"Quiz creation failed: {e}")

            else:
                page_url = create_page(canvas_domain, course_id, page_title, html_result, canvas_token)
                if page_url and add_to_module(canvas_domain, course_id, mid, "Page", page_url, page_title, canvas_token):
                    st.success(f"✅ Page '{page_title}' created and added to '{module_name}'")
