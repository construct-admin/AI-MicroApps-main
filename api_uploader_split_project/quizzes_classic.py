# ------------------------------------------------------------------------------
# File: quizzes_classic.py
# Refactor date: 2025-11-13
# Refactored by: Imaad Fakier
#
# Purpose:
#     Provide simple wrappers around the Canvas LMS Classic Quizzes API.
#     These utilities allow the OES GenAI micro-apps to:
#         • Create a classic quiz
#         • Add questions (MCQ, MA, TF, etc.)
#
# Notes:
#     - Classic Quizzes differ from New Quizzes (LTI) and use a different API
#       surface. This module must remain stable for backwards compatibility.
#     - All behaviour is preserved exactly as the original implementation.
#     - No GPT formatting, parsing, or upload logic is touched here.
#
# Dependencies:
#     - requests
#     - Canvas REST API v1
#
# ------------------------------------------------------------------------------

import requests
from typing import Dict, Any, Optional


# ==============================================================================
# Internal Helpers
# ==============================================================================


def _headers(token: str) -> Dict[str, str]:
    """
    Return Canvas-compatible Authorization headers.

    Parameters:
        token (str): Canvas API token.

    Returns:
        dict: {'Authorization': 'Bearer <token>'}
    """
    return {"Authorization": f"Bearer {token}"}


def _url(base: str, path: str) -> str:
    """
    Build a fully qualified Canvas API URL.

    Behaviour:
        - Supports both formats:
            ▸ "https://domain.instructure.com"
            ▸ "domain.instructure.com"
        - Ensures no double slashes.

    Parameters:
        base (str): Canvas domain or full base url.
        path (str): API route (starting with /api/...).

    Returns:
        str: Fully qualified URL.
    """
    base = base.rstrip("/")
    if base.startswith("http"):
        return f"{base}{path}"
    return f"https://{base}{path}"


# ==============================================================================
# Classic Quiz Creation
# ==============================================================================


def add_quiz(
    base: str, course_id: str, title: str, description_html: str, token: str
) -> Optional[int]:
    """
    Create a Classic Quiz in Canvas.

    Parameters:
        base (str): Canvas base domain.
        course_id (str): Canvas course ID.
        title (str): Quiz title.
        description_html (str): Quiz description (HTML expected).
        token (str): Canvas API token.

    Returns:
        int | None:
            Newly created quiz ID, or None if Canvas does not return one.

    Behaviour:
        - Always creates a published quiz.
        - Uses `quiz_type=assignment` (Canvas' required value).
        - Enables answer shuffling by default (same as original).
    """
    url = _url(base, f"/api/v1/courses/{course_id}/quizzes")
    payload = {
        "quiz": {
            "title": title,
            "description": description_html,
            "quiz_type": "assignment",
            "published": True,
            "shuffle_answers": True,
        }
    }

    r = requests.post(url, headers=_headers(token), json=payload)
    r.raise_for_status()
    return r.json().get("id")


# ==============================================================================
# Classic Quiz Question Helpers
# ==============================================================================


def add_quiz_question(
    base: str, course_id: str, quiz_id: int, q: Dict[str, Any], token: str
) -> bool:
    """
    Add a single question to a Classic Quiz.

    Parameters:
        base (str): Canvas base domain.
        course_id (str): Canvas course ID.
        quiz_id (int): ID of quiz created via `add_quiz`.
        q (dict): Question definition. Expected format:

            {
                "question_type": "multiple_choice_question",
                "question_text": "What is 2+2?",
                "answers": [
                    {"text": "3", "weight": 0},
                    {"text": "4", "weight": 100}
                ],
                "shuffle": True
            }

        token (str): Canvas API token.

    Returns:
        bool:
            True if question was added successfully,
            False if Canvas returned an error.

    Behaviour (unchanged from original):
        - Defaults the `question_name` to a truncated version of question text.
        - Sends payload exactly as Canvas Classic Quizzes expects.
        - Errors are swallowed and returned as False (for robustness).
    """
    url = _url(base, f"/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions")

    payload = {
        "question": {
            "question_name": q.get("question_name") or q.get("question_text", "")[:50],
            "question_text": q.get("question_text", ""),
            "question_type": q.get("question_type", "multiple_choice_question"),
            "answers": q.get("answers", []),
        }
    }

    r = requests.post(url, headers=_headers(token), json=payload)

    try:
        r.raise_for_status()
        return True
    except Exception:
        return False
