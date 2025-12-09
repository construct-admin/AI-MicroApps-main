# ------------------------------------------------------------------------------
# File: quizzes_new.py
# Refactor date: 2025-11-13
# Refactored by: Imaad Fakier
#
# Purpose:
#     Provide the full suite of helper utilities for creating **New Quizzes (LTI)**
#     items in Canvas LMS. Supports all required item types for OES GenAI
#     storyboard → Canvas automation:
#
#         • Multiple Choice (single correct)
#         • Multiple Answers (checkbox)
#         • True/False
#         • Short Answer
#         • Essay
#         • Fill-in-Multiple-Blanks
#         • Matching
#         • Numerical
#
# Behaviour:
#     - Absolutely no logic or structure changed from the original version.
#     - API payloads preserved exactly.
#     - All functions remain thin wrappers around Canvas’s New Quizzes REST API.
#     - Dispatcher routing unchanged.
#     - Per-answer feedback and question-level feedback preserved as-is.
#
# External API:
#     Canvas New Quizzes (LTI) API:
#         POST /api/quiz/v1/courses/:course_id/quizzes
#         POST /api/quiz/v1/courses/:course_id/quizzes/:assignment_id/items
#
# Notes:
#     - Canvas New Quizzes uses an LTI tool. These endpoints differ from classic quizzes.
#     - This module is purely backend logic. No Streamlit, no UI, no GPT.
# ------------------------------------------------------------------------------

import uuid
import requests


# ==============================================================================
# Internal Shortcuts
# ==============================================================================


def _BASE(domain: str) -> str:
    """
    Normalize domain into a fully-qualified Canvas base URL.
    Example: "canvas.myuni.edu" → "https://canvas.myuni.edu"
    """
    return f"https://{domain}".rstrip("/")


def _H(token: str) -> dict:
    """
    Authorization headers used for all New Quizzes API calls.
    """
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ==============================================================================
# Quiz Shell (LTI Quiz Creation)
# ==============================================================================


def add_new_quiz(domain, course_id, title, description_html, token, points_possible=1):
    """
    Create a New Quiz (LTI) shell.

    Parameters:
        domain (str): Canvas base domain (without https).
        course_id (str|int): Course identifier.
        title (str): Quiz title.
        description_html (str): HTML-formatted quiz description.
        token (str): Canvas API token.
        points_possible (int): Default total points.

    Returns:
        (assignment_id, error, status_code, raw_response)
        assignment_id may be under data["assignment_id"] or data["id"].

    Behaviour:
        - Completely preserved API call & response handling.
        - Consumers may pass assignment_id to item constructors.
    """
    url = f"{_BASE(domain)}/api/quiz/v1/courses/{course_id}/quizzes"

    payload = {
        "quiz": {
            "title": title,
            "points_possible": max(1, int(points_possible or 1)),
            "instructions": description_html or "",
        }
    }

    r = requests.post(url, headers=_H(token), json=payload, timeout=60)

    try:
        data = r.json()
    except Exception:
        data = None

    if r.status_code in (200, 201):
        aid = (data or {}).get("assignment_id") or (data or {}).get("id")
        return aid, None, r.status_code, (data or r.text)

    return None, (data or r.text), r.status_code, (data or r.text)


# ==============================================================================
# Choice-Based Questions (MCQ, Multi-Select, True/False)
# ==============================================================================


def _mc_scoring_for(answers):
    """
    Determine Canvas scoring algorithm for choice-based items:
        - Single correct answer → "Equivalence"
        - Multiple correct answers → "Set"
    """
    correct = [a["_choice_id"] for a in answers if a.get("is_correct")]
    if len(correct) <= 1:
        return "Equivalence", (correct[0] if correct else None)
    return "Set", correct


def add_choice_item(domain, course_id, assignment_id, q, token, position=1):
    """
    Add a choice-style item.

    Supports:
        - multiple_choice_question
        - multiple_answers_question
        - true_false_question

    Features:
        - Per-answer feedback
        - Question-level feedback
        - Shuffle rules
        - Multi-correct scoring logic (Set vs Equivalence)

    Returns:
        (ok: bool, debug: any)
    """
    url = (
        f"{_BASE(domain)}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    )

    answers = q.get("answers", []) or []
    if not answers:
        return False, "No answers provided."

    # Build choice options + per-answer feedback
    choices = []
    answer_feedback = {}

    for idx, a in enumerate(answers, start=1):
        cid = a.get("_choice_id") or str(uuid.uuid4())
        a["_choice_id"] = cid

        choices.append(
            {"id": cid, "position": idx, "itemBody": f"<p>{a.get('text', '')}</p>"}
        )

        if a.get("feedback"):
            answer_feedback[cid] = a["feedback"]

    scoring_algorithm, scoring_value = _mc_scoring_for(answers)

    entry = {
        "interaction_type_slug": "choice",
        "title": q.get("question_name") or "Question",
        "item_body": q.get("question_text") or "",
        "calculator_type": "none",
        "interaction_data": {"choices": choices},
        "properties": {
            "shuffleRules": {
                "choices": {"toLock": [], "shuffled": bool(q.get("shuffle", False))}
            },
            "varyPointsByAnswer": False,
        },
        "scoring_algorithm": scoring_algorithm,
        "scoring_data": {"value": scoring_value},
    }

    # Question-level feedback
    fb = q.get("feedback") or {}
    qlevel = {k: v for k, v in fb.items() if v}
    if qlevel:
        entry["feedback"] = qlevel

    # Per-answer feedback
    if answer_feedback:
        entry["answer_feedback"] = answer_feedback

    payload = {
        "item": {
            "entry_type": "Item",
            "points_possible": q.get("points_possible", 1),
            "position": position,
            "entry": entry,
        }
    }

    r = requests.post(url, headers=_H(token), json=payload, timeout=60)

    if r.status_code in (200, 201):
        return True, None

    try:
        return False, r.json()
    except Exception:
        return False, r.text


# ==============================================================================
# Short Answer (Exact Match)
# ==============================================================================


def add_short_answer_item(domain, course_id, assignment_id, q, token, position=1):
    """
    Supports: short_answer_question
    Acceptable answers come from q['answers'] = [{'text': '...'}, ...].
    Case-insensitive equivalence.
    """
    url = (
        f"{_BASE(domain)}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    )

    acceptable = [a.get("text", "") for a in (q.get("answers") or []) if a.get("text")]

    entry = {
        "interaction_type_slug": "short_answer",
        "title": q.get("question_name") or "Question",
        "item_body": q.get("question_text") or "",
        "calculator_type": "none",
        "interaction_data": {"caseSensitive": False},
        "scoring_algorithm": "Equivalence",
        "scoring_data": {"values": acceptable},
    }

    # Question-level feedback
    fb = q.get("feedback") or {}
    qlevel = {k: v for k, v in fb.items() if v}
    if qlevel:
        entry["feedback"] = qlevel

    payload = {
        "item": {
            "entry_type": "Item",
            "points_possible": q.get("points_possible", 1),
            "position": position,
            "entry": entry,
        }
    }

    r = requests.post(url, headers=_H(token), json=payload, timeout=60)

    if r.status_code in (200, 201):
        return True, None

    try:
        return False, r.json()
    except Exception:
        return False, r.text


# ==============================================================================
# Essay (Instructor Graded)
# ==============================================================================


def add_essay_item(domain, course_id, assignment_id, q, token, position=1):
    """
    Supports: essay_question
    Essay items contain no scoring algorithm; instructor-graded.
    """
    url = (
        f"{_BASE(domain)}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    )

    entry = {
        "interaction_type_slug": "essay",
        "title": q.get("question_name") or "Question",
        "item_body": q.get("question_text") or "",
        "calculator_type": "none",
    }

    fb = q.get("feedback") or {}
    qlevel = {k: v for k, v in fb.items() if v}
    if qlevel:
        entry["feedback"] = qlevel

    payload = {
        "item": {
            "entry_type": "Item",
            "points_possible": q.get("points_possible", 1),
            "position": position,
            "entry": entry,
        }
    }

    r = requests.post(url, headers=_H(token), json=payload, timeout=60)

    if r.status_code in (200, 201):
        return True, None

    try:
        return False, r.json()
    except Exception:
        return False, r.text


# ==============================================================================
# Fill-In-Multiple-Blanks (FIMB)
# ==============================================================================


def add_fimb_item(domain, course_id, assignment_id, q, token, position=1):
    """
    Supports: fill_in_multiple_blanks_question

    q['question_text']
        must contain placeholders: {{blank_id}}

    q['answers']
        [{'blank_id': 'b1', 'text': '2'},
         {'blank_id': 'b2', 'text': 'water'}, ...]
    """
    url = (
        f"{_BASE(domain)}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    )

    blanks = {}
    for a in q.get("answers") or []:
        b = a.get("blank_id")
        t = a.get("text")
        if b and t:
            blanks.setdefault(b, []).append(t)

    entry = {
        "interaction_type_slug": "fill_in_multiple_blanks",
        "title": q.get("question_name") or "Question",
        "item_body": q.get("question_text") or "",
        "calculator_type": "none",
        "scoring_algorithm": "Equivalence",
        "scoring_data": {"values": blanks},
        "interaction_data": {"blanks": [{"id": k} for k in blanks.keys()]},
    }

    fb = q.get("feedback") or {}
    qlevel = {k: v for k, v in fb.items() if v}
    if qlevel:
        entry["feedback"] = qlevel

    payload = {
        "item": {
            "entry_type": "Item",
            "points_possible": q.get("points_possible", 1),
            "position": position,
            "entry": entry,
        }
    }

    r = requests.post(url, headers=_H(token), json=payload, timeout=60)

    if r.status_code in (200, 201):
        return True, None

    try:
        return False, r.json()
    except Exception:
        return False, r.text


# ==============================================================================
# Matching
# ==============================================================================


def add_matching_item(domain, course_id, assignment_id, q, token, position=1):
    """
    Supports: matching_question

    q['matches']
        [{'prompt': 'H2O', 'match': 'water'}, ...]
    """
    url = (
        f"{_BASE(domain)}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    )

    stems = []
    choices = []
    pairs = []

    for idx, m in enumerate(q.get("matches", []) or [], start=1):
        sid = str(uuid.uuid4())
        cid = str(uuid.uuid4())

        stems.append(
            {"id": sid, "position": idx, "itemBody": f"<p>{m.get('prompt', '')}</p>"}
        )

        choices.append(
            {"id": cid, "position": idx, "itemBody": f"<p>{m.get('match', '')}</p>"}
        )

        pairs.append({"stem_id": sid, "choice_id": cid})

    entry = {
        "interaction_type_slug": "matching",
        "title": q.get("question_name") or "Question",
        "item_body": q.get("question_text") or "",
        "calculator_type": "none",
        "interaction_data": {"stems": stems, "choices": choices},
        "scoring_algorithm": "Equivalence",
        "scoring_data": {"pairs": pairs},
    }

    fb = q.get("feedback") or {}
    qlevel = {k: v for k, v in fb.items() if v}
    if qlevel:
        entry["feedback"] = qlevel

    payload = {
        "item": {
            "entry_type": "Item",
            "points_possible": q.get("points_possible", 1),
            "position": position,
            "entry": entry,
        }
    }

    r = requests.post(url, headers=_H(token), json=payload, timeout=60)

    if r.status_code in (200, 201):
        return True, None

    try:
        return False, r.json()
    except Exception:
        return False, r.text


# ==============================================================================
# Numerical (Exact + Optional Tolerance)
# ==============================================================================


def add_numerical_item(domain, course_id, assignment_id, q, token, position=1):
    """
    Supports: numerical_question

    q['numerical_answer'] = {
         'exact': 12.5,
         'tolerance': 0.5   # optional
    }
    """
    url = (
        f"{_BASE(domain)}/api/quiz/v1/courses/{course_id}/quizzes/{assignment_id}/items"
    )

    na = q.get("numerical_answer") or {}
    exact = na.get("exact")
    tol = na.get("tolerance", 0)

    entry = {
        "interaction_type_slug": "numeric",
        "title": q.get("question_name") or "Question",
        "item_body": q.get("question_text") or "",
        "calculator_type": "none",
        "scoring_algorithm": "Numeric",
        "scoring_data": {"value": exact, "tolerance": tol},
    }

    fb = q.get("feedback") or {}
    qlevel = {k: v for k, v in fb.items() if v}
    if qlevel:
        entry["feedback"] = qlevel

    payload = {
        "item": {
            "entry_type": "Item",
            "points_possible": q.get("points_possible", 1),
            "position": position,
            "entry": entry,
        }
    }

    r = requests.post(url, headers=_H(token), json=payload, timeout=60)

    if r.status_code in (200, 201):
        return True, None

    try:
        return False, r.json()
    except Exception:
        return False, r.text


# ==============================================================================
# Dispatcher — Route to Correct Builder
# ==============================================================================


def add_item_for_question(domain, course_id, assignment_id, q, token, position=1):
    """
    Dispatcher for New Quizzes item creation.

    Expects:
        q['question_type'] ∈ {
            "multiple_choice_question",
            "multiple_answers_question",
            "true_false_question",
            "short_answer_question",
            "essay_question",
            "fill_in_multiple_blanks_question",
            "matching_question",
            "numerical_question"
        }

    Returns:
        (ok: bool, debug: any)
    """
    qtype = (q.get("question_type") or "").strip()

    # Choice-based
    if qtype in (
        "multiple_choice_question",
        "multiple_answers_question",
        "true_false_question",
    ):
        return add_choice_item(
            domain, course_id, assignment_id, q, token, position=position
        )

    # Short answer
    if qtype == "short_answer_question":
        return add_short_answer_item(
            domain, course_id, assignment_id, q, token, position=position
        )

    # Essay
    if qtype == "essay_question":
        return add_essay_item(
            domain, course_id, assignment_id, q, token, position=position
        )

    # Fill-in-multiple-blanks
    if qtype == "fill_in_multiple_blanks_question":
        return add_fimb_item(
            domain, course_id, assignment_id, q, token, position=position
        )

    # Matching
    if qtype == "matching_question":
        return add_matching_item(
            domain, course_id, assignment_id, q, token, position=position
        )

    # Numerical
    if qtype == "numerical_question":
        return add_numerical_item(
            domain, course_id, assignment_id, q, token, position=position
        )

    # Unsupported fallback
    return False, f"Unsupported question_type: {qtype}"
