import streamlit as st
import requests
import re
from docx import Document
from bs4 import BeautifulSoup
import os
import zipfile
import uuid
import xml.etree.ElementTree as ET

# --- UI Setup ---
st.set_page_config(page_title="Canvas Storyboard Importer + New Quiz QTI Export", layout="centered")
st.title("🧩 Canvas Storyboard Importer + New Quiz QTI Export")

canvas_domain = st.text_input("Canvas Base URL (e.g. canvas.instructure.com)")
course_id = st.text_input("Canvas Course ID")
token = st.text_input("Canvas API Token", type="password")
uploaded_file = st.file_uploader("Upload storyboard (.docx)", type="docx")

# --- QTI Helper ---
def create_qti_package(quiz_title, questions):
    qti_id = f"quiz_{uuid.uuid4().hex}"
    os.makedirs(qti_id, exist_ok=True)

    # === assessment.xml ===
    ET.register_namespace('', "http://www.imsglobal.org/xsd/ims_qtiasiv1p2")

    questestinterop = ET.Element("questestinterop")
    assessment = ET.SubElement(questestinterop, "assessment", {"title": quiz_title})
    section = ET.SubElement(assessment, "section", {"ident": "root_section"})

    for i, q in enumerate(questions):
        item_id = f"q{i+1}"
        item = ET.SubElement(section, "item", {"ident": item_id, "title": f"Q{i+1}"})

        # Metadata
        metadata = ET.SubElement(item, "itemmetadata")
        ET.SubElement(metadata, "qtimetadata")
        ET.SubElement(ET.SubElement(metadata, "qtimetadatafield"), "fieldlabel").text = "qmd_itemtype"
        ET.SubElement(ET.SubElement(metadata, "qtimetadatafield"), "fieldentry").text = "Multiple Choice"

        # Question text
        presentation = ET.SubElement(item, "presentation")
        material = ET.SubElement(ET.SubElement(presentation, "material"), "mattext", {"texttype": "text/html"})
        material.text = q["text"]

        # Answers
        response_lid = ET.SubElement(presentation, "response_lid", {"ident": "response1", "rcardinality": "Single"})
        render_choice = ET.SubElement(response_lid, "render_choice")

        feedback_refs = []

        for j, ans in enumerate(q["answers"]):
            ans_id = f"A{j+1}"
            resp = ET.SubElement(render_choice, "response_label", {"ident": ans_id})
            mat = ET.SubElement(ET.SubElement(resp, "material"), "mattext")
            mat.text = ans["text"]
            if ans.get("feedback"):
                fb_id = f"feedback_{item_id}_{ans_id}"
                feedback_refs.append((ans_id, fb_id, ans["feedback"]))

        # Scoring logic
        resprocessing = ET.SubElement(item, "resprocessing")
        ET.SubElement(ET.SubElement(resprocessing, "outcomes"), "decvar", {"varname": "SCORE", "vartype": "Decimal", "defaultval": "0"})

        for j, ans in enumerate(q["answers"]):
            ans_id = f"A{j+1}"
            rc = ET.SubElement(resprocessing, "respcondition", {"continue": "Yes"})
            cond = ET.SubElement(rc, "conditionvar")
            ET.SubElement(cond, "varequal", {"respident": "response1"}).text = ans_id
            setvar = ET.SubElement(rc, "setvar", {"action": "Set"})
            setvar.text = "1" if ans["correct"] else "0"

            # Link to feedback
            for ref_ans, fb_id, fb_text in feedback_refs:
                if ref_ans == ans_id:
                    displayfeedback = ET.SubElement(rc, "displayfeedback", {"feedbacktype": "Response", "linkrefid": fb_id})

        # Feedback elements
        for _, fb_id, fb_text in feedback_refs:
            fb = ET.SubElement(item, "itemfeedback", {"ident": fb_id})
            mat = ET.SubElement(ET.SubElement(fb, "material"), "mattext")
            mat.text = fb_text

    # Write XML
    assessment_xml = os.path.join(qti_id, "assessment.xml")
    ET.ElementTree(questestinterop).write(assessment_xml, encoding="utf-8", xml_declaration=True)

    # === imsmanifest.xml ===
    manifest = ET.Element("manifest", {"identifier": qti_id, "xmlns": "http://www.imsglobal.org/xsd/imscp_v1p1"})
    resources = ET.SubElement(manifest, "resources")
    ET.SubElement(resources, "resource", {
        "identifier": "res1",
        "type": "imsqti_xmlv1p1",
        "href": "assessment.xml"
    })
    imsmanifest_xml = os.path.join(qti_id, "imsmanifest.xml")
    ET.ElementTree(manifest).write(imsmanifest_xml, encoding="utf-8", xml_declaration=True)

    # === Zip QTI Package ===
    zip_path = f"{qti_id}.zip"
    with zipfile.ZipFile(zip_path, "w") as zipf:
        zipf.write(assessment_xml, arcname="assessment.xml")
        zipf.write(imsmanifest_xml, arcname="imsmanifest.xml")

    return zip_path


# --- Upload QTI to Canvas ---
def upload_qti_to_canvas(canvas_domain, course_id, token, zip_path):
    url = f"https://{canvas_domain}/api/v1/courses/{course_id}/content_imports"
    headers = {"Authorization": f"Bearer {token}"}
    with open(zip_path, "rb") as f:
        files = {'attachment': (os.path.basename(zip_path), f, 'application/zip')}
        data = {'import_type': 'qti'}
        response = requests.post(url, headers=headers, files=files, data=data)
    return response.ok, response.text

# --- Parser ---
def extract_canvas_pages(docx_file):
    doc = Document(docx_file)
    full_text = "\n".join([p.text for p in doc.paragraphs])
    return re.findall(r"<canvas_page>(.*?)</canvas_page>", full_text, re.DOTALL)

def parse_page_block(block_text):
    def extract_tag(tag):
        match = re.search(fr"<{tag}>(.*?)</{tag}>", block_text)
        return match.group(1).strip() if match else ""
    page_type = extract_tag("page_type").lower()
    page_name = extract_tag("page_name")
    module_name = extract_tag("module_name") or "General"
    content = re.sub(r"<(page_type|page_name|module_name)>.*?</\\1>", "", block_text, flags=re.DOTALL).strip()
    return page_type, page_name, module_name, content

def parse_new_quiz_questions(raw):
    questions = []
    blocks = re.findall(r"<question><multiple choice>(.*?)</question>", raw, re.DOTALL)
    for qblock in blocks:
        lines = [line.strip() for line in qblock.strip().splitlines() if line.strip()]
        question_text = lines[0]
        answers = []
        for line in lines[1:]:
            correct = line.startswith("*")
            feedback_match = re.search(r"<answer feedback=\"(.*?)\">(.+?)</answer>", line)
            if feedback_match:
                feedback, ans = feedback_match.groups()
            else:
                feedback = ""
                ans = re.sub(r"^\*?[A-E][.:]\s*", "", line).strip()
            answers.append({"text": ans, "correct": correct, "feedback": feedback})
        questions.append({"text": question_text, "answers": answers})
    return questions

# --- Main Logic ---
if uploaded_file:
    pages = extract_canvas_pages(uploaded_file)
    all_new_quiz_questions = []
    found_new_quiz = False

    st.subheader("Detected Pages")
    for i, block in enumerate(pages):
        page_type, page_title, module_name, raw = parse_page_block(block)
        st.markdown(f"### {i+1}. {page_title} ({page_type}) in {module_name}")

        if page_type == "new_quiz":
            found_new_quiz = True
            questions = parse_new_quiz_questions(raw)
            all_new_quiz_questions.extend(questions)
            st.success(f"📝 Detected {len(questions)} New Quiz questions.")
        else:
            st.code(raw)

    if found_new_quiz:
        if st.button("📦 Generate and Upload QTI for New Quizzes"):
            zip_path = create_qti_package("New Quiz from Storyboard", all_new_quiz_questions)
            if canvas_domain and course_id and token:
                success, msg = upload_qti_to_canvas(canvas_domain, course_id, token, zip_path)
                if success:
                    st.success("✅ QTI package uploaded to Canvas successfully.")
                else:
                    st.error(f"❌ Failed to upload QTI: {msg}")
            with open(zip_path, "rb") as f:
                st.download_button("⬇️ Download QTI Package", f, file_name=os.path.basename(zip_path))
