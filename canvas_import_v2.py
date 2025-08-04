
import streamlit as st
import requests
import re
from docx import Document
import openai

# --- UI Setup ---
st.set_page_config(page_title="Canvas Storyboard Importer with AI", layout="centered")
st.title("🧩 Canvas Storyboard Importer with AI HTML Generator")

canvas_domain = st.text_input("Canvas Base URL (e.g. canvas.instructure.com)")
course_id = st.text_input("Canvas Course ID")
token = st.text_input("Canvas API Token", type="password")

uploaded_file = st.file_uploader("Upload storyboard (.docx)", type="docx")

TEMPLATES = {
    "accordion": '<details><summary style="cursor: pointer; font-weight: bold; background-color:#0077b6; color:white; padding:10px; border-radius:5px;">{title} <small>(click to reveal)</small></summary><div style="padding:10px 20px; margin-top: 10px; background-color:#f2f2f2; color:#333;">{body}</div></details>',
    "callout": '<blockquote><p>{body}</p></blockquote>'
}

def convert_bullets(text):
    lines = text.split("\n")
    out, in_list = [], False
    for line in lines:
        if line.strip().startswith("-"):
            if not in_list:
                out.append("<ul>"); in_list = True
            out.append(f"<li>{line.strip()[1:].strip()}</li>")
        else:
            if in_list:
                out.append("</ul>"); in_list = False
            out.append(line)
    if in_list: out.append("</ul>")
    return '\n'.join(out)

def extract_canvas_pages(docx_file):
    doc = Document(docx_file)
    pages = []
    current = {"module": "General", "title": "", "type": "page", "content": ""}
    for para in doc.paragraphs:
        text = para.text.strip()
        style = para.style.name.lower() if para.style else ""
        if "horizontal" in style or para._element.xpath('.//w:hr'):  # handle horizontal line
            if current["title"]:
                pages.append(current.copy())
                current = {"module": current["module"], "title": "", "type": "page", "content": ""}
            continue
        if "[module]" in text.lower():
            current["module"] = text.replace("[module]", "").strip()
        elif "[lesson]" in text.lower():
            if current["title"]:
                pages.append(current.copy())
                current = {"module": current["module"], "title": "", "type": "page", "content": ""}
            current["title"] = text.replace("[lesson]", "").strip()
        elif "[assignment]" in text.lower(): current["type"] = "assignment"
        elif "[quiz]" in text.lower(): current["type"] = "quiz"
        elif "[discussion]" in text.lower(): current["type"] = "discussion"
        else: current["content"] += text + "\n"
    if current["title"]: pages.append(current.copy())
    return pages

def convert_to_html_with_openai(docx_text, fallback_html):
    try:
        from openai import OpenAI
        client = OpenAI(api_key=st.secrets["OPENAI_API_KEY"])
        prompt = f"""Convert the following storyboard content to HTML. Preserve formatting like headings, bold, italics, and lists. Replace <accordion> and <callout> with HTML.\nContent:\n{docx_text}\nOutput only valid HTML."""
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        st.warning(f"⚠️ OpenAI processing failed, using fallback: {e}")
        return fallback_html

def process_html_content(raw_text):
    fallback_html = re.sub(r"<accordion>\s*Title:\s*(.*?)\s*Content:\s*(.*?)</accordion>",
        lambda m: TEMPLATES["accordion"].format(title=m.group(1).strip(), body=m.group(2).strip()), raw_text, flags=re.DOTALL)
    fallback_html = re.sub(r"<callout>(.*?)</callout>",
        lambda m: TEMPLATES["callout"].format(body=m.group(1)), fallback_html, flags=re.DOTALL)
    fallback_html = convert_bullets(fallback_html)
    return convert_to_html_with_openai(raw_text, fallback_html)

def get_or_create_module(name, domain, course_id, token, cache):
    if name in cache: return cache[name]
    url = f"https://{domain}/api/v1/courses/{course_id}/modules"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.get(url, headers=headers)
    if r.ok:
        for m in r.json():
            if m["name"].strip().lower() == name.lower():
                cache[name] = m["id"]
                return m["id"]
    r = requests.post(url, headers=headers, json={"module": {"name": name}})
    if r.ok:
        cache[name] = r.json()["id"]
        return cache[name]
    return None

def create_page(domain, course_id, title, html, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/pages"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.post(url, headers=headers, json={"wiki_page": {"title": title, "body": html, "published": True}})
    return r.json().get("url") if r.ok else None

def create_assignment(domain, course_id, title, html, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/assignments"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.post(url, headers=headers, json={"assignment": {"name": title, "description": html, "published": True}})
    return r.json().get("id") if r.ok else None

def create_discussion(domain, course_id, title, html, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/discussion_topics"
    headers = {"Authorization": f"Bearer {token}"}
    r = requests.post(url, headers=headers, json={"title": title, "message": html, "published": True})
    return r.json().get("id") if r.ok else None

def add_to_module(domain, course_id, module_id, item_type, item_ref, title, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page": payload["module_item"]["page_url"] = item_ref
    else: payload["module_item"]["content_id"] = item_ref
    return requests.post(url, headers=headers, json=payload).ok

if uploaded_file and canvas_domain and course_id and token:
    pages = extract_canvas_pages(uploaded_file)
    module_cache = {}
    st.subheader("Detected Pages")
    for i, block in enumerate(pages):
        page_type = block["type"]
        title = block["title"]
        module = block["module"]
        html = process_html_content(block["content"])

        st.markdown(f"### {i+1}. {title} ({page_type}) in {module}")
        st.code(html, language="html")
        with st.expander("📄 Preview Render"):
            st.markdown(html, unsafe_allow_html=True)

        if st.button(f"Upload '{title}'", key=i):
            mid = get_or_create_module(module, canvas_domain, course_id, token, module_cache)
            if not mid: continue

            if page_type == "page":
                ref = create_page(canvas_domain, course_id, title, html, token)
                success = add_to_module(canvas_domain, course_id, mid, "Page", ref, title, token)
            elif page_type == "assignment":
                ref = create_assignment(canvas_domain, course_id, title, html, token)
                success = add_to_module(canvas_domain, course_id, mid, "Assignment", ref, title, token)
            elif page_type == "discussion":
                ref = create_discussion(canvas_domain, course_id, title, html, token)
                success = add_to_module(canvas_domain, course_id, mid, "Discussion", ref, title, token)
            else:
                st.warning(f"⚠️ Unsupported type: {page_type}"); continue

            if success:
                st.success(f"✅ '{title}' uploaded to '{module}'!")
            else:
                st.error(f"❌ Failed to upload '{title}'")
