import streamlit as st
import requests
import re
from docx import Document
import openai
from bs4 import BeautifulSoup

# --- UI Setup ---
st.set_page_config(page_title="Canvas Storyboard Importer with AI", layout="centered")
st.title("🧩 Canvas Storyboard Importer with AI HTML Generator")

canvas_domain = st.text_input("Canvas Base URL (e.g. canvas.instructure.com)")
course_id = st.text_input("Canvas Course ID")
token = st.text_input("Canvas API Token", type="password")

uploaded_file = st.file_uploader("Upload storyboard (.docx)", type="docx")

# --- Custom Component Templates ---
TEMPLATES = {
    "accordion": '<details><summary style="cursor: pointer; font-weight: bold; background-color:#0077b6; color:white; padding:10px; border-radius:5px;">{title} <small>(click to reveal)</small></summary><div style="padding:10px 20px; margin-top: 10px; background-color:#f2f2f2; color:#333;">{body}</div></details>',
    "callout": '<blockquote><p>{body}</p></blockquote>'
}

# --- Bullet Conversion ---
def convert_bullets(text):
    lines = text.split("\n")
    out = []
    in_list = False
    for line in lines:
        if line.strip().startswith("-"):
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{line.strip()[1:].strip()}</li>")
        else:
            if in_list:
                out.append("</ul>")
                in_list = False
            out.append(line)
    if in_list:
        out.append("</ul>")
    return '\n'.join(out)

# --- HTML Processing ---
def process_html_content(raw_text):
    fallback_html = raw_text
    fallback_html = re.sub(r"<accordion>\s*Title:\s*(.*?)\s*Content:\s*(.*?)</accordion>",
                           lambda m: TEMPLATES["accordion"].format(title=m.group(1).strip(), body=m.group(2).strip()),
                           fallback_html, flags=re.DOTALL)
    fallback_html = re.sub(r"<callout>(.*?)</callout>",
                           lambda m: TEMPLATES["callout"].format(body=m.group(1)),
                           fallback_html, flags=re.DOTALL)
    fallback_html = convert_bullets(fallback_html)
    return convert_to_html_with_openai(raw_text, fallback_html)

# --- OpenAI HTML Conversion ---
def convert_to_html_with_openai(docx_text, fallback_html):
    try:
        from openai import OpenAI
        client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])

        prompt = f"""Convert the following storyboard content to HTML. Preserve formatting like headings (<h1>, <h2>), bold (<strong>), italics (<em>), and lists. 
Replace the following tags with valid HTML (using inline CSS):
- <accordion> → <details><summary style=\"cursor: pointer; font-weight: bold; background-color:#0077b6; color:white; padding:10px; border-radius:5px;\">Title <small>(click to reveal)</small></summary><div style=\"padding:10px 20px; margin-top: 10px; background-color:#f2f2f2; color:#333;\">Content</div></details>
- <callout> → <blockquote><p>...</p></blockquote>
- Bullet points starting with '-' → <ul><li>...</li></ul>
- <question><multiple choice> → Parse as Canvas-style questions: detect feedback using <answer feedback=\"...\">*Correct answer</answer>

Storyboard Content:
{docx_text}

Output only valid HTML. No explanation or preamble."""

        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        st.warning(f"⚠️ OpenAI processing failed, using fallback: {e}")
        return fallback_html

# --- Canvas API Integration ---
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

def create_discussion(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"title": title, "message": html_body, "published": True}
    response = requests.post(url, headers=headers, json=payload)
    return response.json().get("id") if response.status_code in (200, 201) else None

def create_legacy_quiz(domain, course_id, title, html_body, token, questions):
    url = f"https://{domain}/api/v1/courses/{course_id}/quizzes"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "quiz": {
            "title": title,
            "description": html_body,
            "published": True,
            "quiz_type": "assignment",
            "scoring_policy": "keep_highest"
        }
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code not in (200, 201):
        st.error(f"Failed to create legacy quiz '{title}': {response.text}")
        return None

    quiz_id = response.json().get("id")
    for q in questions:
        question_type = "multiple_choice_question" if q['type'] == "multiple choice" else "essay_question"
        question_payload = {
            "question": {
                "question_name": "Q",
                "question_text": q['text'],
                "question_type": question_type,
                "points_possible": 1,
                "answers": [
                    {"text": a['text'], "weight": 100 if a['correct'] else 0} for a in q.get('answers', [])
                ] if question_type == "multiple_choice_question" else []
            }
        }
        q_url = f"https://{domain}/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions"
        requests.post(q_url, headers=headers, json=question_payload)
    return quiz_id

def create_new_quiz(domain, course_id, title, html_body, token, questions):
    return create_legacy_quiz(domain, course_id, title, html_body, token, questions)  # Placeholder for actual New Quizzes API

def add_to_module(domain, course_id, module_id, item_type, item_ref, title, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = item_ref
    else:
        payload["module_item"]["content_id"] = item_ref
    response = requests.post(url, headers=headers, json=payload)
    return response.status_code in (200, 201)

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

# --- Helpers ---
def is_new_quiz(raw_text):
    return "<new_quiz>" in raw_text.lower()

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
                feedback = "Example feedback here"
                answers.append({"text": text, "correct": correct, "feedback": feedback})
        if qtext:
            questions.append({"type": qtype.strip().lower(), "text": qtext, "answers": answers})
    return questions

def extract_canvas_pages(docx_file):
    doc = Document(docx_file)
    full_text = '\n'.join([para.text for para in doc.paragraphs])
    return re.findall(r"<canvas_page>(.*?)</canvas_page>", full_text, re.DOTALL)

def parse_page_block(block_text):
    def extract_tag(tag):
        match = re.search(fr"<{tag}>(.*?)</{tag}>", block_text)
        return match.group(1).strip() if match else ""
    page_type = extract_tag("page_type").lower()
    page_name = extract_tag("page_name")
    module_name = extract_tag("module_name") or "General"
    content = re.sub(r"<(page_type|page_name|module_name)>.*?</\1>", "", block_text, flags=re.DOTALL).strip()
    return page_type, page_name, module_name, content

# --- Main Logic ---
if uploaded_file and canvas_domain and course_id and token:
    pages = extract_canvas_pages(uploaded_file)
    module_cache = {}

    for block in pages:
        page_type, title, module, content = parse_page_block(block)
        html = process_html_content(content)
        mid = get_or_create_module(module, canvas_domain, course_id, token, module_cache)
        if not mid:
            continue

        if page_type == "quiz":
            questions = parse_quiz_questions(content)
            if is_new_quiz(content):
                qid = create_new_quiz(canvas_domain, course_id, title, html, token, questions)
            else:
                qid = create_legacy_quiz(canvas_domain, course_id, title, html, token, questions)
            if qid and add_to_module(canvas_domain, course_id, mid, "Quiz", qid, title, token):
                st.success(f"✅ Quiz '{title}' created and added to module '{module}'")
        elif page_type == "assignment":
            aid = create_assignment(canvas_domain, course_id, title, html, token)
            if aid and add_to_module(canvas_domain, course_id, mid, "Assignment", aid, title, token):
                st.success(f"✅ Assignment '{title}' created and added to module '{module}'")
        elif page_type == "discussion":
            did = create_discussion(canvas_domain, course_id, title, html, token)
            if did and add_to_module(canvas_domain, course_id, mid, "Discussion", did, title, token):
                st.success(f"✅ Discussion '{title}' created and added to module '{module}'")
        else:
            page_url = create_page(canvas_domain, course_id, title, html, token)
            if page_url and add_to_module(canvas_domain, course_id, mid, "Page", page_url, title, token):
                st.success(f"✅ Page '{title}' created and added to module '{module}'")
