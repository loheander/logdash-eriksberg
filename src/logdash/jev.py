"""Small stdlib client for the TypeSafe Jev decision model on OpenRouter.

One call sends a ``state`` (the item, with the shared context alongside) and a set of
questions to ``POST /api/alpha/decisions`` and returns one answer per question, normalised to
``type, value, confidence, probabilities, legend``. For a noul ``value`` is P(yes), for a choice
the chosen option, for a score the expected level (0 = the first criterion).

The client is used live, while someone watches the call, so it retries once at most (429, 5xx,
network errors and timeouts) and fails at once on any other 4xx. The poster is injectable, so
the tests run offline. Based on the ``jev_decide.py`` script of the jev-classify skill.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_MODEL = "typesafe/jev-1.14"
URL = "https://openrouter.ai/api/alpha/decisions"
KEY_ENV = "OPENROUTER_API_KEY"
DEFAULT_TIMEOUT_S = 10.0
ATTEMPTS = 2  # one retry: a person is waiting for the answer
RETRY_PAUSE_S = 0.3
TYPES = ("noul", "choice", "score")


class JevError(Exception):
    """A call that failed for good (4xx, out of retries, or an unexpected answer)."""


class SpecError(JevError):
    """The questions do not match the shapes the Decisions API accepts."""


class Retryable(Exception):  # noqa: N818 - a signal to the retry loop, not an error to report
    """A failure worth one more attempt: 429, 5xx, a network error or a timeout."""


# (key, body, timeout_s) -> response JSON; raises Retryable or JevError
Poster = Callable[[str, dict[str, Any], float], dict[str, Any]]


def api_key(env: Mapping[str, str] | None = None) -> str | None:
    """The OpenRouter key from the environment (None if unset)."""
    value = (os.environ if env is None else env).get(KEY_ENV, "").strip()
    return value or None


def validate_questions(questions: Mapping[str, Any]) -> None:
    """Raise ``SpecError`` unless ``questions`` match the Decisions API shapes."""
    if not questions:
        raise SpecError("questions must be a non-empty mapping keyed by question id")
    for qid, q in questions.items():
        kind = q.get("type")
        if kind not in TYPES:
            raise SpecError(f"{qid}: type must be one of {', '.join(TYPES)}")
        if not q.get("instructions"):
            raise SpecError(f"{qid}: instructions are required")
        criteria = q.get("criteria")
        if kind == "choice" and (not isinstance(criteria, dict) or len(criteria) < 2):
            raise SpecError(f"{qid}: a choice needs criteria for at least two options")
        if kind == "score" and (not isinstance(criteria, list) or len(criteria) < 2):
            raise SpecError(f"{qid}: a score needs criteria as an ordered list, lowest first")
        if (
            kind == "noul"
            and criteria is not None
            and (not isinstance(criteria, dict) or set(criteria) != {"true", "false"})
        ):
            raise SpecError(f"{qid}: noul criteria must have exactly 'true' and 'false'")


def with_examples(question: Mapping[str, Any]) -> dict[str, Any]:
    """The question with its ``examples`` folded into ``instructions`` (the API has no field)."""
    out = {k: v for k, v in question.items() if k != "examples"}
    if question.get("examples"):
        out["instructions"] = {"task": question["instructions"], "examples": question["examples"]}
    return out


def request_body(
    model: str, context: str | None, questions: Mapping[str, Any], item: Any
) -> dict[str, Any]:
    """The request: the item as ``state`` (with the shared context) and the questions."""
    validate_questions(questions)
    state = item if context is None else {"context": context, "item": item}
    return {
        "model": model,
        "state": state,
        "questions": {qid: with_examples(q) for qid, q in questions.items()},
    }


def http_post(
    key: str,
    body: dict[str, Any],
    timeout_s: float,
    *,
    url: str = URL,
    opener: Callable[..., Any] | None = None,
) -> dict[str, Any]:
    """POST one decision request; failures become ``Retryable`` or ``JevError``."""
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with (opener or urllib.request.urlopen)(req, timeout=timeout_s) as resp:
            return dict(json.load(resp))
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:300]
        message = f"HTTP {error.code}: {detail}"
        if error.code == 429 or error.code >= 500:
            raise Retryable(message) from None
        raise JevError(message) from None
    except TimeoutError:
        raise Retryable(f"no answer within {timeout_s:g} s") from None
    except (urllib.error.URLError, OSError, json.JSONDecodeError) as error:
        raise Retryable(str(error)) from None


def post_with_retry(
    poster: Poster,
    key: str,
    body: dict[str, Any],
    timeout_s: float,
    attempts: int = ATTEMPTS,
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """``poster`` with at most ``attempts - 1`` retries of a ``Retryable`` failure."""
    last = ""
    for attempt in range(1, attempts + 1):
        try:
            return poster(key, body, timeout_s)
        except Retryable as error:
            last = str(error)
        if attempt < attempts:
            sleep(RETRY_PAUSE_S)
    raise JevError(f"gave up after {attempts} attempts: {last}")


def normalize_answer(qid: str, answer: Any, expected: str) -> dict[str, Any]:
    """One API answer as ``{type, value, confidence, probabilities, legend}``.

    An answer of another type than the question asked for is an error, never a default value.
    """
    if not isinstance(answer, dict):
        raise JevError(f"{qid}: no answer")
    kind = answer.get("type")
    if kind != expected or kind not in answer:
        raise JevError(f"{qid}: expected a {expected} answer, got {kind!r}")
    out = {"type": kind, "value": answer[kind]}
    for key in ("confidence", "probabilities", "legend"):
        if key in answer:
            out[key] = answer[key]
    return out


@dataclass(frozen=True)
class Decision:
    """The normalised answers of one call, its cost in USD and the model that answered."""

    answers: dict[str, dict[str, Any]]
    cost: float
    model: str


def decide(
    key: str,
    item: Any,
    *,
    questions: Mapping[str, Any],
    context: str | None = None,
    model: str = DEFAULT_MODEL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    poster: Poster = http_post,
) -> Decision:
    """Ask every question about ``item``; raises ``JevError`` if the call or an answer fails."""
    body = request_body(model, context, questions, item)
    resp = post_with_retry(poster, key, body, timeout_s)
    got = resp.get("answers")
    if not isinstance(got, dict):
        raise JevError("the response has no answers")
    answers = {qid: normalize_answer(qid, got.get(qid), q["type"]) for qid, q in questions.items()}
    cost = float((resp.get("usage") or {}).get("cost") or 0.0)
    return Decision(answers, cost, str(resp.get("model") or model))
