import streamlit as st
import requests
import re
from docx import Document

# --- UI Setup ---
st.set_page_config(page_title="Canvas Storyboard Importer", layout="centered")
st.title("🧩 Canvas Storyboard Importer with AI HTML Generator")

canvas_domain = st.text_input("Canvas Base URL (e.g. canvas.instructure.com)")
course_id = st.text_input("Canvas Course ID")
token = st.text_input("Canvas API Token", type="password")

uploaded_file = st.file_uploader("Upload storyboard (.docx)", type="docx")

# --- Custom Component Templates ---
TEMPLATES = {
    "accordion": '<details><summary style="cursor: pointer; font-weight: bold; background-color:#0077b6; color:white; padding:10px; border-radius:5px;">{title} <small>(click to reveal)</small></summary><div style="padding:10px 20px; margin-top: 10px; background-color:#f2f2f2; color:#333;">{body}</div></details>',
    "callout": '<blockquote><p>{body}</p></blockquote>',
    "bullets": lambda items: '<ul>' + ''.join([f'<li>{item.strip().lstrip("-•").strip()}</li>' for item in items.split("\n") if item.strip()]) + '</ul>'
}

# --- Canvas API Integration ---
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
    if response.status_code in (200, 201):
        return response.json().get("url")
    else:
        st.error(f"Failed to create page '{title}': {response.text}")
        return None

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
    if response.status_code in (200, 201):
        return response.json().get("id")
    else:
        st.error(f"❌ Failed to create assignment '{title}': {response.text}")
        return None

def create_quiz(domain, course_id, title, html_body, token):
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
    if response.status_code in (200, 201):
        return response.json().get("id")
    else:
        st.error(f"❌ Failed to create quiz '{title}': {response.text}")
        return None

def create_discussion(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {
        "title": title,
        "message": html_body,
        "published": True
    }
    response = requests.post(url, headers=headers, json=payload)
    if response.status_code in (200, 201):
        return response.json().get("id")
    else:
        st.error(f"❌ Failed to create discussion '{title}': {response.text}")
        return None

def add_to_module(domain, course_id, module_id, page_url, title, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"module_item": {"title": title, "type": "Page", "page_url": page_url, "published": True}}
    response = requests.post(url, headers=headers, json=payload)
    return response.status_code in (200, 201)

# --- AI HTML Conversion ---
def process_html_content(raw_text):
    content = raw_text
    content = re.sub(r"<accordion>\s*Title:\s*(.*?)\s*Content:\s*(.*?)</accordion>",
                     lambda m: TEMPLATES["accordion"].format(title=m.group(1).strip(), body=m.group(2).strip()),
                     content, flags=re.DOTALL)

    content = re.sub(r"<callout>(.*?)</callout>",
                     lambda m: TEMPLATES["callout"].format(body=m.group(1)),
                     content, flags=re.DOTALL)

    def bullet_transform(text):
        lines = text.split('\n')
        result = []
        in_list = False
        for line in lines:
            if line.strip().startswith("-"):
                if not in_list:
                    result.append("<ul>")
                    in_list = True
                result.append(f"<li>{line.strip()[1:].strip()}</li>")
            else:
                if in_list:
                    result.append("</ul>")
                    in_list = False
                result.append(line)
        if in_list:
            result.append("</ul>")
        return '\n'.join(result)

    content = bullet_transform(content)

    def quiz_transform(text):
        lines = text.split('\n')
        output = []
        question = None
        choices = []
        for line in lines:
            line = line.strip()
            if line.lower().startswith("question"):
                if question:
                    output.append(f"<p><strong>{question}</strong></p><ul>{''.join(choices)}</ul>")
                    choices = []
                question = line
            elif re.match(r"^\*?[A-Da-d]\.", line):
                is_correct = line.startswith("*")
                clean_line = re.sub(r"^\*?([A-Da-d]\. )", '', line).strip()
                li = f"<li><strong>{line[:2]}</strong> {clean_line}</li>"
                if is_correct:
                    li = li.replace("<li>", "<li style='background-color:#e0ffe0;'>")
                choices.append(li)
            else:
                output.append(f"<p>{line}</p>")

        if question:
            output.append(f"<p><strong>{question}</strong></p><ul>{''.join(choices)}</ul>")

        return '\n'.join(output)

    content = quiz_transform(content)

    return content

# --- Text Parsing from DOCX ---
def extract_canvas_pages(docx_file):
    doc = Document(docx_file)
    full_text = '\n'.join([para.text for para in doc.paragraphs])
    return re.findall(r"<canvas_page>(.*?)</canvas_page>", full_text, re.DOTALL)

def parse_page_block(block_text):
    def extract_tag(tag):
        match = re.search(fr"<{tag}>(.*?)</{tag}>", block_text)
        return match.group(1).strip() if match else ""

    page_type = extract_tag("page_type") or "Pages"
    page_name = extract_tag("page_name") or "Untitled Page"
    module_name = extract_tag("module_name") or "General"
    clean_text = re.sub(r"<(page_type|page_name|module_name)>.*?</\1>", "", block_text, flags=re.DOTALL).strip()
    return page_type, page_name, module_name, clean_text

# --- Main Logic ---
if uploaded_file and canvas_domain and course_id and token:
    pages = extract_canvas_pages(uploaded_file)
    module_cache = {}

    st.subheader("Detected Pages")
    for i, block in enumerate(pages):
        page_type, page_title, module_name, raw = parse_page_block(block)
        html_body = process_html_content(raw)

        st.markdown(f"### {i+1}. {page_title} ({page_type}) in {module_name}")
        st.code(html_body, language="html")

        if st.button(f"Send '{page_title}' to Canvas", key=i):
            mid = get_or_create_module(module_name, canvas_domain, course_id, token, module_cache)
            if not mid:
                continue

            if page_type.lower() == "assignment":
                aid = create_assignment(canvas_domain, course_id, page_title, html_body, token)
                if aid:
                    st.success(f"✅ Assignment '{page_title}' created successfully.")

            elif page_type.lower() == "quiz":
                qid = create_quiz(canvas_domain, course_id, page_title, html_body, token)
                if qid:
                    st.success(f"✅ Quiz '{page_title}' created successfully.")

            elif page_type.lower() == "discussion":
                did = create_discussion(canvas_domain, course_id, page_title, html_body, token)
                if did:
                    st.success(f"✅ Discussion '{page_title}' created successfully.")

            else:
                page_url = create_page(canvas_domain, course_id, page_title, html_body, token)
                if not page_url:
                    continue

                success = add_to_module(canvas_domain, course_id, mid, page_url, page_title, token)
                if success:
                    st.success(f"✅ {page_type} '{page_title}' added to module '{module_name}'")
                else:
                    st.error(f"Failed to add page '{page_title}' to module '{module_name}'")
