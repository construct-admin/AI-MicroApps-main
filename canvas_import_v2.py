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
st.set_page_config(page_title="Canvas Storyboard Importer with AI", layout="centered")
st.title("🧩 Canvas Storyboard Importer + New Quiz QTI Export")

canvas_domain = st.text_input("Canvas Base URL (e.g. canvas.instructure.com)")
course_id = st.text_input("Canvas Course ID")
token = st.text_input("Canvas API Token", type="password")
uploaded_file = st.file_uploader("Upload storyboard (.docx)", type="docx")

# --- QTI Helper ---
def create_qti_package(quiz_title, questions):
    qti_id = f"quiz_{uuid.uuid4().hex}"
    os.makedirs(qti_id, exist_ok=True)

    # Create assessment.xml
    assessment = ET.Element("questestinterop")
    assessment_section = ET.SubElement(assessment, "assessment", attrib={"title": quiz_title})
    section = ET.SubElement(assessment_section, "section", attrib={"ident": "root_section"})

    for i, q in enumerate(questions):
        item = ET.SubElement(section, "item", attrib={"ident": f"q{i+1}", "title": q['text']})
        presentation = ET.SubElement(item, "presentation")
        material = ET.SubElement(ET.SubElement(presentation, "material"), "mattext", attrib={"texttype": "text/html"})
        material.text = q['text']

        response_lid = ET.SubElement(presentation, "response_lid", attrib={"ident": "response1", "rcardinality": "Single"})
        render_choice = ET.SubElement(response_lid, "render_choice")

        for j, ans in enumerate(q['answers']):
            resp = ET.SubElement(render_choice, "response_label", attrib={"ident": f"A{j+1}"})
            mat = ET.SubElement(ET.SubElement(resp, "material"), "mattext")
            mat.text = ans['text']

        resprocessing = ET.SubElement(item, "resprocessing")
        outcomes = ET.SubElement(resprocessing, "outcomes")
        ET.SubElement(outcomes, "decvar", attrib={"vartype": "Decimal", "defaultval": "0"})

        for j, ans in enumerate(q['answers']):
            respcondition = ET.SubElement(resprocessing, "respcondition", attrib={"continue": "Yes"})
            conditionvar = ET.SubElement(respcondition, "conditionvar")
            ET.SubElement(conditionvar, "varequal", attrib={"respident": "response1"}).text = f"A{j+1}"
            setvar = ET.SubElement(respcondition, "setvar", attrib={"action": "Set"})
            setvar.text = "1" if ans['correct'] else "0"

            if ans.get("feedback"):
                feedback = ET.SubElement(item, "itemfeedback", attrib={"ident": f"feedback{j+1}"})
                ET.SubElement(ET.SubElement(feedback, "material"), "mattext").text = ans["feedback"]

    tree = ET.ElementTree(assessment)
    tree.write(f"{qti_id}/assessment.xml", encoding="utf-8", xml_declaration=True)

    # Create imsmanifest.xml
    manifest = ET.Element("manifest", attrib={"identifier": qti_id, "xmlns": "http://www.imsglobal.org/xsd/imscp_v1p1"})
    resources = ET.SubElement(manifest, "resources")
    ET.SubElement(resources, "resource", attrib={
        "identifier": "res1",
        "type": "imsqti_xmlv1p1",
        "href": "assessment.xml"
    })
    tree = ET.ElementTree(manifest)
    tree.write(f"{qti_id}/imsmanifest.xml", encoding="utf-8", xml_declaration=True)

    # Zip it
    zip_path = f"{qti_id}.zip"
    with zipfile.ZipFile(zip_path, "w") as zipf:
        zipf.write(f"{qti_id}/assessment.xml", arcname="assessment.xml")
        zipf.write(f"{qti_id}/imsmanifest.xml", arcname="imsmanifest.xml")
    return zip_path

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

    if found_new_quiz and st.button("📦 Generate QTI for New Quizzes"):
        zip_path = create_qti_package("New Quiz from Storyboard", all_new_quiz_questions)
        with open(zip_path, "rb") as f:
            st.download_button("⬇️ Download QTI Package", f, file_name=os.path.basename(zip_path))
