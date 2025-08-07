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

def create_page(domain, course_id, title, html_body, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/pages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"wiki_page": {"title": title, "body": html_body, "published": True}}
    response = requests.post(url, headers=headers, json=payload)
    return response.json().get("url") if response.status_code in (200, 201) else None

def add_to_module(domain, course_id, module_id, item_type, item_url, title, token):
    headers = {"Authorization": f"Bearer {token}"}
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    payload = {
        "module_item": {
            "type": "Page",
            "page_url": item_url,
            "title": title,
            "published": True
        }
    }
    response = requests.post(url, headers=headers, json=payload)
    return response.status_code in (200, 201)

def load_docx_text(file):
    doc = Document(file)
    return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])

# --- Main Logic ---
if uploaded_file and template_file and canvas_domain and course_id and canvas_token and openai_api_key:
    if "gpt_results" not in st.session_state:
        st.session_state.gpt_results = {}

    pages = extract_canvas_pages(uploaded_file)
    template_text = load_docx_text(template_file)
    doc_obj = Document(uploaded_file)
    client = OpenAI(api_key=openai_api_key)
    module_cache = {}
    last_known_module_name = None

    st.subheader("Detected Pages")
    for i, block in enumerate(pages):
        block = block.strip()
        page_type = extract_tag("page_type", block).lower() or "page"
        page_title = extract_tag("page_title", block) or f"Page {i+1}"
        module_name = extract_tag("module_name", block)

        if not module_name:
            h1_match = re.search(r"<h1>(.*?)</h1>", block, flags=re.IGNORECASE)
            if h1_match:
                module_name = h1_match.group(1).strip()
                st.info(f"📘 Using <h1> as module name: '{module_name}'")

        if not module_name:
            title_match = re.search(r"\d+\.\d+\s+(Module\s+[\w\s]+)", page_title, flags=re.IGNORECASE)
            if title_match:
                module_name = title_match.group(1).strip()
                st.info(f"📘 Extracted module name from title: '{module_name}'")

        if not module_name:
            if last_known_module_name:
                module_name = last_known_module_name
                st.info(f"📘 Using previously found module name: '{module_name}'")
            else:
                module_name = "General"
                st.warning(f"⚠️ No <module_name> tag or Heading 1 found for page {page_title}. Using default 'General'.")
        else:
            last_known_module_name = module_name

        cache_key = f"{page_title}-{i}"
        if cache_key not in st.session_state.gpt_results:
            with st.spinner(f"🤖 Converting page {i+1} [{page_title}] via GPT..."):
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
                    except Exception as e:
                        quiz_json = None
                        st.error(f"❌ Quiz JSON parsing failed: {e}")
                else:
                    html_result = cleaned
                    quiz_json = None

                st.session_state.gpt_results[cache_key] = {
                    "html": html_result,
                    "quiz_json": quiz_json
                }
        else:
            html_result = st.session_state.gpt_results[cache_key]["html"]
            quiz_json = st.session_state.gpt_results[cache_key]["quiz_json"]

        with st.expander(f"📄 {page_title} ({page_type}) | Module: {module_name}", expanded=True):
            st.code(html_result, language="html")

            if bulk_upload or dry_run:
                st.info("Dry run or bulk mode – skipping form button.")
            else:
                with st.form(f"upload_form_{i}"):
                    if st.form_submit_button("🚀 Upload"):
                        mid = get_or_create_module(module_name, canvas_domain, course_id, canvas_token, module_cache)
                        if not mid:
                            st.stop()
                        if page_type == "page":
                            page_url = create_page(canvas_domain, course_id, page_title, html_result, canvas_token)
                            if page_url and add_to_module(canvas_domain, course_id, mid, "Page", page_url, page_title, canvas_token):
                                st.success(f"✅ Page '{page_title}' created and added to '{module_name}'")
