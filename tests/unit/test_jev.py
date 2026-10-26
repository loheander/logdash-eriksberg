import io
import json
import urllib.error

import pytest

from logdash import jev

QUESTIONS = {
    "bad": {"type": "noul", "instructions": "Is it bad?", "criteria": {"true": "y", "false": "n"}},
    "kind": {"type": "choice", "instructions": "Kind?", "criteria": {"a": "A", "b": "B"}},
    "level": {
        "type": "score",
        "instructions": "How bad?",
        "criteria": ["low", "high"],
        "examples": [{"item": "x", "level": 1}],
    },
}
ANSWERS = {
    "bad": {"type": "noul", "noul": 0.9},
    "kind": {
        "type": "choice",
        "choice": "a",
        "probabilities": {"a": 0.8, "b": 0.2},
        "confidence": 1,
    },
    "level": {
        "type": "score",
        "score": 0.4,
        "legend": {"0": "low", "1": "high"},
        "probabilities": {"0": 0.6, "1": 0.4},
        "confidence": 0.5,
    },
}
RESPONSE = {"model": "typesafe/jev-1.14-x", "answers": ANSWERS, "usage": {"cost": 2e-5}}


class Fake:
    """A poster that plays back the given outcomes (a dict is returned, an exception raised)."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.bodies = []

    def __call__(self, key, body, timeout_s):
        self.bodies.append((key, body, timeout_s))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def decide(poster, **kw):
    return jev.decide("k", {"x": 1}, questions=QUESTIONS, context="ctx", poster=poster, **kw)


def test_success_normalises_and_folds_examples():
    fake = Fake(RESPONSE)
    out = decide(fake, timeout_s=3)
    assert out.cost == 2e-5 and out.model == "typesafe/jev-1.14-x"
    assert out.answers["bad"] == {"type": "noul", "value": 0.9}
    assert out.answers["kind"]["value"] == "a" and out.answers["kind"]["confidence"] == 1
    assert out.answers["level"]["legend"] == {"0": "low", "1": "high"}
    key, body, timeout = fake.bodies[0]
    assert (key, timeout) == ("k", 3) and body["model"] == jev.DEFAULT_MODEL
    assert body["state"] == {"context": "ctx", "item": {"x": 1}}
    level = body["questions"]["level"]
    assert "examples" not in level and level["instructions"]["examples"][0]["level"] == 1
    assert body["questions"]["bad"]["instructions"] == "Is it bad?"


def test_one_retry_then_success(monkeypatch):
    monkeypatch.setattr(jev.time, "sleep", lambda _s: None)
    fake = Fake(jev.Retryable("HTTP 503"), RESPONSE)
    assert decide(fake).answers["kind"]["value"] == "a" and len(fake.bodies) == 2


def test_gives_up_after_one_retry(monkeypatch):
    monkeypatch.setattr(jev.time, "sleep", lambda _s: None)
    fake = Fake(jev.Retryable("HTTP 503"), jev.Retryable("no answer within 10 s"), RESPONSE)
    with pytest.raises(jev.JevError, match="gave up after 2 attempts: no answer within 10 s"):
        decide(fake)
    assert len(fake.bodies) == 2


def test_client_error_is_not_retried():
    fake = Fake(jev.JevError("HTTP 401: bad key"), RESPONSE)
    with pytest.raises(jev.JevError, match="401"):
        decide(fake)
    assert len(fake.bodies) == 1


@pytest.mark.parametrize(
    ("answers", "message"),
    [
        ({**ANSWERS, "level": {"type": "choice", "choice": "a"}}, "level: expected a score"),
        ({**ANSWERS, "bad": {"type": "noul"}}, "bad: expected a noul"),
        ({k: v for k, v in ANSWERS.items() if k != "kind"}, "kind: no answer"),
        (None, "no answers"),
    ],
)
def test_wrong_or_missing_answers_are_errors(answers, message):
    with pytest.raises(jev.JevError, match=message):
        decide(Fake({"answers": answers}))


def test_cost_and_model_default():
    out = decide(Fake({"answers": ANSWERS}))
    assert out.cost == 0.0 and out.model == jev.DEFAULT_MODEL


@pytest.mark.parametrize(
    ("questions", "message"),
    [
        ({}, "non-empty"),
        ({"q": {"type": "x", "instructions": "i"}}, "type must be"),
        ({"q": {"type": "noul"}}, "instructions"),
        ({"q": {"type": "choice", "instructions": "i", "criteria": {"a": "A"}}}, "two options"),
        ({"q": {"type": "score", "instructions": "i", "criteria": {"a": "A"}}}, "ordered list"),
        ({"q": {"type": "noul", "instructions": "i", "criteria": {"yes": "y"}}}, "'true'"),
    ],
)
def test_spec_validation(questions, message):
    with pytest.raises(jev.SpecError, match=message):
        jev.request_body("m", None, questions, "item")


def test_request_body_without_context():
    body = jev.request_body("m", None, {"q": {"type": "noul", "instructions": "i"}}, "text")
    assert body["state"] == "text"


def test_api_key():
    assert jev.api_key({jev.KEY_ENV: " sk "}) == "sk"
    assert jev.api_key({jev.KEY_ENV: " "}) is None and jev.api_key({}) is None


class Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code):
    return urllib.error.HTTPError(jev.URL, code, "x", {}, io.BytesIO(b"detail"))  # type: ignore[arg-type]


def opener_of(outcome):
    seen = {}

    def opener(req, timeout):
        seen.update(url=req.full_url, auth=req.get_header("Authorization"), timeout=timeout)
        seen["body"] = json.loads(req.data)
        if isinstance(outcome, Exception):
            raise outcome
        return Response(json.dumps(outcome).encode())

    return opener, seen


def test_http_post_success():
    opener, seen = opener_of(RESPONSE)
    assert jev.http_post("sk", {"a": 1}, 4.0, opener=opener) == RESPONSE
    assert seen == {"url": jev.URL, "auth": "Bearer sk", "timeout": 4.0, "body": {"a": 1}}


@pytest.mark.parametrize(
    ("error", "kind", "message"),
    [
        (http_error(400), jev.JevError, "HTTP 400: detail"),
        (http_error(429), jev.Retryable, "HTTP 429"),
        (http_error(502), jev.Retryable, "HTTP 502"),
        (TimeoutError(), jev.Retryable, "no answer within 4 s"),
        (urllib.error.URLError("down"), jev.Retryable, "down"),
    ],
)
def test_http_post_failures(error, kind, message):
    opener, _ = opener_of(error)
    with pytest.raises(kind, match=message):
        jev.http_post("sk", {}, 4.0, opener=opener)


def test_http_post_bad_json():
    def opener(req, timeout):
        return Response(b"not json")

    with pytest.raises(jev.Retryable):
        jev.http_post("sk", {}, 4.0, opener=opener)


def test_real_requests_are_blocked_in_tests():
    with pytest.raises(AssertionError, match="real HTTP request"):
        jev.http_post("sk", {}, 1.0)
    assert jev.api_key() is None
