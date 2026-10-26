# ADR004: Chat query tools

## Status

accepted (issue #55, parent #43). Binds #56, #57, #44 and #47.

## Context

The chat model may only read the logs through a fixed set of tools. Questions name hosts,
services and times loosely, and the `events` table has 38.8 M rows, so a tool that scans it
without limits would be slow and a free-form query would be unsafe.

## Decision

1. Tools live in `logdash.chat.tools`. Each is registered with `register_tool` and a JSON
   schema that `check_arguments` enforces, the same schema the model sees. There is no
   free-form SQL.
2. Bad input returns `{"error": {"code", "argument", "message", "hint"?, ...}}` with the codes
   `unknown_tool`, `bad_arguments`, `bad_argument`, `window_too_long` and `unavailable`. Other
   exceptions are bugs and propagate.
3. Time arguments follow `WINDOW_RULE` in `logdash.chat.window`: UTC, relative to the end of
   the data, whole minutes, clipped to the span, exclusive end.
4. Tools read pre-aggregated tables first. A query on `events` always has `hour IN (...)` and a
   window of at most `MAX_EVENTS_MINUTES` = 120 (a payload text search at most 60, #56).
5. All SQL goes through `Toolbox.sql`, which records it. Every new tool adds cases to
   `PUSHDOWN_CASES` in `tests/unit/test_chat_tools.py`.
6. Results pass through `to_json`, and links are built with `explorer_url` from `LINK_KEYS`.

## Consequences

Whole-day counts cost a few milliseconds. Some filter combinations (host with level or
service) only work for windows up to 2 h, and the model gets an error that names the limit.
Adding a tool means adding its pushdown cases.
