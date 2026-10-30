import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REVIEW_QUESTIONS = (
    {
        "id": "usefulness",
        "prompt": "How useful was this task for practicing the lesson?",
        "options": (
            ("very_useful", "Very useful"),
            ("useful", "Useful"),
            ("neutral", "Neither useful nor unhelpful"),
            ("not_useful", "Not very useful"),
        ),
    },
    {
        "id": "difficulty",
        "prompt": "How difficult was the task?",
        "options": (
            ("very_easy", "Very easy"),
            ("easy", "Easy"),
            ("moderate", "Moderate"),
            ("difficult", "Difficult"),
            ("very_difficult", "Very difficult"),
        ),
    },
    {
        "id": "confidence",
        "prompt": "How did the task affect your confidence with this topic?",
        "options": (
            ("much_more_confident", "Much more confident"),
            ("slightly_more_confident", "Slightly more confident"),
            ("no_change", "No change"),
            ("less_confident", "Less confident"),
        ),
    },
    {
        "id": "skill",
        "prompt": "Which skill did you practice most?",
        "options": (
            ("problem_solving", "Problem solving"),
            ("coding", "Coding"),
            ("debugging", "Debugging"),
            ("research", "Research"),
            ("design", "Design"),
        ),
    },
    {
        "id": "experience",
        "prompt": "How would you rate your overall experience?",
        "options": (
            ("excellent", "Excellent"),
            ("good", "Good"),
            ("average", "Average"),
            ("poor", "Poor"),
        ),
    },
)


class ReviewProviderError(Exception):
    pass


def generate_review(
    course_title: str,
    lesson_title: str,
    answers: dict[str, str],
    api_key: str,
    model: str,
) -> str:
    answer_labels = {}
    for question in REVIEW_QUESTIONS:
        selected_value = answers[question["id"]]
        answer_labels[question["prompt"]] = next(
            label
            for value, label in question["options"]
            if value == selected_value
        )

    request_body = json.dumps({
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": (
                    "Write a concise, first-person student review using only the "
                    "provided multiple-choice answers and course/task names. Do not "
                    "invent events, outcomes, features, or claims. Reflect negative "
                    "or neutral answers honestly; do not turn them into praise. "
                    "Return only the review text in 2-4 sentences."
                ),
            },
            {
                "role": "user",
                "content": json.dumps({
                    "course": course_title,
                    "task": lesson_title,
                    "answers": answer_labels,
                }),
            },
        ],
        "temperature": 0.4,
        "max_tokens": 180,
    }).encode("utf-8")
    request = Request(
        "https://api.openai.com/v1/chat/completions",
        data=request_body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )

    try:
        with urlopen(request, timeout=30) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise ReviewProviderError(
            f"The AI review provider returned HTTP {error.code}."
        ) from error
    except URLError as error:
        raise ReviewProviderError("The AI review provider could not be reached.") from error
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        raise ReviewProviderError("The AI review provider returned an invalid response.") from error

    try:
        review = result["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, AttributeError, TypeError) as error:
        raise ReviewProviderError("The AI review provider returned no review text.") from error
    if not review:
        raise ReviewProviderError("The AI review provider returned an empty review.")
    return review
