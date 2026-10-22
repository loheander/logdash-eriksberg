"""Tagging package: request shape, answer validation, errors, retries and memo."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from logdash.tagging import openrouter
from logdash.tagging.model import (
    COMPONENTS,
    JSON_SCHEMA,
    SUMMARY_CHARS,
    FatalError,
    InvalidAnswer,
    Tag,
    TransientError,
    parse_tag,
)
from logdash.tagging.openrouter import OpenRouterModel
from logdash.tagging.openrouter import post_json as real_post_json  # before the autouse patch
from logdash.tagging.runner import (
    BACKOFF_S,
    MAX_ATTEMPTS,
    PROGRESS_EVERY,
    load_memo,
    memo_key,
    save_memo,
    tag_templates,
)

GOOD = {"actionable": True, "component": "network", "noise": False, "summary": "A port failed."}
TAG = Tag(True, "network", False, "A port failed.")


class StubModel:
    def __init__(self, answers=None):
        self.answers = {k: list(v) for k, v in (answers or {}).items()}
        self.calls: list[str] = []

    def tag(self, text):
        self.calls.append(text)
        queue = self.answers.get(text)
        if queue:
            answer = queue.pop(0)
            if isinstance(answer, Exception):
                raise answer
            return answer
        return Tag(False, "other", False, f"summary of {text[:20]}")


def test_payload_requests_strict_json_schema():
    p = OpenRouterModel("k", "vendor/model").payload("t")
    assert p["response_format"]["type"] == "json_schema"
    assert p["response_format"]["json_schema"]["strict"] is True
    assert p["response_format"]["json_schema"]["schema"] == JSON_SCHEMA
    assert p["provider"] == {"require_parameters": True}
    assert p["model"] == "vendor/model"
    assert set(p) == {"model", "max_tokens", "messages", "response_format", "provider"}


GOOD_JSON = json.dumps(GOOD)


def reply(content=GOOD_JSON, finish="stop"):
    return {"choices": [{"message": {"content": content}, "finish_reason": finish}]}


def test_tag_parses_reply_content(monkeypatch):
    seen = {}

    def fake(url, payload, headers, timeout):
        seen["headers"] = headers
        return reply()

    monkeypatch.setattr(openrouter, "post_json", fake)
    assert OpenRouterModel("k", "m").tag("t") == TAG
    assert seen["headers"]["Authorization"] == "Bearer k"
    assert "sk-secret" not in repr(OpenRouterModel("sk-secret", "m"))


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"error": {"message": "boom"}},
        {"choices": []},
        reply(content=None),
        reply(finish="length"),
        "not a dict",
    ],
)
def test_reply_without_usable_content_is_invalid(monkeypatch, body):
    monkeypatch.setattr(openrouter, "post_json", lambda *a, **k: body)
    with pytest.raises(InvalidAnswer):
        OpenRouterModel("k", "m").tag("t")


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[1]",
        {"actionable": True, "component": "network", "noise": False},
        {**GOOD, "actionable": "true"},
        {**GOOD, "component": "kernel"},
        {**GOOD, "component": 3},
        {**GOOD, "summary": "   "},
        {**GOOD, "summary": 5},
    ],
)
def test_parse_tag_rejects(content):
    with pytest.raises(InvalidAnswer):
        parse_tag(content)


def test_parse_tag_normalises():
    tag = parse_tag(
        json.dumps({**GOOD, "component": " Network ", "summary": "two\nlines  here", "extra": 1})
    )
    assert tag.component == "network" and tag.summary == "two lines here"
    long = parse_tag({**GOOD, "summary": "x" * 300})
    assert len(long.summary) == SUMMARY_CHARS and long.summary.endswith("…")
    assert parse_tag(GOOD).component in COMPONENTS


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(code):
    return urllib.error.HTTPError("u", code, "reason", {}, io.BytesIO(b""))  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("raised", "expected"),
    [
        (http_error(429), TransientError),
        (http_error(503), TransientError),
        (http_error(401), FatalError),
        (http_error(400), FatalError),
        (http_error(404), FatalError),
        (urllib.error.URLError("dns"), FatalError),
        (TimeoutError(), TransientError),
    ],
)
def test_post_json_maps_errors(monkeypatch, raised, expected):
    def boom(*_a, **_k):
        raise raised

    monkeypatch.setattr(openrouter.urllib.request, "urlopen", boom)
    with pytest.raises(expected) as info:
        real_post_json("https://x", {}, {"Authorization": "Bearer sk-secret"}, 1.0)
    assert "sk-secret" not in str(info.value)


def test_post_json_decodes_body(monkeypatch):
    monkeypatch.setattr(
        openrouter.urllib.request, "urlopen", lambda *a, **k: FakeResponse(b'{"a": 1}')
    )
    assert real_post_json("https://x", {}, {}, 1.0) == {"a": 1}
    monkeypatch.setattr(openrouter.urllib.request, "urlopen", lambda *a, **k: FakeResponse(b"<"))
    with pytest.raises(InvalidAnswer):
        real_post_json("https://x", {}, {}, 1.0)


def run(texts, model, memo=None, **kw):
    memo = {} if memo is None else memo
    sleeps: list[float] = []
    out = tag_templates(
        texts, model, memo, "m", log=lambda _: None, sleep=sleeps.append, workers=1, **kw
    )
    return out, sleeps


def test_invalid_answer_is_retried():
    model = StubModel({"a": [InvalidAnswer("bad"), TAG]})
    out, _ = run(["a"], model)
    assert out.tags == {"a": TAG} and out.calls == 2 and not out.failed


def test_invalid_answer_gives_up_after_max_attempts():
    model = StubModel({"a": [InvalidAnswer("bad")] * 5})
    out, _ = run(["a"], model)
    assert out.calls == MAX_ATTEMPTS and "InvalidAnswer" in out.failed["a"] and not out.tags


def test_transient_backs_off():
    model = StubModel({"a": [TransientError("HTTP 429"), TAG]})
    out, sleeps = run(["a"], model)
    assert out.tags == {"a": TAG} and sleeps == [BACKOFF_S]


def test_fatal_error_stops_calling():
    model = StubModel({"a": [FatalError("HTTP 401 Unauthorized")]})
    out, _ = run(["a", "b", "c"], model)
    assert out.calls == 1 and set(out.failed) == {"a", "b", "c"}
    assert out.aborted == "HTTP 401 Unauthorized" and not out.tags
    assert out.failed["b"].startswith("aborted")


def test_unexpected_exception_is_untagged():
    model = StubModel({"a": [RuntimeError("oops")]})
    out, _ = run(["a"], model)
    assert out.calls == 1 and "RuntimeError" in out.failed["a"] and not out.tags


def test_memo_hits_make_no_calls():
    memo = {memo_key("m", "a"): TAG}
    model = StubModel()
    out, _ = run(["a"], model, memo)
    assert out.calls == 0 and out.tags == {"a": TAG} and model.calls == []
    out, _ = run(["b", "b", "b"], model, memo)
    assert out.calls == 1 and model.calls == ["b"]
    assert memo_key("m", "b") in memo


def test_progress_is_logged():
    logs: list[str] = []
    texts = [f"t{i}" for i in range(PROGRESS_EVERY)]
    tag_templates(texts, StubModel(), {}, "m", log=logs.append, workers=2)
    assert f"tagged {PROGRESS_EVERY}/{PROGRESS_EVERY}" in logs


def test_memo_roundtrip_and_corrupt_file(tmp_path):
    path = tmp_path / "sub" / "memo.parquet"
    assert load_memo(path) == {}
    memo = {memo_key("m", "a"): TAG, memo_key("m", "b"): Tag(False, "other", True, "x")}
    save_memo(path, memo)
    assert load_memo(path) == memo
    path.write_bytes(b"garbage")
    logs: list[str] = []
    assert load_memo(path, logs.append) == {} and len(logs) == 1
