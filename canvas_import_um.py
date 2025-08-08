
import streamlit as st
from docx import Document
from openai import OpenAI
import requests
import re
import json

# --- Streamlit Page Config ---
st.set_page_config(page_title="📄 DOCX → GPT → Canvas (All Content Types)", layout="wide")
st.title("📄 DOCX → GPT → Canvas Uploader (Pages, Quizzes, Assignments, Discussions)")

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
def normalize_base(domain):
    return domain.replace("https://", "").replace("http://", "").strip("/")

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

def load_docx_text(file):
    doc = Document(file)
    return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])

def get_or_create_module(module_name, domain, course_id, token, module_cache):
    if module_name in module_cache:
        return module_cache[module_name]
    url = f"https://{normalize_base(domain)}/api/v1/courses/{course_id}/modules"
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
        st.error(f"❌ Failed to create/find module: {module_name} — {resp.text}")
        return None

def create_page(domain, course_id, title, html_body, token):
    url = f"https://{normalize_base(domain)}/api/v1/courses/{course_id}/pages"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"wiki_page": {"title": title[:250], "body": html_body, "published": True}}
    r = requests.post(url, headers=headers, json=payload)
    return r.json().get("url") if r.status_code in (200, 201) else None

def create_assignment(domain, course_id, title, html_body, token, points=10):
    url = f"https://{normalize_base(domain)}/api/v1/courses/{course_id}/assignments"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {
        "assignment": {
            "name": title[:250],
            "description": html_body,
            "points_possible": points,
            "submission_types": ["online_upload"],
            "published": True
        }
    }
    r = requests.post(url, headers=headers, json=payload)
    return r.json().get("id") if r.status_code in (200, 201) else None

def create_discussion(domain, course_id, title, html_body, token):
    url = f"https://{normalize_base(domain)}/api/v1/courses/{course_id}/discussion_topics"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"title": title[:250], "message": html_body, "published": True}
    r = requests.post(url, headers=headers, json=payload)
    return r.json().get("id") if r.status_code in (200, 201) else None

def create_quiz(domain, course_id, title, description, quiz_json, token):
    quiz_url = f"https://{normalize_base(domain)}/api/v1/courses/{course_id}/quizzes"
    headers = {"Authorization": f"Bearer {token}"}
    quiz_payload = {
        "quiz": {
            "title": title[:250],
            "description": description,
            "quiz_type": "assignment",
            "published": True
        }
    }
    quiz_resp = requests.post(quiz_url, headers=headers, json=quiz_payload)
    if quiz_resp.status_code not in (200, 201):
        st.error(f"❌ Quiz creation failed: {quiz_resp.text}")
        return None
    quiz_id = quiz_resp.json().get("id")
    for q in quiz_json.get("questions", []):
        q_url = f"{quiz_url}/{quiz_id}/questions"
        q_payload = {
            "question": {
                "question_name": q.get("question_name"),
                "question_text": q.get("question_text"),
                "question_type": "multiple_choice_question",
                "points_possible": 1,
                "answers": [
                    {"text": a.get("text"), "weight": 100 if a.get("is_correct") else 0}
                    for a in q.get("answers", [])
                ]
            }
        }
        requests.post(q_url, headers=headers, json=q_payload)
    return quiz_id

def add_to_module(domain, course_id, module_id, item_type, item_id_or_url, title, token):
    url = f"https://{normalize_base(domain)}/api/v1/courses/{course_id}/modules/{module_id}/items"
    headers = {"Authorization": f"Bearer {token}"}
    payload = {"module_item": {"type": item_type, "title": title[:250], "published": True}}
    if item_type == "Page":
        payload["module_item"]["page_url"] = item_id_or_url
    else:
        payload["module_item"]["content_id"] = item_id_or_url
    r = requests.post(url, headers=headers, json=payload)
    return r.status_code in (200, 201)

# --- Main ---
if uploaded_file and template_file and canvas_domain and course_id and canvas_token and openai_api_key:
    if "gpt_results" not in st.session_state:
        st.session_state.gpt_results = {}
    pages = extract_canvas_pages(uploaded_file)
    template_text = load_docx_text(template_file)
    client = OpenAI(api_key=openai_api_key)
    module_cache = {}
    last_known_module_name = None

    st.subheader("Detected Pages")
    for i, block in enumerate(pages):
        page_type = extract_tag("page_type", block).lower() or "page"
        page_title = extract_tag("page_title", block) or f"Page {i+1}"
        module_name = extract_tag("module_name", block) or last_known_module_name or "General"
        last_known_module_name = module_name

        cache_key = f"{page_title}-{i}"
        if cache_key not in st.session_state.gpt_results:
            with st.spinner(f"🤖 Processing page {i+1}: {page_title}"):
                system_prompt = f"""You are an expert Canvas HTML generator.
Below is a set of Canvas LMS HTML templates followed by a storyboard page.
TEMPLATES:
{template_text}
Return the page HTML and JSON quiz data if quiz."""
                resp = client.chat.completions.create(
                    model="gpt-4o",
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": block}
                    ],
                    temperature=0.3
                )
                raw = resp.choices[0].message.content.strip()
                cleaned = re.sub(r"```(html|json)?", "", raw, flags=re.IGNORECASE).strip()
                match = re.search(r"({[\s\S]+})$", cleaned)
                if match:
                    html_result = cleaned[:match.start()].strip()
                    try:
                        quiz_json = json.loads(match.group(1))
                    except:
                        quiz_json = None
                else:
                    html_result = cleaned
                    quiz_json = None
                st.session_state.gpt_results[cache_key] = {"html": html_result, "quiz_json": quiz_json}
        else:
            html_result = st.session_state.gpt_results[cache_key]["html"]
            quiz_json = st.session_state.gpt_results[cache_key]["quiz_json"]

    with st.expander(f"📄 {page_title} ({page_type}) | Module: {module_name}", expanded=True):
        st.code(html_result, language="html")

        # Always show the button; disable if Dry Run or missing Canvas creds
        disabled_btn = (dry_run or not have_canvas)
        upload_clicked = st.button(f"🚀 Upload '{page_title}'", key=f"btn_{i}", disabled=disabled_btn)

        if upload_clicked:
            mid = get_or_create_module(module_name, canvas_domain, course_id, canvas_token, module_cache)
            if not mid:
                st.error(f"❌ Could not create/find module '{module_name}'")
                st.stop()

            if page_type == "page":
                url = create_page(canvas_domain, course_id, page_title, html_result, canvas_token)
                ok = url and add_to_module(canvas_domain, course_id, mid, "Page", url, page_title, canvas_token)

            elif page_type == "assignment":
                aid = create_assignment(canvas_domain, course_id, page_title, html_result, canvas_token)
                ok = aid and add_to_module(canvas_domain, course_id, mid, "Assignment", aid, page_title, canvas_token)

            elif page_type == "discussion":
                did = create_discussion(canvas_domain, course_id, page_title, html_result, canvas_token)
                ok = did and add_to_module(canvas_domain, course_id, mid, "Discussion", did, page_title, canvas_token)

            elif page_type == "quiz" and quiz_json:
                qid = create_quiz(canvas_domain, course_id, page_title, html_result, quiz_json, canvas_token)
                ok = qid and add_to_module(canvas_domain, course_id, mid, "Quiz", qid, page_title, canvas_token)
            else:
                ok = False

            st.success(f"✅ Uploaded '{page_title}' to module '{module_name}'") if ok else st.error("❌ Upload failed.")

    disabled_all = (dry_run or not have_canvas)
    if st.button("🚀 Upload ALL", disabled=disabled_all):

        for i, block in enumerate(pages):
            page_type = extract_tag("page_type", block).lower() or "page"
            page_title = extract_tag("page_title", block) or f"Page {i+1}"
            module_name = extract_tag("module_name", block) or "General"
            html_result = st.session_state.gpt_results[f"{page_title}-{i}"]["html"]
            quiz_json = st.session_state.gpt_results[f"{page_title}-{i}"]["quiz_json"]
            mid = get_or_create_module(module_name, canvas_domain, course_id, canvas_token, module_cache)
            if page_type == "page":
                url = create_page(canvas_domain, course_id, page_title, html_result, canvas_token)
                add_to_module(canvas_domain, course_id, mid, "Page", url, page_title, canvas_token)
            elif page_type == "assignment":
                aid = create_assignment(canvas_domain, course_id, page_title, html_result, canvas_token)
                add_to_module(canvas_domain, course_id, mid, "Assignment", aid, page_title, canvas_token)
            elif page_type == "discussion":
                did = create_discussion(canvas_domain, course_id, page_title, html_result, canvas_token)
                add_to_module(canvas_domain, course_id, mid, "Discussion", did, page_title, canvas_token)
            elif page_type == "quiz" and quiz_json:
                qid = create_quiz(canvas_domain, course_id, page_title, html_result, quiz_json, canvas_token)
                add_to_module(canvas_domain, course_id, mid, "Quiz", qid, page_title, canvas_token)
        st.success("✅ All pages uploaded")
