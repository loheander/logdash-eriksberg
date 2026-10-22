"""Tag shape, structured-output schema, prompt and answer validation."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

COMPONENTS = ("network", "storage", "auth", "scheduling", "configuration", "hypervisor", "other")
PROMPT_VERSION = "1"
SUMMARY_CHARS = 160
TEMPLATE_CHARS = 2000


@dataclass(frozen=True)
class Tag:
    actionable: bool
    component: str
    noise: bool
    summary: str


class InvalidAnswer(ValueError):
    """The model's answer is unusable; retry, then leave the template untagged."""


class TransientError(Exception):
    """A temporary failure (rate limit, 5xx, timeout); back off and retry."""


class FatalError(Exception):
    """A failure that repeats for every template (bad key, no credit, offline)."""


class TagModel(Protocol):
    def tag(self, template: str) -> Tag: ...


JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actionable": {
            "type": "boolean",
            "description": (
                "describes a real failure an operator should act on, whatever its log level"
            ),
        },
        "component": {"type": "string", "enum": list(COMPONENTS)},
        "noise": {
            "type": "boolean",
            "description": "routine background chatter that is safe to hide during triage",
        },
        "summary": {
            "type": "string",
            "description": "one plain-language sentence, no placeholders",
        },
    },
    "required": ["actionable", "component", "noise", "summary"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You classify log message templates from an OpenStack cloud (nova, neutron, cinder,
keystone, glance, placement, libvirt, haproxy, mariadb, rabbitmq and their agents).
Variable parts are masked: <NUM>, <IP>, <UUID>, <HEX>, <PATH>; <*> is a varying word.
Judge what the message means, not the level it was logged at: many WARNING messages
are harmless and some INFO messages describe failures.

- actionable: true only if the message reports a failure or degraded state an operator
  should look into (errors, timeouts, refused connections, failed operations, lost
  agents). Routine progress, success, periodic status, access logs and debug detail are
  false.
- component, the one area the message is about:
  network (neutron, OVS, iptables, DHCP, L3, ports, load balancers, HTTP connectivity),
  storage (volumes, images, disks, databases, Ceph), auth (keystone, tokens,
  credentials, policies), scheduling (scheduler, placement, resource allocation,
  quotas), configuration (config options, deprecations, missing settings),
  hypervisor (libvirt, QEMU/KVM, instance lifecycle on a compute host, PCI devices),
  other (anything else, including message-queue and generic service chatter).
- noise: true for high-volume background chatter that is safe to hide while triaging,
  such as periodic loop iterations, health checks, access logs and known-harmless
  warnings like "Duplicate iptables rule detected". A message is rarely both actionable
  and noise.
- summary: one short plain-language sentence (at most 120 characters) saying what
  happened, without placeholders.
"""


def user_prompt(template: str) -> str:
    return f"Log template:\n{template[:TEMPLATE_CHARS]}"


def parse_tag(content: str | Mapping[str, Any]) -> Tag:
    """Validate a model answer (JSON text or decoded object); raise InvalidAnswer."""
    if isinstance(content, str):
        try:
            data: Any = json.loads(content)
        except ValueError as exc:
            raise InvalidAnswer(f"not JSON: {exc}") from None
    else:
        data = content
    if not isinstance(data, dict):
        raise InvalidAnswer("answer is not an object")
    missing = [k for k in ("actionable", "component", "noise", "summary") if k not in data]
    if missing:
        raise InvalidAnswer(f"missing keys: {', '.join(missing)}")
    actionable, noise = data["actionable"], data["noise"]
    if not isinstance(actionable, bool) or not isinstance(noise, bool):
        raise InvalidAnswer("actionable and noise must be booleans")
    component = data["component"]
    if not isinstance(component, str) or component.strip().lower() not in COMPONENTS:
        raise InvalidAnswer(f"unknown component {component!r}")
    summary = data["summary"]
    if not isinstance(summary, str):
        raise InvalidAnswer("summary must be a string")
    summary = " ".join(summary.split())
    if not summary:
        raise InvalidAnswer("empty summary")
    if len(summary) > SUMMARY_CHARS:
        summary = summary[: SUMMARY_CHARS - 1] + "…"
    return Tag(actionable, component.strip().lower(), noise, summary)
