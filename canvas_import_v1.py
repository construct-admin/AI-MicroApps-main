import re
import requests
import streamlit as st
from docx import Document
import time

try:
    import openai
except ImportError:
    openai = None

SYSTEM_PROMPT = "Convert the content into Canvas LMS-compatible HTML. Use only inline styles. Avoid using <style>, classes, or JavaScript. Format accordions, callouts, banners using <details>, <div>, <hr>, etc. Avoid advanced CSS."

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

def generate_html_via_ai(page_title, module_title, content):
    openai_api_key = st.secrets.get("OPENAI_API_KEY")
    if not openai_api_key:
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

    if response.status_code == 200:
        return response.json()["choices"][0]["message"]["content"].strip("`")
    return None

def convert_tags_to_html(text):
    text = re.sub(
        r"<accordion>\\s*Title: \\s*(.*?)\\s*Content: \\s*(.*?)</accordion>",
        r'''<div style="background-color:#007BFF; color:white; padding:12px; border-radius:8px; margin-bottom:10px;">
<details><summary style="font-weight:bold; cursor:pointer;">\1</summary><div style="margin-top:10px;">\2</div></details>
</div>''',
        text, flags=re.DOTALL
    )
    text = re.sub(
        r"<callout>\\s*(.*?)</callout>",
        r'''<div style="background-color:#fef3c7; border-left:6px solid #f59e0b; padding:12px 20px; margin:20px 0; border-radius:6px;"><strong>\1</strong></div>''',
        text, flags=re.DOTALL
    )
    text = text.replace("<thick_line />", '<hr style="border:5px solid #333;" />')
    text = text.replace("<line />", '<hr style="border:1px solid #ccc;" />')
    text = re.sub(r"<h2>(.*?)</h2>", r"<h2>\1</h2>", text)
    text = re.sub(r"<h3>(.*?)</h3>", r"<h3>\1</h3>", text)
    text = re.sub(r"<paragraph>(.*?)</paragraph>", r"<p>\1</p>", text, flags=re.DOTALL)
    text = re.sub(r"<text>(.*?)</text>", r"<p>\1</p>", text, flags=re.DOTALL)
    text = re.sub(
        r"<bullets>(.*?)</bullets>",
        lambda m: "<ul>" + "".join(
            f"<li>{line.strip()}</li>" for line in m.group(1).split("\n") if line.strip().startswith("-")
        ) + "</ul>",
        text, flags=re.DOTALL
    )
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
    resp = requests.post(url, headers=headers, json={"name": module_name})
    if resp.status_code in (200,201):
        mid = resp.json().get("id")
        module_cache[module_name] = mid
        return mid
    return None

def post_to_canvas(course_id, title, html_body, token, domain, page_type):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    base = f"https://{domain}/api/v1/courses/{course_id}"
    if page_type == "Pages":
        url = f"{base}/pages"
        payload = {"wiki_page": {"title": title, "body": html_body, "published": True}}
        r = requests.post(url, headers=headers, json=payload)
        item_ref = r.json().get("url")
    else:
        if page_type == "Assignments":
            url = f"{base}/assignments"
            payload = {"assignment": {"name": title, "description": html_body,
                                      "submission_types": ["online_text_entry"], "published": True}}
        elif page_type == "Quizzes":
            url = f"{base}/quizzes"
            payload = {"quiz": {"title": title, "description": html_body,
                                "quiz_type": "assignment", "published": True}}
        else:
            url = f"{base}/discussion_topics"
            payload = {"title": title, "message": html_body, "published": True}
        r = requests.post(url, headers=headers, json=payload)
        item_ref = r.json().get("id")
    return r.status_code, item_ref

def add_to_module(course_id, module_id, item_type, item_ref, token, domain):
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = {"module_item": {"type": item_type, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = item_ref
    else:
        payload["module_item"]["content_id"] = item_ref
    return requests.post(url, headers=headers, json=payload)

# --- Streamlit App ---
st.set_page_config(page_title="Canvas Storyboard Importer", layout="centered")
st.title("🧩 Canvas Storyboard Importer with AI HTML Support")

uploaded = st.file_uploader("Upload storyboard (.docx)", type="docx")
course_id = st.text_input("Canvas Course ID")
domain = st.text_input("Canvas Domain", placeholder="canvas.instructure.com")
token = st.text_input("Canvas API Token", type="password")

if uploaded and course_id and domain and token:
    pages = extract_canvas_pages(uploaded)
    module_cache = {}
    st.subheader("Detected Pages")
    for i, block in enumerate(pages):
        ptype, pname, mname, raw = parse_page_block(block)

        html = generate_html_via_ai(pname, mname, raw)
        if not html:
            html = convert_tags_to_html(raw)

        with st.expander(f"{i+1}. {pname} ({ptype} in '{mname}')"):
            st.code(html, language="html")
            if st.button(f"Send '{pname}' to Canvas", key=i):
                mid = get_or_create_module(course_id, mname, token, domain, module_cache)
                if not mid:
                    st.error(f"Failed to get/create module '{mname}'")
                    continue
                status, ref = post_to_canvas(course_id, pname, html, token, domain, ptype)
                if status in (200,201):
                    if not ref:
                        st.error("No item reference returned from Canvas.")
                        continue
                    time.sleep(1.5)
                    itype = "Page" if ptype=="Pages" else ptype[:-1].capitalize()
                    modr = add_to_module(course_id, mid, itype, ref, token, domain)
                    if modr.status_code in (200,201):
                        st.success(f"{ptype} '{pname}' added to module '{mname}'!")
                    else:
                        st.error(f"Created but failed to add to module: {modr.text}")
                else:
                    st.error(f"Failed to create {ptype}: {status}")