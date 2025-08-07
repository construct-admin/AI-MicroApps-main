# canvas_importer_ai.py

import streamlit as st
import openai
import requests
import re
from google.oauth2 import service_account
from googleapiclient.discovery import build
import json

# --- CONFIG ---
TEMPLATES = {
    "overview": '''<div class="canvasPageCon">
        <div class="bluePageHeader">&nbsp;</div>
        <div class="pageBody">
            <div class="header">
                <h2>Overview</h2>
            </div>
            <div class="divisionLineYellow">
                <!-- Content Here -->
            </div>
        </div>
    </div>''',

    "reading": '''<div class="canvasPageCon">
        <div class="bluePageHeader">&nbsp;</div>
        <div class="pageBody">
            <div class="header">
                <h2>Title</h2>
            </div>
            <p><!-- Content Here --></p>
        </div>
    </div>''',

    "activity": '''<div class="canvasPageCon">
        <div class="bluePageHeader">&nbsp;</div>
        <div class="canvasPageCon">
            <div class="pageBody">
                <div class="header">
                    <h2>Activity</h2>
                </div>
                <p><!-- Content Here --></p>
            </div>
        </div>
    </div>''',

    "assignment": '''<div class="canvasPageCon">
        <div class="bluePageHeader">&nbsp;</div>
        <div class="pageBody">
            <div class="header">
                <h2>Overview</h2>
            </div>
            <p><span>To complete this assignment, you will <!-- context --></span></p>
            <div class="header">
                <h2>Objectives</h2>
            </div>
            <ul>
                <li>Objective 1</li>
            </ul>
            <div class="header">
                <h2>Instructions</h2>
            </div>
            <ol>
                <li>Respond to prompts</li>
                <li>Submit via quiz</li>
            </ol>
            <h3>Question Prompts</h3>
            <ul>
                <li>Prompt 1</li>
            </ul>
        </div>
    </div>''',

    "discussion": '''<div class="canvasPageCon">
        <div class="bluePageHeader">&nbsp;</div>
        <p><!-- Discussion content here --></p>
        <ul>
            <li><!-- Prompt 1 --></li>
        </ul>
        <p><strong>Respond to at least two peers.</strong></p>
    </div>''',

    "video": '''<div class="canvasPageCon">
        <div class="bluePageHeader">&nbsp;</div>
        <div class="pageBody">
            <h2>Video Title</h2>
            <p><!-- Intro --></p>
            <div class="videoCon">
                <p>ADD VIDEO HERE</p>
            </div>
        </div>
    </div>''',

    "accordion": '''<details><summary style="cursor:pointer; font-weight:bold; background-color:#0077b6; color:white; padding:10px; border-radius:5px;">{title} <small>(click to reveal)</small></summary><div style="padding:10px 20px; margin-top:10px; background-color:#f2f2f2; color:#333;">{body}</div></details>''',

    "quote": '''<blockquote><p>{body}</p></blockquote>'''
}

SCOPES = ["https://www.googleapis.com/auth/drive.readonly", "https://www.googleapis.com/auth/documents.readonly"]

# --- UI ---
st.set_page_config(page_title="Canvas Importer with AI & Google Docs", layout="centered")
st.title("📥 Canvas Storyboard Importer (Google Drive Folder + AI)")

folder_url = st.text_input("Google Drive Folder URL")
canvas_domain = st.text_input("Canvas Base URL (e.g. canvas.instructure.com)")
course_id = st.text_input("Canvas Course ID")
token = st.text_input("Canvas API Token", type="password")
creds_json = st.file_uploader("Upload Google Service Account JSON", type="json")

def extract_folder_id(folder_url):
    match = re.search(r"/folders/([a-zA-Z0-9_-]+)", folder_url)
    return match.group(1) if match else None

def list_google_docs_in_folder(folder_id, creds):
    service = build('drive', 'v3', credentials=creds)
    results = service.files().list(
        q=f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.document'",
        fields="files(id, name)").execute()
    return results.get("files", [])

def get_gdoc_text(doc_id, creds):
    service = build('docs', 'v1', credentials=creds)
    doc = service.documents().get(documentId=doc_id).execute()
    content = doc.get("body", {}).get("content", [])
    return "\n".join([
        el.get("paragraph", {}).get("elements", [{}])[0].get("textRun", {}).get("content", "")
        for el in content if "paragraph" in el
    ])

def extract_tagged_blocks(text):
    pages = []
    current = {"module": "General", "title": "", "type": "page", "content": ""}
    for line in text.splitlines():
        line = line.strip()
        if line.lower().startswith("<module_name>"):
            current["module"] = re.sub(r'<.*?>', '', line).strip()
        elif line.lower().startswith("<page_name>"):
            if current["title"]:
                pages.append(current.copy())
            current = {"module": current["module"], "title": re.sub(r'<.*?>', '', line).strip(), "type": "page", "content": ""}
        elif line.lower().startswith("<page_type>"):
            current["type"] = re.sub(r'<.*?>', '', line).strip().lower()
        else:
            current["content"] += line + "\n"
    if current["title"]:
        pages.append(current.copy())
    return pages

def build_html_from_template(page_type, content):
    template = TEMPLATES.get(page_type, TEMPLATES["reading"])
    return template.replace("{body}", content.strip()).replace("{title}", "Details")

def convert_to_html_with_openai(text):
    try:
        client = openai.OpenAI(api_key=st.secrets["OPENAI_API_KEY"])
        prompt = f"""
Convert this storyboard content into styled Canvas LMS HTML. Use the right template structure based on page type and preserve formatting (lists, bold, quotes). Template examples: overview, reading, video, activity. Avoid using <script> tags. Output valid HTML only.

{text}
"""
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        st.warning(f"⚠️ GPT fallback failed: {e}")
        return text

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

def add_to_module(domain, course_id, module_id, item_type, item_ref, title, token):
    url = f"https://{domain}/api/v1/courses/{course_id}/modules/{module_id}/items"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"module_item": {"title": title, "type": item_type, "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = item_ref
    else:
        payload["module_item"]["content_id"] = item_ref
    return requests.post(url, headers=headers, json=payload).ok

# --- MAIN LOGIC ---
if folder_url and canvas_domain and course_id and token and creds_json:
    folder_id = extract_folder_id(folder_url)
    if not folder_id:
        st.error("❌ Invalid folder URL")
    else:
        creds = json.loads(creds_json.read())
        g_creds = service_account.Credentials.from_service_account_info(creds, scopes=SCOPES)
        files = list_google_docs_in_folder(folder_id, g_creds)
        module_cache = {}

        for file in files:
            st.header(f"📄 {file['name']}")
            text = get_gdoc_text(file['id'], g_creds)
            pages = extract_tagged_blocks(text)

            if not pages:
                st.warning(f"⚠️ No pages found in {file['name']}")
                continue

            for i, page in enumerate(pages):
                st.markdown(f"### {i+1}. {page['title']} ({page['type']})")
                html = build_html_from_template(page['type'], page['content'])
                html = convert_to_html_with_openai(html)
                st.code(html, language="html")
                with st.expander("🔍 Preview"):
                    st.markdown(html, unsafe_allow_html=True)

                if st.button(f"Upload '{page['title']}' from {file['name']}", key=f"{file['id']}-{i}"):
                    mid = get_or_create_module(page['module'], canvas_domain, course_id, token, module_cache)
                    if mid:
                        ref = create_page(canvas_domain, course_id, page['title'], html, token)
                        if ref and add_to_module(canvas_domain, course_id, mid, "Page", ref, page['title'], token):
                            st.success(f"✅ Uploaded '{page['title']}' to '{page['module']}'!")
                        else:
                            st.error("❌ Upload failed.")
