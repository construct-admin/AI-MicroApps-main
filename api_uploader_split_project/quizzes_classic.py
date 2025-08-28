import requests
from typing import Dict, Any, Optional

def _headers(token: str) -> Dict[str, str]:
    return {"Authorization": f"Bearer {token}"}

def _url(base: str, path: str) -> str:
    base = base.rstrip("/")
    if base.startswith("http"):
        return f"{base}{path}"
    return f"https://{base}{path}"

def add_quiz(base: str, course_id: str, title: str, description_html: str, token: str) -> Optional[int]:
    url = _url(base, f"/api/v1/courses/{course_id}/quizzes")
    payload = {
        "quiz": {
            "title": title,
            "description": description_html,
            "quiz_type": "assignment",
            "published": True,
            "shuffle_answers": True
        }
    }
    r = requests.post(url, headers=_headers(token), json=payload)
    r.raise_for_status()
    return r.json().get("id")

def add_quiz_question(base: str, course_id: str, quiz_id: int, q: Dict[str, Any], token: str) -> bool:
    """
    q example for multiple choice:
      {
        "question_type": "multiple_choice_question",
        "question_text": "What is 2+2?",
        "answers": [{"text":"3","weight":0},{"text":"4","weight":100}],
        "shuffle": True
      }
    """
    url = _url(base, f"/api/v1/courses/{course_id}/quizzes/{quiz_id}/questions")
    payload = {"question": {
        "question_name": q.get("question_name") or q.get("question_text", "")[:50],
        "question_text": q.get("question_text", ""),
        "question_type": q.get("question_type", "multiple_choice_question"),
        "answers": q.get("answers", [])
    }}
    r = requests.post(url, headers=_headers(token), json=payload)
    try:
        r.raise_for_status()
        return True
    except Exception:
        return False
