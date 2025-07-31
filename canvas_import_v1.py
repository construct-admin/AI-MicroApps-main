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
    "accordion": '<details style="margin: 10px 0; border: 1px solid #ccc; border-radius: 5px; padding: 10px;"><summary style="font-weight: bold; cursor: pointer;">{title}</summary><div style="margin-top:10px">{body}</div></details>',
    "callout": '<div style="border-left:5px solid #2196F3;background:#f1f9ff;padding:10px 15px;margin:10px 0;">{body}</div>',
    "bullets": lambda items: '<ul>' + ''.join([f'<li>{item.strip()}</li>' for item in items.split("\n") if item.strip()]) + '</ul>'
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

def add_to_module(domain, course_id, module_id, page_url, title, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"module_item": {"title": title, "type": "Page", "page_url": page_url, "published": True}}
    response = requests.post(url, headers=headers, json=payload)
    return response.status_code in (200, 201)

# --- AI HTML Conversion (or Fallback to Regex Template Injection) ---
def process_html_content(raw_text):
    content = raw_text

    # Replace <accordion title=...>...</accordion>
    content = re.sub(r"<accordion title=\"(.*?)\">(.*?)</accordion>",
                     lambda m: TEMPLATES["accordion"].format(title=m.group(1), body=m.group(2)),
                     content, flags=re.DOTALL)

    # Replace <callout>...</callout>
    content = re.sub(r"<callout>(.*?)</callout>",
                     lambda m: TEMPLATES["callout"].format(body=m.group(1)),
                     content, flags=re.DOTALL)

    # Replace <bullets>...</bullets>
    content = re.sub(r"<bullets>(.*?)</bullets>",
                     lambda m: TEMPLATES["bullets"](m.group(1)),
                     content, flags=re.DOTALL)

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

            page_url = create_page(canvas_domain, course_id, page_title, html_body, token)
            if not page_url:
                continue

            success = add_to_module(canvas_domain, course_id, mid, page_url, page_title, token)
            if success:
                st.success(f"✅ {page_type} '{page_title}' added to module '{module_name}'")
            else:
                st.error(f"Failed to add page '{page_title}' to module '{module_name}'")
