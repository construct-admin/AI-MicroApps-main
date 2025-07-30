#!/usr/bin/env python3
import re
import requests
import streamlit as st
from docx import Document

PUBLISHED = True

# --- Helpers ---

def extract_canvas_pages(docx_file):
    doc = Document(docx_file)
    full_text = '\n'.join([para.text for para in doc.paragraphs])
    return re.findall(r"<canvas_page>(.*?)</canvas_page>", full_text, re.DOTALL)

def parse_page_block(block_text):
    def extract_tag(tag, default=""):
        match = re.search(fr"<{tag}>(.*?)</{tag}>", block_text)
        return match.group(1).strip() if match else default

    page_type = extract_tag("page_type", "Pages").capitalize()
    page_name = extract_tag("page_name", "Untitled Page")
    module_name = extract_tag("module_name", "General")
    clean_text = re.sub(r"<(page_type|page_name|module_name)>.*?</\\1>", "", block_text, flags=re.DOTALL).strip()
    return page_type, page_name, module_name, clean_text

def convert_tags_to_html(text):
    text = re.sub(r"<accordion>\s*Title:\s*(.*?)\s*Content:\s*(.*?)</accordion>",
                  r"""<div style=\"background-color: #007BFF; color: white; padding: 12px; border-radius: 8px; margin-bottom: 10px;\">
  <details><summary style=\"font-weight: bold; cursor: pointer;\">\1</summary><div style=\"margin-top: 10px;\">\2</div></details>
</div>""",
                  text, flags=re.DOTALL)
    text = re.sub(r"<callout>\s*(.*?)</callout>",
                  r"""<div style=\"background-color: #fef3c7; border-left: 6px solid #f59e0b; padding: 12px 20px; margin: 20px 0; border-radius: 6px;\"><strong>\1</strong></div>""",
                  text, flags=re.DOTALL)
    text = text.replace("<thick_line />", '<hr style="border: 5px solid #333;" />')
    text = text.replace("<line />", '<hr style="border: 1px solid #ccc;" />')
    text = re.sub(r"<h2>(.*?)</h2>", r"<h2>\1</h2>", text)
    text = re.sub(r"<h3>(.*?)</h3>", r"<h3>\1</h3>", text)
    text = re.sub(r"<paragraph>(.*?)</paragraph>", r"<p>\1</p>", text, flags=re.DOTALL)
    text = re.sub(r"<text>(.*?)</text>", r"<p>\1</p>", text, flags=re.DOTALL)
    text = re.sub(r"<bullets>(.*?)</bullets>",
                  lambda m: "<ul>" + "".join(f"<li>{line.strip()}</li>" for line in m.group(1).split("\n") if line.strip().startswith("-")) + "</ul>",
                  text, flags=re.DOTALL)
    return text

def get_or_create_module(course_id, module_name, token, domain, module_cache):
    if module_name in module_cache:
        return module_cache[module_name]
    url = f"https://{domain}/api/v1/courses/{course_id}/modules"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code != 200:
        return None
    for m in resp.json():
        if m["name"].lower() == module_name.lower():
            module_cache[module_name] = m["id"]
            return m["id"]
    resp = requests.post(url, headers=headers, json={"module": {"name": module_name, "published": PUBLISHED}})
    if resp.status_code in (200,201):
        mid = resp.json().get("id")
        module_cache[module_name] = mid
        return mid
    return None

def create_wiki_page(page_title, html_body, canvas_domain, course_id, headers):
    url = f"https://{canvas_domain}/api/v1/courses/{course_id}/pages"
    payload = {"wiki_page": {"title": page_title, "body": html_body, "published": PUBLISHED}}
    response = requests.post(url, headers=headers, json=payload)
    return response.json() if response.status_code in [200, 201] else None

def add_page_to_module(module_id, page_title, page_url, canvas_domain, course_id, headers):
    url = f"https://{canvas_domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    payload = {"module_item": {"title": page_title, "type": "Page", "page_url": page_url, "published": PUBLISHED}}
    return requests.post(url, headers=headers, json=payload).json()

# --- Streamlit App ---

st.set_page_config(page_title="Canvas Storyboard Importer", layout="centered")
st.title("🧩 Canvas Storyboard Importer")

uploaded = st.file_uploader("Upload storyboard (.docx)", type="docx")
course_id = st.text_input("Canvas Course ID")
domain = st.text_input("Canvas Domain", placeholder="canvas.instructure.com")
token = st.text_input("Canvas API Token", type="password")

if uploaded and course_id and domain and token:
    pages = extract_canvas_pages(uploaded)
    module_cache = {}
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    st.subheader("Detected Pages")
    for i, block in enumerate(pages):
        ptype, pname, mname, raw = parse_page_block(block)
        html = convert_tags_to_html(raw)
        with st.expander(f"{i+1}. {pname} ({ptype} in '{mname}')"):
            st.code(html, language="html")
            if st.button(f"Send '{pname}' to Canvas", key=i):
                mid = get_or_create_module(course_id, mname, token, domain, module_cache)
                if not mid:
                    st.error(f"Failed to get/create module '{mname}'")
                    continue
                page_data = create_wiki_page(pname, html, domain, course_id, headers)
                if not page_data:
                    st.error("Page creation failed.")
                    continue
                page_url = page_data.get("url") or pname.lower().replace(" ", "-")
                modr = add_page_to_module(mid, pname, page_url, domain, course_id, headers)
                if "id" in modr:
                    st.success(f"'{pname}' added to module '{mname}'!")
                else:
                    st.error(f"Created page but failed to add to module: {modr}")
