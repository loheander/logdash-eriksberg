"""OpenRouter chat-completions client (stdlib only). The only network access."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Any

from logdash.tagging.model import (
    JSON_SCHEMA,
    SYSTEM_PROMPT,
    FatalError,
    InvalidAnswer,
    Tag,
    TransientError,
    parse_tag,
    user_prompt,
)

ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_S = 60.0
MAX_TOKENS = 1000
TRANSIENT_4XX = frozenset({408, 429})


def post_json(
    url: str, payload: Mapping[str, Any], headers: Mapping[str, str], timeout: float
) -> Any:
    """POST JSON and decode the JSON reply; the only network access (tests replace it)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 - fixed https URL
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in TRANSIENT_4XX or exc.code >= 500:
            raise TransientError(f"HTTP {exc.code}") from None
        raise FatalError(f"HTTP {exc.code} {exc.reason}") from None
    except TimeoutError:
        raise TransientError("timeout") from None
    except urllib.error.URLError as exc:
        raise FatalError(f"cannot reach OpenRouter: {exc.reason}") from None
    try:
        return json.loads(raw)
    except ValueError:
        raise InvalidAnswer("reply is not JSON") from None


class OpenRouterModel:
    """Tags one template per request using a strict JSON schema."""

    def __init__(self, api_key: str, model_id: str, timeout: float = TIMEOUT_S) -> None:
        self._api_key = api_key
        self.model_id = model_id
        self.timeout = timeout

    def payload(self, template: str) -> dict[str, Any]:
        # No temperature: no Haiku 5.5 endpoint supports it, so require_parameters would
        # leave no provider (ADR001).
        return {
            "model": self.model_id,
            "max_tokens": MAX_TOKENS,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt(template)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "template_tags", "strict": True, "schema": JSON_SCHEMA},
            },
            "provider": {"require_parameters": True},
        }

    def tag(self, template: str) -> Tag:
        body = post_json(
            ENDPOINT,
            self.payload(template),
            {"Authorization": f"Bearer {self._api_key}", "X-Title": "logdash"},
            self.timeout,
        )
        try:
            choice = body["choices"][0]
            finish = choice.get("finish_reason")
            content = choice["message"]["content"]
        except (KeyError, IndexError, TypeError, AttributeError):
            raise InvalidAnswer("no content in reply") from None
        if finish == "length":
            raise InvalidAnswer("answer cut off")
        if content is None:
            raise InvalidAnswer("no content in reply")
        return parse_tag(content)
