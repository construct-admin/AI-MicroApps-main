import streamlit as st
from docx import Document
import openai
import requests
import re

st.set_page_config(page_title="📄 DOCX (Multi-Page) → GPT → Canvas", layout="centered")
st.title("📄 Upload DOCX → Split into Pages → Convert with GPT → Upload to Canvas")

uploaded_file = st.file_uploader("Upload your storyboard (.docx)", type="docx")
canvas_domain = st.text_input("Canvas Base URL (e.g. https://canvas.instructure.com)")
course_id = st.text_input("Canvas Course ID")
canvas_token = st.text_input("Canvas API Token", type="password")
openai_api_key = st.text_input("OpenAI API Key", type="password")

def split_into_pages(text):
    # Looks for lines like <page_title> or <page_type> to divide the document into logical sections
    return re.split(r"<page_type>.*?</page_type>", text, flags=re.IGNORECASE | re.DOTALL)

if uploaded_file and st.button("🚀 Convert and Upload"):
    with st.spinner("📖 Reading DOCX..."):
        doc = Document(uploaded_file)
        text_content = "\n".join([p.text for p in doc.paragraphs if p.text.strip()])
        pages = split_into_pages(text_content)

    st.success(f"📄 Found {len(pages)} page(s) in the document.")
    openai.api_key = openai_api_key

    for i, page_text in enumerate(pages):
        page_title = f"Page {i+1} from DOCX"
        st.markdown(f"### ✨ Processing: {page_title}")

        with st.spinner(f"Sending page {i+1} to GPT..."):
            response = openai.ChatCompletion.create(
                model="gpt-4o",
                messages=[
                    {"role": "system", "content": """I am going to upload a storyboard document containing tags. You will find these tags in the storyboard and match them to the uMich_template_code document that I have uploaded. When you receive a storyboard, you will find the tags, match them to the template code document and convert the storyboard content to html as well as adapt the relevant code found to the storyboard content. 
<canvas_page> --> indicates the beginning of the canvas page
</canvas_page> --> indicates the end of the canvas page
<page_type> --> indicates the canvas lms page type
<page_title> --> the name of the page
<module_name> --> the name of the module being created 
<quiz_title> --> name of quiz
<question> ->indicates a question
<multiple_choice> indicates a multiple choice question
* indicates the correct answer 
"""},
                    {"role": "user", "content": page_text}
                ],
                temperature=0.3
            )
            html_result = response['choices'][0]['message']['content']
            st.code(html_result, language='html')

        with st.spinner("Uploading to Canvas..."):
            headers = {"Authorization": f"Bearer {canvas_token}"}
            payload = {
                "wiki_page": {
                    "title": page_title,
                    "body": html_result,
                    "published": True
                }
            }
            url = f"{canvas_domain}/api/v1/courses/{course_id}/pages"
            r = requests.post(url, headers=headers, json=payload)

            if r.status_code == 200:
                st.success(f"✅ Page {i+1} uploaded to Canvas as '{page_title}'")
            else:
                st.error(f"❌ Failed to upload Page {i+1}: {r.text}")
