import re
import requests
import streamlit as st
from docx import Document

# --- Extract multiple canvas_page blocks from docx ---
def extract_canvas_pages(docx_file):
    doc = Document(docx_file)
    full_text = '\n'.join([para.text for para in doc.paragraphs])
    return re.findall(r"<canvas_page>(.*?)</canvas_page>", full_text, re.DOTALL)

# --- Parse tags within a single canvas_page block ---
def parse_page_block(block_text):
    def extract_tag(tag, default=""):
        match = re.search(fr"<{tag}>(.*?)</{tag}>", block_text)
        return match.group(1).strip() if match else default

    page_type = extract_tag("page_type", "Pages").capitalize()
    page_name = extract_tag("page_name", "Untitled Page")
    module_name = extract_tag("module_name", "General")
    clean_text = re.sub(r"<(page_type|page_name|module_name)>.*?</\1>", "", block_text, flags=re.DOTALL).strip()
    return page_type, page_name, module_name, clean_text

# --- Convert custom tags to Canvas-compatible HTML ---
def convert_tags_to_html(text):
    text = re.sub(
        r"<accordion>\s*Title:\s*(.*?)\s*Content:\s*(.*?)</accordion>",
        r"""<div style="background-color: #007BFF; color: white; padding: 12px; border-radius: 8px; margin-bottom: 10px;">
  <details><summary style="font-weight: bold; cursor: pointer;">\1</summary><div style="margin-top: 10px;">\2</div></details>
</div>""", text, flags=re.DOTALL)

    text = re.sub(
        r"<callout>\s*(.*?)</callout>",
        r"""<div style="background-color: #fef3c7; border-left: 6px solid #f59e0b; padding: 12px 20px; margin: 20px 0; border-radius: 6px;"><strong>\1</strong></div>""",
        text, flags=re.DOTALL)

    text = text.replace("<thick_line />", '<hr style="border: 5px solid #333;" />')
    text = text.replace("<line />", '<hr style="border: 1px solid #ccc;" />')
    text = re.sub(r"<h2>(.*?)</h2>", r"<h2>\1</h2>", text)
    text = re.sub(r"<h3>(.*?)</h3>", r"<h3>\1</h3>", text)
    text = re.sub(r"<paragraph>(.*?)</paragraph>", r"<p>\1</p>", text, flags=re.DOTALL)
    text = re.sub(r"<text>(.*?)</text>", r"<p>\1</p>", text, flags=re.DOTALL)
    text = re.sub(
        r"<bullets>(.*?)</bullets>",
        lambda m: "<ul>" + "".join(
            f"<li>{line.strip()}</li>" for line in m.group(1).split("\n") if line.strip().startswith("-")
        ) + "</ul>", text, flags=re.DOTALL)
    return text

# --- Module creation & caching ---
def get_or_create_module(course_id, module_name, token, domain, module_cache):
    if module_name in module_cache:
        return module_cache[module_name]

    url = f"https://{domain}/api/v1/courses/{course_id}/modules"
    headers = {"Authorization": f"Bearer {token}"}
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return None
    modules = response.json()
    for module in modules:
        if module["name"].lower() == module_name.lower():
            module_cache[module_name] = module["id"]
            return module["id"]

    response = requests.post(url, headers=headers, json={"name": module_name})
    if response.status_code in [200, 201]:
        module_id = response.json().get("id")
        module_cache[module_name] = module_id
        return module_id
    return None

# --- Canvas item creation ---
def post_to_canvas(course_id, title, html_body, token, domain, page_type):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base_url = f"https://{domain}/api/v1/courses/{course_id}"

    if page_type == "Pages":
        url = f"{base_url}/pages"
        payload = {"wiki_page": {"title": title, "body": html_body, "published": True}}
        r = requests.post(url, headers=headers, json=payload)
        item_ref = r.json().get("url")  # For module reference
    else:
        if page_type == "Assignments":
            url = f"{base_url}/assignments"
            payload = {"assignment": {"name": title, "description": html_body, "submission_types": ["online_text_entry"], "published": True}}
        elif page_type == "Quizzes":
            url = f"{base_url}/quizzes"
            payload = {"quiz": {"title": title, "description": html_body, "quiz_type": "assignment", "published": True}}
        elif page_type == "Discussions":
            url = f"{base_url}/discussion_topics"
            payload = {"title": title, "message": html_body, "published": True}
        r = requests.post(url, headers=headers, json=payload)
        item_ref = r.json().get("id")
    return r.status_code, item_ref

# --- Add item to Canvas module ---
def add_to_module(course_id, module_id, item_type, item_ref, token, domain):
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"module_item": {"type": item_type, "published": True}}

    if item_type == "Page":
        payload["module_item"]["page_url"] = item_ref
    else:
        payload["module_item"]["content_id"] = item_ref

    return requests.post(url, headers=headers, json=payload)

# --- Streamlit App UI ---
st.set_page_config(page_title="Canvas Storyboard Importer", layout="centered")
st.title("🧩 Canvas Storyboard Importer with Module Caching")

uploaded_file = st.file_uploader("📄 Upload storyboard (.docx)", type=["docx"])
course_id = st.text_input("📘 Canvas Course ID")
canvas_domain = st.text_input("🌍 Canvas Domain", placeholder="canvas.instructure.com")
canvas_token = st.text_input("🔐 Canvas API Token", type="password")

if uploaded_file and course_id and canvas_domain and canvas_token:
    canvas_pages = extract_canvas_pages(uploaded_file)
    st.subheader("🧾 Detected Pages")

    module_cache = {}

    for idx, block in enumerate(canvas_pages):
        page_type, page_name, module_name, raw_text = parse_page_block(block)
        html_content = convert_tags_to_html(raw_text)

        with st.expander(f"{idx+1}. {page_name} ({page_type} in module '{module_name}')", expanded=False):
            st.code(html_content, language="html")

            if st.button(f"🚀 Send to Canvas & Add to Module", key=f"send_{idx}"):
                module_id = get_or_create_module(course_id, module_name, canvas_token, canvas_domain, module_cache)
                if not module_id:
                    st.error("❌ Failed to find or create module.")
                    continue

                status, item_ref = post_to_canvas(course_id, page_name, html_content, canvas_token, canvas_domain, page_type)
                if status in [200, 201]:
                    item_type = "Page" if page_type == "Pages" else page_type[:-1].capitalize()
                    mod_response = add_to_module(course_id, module_id, item_type, item_ref, canvas_token, canvas_domain)
                    if mod_response.status_code in [200, 201]:
                        st.success(f"✅ {page_type} '{page_name}' added to module '{module_name}'!")
                    else:
                        st.error(f"⚠️ Item created but failed to add to module. {mod_response.text}")
                else:
                    st.error(f"❌ Failed to create {page_type}. Status: {status}")
