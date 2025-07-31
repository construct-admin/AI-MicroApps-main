#!/usr/bin/env python3
import os
import re
import requests
import streamlit as st
import time
from docx import Document

try:
    import openai
except ImportError:
    openai = None

PUBLISHED = True
SYSTEM_PROMPT = "Convert the content into Canvas LMS-compatible HTML. Use only inline styles. Avoid using <style>, classes, or JavaScript. Format accordions, callouts, banners using <details>, <div>, <hr>, etc. Avoid advanced CSS."

# ---------------------------
# File Text Extraction
# ---------------------------
def extract_canvas_pages(docx_file):
    doc = Document(docx_file)
    full_text = '\n'.join([para.text for para in doc.paragraphs])
    return re.findall(r"<canvas_page>(.*?)</canvas_page>", full_text, re.DOTALL)

# ---------------------------
# Page Metadata Extraction
# ---------------------------
def parse_page_block(block_text):
    def extract_tag(tag, default=""):
        match = re.search(fr"<{tag}>(.*?)</{tag}>", block_text)
        return match.group(1).strip() if match else default

    page_type = extract_tag("page_type", "Pages").capitalize()
    page_name = extract_tag("page_name", "Untitled Page")
    module_name = extract_tag("module_name", "General")
    clean_text = re.sub(r"<(page_type|page_name|module_name)>.*?</\\1>", "", block_text, flags=re.DOTALL).strip()
    return page_type, page_name, module_name, clean_text

# ---------------------------
# HTML Conversion via AI
# ---------------------------
def generate_html_via_ai(page_title, module_title, content):
    openai_api_key = st.secrets.get("OPENAI_API_KEY")
    if not openai_api_key:
        st.warning("Missing OpenAI key — falling back to manual tag conversion.")
        return None

    headers = {
        "Authorization": f"Bearer {openai_api_key}",
        "Content-Type": "application/json"
    }
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Module: {module_title}\nPage Title: {page_title}\nContent:\n{content}"}
    ]

    response = requests.post("https://api.openai.com/v1/chat/completions", headers=headers, json={
        "model": "gpt-4o",
        "messages": messages,
        "temperature": 0.3
    })

    st.write("OpenAI API Response Status:", response.status_code)

    if response.status_code == 200:
        result = response.json()
        st.write("AI Response:", result)
        return result["choices"][0]["message"]["content"].strip("`")
    st.error(f"OpenAI API error: {response.status_code} - {response.text}")
    return None

# ---------------------------
# Fallback Inline Tag Conversion
# ---------------------------
def convert_tags_to_html(text):
    text = re.sub(r"<accordion>\s*Title: \s*(.*?)\s*Content: \s*(.*?)</accordion>",
        r'''<div style="background-color:#007BFF; color:white; padding:12px; border-radius:8px; margin-bottom:10px;">
<details><summary style="font-weight:bold; cursor:pointer;">\1</summary><div style="margin-top:10px;">\2</div></details>
</div>''', text, flags=re.DOTALL)
    text = re.sub(r"<callout>\s*(.*?)</callout>",
        r'''<div style="background-color:#fef3c7; border-left:6px solid #f59e0b; padding:12px 20px; margin:20px 0; border-radius:6px;"><strong>\1</strong></div>''', text, flags=re.DOTALL)
    text = text.replace("<thick_line />", '<hr style="border:5px solid #333;" />')
    text = text.replace("<line />", '<hr style="border:1px solid #ccc;" />')
    text = re.sub(r"<h2>(.*?)</h2>", r"<h2>\1</h2>", text)
    text = re.sub(r"<h3>(.*?)</h3>", r"<h3>\1</h3>", text)
    text = re.sub(r"<paragraph>(.*?)</paragraph>", r"<p>\1</p>", text, flags=re.DOTALL)
    text = re.sub(r"<text>(.*?)</text>", r"<p>\1</p>", text, flags=re.DOTALL)
    text = re.sub(r"<bullets>(.*?)</bullets>",
        lambda m: "<ul>" + "".join(f"<li>{line.strip()}</li>" for line in m.group(1).split("\n") if line.strip().startswith("-")) + "</ul>",
        text, flags=re.DOTALL)
    return text

# ---------------------------
# Canvas API Handlers
# ---------------------------
def get_or_create_module(course_id, module_name, token, domain, module_cache):
    if module_name in module_cache:
        return module_cache[module_name]
    url = f"https://{domain}/api/v1/courses/{course_id}/modules"
    headers = {"Authorization": f"Bearer {token}"}
    resp = requests.get(url, headers=headers)
    st.write("Fetching existing modules:", resp.status_code)
    st.write(resp.json())

    if resp.status_code == 200:
        for m in resp.json():
            if m["name"].strip().lower() == module_name.strip().lower():
                module_cache[module_name] = m["id"]
                return m["id"]
    time.sleep(1)
    resp = requests.post(url, headers=headers, json={"name": module_name, "published": True})
    st.write("Creating new module:", resp.status_code, resp.json())
    if resp.status_code in (200, 201):
        mid = resp.json().get("id")
        module_cache[module_name] = mid
        return mid
    return None

# New Canvas Item Creation and Module Insertion Logic
def create_canvas_item(course_id, module_id, item_type, title, html_body, token, domain):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if item_type == "Pages":
        page_url = title.lower().replace(" ", "-")
        page_resp = requests.post(
            f"https://{domain}/api/v1/courses/{course_id}/pages",
            headers=headers,
            json={"wiki_page": {"title": title, "body": html_body, "published": PUBLISHED}}
        )
        st.write(f"Created page: {title}", page_resp.status_code, page_resp.json())
        if page_resp.status_code in (200, 201):
            item_resp = requests.post(
                f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items",
                headers=headers,
                json={"module_item": {"title": title, "type": "Page", "page_url": page_url, "published": PUBLISHED}}
            )
            st.write(f"Added page to module: {title}", item_resp.status_code, item_resp.json())
    else:
        st.warning(f"Item type '{item_type}' not implemented yet.")

# ---------------------------
# Streamlit UI Flow
# ---------------------------
def main():
    st.set_page_config(page_title="Canvas Storyboard Importer")
    st.title("Canvas Storyboard Importer")
    canvas_domain = st.text_input("Canvas Domain (e.g., canvas.instructure.com):")
    canvas_token = st.text_input("Canvas API Token:", type="password")
    canvas_course_id = st.text_input("Canvas Course ID:")

    uploaded_file = st.file_uploader("Upload DOCX with <canvas_page> tags", type=["docx"])

    if uploaded_file and canvas_token and canvas_domain and canvas_course_id:
        pages = extract_canvas_pages(uploaded_file)
        if not pages:
            st.warning("No <canvas_page> blocks found.")
            return

        module_cache = {}
        for page in pages:
            page_type, page_title, module_name, raw_content = parse_page_block(page)
            st.markdown(f"### Processing: {page_title} ({page_type}) in Module '{module_name}'")

            html = generate_html_via_ai(page_title, module_name, raw_content)
            if not html:
                html = convert_tags_to_html(raw_content)

            if html:
                mid = get_or_create_module(canvas_course_id, module_name, canvas_token, canvas_domain, module_cache)
                if mid:
                    create_canvas_item(canvas_course_id, mid, page_type, page_title, html, canvas_token, canvas_domain)
                else:
                    st.error(f"Failed to create/find module: {module_name}")
            else:
                st.error("HTML conversion failed.")
    else:
        st.info("Please enter all Canvas details and upload a file to begin.")

if __name__ == "__main__":
    main()
