# ─────────────────────────────────────────────────────────────
# Refactor date: 2025-11-12
# 📘 OES GenAI App: Umich Feedback Bot
# Maintained by: Imaad Fakier (Senior GenAI Developer)
# Purpose:
# Secure Streamlit micro-app for generating CAI-aligned elaborative feedback
# for Yes/No quiz questions in the Umich course 'Justice and Equity in Technology Policy'.
# This version retains *every* GPT call, regex, and function exactly as authored,
# but is formatted and sectioned according to OES GenAI Streamlit standards.
# ─────────────────────────────────────────────────────────────

# ==============================================================
# ⚙️ 1. Configuration & Environment Setup
# ==============================================================


import os
import io
from typing import List, Tuple
import streamlit as st
from dotenv import load_dotenv
from openai import OpenAI
import re


# ---- hotfix block ----
def _block_streamlit_proxies():
    for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy"]:
        if k in os.environ:
            del os.environ[k]


_block_streamlit_proxies()

# Load environment and set page config
load_dotenv()

st.set_page_config(
    page_title="Umich Feedback Bot",
    page_icon="🎓",
    layout="centered",
    initial_sidebar_state="expanded",
)

ACCENT = "#F28C28"
MAX_LEN = 8000
MAX_PREVIEW_CHARS = 80_000


# ==============================================================
# 🔐 2. API Key Validation
# ==============================================================


API_KEY = os.getenv("OPENAI_API_KEY", "")
if not API_KEY:
    st.error("⚠️ Missing OPENAI_API_KEY. Please add it to your .env file.")
client = OpenAI(api_key=API_KEY) if API_KEY else None


# ==============================================================
# 🧠 3. System Prompt (Scaffold)
# ==============================================================


SYSTEM_SCAFFOLD = """
You are an instructional coach generating **CAI-aligned elaborative feedback** for *Yes/No* quiz questions in the course **Justice and Equity in Technology Policy**.

Your purpose is to model *how strong answers think* — never to judge correctness.

Each feedback block must:
1. Begin with **"Based on your answer, your response should..."** as the *first sentence* of the first paragraph.
2. Begin the second paragraph with **"Your response should also..."** (do NOT repeat “Based on your answer” again).
3. Write exactly **two paragraphs**, each with 3-5 sentences (no more).

Ensure both anchor phrases appear **verbatim** and at the **start** of their respective paragraphs.

Each response should:
- Maintain an **academic, summative, and inclusive tone** aligned with CAI's elaborative feedback model.
- Follow CAI's four-part logic (positive opening, concept explanation, reflective questioning, actionable next steps).
- Avoid evaluative or corrective language (e.g., “correct,” “incorrect,” “right,” “wrong”).
- Integrate key course themes whenever relevant:
  - equity-by-design
  - inclusive governance
  - community co-creation
  - sociotechnical systems
  - stakeholder inclusion and justice frameworks

Style and phrasing guidelines:
- Avoid redundant filler such as “It is important/essential/crucial to recognize” or “Your response should reflect.”
- Vary the opening verbs across feedback blocks to maintain a natural rhythm (use alternatives such as **reflect, examine, analyze, explore, evaluate, acknowledge, synthesize, interrogate**).
- Keep transitions smooth and cohesive between ideas.
- Maintain clarity and concision; avoid bullet points unless absolutely necessary for readability.

Goal:
Produce feedback that reads like a thoughtful academic coach guiding reflection and deep reasoning about justice, equity, and policy in technology — not like a grader.
""".strip()


# ==============================================================
# 📄 3. DOCX Extraction & Parsing Utilities
# ==============================================================


# Lazy import of python-docx
def try_load_docx():
    try:
        import docx  # type: ignore

        return docx
    except Exception:
        return None


DOCX_MOD = try_load_docx()


def read_docx_bytes(file_bytes: bytes) -> Tuple[str, List[str]]:
    """Return full text and heading list from DOCX."""
    if not DOCX_MOD:
        return "", []
    document = DOCX_MOD.Document(io.BytesIO(file_bytes))
    headings, all_lines = [], []
    for para in document.paragraphs:
        text = para.text.strip()
        if not text:
            continue
        all_lines.append(text)
        style_name = getattr(para.style, "name", "") or ""
        if style_name.lower().startswith("heading"):
            headings.append(text)
    return "\n".join(all_lines), headings


# ==============================================================
# 🧩 4. GPT-Powered CLD Parsing Functions
# ==============================================================


def gpt_group_modules(raw_headings: List[str], raw_text: str) -> str:
    """Use GPT to group headings into Module-based ToC."""
    if not client:
        return ""
    system_prompt = """
You are a precise academic text parser that structures extracted headings from a Course Learning Design (CLD) document.

Your goal is to produce a *clean, hierarchical table of contents* of all modules and their subtopics, like this:

Module One: How Do Values Shape Technology
- Social Values
- Political Priorities
- Impacts of Values, Biases, and Assumptions That Shape Design

Module Two: Technology and Equity
- Traditional Goals and Values
- Hiding Bias and Inequities in Language
- Hidden Assumptions and Embedded Inequalities in Technology Design and Development

Guidelines:
- Preserve document order.
- Only include *Modules and their subtopics* (omit admin pages like “Welcome!”, “Course Support”, “Resources”, “Files for Download”).
- Use concise phrasing — keep topic titles as they appear.
- Always start each new section with “Module X:” in title case.
- Do NOT invent topics or rename modules; only clean/organize existing ones.
- Output plain text (no Markdown or numbering beyond module numbering).
""".strip()
    user_prompt = f"""
Extracted Headings:
{chr(10).join(raw_headings)}

Full Text (for context, truncated):
{raw_text[:MAX_PREVIEW_CHARS]}
""".strip()
    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=900,
    )
    return response.choices[0].message.content.strip()


def gpt_extract_section(section_names, full_text: str) -> str:
    """Extract named section (e.g., 'Course Objectives', 'Assignment Instructions') from CLD text."""
    if not client or not full_text.strip():
        return ""

    if isinstance(section_names, str):
        section_names = [section_names]

    section_list_str = ", ".join(section_names)
    system_prompt = f"""
You are a structured document parser reading a Course Learning Design (CLD) document.

Task:
Extract the section corresponding to one of the following names:
{section_list_str}

Rules:
- Return only the exact text content belonging to that section.
- Preserve bullet points, numbering, and formatting.
- Do NOT include unrelated content or section headers.
- If none of these sections exist, respond with the word: "NOT_FOUND"
- Do NOT infer or invent content.
- If multiple candidates exist, pick the one that most closely matches the name.
- Return plain text only (no markdown formatting unless in original).
""".strip()

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.0,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": full_text[:MAX_PREVIEW_CHARS]},
        ],
        max_tokens=700,
    )

    content = response.choices[0].message.content.strip()
    if content.upper().startswith("NOT_FOUND") or len(content) < 20:
        return ""
    return content


def extract_assignment_instructions(full_text: str) -> str:
    """Extract assignment instructions from CLD text by scanning for known anchor patterns."""
    text = re.sub(r"\s+", " ", full_text)
    anchors = [
        r"<page_title>Discussion Prompt</page_title>",
        r"To sum up Module",
        r"Complete the following assessment",
        r"This assignment asks you to",
        r"Reflection Prompt",
        r"Peer Review Assignment",
    ]
    pattern = "(" + "|".join(anchors) + ")"
    match = re.search(pattern, text, flags=re.IGNORECASE)
    if not match:
        return ""
    start = match.start()
    end_match = re.search(
        r"(<page_title>|Module [A-Z][a-z]+|</canvas_page>|End of Document)",
        text[start + 1 :],
        flags=re.IGNORECASE,
    )
    end = start + end_match.start() if end_match else len(text)
    section = text[start:end].strip()
    section = re.sub(r"</?[^>]+>", "", section)
    return section.strip()


# ==============================================================
# 🧮 5. Feedback Generation Logic
# ==============================================================


def _build_context_block(
    course_objectives: str, assignment_block: str, topics_list: str
) -> str:
    return f"""
COURSE OBJECTIVES:
{course_objectives.strip()}

ASSIGNMENT INSTRUCTIONS & OBJECTIVES:
{assignment_block.strip()}

COURSE TOPICS (TABLE OF CONTENTS):
{topics_list.strip()}
""".strip()


def _build_user_prompt(single_question: str, course_context_block: str) -> str:
    return f"""
{course_context_block}

QUIZ QUESTION:
{single_question.strip()}
""".strip()


def generate_feedback_for_one(
    question_text: str, course_context_block: str, model: str = "gpt-4o-mini"
) -> str:
    messages = [
        {"role": "system", "content": SYSTEM_SCAFFOLD},
        {
            "role": "user",
            "content": _build_user_prompt(question_text, course_context_block),
        },
    ]
    completion = client.chat.completions.create(
        model=model,
        messages=messages,
        temperature=0.4,
        max_tokens=600,
    )
    return completion.choices[0].message.content.strip()


def generate_bulk_feedback(
    course_objectives: str,
    assignment_instructions_and_objectives: str,
    topics_list: str,
    quiz_questions: List[str],
    model: str = "gpt-4o-mini",
) -> List[str]:
    context_block = _build_context_block(
        course_objectives, assignment_instructions_and_objectives, topics_list
    )
    outputs = []
    for q in quiz_questions:
        if not q.strip():
            outputs.append("")
            continue
        feedback = generate_feedback_for_one(q, context_block, model=model)
        outputs.append(feedback)
    return outputs


# ==============================================================
# 🧩 6. Utility Helpers
# ==============================================================


def lines_to_questions(text: str) -> List[str]:
    """
    Parse CLD 'Quiz Questions' pasted as one long, taggy line.
    - Removes <Feedback> blurbs
    - Extracts <question>...</question> blocks (or falls back if no tags)
    - Keeps only the actual human prompt (e.g., 'Did you thoughtfully...')
    - Works even when there are no newlines in the paste
    """
    import unicodedata

    # 1) Normalize & strip hidden chars
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"[\u200B-\u200F\uFEFF]", "", text)

    # 2) Remove <Feedback> paragraphs (unclosed; inline)
    # Remove EVERY <Feedback> up to the next <question>, </question>, </quiz> or end
    text = re.sub(
        r"(?is)<\s*feedback\s*>.*?(?=<\s*question\b|</\s*question\s*>|</\s*quiz\s*>|$)",
        "",
        text,
    )

    # 3) Extract <question>...</question> blocks (case-insensitive)
    blocks = re.findall(r"(?is)<\s*question\b[^>]*>(.*?)</\s*question\s*>", text)

    # Fallback: if there are no explicit <question> blocks, run on whole text
    if not blocks:
        blocks = [text]

    extracted: List[str] = []

    for raw in blocks:
        # Strip all tags (keep plain text)
        clean = re.sub(r"(?is)</?[^>]+>", " ", raw)
        clean = re.sub(r"\s+", " ", clean).strip()

        if not clean:
            continue

        # Cut anything after 'Options:' (we only want the prompt)
        clean = re.split(r"(?is)\bOptions\s*:", clean, maxsplit=1)[0].strip()

        # Heuristic 1: look for the canonical “Did you thoughtfully …” opener
        m = re.search(r"(?is)(did you thoughtfully.*?)(?:$)", clean)
        if m:
            q = m.group(1).strip()
            # If there is a trailing sentence beginning with 'Identify/Explain/Describe/Support' etc, keep it.
            # This captures patterns like "... prompt: <follow-up sentence>"
            # Already included by the greedy '.*?' above, but we also cut any trailing rubric fragments:
            q = re.sub(r"\s*(A:\s*Yes|B:\s*No)\s*$", "", q, flags=re.IGNORECASE).strip()
            extracted.append(q)
            continue

        # Heuristic 2: grab the first sentence ending in '?' as the question
        m2 = re.search(r"([^?]{5,}\?)", clean)
        if m2:
            extracted.append(m2.group(1).strip())
            continue

        # Heuristic 3: look for '<something> prompt:' and keep that line+continuation until a rubric keyword
        m3 = re.search(
            r"(?is)([A-Z].{0,120}prompt:\s*.*?)(?:A:\s*Yes|B:\s*No|$)", clean
        )
        if m3:
            extracted.append(re.sub(r"\s+", " ", m3.group(1)).strip())
            continue

        # Last resort: if nothing matched, keep a short, readable slice (but only if it looks like natural text)
        snippet = re.sub(r"\s+", " ", clean).strip()
        if len(snippet) >= 20 and re.search(r"[A-Za-z]", snippet):
            extracted.append(snippet)

    # Final tidy: de-dup, drop junky short items
    deduped = []
    seen = set()
    for q in extracted:
        q = q.strip(" .")
        if len(q) < 15:
            continue
        key = q.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(q)

    return deduped


def over_limit(s: str) -> bool:
    return len(s) > MAX_LEN


# ==============================================================
# 🧱 7. Streamlit UI Rendering
# ==============================================================


# ── Header ────────────────────────────────
st.markdown(
    f"""
    <div style="text-align:center">
      <h1 style="margin-bottom:0">🎓 Umich Feedback Bot</h1>
      <p style="color:#475569;margin-top:6px">
        Step-by-step workflow: Upload CLD → Extract Topics → Generate Feedback
      </p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.expander("📘 How this works (Quick Start Guide)", expanded=False):
    st.markdown(
        """
    **Step 1:** Upload your Course Learning Design (CLD) file and click **Generate Structured Topics**  
    → This automatically builds a Table of Contents for your course.

    **Step 2:** Click **Populate from CLD** to auto-fill Course Objectives, then paste each assignment under *Assignment Inputs*.

    **Step 3:** Press **Generate feedback for all assignments**  
    → The bot will create CAI-aligned elaborative feedback for each assignment section.

    *(Each step includes hover tips and short explanations below the buttons for clarity.)*
    """
    )

# ──────────────────────────── STEP 1 ────────────────────────────
st.header("🪜 Step 1: Upload CLD to Extract Topics")
with st.expander("Why this step matters", expanded=False):
    st.markdown(
        """
This step extracts module titles and page topics directly from the Course Learning Design (CLD) document.  
The generated structured list will automatically populate the “List of Topics” field in Step 2.
"""
    )

uploaded = st.file_uploader("Upload CLD .docx", type=["docx"])

if uploaded and DOCX_MOD:
    file_bytes = uploaded.read()
    full_text, raw_headings = read_docx_bytes(file_bytes)

    generate_toc = st.button(
        "Generate Structured Topics",
        use_container_width=True,
        help="Click to automatically organize extracted CLD headings into modules and subtopics.",
    )
    st.caption("Creates a structured Table of Contents from your uploaded CLD.")

    if generate_toc:
        with st.spinner("Structuring topics into modules…"):
            structured_output = gpt_group_modules(raw_headings, full_text)
        if structured_output:
            st.session_state["topics_toc"] = structured_output
            st.success("✅ Topics structured successfully and loaded into Step 2!")
            st.text_area(
                "Structured Topics (editable):", value=structured_output, height=350
            )
        else:
            st.error(
                "❌ No structured topics generated. Try again or check CLD formatting."
            )

# ──────────────────────────── STEP 2 ────────────────────────────
st.header("🪜 Step 2: Generate Feedback")

# ===============================================================
# 🧩 COURSE-LEVEL OBJECTIVES  (unchanged logic)
# ===============================================================
if st.session_state.get("apply_course_objectives", False):
    st.session_state["course_objectives"] = st.session_state.get(
        "pending_course_objectives", ""
    )
    st.session_state["apply_course_objectives"] = False

course_objectives = st.text_area(
    label="Course-level objectives",
    key="course_objectives",
    height=140,
    placeholder="Paste all course-level objectives from the CLD…",
)

if uploaded and st.button(
    "📄 Populate from CLD (auto-detect)",
    key="extract_obj",
    use_container_width=True,
    help="Automatically detect and extract the Course-Level Objectives from your CLD.",
):
    st.caption(
        "Scans the CLD to locate and insert all course-level objectives automatically."
    )
    with st.spinner("Extracting Course Objectives…"):
        extracted_obj = gpt_extract_section(
            [
                "Course-Level Objectives",
                "Learning Objectives",
                "Course Objectives",
                "Learning Goals",
            ],
            full_text,
        )
    if extracted_obj:
        st.session_state["course_objectives_temp"] = extracted_obj
        st.session_state["show_course_preview"] = True
        st.success("✅ Extracted text ready below. Click 'Apply' to insert.")
    else:
        st.warning("No Course Objectives section found in CLD.")

if st.session_state.get("show_course_preview", False):
    extracted_obj = st.session_state.get("course_objectives_temp", "")
    st.text_area("Extracted Preview:", value=extracted_obj, height=200)
    if st.button("✅ Apply to field", key="apply_obj", use_container_width=True):
        st.session_state["pending_course_objectives"] = extracted_obj
        st.session_state["apply_course_objectives"] = True
        st.session_state["show_course_preview"] = False
        st.rerun()

# ===============================================================
# 🧩 TOPICS (TABLE OF CONTENTS)
# ===============================================================
topics_toc = st.text_area(
    "List of topics (modules + section titles)",
    key="topics_toc",
    height=160,
    placeholder="Paste modules and their section titles (Table of Contents)…",
)

st.divider()

# ===============================================================
# 🧩 MULTI-ASSIGNMENT WORKFLOW (Simplified: Paste Entire Assignment)
# ===============================================================
st.subheader("Assignment Inputs")

num_assignments = st.number_input(
    "How many assignments does this course have?",
    min_value=1,
    max_value=10,
    step=1,
    key="num_assignments",
    help="Specify how many assignments appear in this course. A section will appear for each.",
)

assignments_data = []

for i in range(num_assignments):
    st.markdown(f"### 🧾 Assignment {i + 1}")

    instructions = st.text_area(
        f"Assignment {i + 1}: Instructions & Prompts",
        key=f"assignment_{i}_instructions",
        height=260,
        placeholder="Paste the full assignment instructions (overview, objectives, submission details)…",
    )

    quiz_questions = st.text_area(
        f"Assignment {i + 1}: Quiz Questions (one per line)",
        key=f"assignment_{i}_quiz",
        height=200,
        placeholder="Paste or type each quiz question here, one per line…",
    )

    with st.expander(
        f"🔍 Detected Quiz Questions for Assignment {i+1}", expanded=False
    ):
        st.write(lines_to_questions(quiz_questions))

    assignments_data.append(
        {
            "assignment_instructions": instructions.strip(),
            "quiz_questions": lines_to_questions(quiz_questions),
        }
    )


st.divider()

# ===============================================================
# 🧩 GENERATE FEEDBACK
# ===============================================================
problems = []
if over_limit(course_objectives):
    problems.append("Course-level objectives exceed 8 000 characters.")
if over_limit(topics_toc):
    problems.append("List of topics (ToC) exceeds 8 000 characters.")

for i, data in enumerate(assignments_data, start=1):
    instr = data.get("assignment_instructions", "")
    quiz = data.get("quiz_questions", [])

    if over_limit(instr):
        problems.append(f"Assignment {i} instructions exceed 8 000 characters.")
    if not instr:
        problems.append(f"Assignment {i} instructions are empty.")
    if not quiz:
        problems.append(f"Assignment {i} has no quiz questions entered.")


if problems:
    st.warning(" ".join(problems))

gen_disabled = any(
    [
        not API_KEY,
        not course_objectives.strip(),
        not topics_toc.strip(),
        len(problems) > 0,
    ]
)

if not API_KEY:
    st.info("Add your OPENAI_API_KEY to a .env file to enable generation.")

# Preserve generated feedback across reruns (e.g., when toggling raw view)
if "all_results" not in st.session_state:
    st.session_state["all_results"] = []

generate_all = st.button(
    "🧠 Generate feedback for all assignments",
    use_container_width=True,
    disabled=gen_disabled,
    help="Click to generate CAI-aligned elaborative feedback for every assignment you’ve added below.",
)
st.caption(
    "Uses the uploaded CLD, course objectives, and assignment details to create structured feedback."
)

if generate_all:
    # Single spinner for all assignments
    with st.spinner("Generating feedback for all assignments…"):
        all_results = []

        for idx, data in enumerate(assignments_data, start=1):
            instr = data.get("assignment_instructions", "").strip()
            questions = data.get("quiz_questions", [])

            if not instr or not questions:
                continue

            # Generate feedback per quiz question
            feedback_blocks = generate_bulk_feedback(
                course_objectives=course_objectives,
                assignment_instructions_and_objectives=instr,
                topics_list=topics_toc,
                quiz_questions=questions,
            )

            formatted_output = []
            for i, (q_text, fb) in enumerate(zip(questions, feedback_blocks), start=1):
                fb = fb.strip()
                q_text = q_text.strip()

                # ── Enforce CAI header only if missing ──────────────────────────────
                if not re.search(r"(?i)<b>\s*based on your answer", fb):
                    fb = f"<b>Based on your answer</b>\n\n{fb}"

                # ── Enforce paragraph scaffolding & spacing ─────────────────────────
                fb = re.sub(
                    r"(?is)^\s*(\*\*|__|<b>)?\s*based on your answer\s*(\*\*|__|</b>)?\s*[:\-–—,]*\s*",
                    "<b>Based on your answer</b>\n\n",
                    fb,
                    count=1,
                )

                second_para_variants = [
                    "Aim to",
                    "Be sure to",
                    "Also, consider",
                    "Additionally, make sure you",
                    "Strive to",
                    "Remember to",
                    "It can also help to",
                    "Consider expanding on",
                    "Reflect further on",
                    "In addition, explore",
                    "You might extend your reflection by",
                    "Another useful step is to",
                    "You could further elaborate on",
                    "It's worth emphasizing",
                    "A valuable next step would be to",
                    "Continue by examining",
                    "Next, you might explore",
                    "Take care to include",
                    "It's helpful to articulate",
                    "Lastly, you may highlight",
                ]

                # Force paragraph break before these openers
                pattern = r"(?<!\n)\s*(?=(" + "|".join(second_para_variants) + r")\b)"
                fb = re.sub(pattern, "\n\n", fb)
                fb = re.sub(
                    r"\n[,\s]*(?=(" + "|".join(second_para_variants) + "))", r"\n", fb
                )

                # Clean markdown artifacts
                fb = re.sub(r"(?<!<b>)\*\*(?!</b>)", "", fb)
                fb = re.sub(r"(?<!<b>)__(?!</b>)", "", fb)

                # Replace *extra* "Your response should" with new paragraphs
                matches = list(re.finditer(r"(?is)\byour response should\b", fb))
                if len(matches) > 1:
                    for m in reversed(matches[1:]):
                        start, end = m.span()
                        fb = fb[:start] + "\n\n" + fb[end:]

                # Final tidy spacing
                fb = re.sub(r"\r", "", fb)
                fb = re.sub(r"\n{3,}", "\n\n", fb)
                fb = re.sub(r"[ \t]+\n", "\n", fb)
                fb = re.sub(r"\n[ \t]+", "\n", fb)

                # Capitalize first letter after paragraph breaks
                fb = re.sub(
                    r"(\n\n)([a-z])", lambda m: m.group(1) + m.group(2).upper(), fb
                )

                # ── Remove duplicate "Based on your answer" line if GPT already included one ────────
                fb = re.sub(
                    r"(?is)(<b>\s*based on your answer\s*</b>\s*\n+\s*)(based on your answer[,:\-\s]*)",
                    r"\1",
                    fb,
                )

                fb = re.sub(
                    r"(<b>Based on your answer</b>\s*\n\n)([a-z])",
                    lambda m: m.group(1) + m.group(2).upper(),
                    fb,
                    count=1,
                )

                formatted_output.append(f"{i}. {q_text}\n\n{fb}\n\n---\n")

            all_results.append({"assignment": idx, "results": formatted_output})

        # ✅ Persist across reruns
        st.session_state["all_results"] = all_results

# Render if results already exist (persists when toggling)
if st.session_state.get("all_results"):
    st.success("✅ Feedback generated successfully for all assignments!")
    st.divider()
    st.subheader("Generated Feedback by Assignment")

    for block in st.session_state["all_results"]:
        st.markdown(f"## Assignment {block['assignment']}")
        combined = "\n".join(block["results"])

        st.download_button(
            label=f"📥 Download Assignment {block['assignment']} Feedback (Markdown)",
            data=combined,
            file_name=f"assignment_{block['assignment']}_feedback.md",
            mime="text/markdown",
            use_container_width=True,
        )

        with st.expander(
            f"View Feedback for Assignment {block['assignment']}", expanded=True
        ):
            st.markdown(combined, unsafe_allow_html=True)
            if st.toggle("View raw text", key=f"raw_{block['assignment']}"):
                st.code(combined, language="markdown")

    st.caption(
        "All feedback blocks expanded by default. Use toggles to view raw Markdown."
    )
