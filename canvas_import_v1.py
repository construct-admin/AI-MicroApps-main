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
    "accordion": lambda title, body: f'<details><summary style="cursor: pointer;">{title}<small> (click to reveal) </small></summary><p style="padding-left: 40px;">{body}</p></details>',
    "callout": lambda body: f'<blockquote><p>{body}</p></blockquote>',
    "bullets": lambda items: '<ul>' + ''.join([f'<li>{item.strip().lstrip("-•")}</li>' for item in items.split("\n") if item.strip()]) + '</ul>'
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

# --- Replace Storyboard Tags with HTML ---
def convert_storyboard_to_html(text):
    pages = re.split(r"<page name=['\"](.*?)['\"] type=['\"](.*?)['\"] module=['\"](.*?)['\"]>", text)
    output = []
    for i in range(1, len(pages), 4):
        title, type_, module, content = pages[i], pages[i+1], pages[i+2], pages[i+3]

        content = re.sub(r"<h2>(.*?)</h2>", r"<h2>\1</h2>", content)
        content = re.sub(r"<paragraph>(.*?)</paragraph>", r"<p>\1</p>", content)
        content = re.sub(r"<line\s*/?>", r"<hr>", content)

        content = re.sub(
            r"<accordion>\s*Title:\s*(?P<title>.*?)\s*Content:\s*(?P<body>.*?)</accordion>",
            lambda m: TEMPLATES["accordion"](m.group("title").strip(), m.group("body").strip()),
            content,
            flags=re.DOTALL
        )

        content = re.sub(r"<callout>(.*?)</callout>", lambda m: TEMPLATES["callout"](m.group(1).strip()), content, flags=re.DOTALL)

        def bullets_repl(match):
            items = match.group(0)
            return TEMPLATES["bullets"](items)

        content = re.sub(r"(?:^|\n)[\-•]\s.*(?:\n[\-•]\s.*)*", bullets_repl, content, flags=re.MULTILINE)

        output.append({"title": title.strip(), "type": type_.strip(), "module": module.strip(), "html": content.strip()})

    return output

# --- Main Processing ---
if uploaded_file:
    doc = Document(uploaded_file)
    raw_text = "\n".join([p.text for p in doc.paragraphs])
    pages = convert_storyboard_to_html(raw_text)

    for p in pages:
        st.markdown(f"### Page: {p['title']} ({p['type']}) in Module: {p['module']}")
        st.code(p['html'], language="html")
