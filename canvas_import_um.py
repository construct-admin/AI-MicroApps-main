import streamlit as st
from docx import Document
from openai import OpenAI
import requests
import re

st.set_page_config(page_title="📄 DOCX → GPT → Canvas (Multi-Page)", layout="centered")
st.title("📄 Upload DOCX → Convert via GPT → Upload to Canvas")

# --- Inputs ---
uploaded_file = st.file_uploader("Upload your storyboard (.docx)", type="docx")
template_file = st.file_uploader("Upload uMich Template Code (.docx)", type="docx")
canvas_domain = st.text_input("Canvas Base URL (e.g. https://canvas.instructure.com)")
course_id = st.text_input("Canvas Course ID")
canvas_token = st.text_input("Canvas API Token", type="password")
openai_api_key = st.text_input("OpenAI API Key", type="password")


def split_into_pages(text):
    return re.split(r"<page_type>.*?</page_type>", text, flags=re.IGNORECASE | re.DOTALL)


def load_docx_text(file):
    doc = Document(file)
    return "\n".join([p.text for p in doc.paragraphs if p.text.strip()])


if uploaded_file and template_file and st.button("🚀 Convert and Upload to Canvas"):
    with st.spinner("📖 Reading storyboard..."):
        storyboard_text = load_docx_text(uploaded_file)
        pages = split_into_pages(storyboard_text)

    with st.spinner("📖 Reading template snippets..."):
        template_text = load_docx_text(template_file)

    st.success(f"📄 Found {len(pages)} page(s) in the storyboard.")

    client = OpenAI(api_key=openai_api_key)

    for i, page_text in enumerate(pages):
        page_type_match = re.search(r"<page_type>(.*?)</page_type>", page_text, re.IGNORECASE)
        page_type = page_type_match.group(1).strip().lower() if page_type_match else "page"

        page_title_match = re.search(r"<page_title>(.*?)</page_title>", page_text, re.IGNORECASE)
        page_title = page_title_match.group(1).strip() if page_title_match else f"Page {i+1}"

        st.markdown(f"### ✨ Processing: {page_title} [{page_type}]")

        with st.spinner(f"🤖 Sending {page_type} to GPT..."):
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
<question> = question block
<multiple_choice> = multiple choice question
* before a choice = correct answer
""" 

            response = client.chat.completions.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": page_text}
                ],
                temperature=0.3
            )

            html_result = response.choices[0].message.content
            st.code(html_result, language='html')

        with st.spinner("📤 Uploading to Canvas..."):
            headers = {"Authorization": f"Bearer {canvas_token}"}

            if page_type == "page":
                url = f"{canvas_domain}/api/v1/courses/{course_id}/pages"
                payload = {
                    "wiki_page": {
                        "title": page_title,
                        "body": html_result,
                        "published": True
                    }
                }

            elif page_type == "discussion":
                url = f"{canvas_domain}/api/v1/courses/{course_id}/discussion_topics"
                payload = {
                    "title": page_title,
                    "message": html_result,
                    "published": True
                }

            elif page_type == "quiz":
                url = f"{canvas_domain}/api/v1/courses/{course_id}/quizzes"
                payload = {
                    "quiz": {
                        "title": page_title,
                        "description": html_result,
                        "published": True,
                        "quiz_type": "assignment"
                    }
                }

            else:
                st.error(f"❌ Unsupported <page_type>: {page_type}")
                continue

            r = requests.post(url, headers=headers, json=payload)

            if r.status_code == 200:
                st.success(f"✅ {page_type.title()} uploaded as '{page_title}'")
            else:
                st.error(f"❌ Failed to upload {page_type}: {r.text}")
