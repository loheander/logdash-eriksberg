# Plan: semantic tags, tagging build stage (story #28), v2

## 1. Story

Story [#28](https://github.com/loheander/logdash-eriksberg/issues/28), parent
[#24](https://github.com/loheander/logdash-eriksberg/issues/24), PR
[#34](https://github.com/loheander/logdash-eriksberg/pull/34). Feature branch
`feature/semantic-template-tags`, story branch `feature/semantic-template-tags-28-tagging-stage`.

**Goal.** A build stage after `templates` tags every template once with a language model
via OpenRouter (actionable, component, known noise, one-line summary) and caches the
tags, so that later stories can show and use them.

**Acceptance criteria** (copied from the story):

- [ ] AC1: `logdash build` writes one row of tags per template to the cache: `actionable` (bool), `component` (network, storage, auth, scheduling, configuration, hypervisor, other), `noise` (bool), `summary` (one line).
- [ ] AC2: The model is asked for structured output (JSON schema / tool call), not free text parsed afterwards. An invalid answer is retried or recorded as untagged, and never crashes the build.
- [ ] AC3: The model ID is a setting (an environment variable with a default), not hard-coded.
- [ ] AC4: `OPENROUTER_API_KEY` is read from the environment or `.env`. Without it the stage is skipped quickly, the build succeeds, and the cache records that tags are absent so that pages can tell.
- [ ] AC5: A rebuild where the template texts are unchanged makes no model calls.
- [ ] AC6: The dashboard never calls the model. Serve reads only the cache.
- [ ] AC7: Tests use a stubbed model (no network). Coverage stays at or above 90 %.

Out of scope: showing or filtering by tags (#29, #30, #31), detectors (#32), judging
tag quality (#33).

## 2. Context

- `src/logdash/build.py:20-24`: `StageResult` (`status` is `"built"` or `"fresh"`).
  Lines 78-136: `run_build`. A stage is rebuilt when it is forced, missing, has a
  changed fingerprint, or when an upstream stage was rebuilt in this run
  (`needs_build`, line 107). The analysis loop is at lines 129-135.
- `src/logdash/build.py:34-54`: `compute_fingerprints`. The data fingerprint is part
  of every stage, and `setting_names` (read with `getattr`, line 50) adds settings.
  Consequence: the stage alone cannot meet AC5, so a memo keyed by template text is
  needed.
- `src/logdash/build.py:149`: `reset_module` wipes the stage directory **before**
  `compute`, so the memo must live outside it.
- `src/logdash/cli.py:77-95`: `cmd_serve`, which calls `run_build(settings)` at line 84
  unless `--no-build` is given. There is no serve test yet.
- `src/logdash/analyses/base.py:20`: `RESERVED_KEYS`. Lines 23-36: the
  `runtime_checkable` `Analysis` protocol (optional attributes such as
  `setting_names` are read with `getattr`, not declared). Line 107: `Registry.ordered`.
- `src/logdash/analyses/__init__.py:7-17`: `BUILTIN`.
- `src/logdash/analyses/templates.py:48`: `KEY = "templates"`. Lines 167-181:
  `templates.parquet` (`template_id, template, count, first_seen, last_seen,
  n_variants`). The texts use `<UUID> <IP> <HEX> <PATH> <NUM>`
  (`src/logdash/ingest/masking.py:13-19`) and Drain's `<*>`.
- `src/logdash/analyses/anomalies.py:394-396`: the pattern for reading upstream stages
  in `compute`: `CacheStore(ctx.out_dir.parent, ctx.settings)`.
- `src/logdash/cache/store.py:53-69`: `write_parquet`, `read_parquet`, `write_json`,
  `read_json`, `path`, `module_dir`. `src/logdash/cache/manifest.py:52-55`:
  `digest_of`.
- `src/logdash/config.py:25-85`: `Settings` (frozen; tests construct it positionally
  as `Settings(data_dir, cache_dir, "1GB", 2)`), `from_env` and `analysis_params`.
- `src/logdash/app/main.py:26` and `:70`, and `src/logdash/app/shell.py:40`: these
  iterate `registry.ordered()` to make pages. `tests/benchmark/test_latency.py:88` does
  the same for layouts.
- `tests/conftest.py`: the session `built` fixture and `build_with`.
  `tests/unit/test_build_pipeline.py:11-32`: the `CountLevels` stub analysis.
- The main checkout has a real `.env`, and `tests/unit/test_build.py:33-45` runs
  `main(["build", …])` with every built-in. Tests must be shielded from the real key.
- OpenRouter, checked on 2026-10-22 with `GET /api/v1/models/anthropic/claude-haiku-5.5/endpoints`:
  - The canonical ID is `anthropic/claude-haiku-5.5`. `claude-haiku-5-5` is not in
    `/api/v1/models`, although the endpoints API resolves it.
  - `structured_outputs` and `response_format` are offered by Anthropic, Google Vertex
    (global/us/europe) and Azure. Amazon Bedrock and "Claude Platform on AWS" lack
    `structured_outputs`, so `require_parameters` excludes them.
  - **No endpoint supports `temperature`**. `max_tokens` and `reasoning` are supported
    everywhere.
  - Price: $0.10 per M prompt tokens and $0.50 per M completion tokens. 281 templates
    cost about $0.02.

## 3. Design

ADR: `adrs/ADR001-language-model-calls-at-build-time.md`, updated in this version
with the serve rule and the parameter rule.

**Stage.** `Tags` (`key = "tags"`, `depends_on = ("templates",)`, `page = False`,
`calls_model = True`) in `src/logdash/analyses/tags.py`, listed in `BUILTIN`.
`compute` runs in one of two modes:

- **No key:** log `skipped (no OPENROUTER_API_KEY)`, write `meta.json` with
  `available: false`, and return. There is no `tags.parquet` and nothing is read.
- **Key:** read `templates.parquet` and load the memo. Tag the unique texts that are
  not in the memo, using 4 worker threads. Save the memo in a `finally`. Write
  `tags.parquet` (one row per template, with nulls where untagged) and `meta.json`.

**Serve never calls the model** (human decision 2, AC6):

- `run_build` gets `allow_model: bool = True`. When it is `False`, an analysis with
  `getattr(a, "calls_model", False)` is neither built nor recorded. If it needs a
  build, the log says `tags: stale, not built by serve (run `logdash build`)` and the
  result is `StageResult(key, "skipped")`. Otherwise it is `"fresh"` as usual.
- A skipped stage is not added to `rebuilt`. Dependents (none today; #32 will add some)
  build against the stage's last cache. The next `logdash build` rebuilds the stage,
  and its dependents follow in the same run.
- `cmd_serve` calls `run_build(settings, allow_model=False)`.
- The Dash app gets no tags page: `Registry.pages()` filters on
  `getattr(a, "page", True)`.

**Fingerprint.** `setting_names = ("tag_model", "tagging_enabled")`. `tagging_enabled`
is a `Settings` property, `bool(openrouter_api_key)`, so adding a key later rebuilds
the stage. The key itself is never in the fingerprint, the manifest or `repr`.
`version = f"1:{PROMPT_VERSION}"`.

**Memo.** `cache/tags_memo/memo.parquet` holds valid answers only. Its key is
`digest_of([PROMPT_VERSION, model_id, text])`. The same text never triggers a call
(AC5), even when Drain renumbers the ids. Untagged templates are retried with
`logdash build --only tags --force` (human decision 4: no automatic retry).
`"tags_memo"` is added to `RESERVED_KEYS`.

**Request** (AC2, human decision 3):

```python
{"model": model_id, "max_tokens": 1000,
 "messages": [system, user],
 "response_format": {"type": "json_schema",
                     "json_schema": {"name": "template_tags", "strict": True, "schema": JSON_SCHEMA}},
 "provider": {"require_parameters": True}}
```

- There is **no `temperature`**: with `require_parameters`, it would leave no provider
  for Haiku 5.5, and every call would fail with a 404.
- `max_tokens` only bounds a runaway answer. A normal answer is about 60 tokens.
- Every answer is validated locally by `parse_tag`.

**Errors** (they never escape `compute`):

| Failure | Class | Handling |
|---|---|---|
| reply not JSON, wrong shape, unknown component, no `choices`/content, `finish_reason == "length"` | `InvalidAnswer` | retry at once, at most 3 attempts in total, then untagged |
| HTTP 408, 429, 5xx, timeout | `TransientError` | `sleep(2 * attempt)`, then retry, at most 3 attempts, then untagged |
| other HTTP 4xx (bad key, no credit, unknown model, no provider for the parameters), `URLError` without status (offline, DNS) | `FatalError` | stop calling for this run; the remaining templates are untagged; `meta.aborted` holds the reason |
| any other `Exception` from the model | – | untagged with `repr(exc)`, no retry |

**Readers for later stories.** `read_tags(store) -> pl.DataFrame | None` and
`read_meta(store) -> dict` read only the cache. `None` / `available: false` means the
page hides its tag UI.

**`.env`.** `config.read_dotenv`, with no new dependency. `Settings.from_env()` merges
`./.env` **under** `os.environ`. When an explicit `env` mapping is given, `.env` is not
read.

**Alternatives rejected.**

- Tagging inside `templates.compute`: it would couple an offline stage to the network.
- The memo inside the stage directory: `reset_module` wipes it.
- A tags page: that is #29's job.
- Special-casing the stage in `build.py` like `events`: it would lose `--only`,
  `status` and dependencies.
- Free-text parsing: forbidden by #24 and AC2.
- The `openai` SDK or `python-dotenv`: new dependencies for about 40 lines of stdlib
  code.
- Batching templates per request: one bad answer would spoil a batch.
- Sending the service or level with the template: it would change the memo key
  without a text change (AC5), and #24 wants the meaning "regardless of its log level".
- Hard-coding `"tags"` in `cmd_serve` instead of `calls_model`: later model stages
  (chat assistant, triage) would each need a CLI edit.
- Skipping the dependents of a skipped stage as well: serve would then show stale
  anomalies after an unrelated data change, only because tags are stale.

## 4. Steps

### Commit 1: steps 1-4 (settings, page-less and model-free builds, test shield part 1)

**Step 1: `src/logdash/config.py`**

- Add `DEFAULT_TAG_MODEL = "anthropic/claude-haiku-5.5"` and the module global
  `DOTENV_PATH = Path(".env")`. Import `field`.
- Add `def read_dotenv(path: Path) -> dict[str, str]`:
  - If the file is missing, return `{}`.
  - For each stripped line: skip it if it is empty, starts with `#` or has no `=`.
  - Remove a leading `export `, split at the first `=` and strip both sides.
  - If the value is at least 2 characters long and starts and ends with the same `'`
    or `"`, remove those quotes.
  - Do not handle interpolation or inline comments.
- Add two `Settings` fields **after** `novel_fraction`:
  ```python
  tag_model: str = DEFAULT_TAG_MODEL  # OpenRouter model ID for semantic tags
  openrouter_api_key: str | None = field(default=None, repr=False)  # never fingerprinted
  ```
  Add a property `tagging_enabled -> bool` that returns `bool(self.openrouter_api_key)`.
- `from_env`: when `env is None`, use `env = {**read_dotenv(DOTENV_PATH), **os.environ}`,
  reading `DOTENV_PATH` at call time. Add
  `tag_model=env.get("LOGDASH_TAG_MODEL") or DEFAULT_TAG_MODEL` and
  `openrouter_api_key=env.get("OPENROUTER_API_KEY") or None`.

**Step 2: `src/logdash/analyses/base.py`**

- Set `RESERVED_KEYS = frozenset({"events", "tags_memo"})`, commented
  `# tags_memo: memo of model answers (analyses/tags.py)`.
- Extend the `Analysis` docstring with: "Optional class attributes: `page = False` (built,
  but no page, link or card) and `calls_model = True` (calls a language model; skipped
  by `logdash serve`'s build, see ADR001)."
- Add to `Registry`:
  ```python
  def pages(self) -> list[Analysis]:
      """Analyses with a dashboard page (``page`` attribute, default True), in build order."""
      return [a for a in self.ordered() if getattr(a, "page", True)]
  ```
- `src/logdash/app/main.py:26` and `:70`, `src/logdash/app/shell.py:40`, and
  `tests/benchmark/test_latency.py:88`: change `ordered()` to `pages()`.

**Step 3: `src/logdash/build.py` and `src/logdash/cli.py`**

- `StageResult.status` comment: `# "built", "fresh" or "skipped" (model stage, serve)`.
- `run_build` gets the keyword `allow_model: bool = True`. Docstring addition: "With
  `allow_model=False` (used by `serve`), stages with `calls_model = True` are not built
  even if stale; they are reported as `skipped` and keep their previous cache." In the
  analysis loop (lines 129-135), before the `needs_build` branch:
  ```python
  if not allow_model and getattr(analysis, "calls_model", False):
      if needs_build(analysis.key, upstream):
          log(f"{analysis.key}: stale, not built by serve (run `logdash build`)")
          results.append(StageResult(analysis.key, "skipped"))
      else:
          log(f"{analysis.key}: up to date")
          results.append(StageResult(analysis.key, "fresh"))
      continue
  ```
- `cli.py:84`: change it to `run_build(settings, allow_model=False)`, with the comment
  `# serve never calls a language model (ADR001)`.

**Step 4: `tests/conftest.py`, shield part 1**

Add an autouse fixture. It extends in commit 2.

```python
@pytest.fixture(autouse=True)
def _no_model_network(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory) -> None:
    """Never read the developer's .env or reach OpenRouter from a test."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LOGDASH_TAG_MODEL", raising=False)
    monkeypatch.setattr(config, "DOTENV_PATH", tmp_path_factory.getbasetemp() / "no.env")
```

Tests in commit 1: the `test_config.py`, `test_registry.py` and
`test_build_pipeline.py` rows and the serve test from section 5.

### Commit 2: steps 5-7 (tagging package, shield part 2)

**Step 5: `src/logdash/tagging/__init__.py`** contains only a docstring: "Semantic
template tags from a language model; called by `logdash build` only (ADR001)."

`src/logdash/tagging/model.py`:

```python
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

class InvalidAnswer(ValueError): ...    # retry, then untagged
class TransientError(Exception): ...    # back off and retry, then untagged
class FatalError(Exception): ...        # stop calling for this run

class TagModel(Protocol):
    def tag(self, template: str) -> Tag: ...

JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "actionable": {"type": "boolean", "description": "describes a real failure an operator should act on, whatever its log level"},
        "component": {"type": "string", "enum": list(COMPONENTS)},
        "noise": {"type": "boolean", "description": "routine background chatter that is safe to hide during triage"},
        "summary": {"type": "string", "description": "one plain-language sentence, no placeholders"},
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
```

`parse_tag` rules:

- If `content` is a `str`, decode it with `json.loads`. On a decode error, raise
  `InvalidAnswer`.
- The result must be a dict with all four keys. Extra keys are ignored.
- `actionable` and `noise` must satisfy `isinstance(v, bool)`. There is no coercion.
- `component` must be a `str`. After `.strip().lower()`, it must be in `COMPONENTS`.
- `summary` must be a `str`. Collapse it with `" ".join(s.split())`. It must not be
  empty. If it is longer than `SUMMARY_CHARS`, cut it to `SUMMARY_CHARS - 1`
  characters and append `"…"`.

**Step 6: `src/logdash/tagging/openrouter.py`**

```python
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_S = 60.0
MAX_TOKENS = 1000
TRANSIENT_4XX = frozenset({408, 429})

def post_json(url: str, payload: Mapping[str, Any], headers: Mapping[str, str], timeout: float) -> Any:
    """POST JSON and decode the JSON reply; the only network access (tests replace it)."""

class OpenRouterModel:
    def __init__(self, api_key: str, model_id: str, timeout: float = TIMEOUT_S) -> None: ...
    def payload(self, template: str) -> dict[str, Any]: ...
    def tag(self, template: str) -> Tag: ...
```

- `post_json` sends a `urllib.request.Request` with the JSON body, the headers
  `{"Content-Type": "application/json", **headers}` and `method="POST"`, using
  `urlopen(req, timeout=timeout)` as a context manager. It returns
  `json.loads(resp.read())`. Error mapping, catching `HTTPError` before `URLError`:
  - `HTTPError` with a code in `TRANSIENT_4XX` or `>= 500`: `TransientError(f"HTTP {code}")`.
  - Any other `HTTPError`: `FatalError(f"HTTP {code} {reason}")`.
  - `TimeoutError`: `TransientError("timeout")`.
  - `URLError`: `FatalError(f"cannot reach OpenRouter: {exc.reason}")`.
  - A reply body that is not JSON: `InvalidAnswer`.

  **No message contains the key or the headers.**
- `payload` returns exactly the request shown in section 3. The messages are
  `[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content":
  user_prompt(template)}]`. **Do not add `temperature`** (see section 3).
- `tag` calls `post_json(ENDPOINT, self.payload(t), {"Authorization": f"Bearer {key}",
  "X-Title": "logdash"}, self.timeout)`.
  - Take `choice = body["choices"][0]`. If this raises `KeyError`, `IndexError` or
    `TypeError`, raise `InvalidAnswer("no content in reply")`. This covers OpenRouter's
    200 replies with an `error` object.
  - If `choice.get("finish_reason") == "length"`, raise
    `InvalidAnswer("answer cut off")`.
  - Take `content = choice["message"]["content"]`, mapping the same errors and a
    `None` content to `InvalidAnswer`.
  - Return `parse_tag(content)`.
- Keep the key in `self._api_key`. Do not define a `__repr__` that shows it.

**Step 7: `src/logdash/tagging/runner.py`**

```python
MAX_ATTEMPTS = 3
BACKOFF_S = 2.0
WORKERS = 4
PROGRESS_EVERY = 25
MEMO_SCHEMA = {"key": pl.String, "actionable": pl.Boolean, "component": pl.String,
               "noise": pl.Boolean, "summary": pl.String}

def memo_key(model_id: str, template: str) -> str:
    return digest_of([PROMPT_VERSION, model_id, template])

def load_memo(path: Path, log: Callable[[str], None] = print) -> dict[str, Tag]:
    """Missing file -> {}; unreadable or wrong columns -> {} and one log line."""

def save_memo(path: Path, memo: Mapping[str, Tag]) -> None:
    """Rows sorted by key; write path.with_suffix('.tmp') then replace; mkdir parents."""

@dataclass
class Outcome:
    tags: dict[str, Tag]          # by template text, memo hits included
    failed: dict[str, str]        # template text -> reason
    calls: int = 0                # model.tag invocations
    aborted: str | None = None    # first FatalError message

def tag_templates(texts: Sequence[str], model: TagModel, memo: dict[str, Tag], model_id: str, *,
                  log: Callable[[str], None] = print,
                  sleep: Callable[[float], None] = time.sleep,
                  workers: int = WORKERS) -> Outcome:
```

`tag_templates` behaviour:

- Deduplicate `texts`, keeping their order. Texts whose `memo_key(model_id, t)` is in
  `memo` are hits. If there are no misses, return without creating a pool, with
  `calls == 0`.
- Otherwise run `_tag_one(text)` for each miss on
  `ThreadPoolExecutor(max_workers=workers)`. `_tag_one` makes at most `MAX_ATTEMPTS`
  attempts. Each attempt:
  - If the `abort` `threading.Event` is set, record `failed[text] = f"aborted:
    {aborted}"` and stop.
  - Increment `calls` under a `threading.Lock`, then call `model.tag(text)`.
  - On success, under the lock: set `memo[key] = tag` and `tags[text] = tag`.
  - On `InvalidAnswer`: retry at once.
  - On `TransientError`: `sleep(BACKOFF_S * attempt)`, then retry.
  - On `FatalError`: if `aborted` is unset, set it to `str(exc)`. Set `abort` and
    record the failure.
  - On any other `Exception`: `failed[text] = repr(exc)`, no retry.
  - When the attempts run out: `failed[text] = f"{type(last).__name__}: {last}"`.
- Log `f"tagged {done}/{len(misses)}"` every `PROGRESS_EVERY` finished misses.
- `memo`, `tags`, `failed` and `calls` are touched only under the lock.

**Shield part 2** (same commit): in the conftest fixture, add

```python
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("tests must not call OpenRouter")
    monkeypatch.setattr(openrouter, "post_json", refuse)
```

Tests in commit 2: the `test_tagging.py` rows from section 5.

### Commit 3: steps 8-9 (the stage, README)

**Step 8: `src/logdash/analyses/tags.py`**

```python
KEY = "tags"
FILE = "tags.parquet"
META = "meta.json"
MEMO_KEY = "tags_memo"
MEMO_FILE = "memo.parquet"
MAX_FAILURES_KEPT = 20
TAGS_SCHEMA = {"template_id": pl.Int64, "template": pl.String, "tagged": pl.Boolean,
               "actionable": pl.Boolean, "component": pl.String, "noise": pl.Boolean,
               "summary": pl.String}
ModelFactory = Callable[[Settings], TagModel]

def openrouter_factory(settings: Settings) -> TagModel:
    if not settings.openrouter_api_key:
        raise FatalError("no OPENROUTER_API_KEY")
    return OpenRouterModel(settings.openrouter_api_key, settings.tag_model)

def tags_frame(templates: pl.DataFrame, tags: Mapping[str, Tag]) -> pl.DataFrame:
    """One row per template (sorted by template_id); tag columns null and tagged False where untagged."""

def read_meta(store: CacheStore) -> dict[str, Any]:
    """meta.json, or {"available": False, "reason": "not built"} if missing."""

def read_tags(store: CacheStore) -> pl.DataFrame | None:
    """tags.parquet, or None when tags are absent (no key, not built). Reads the cache only."""

@register
class Tags:
    key = KEY
    title = "Semantic tags"
    version = f"1:{PROMPT_VERSION}"
    depends_on: tuple[str, ...] = (tpl.KEY,)
    setting_names = ("tag_model", "tagging_enabled")
    page = False
    calls_model = True

    def __init__(self, model_factory: ModelFactory = openrouter_factory,
                 sleep: Callable[[float], None] = time.sleep) -> None: ...
    def compute(self, ctx: ComputeContext) -> None: ...
    def layout(self, store: CacheStore, **_query: Any) -> Component:
        raise RuntimeError("the tags stage has no page")
    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        """No page, no callbacks: the dashboard never calls the model."""
```

Import it as `from logdash.analyses import templates as tpl`. Import `Dash` and
`Component` only under `TYPE_CHECKING`.

`compute(ctx)`:

1. `store = CacheStore(ctx.out_dir.parent, ctx.settings)`.
2. If there is no key (`not ctx.settings.tagging_enabled`):
   - `ctx.progress("skipped (no OPENROUTER_API_KEY)")`
   - `store.write_json(KEY, META, {"available": False, "reason": "no OPENROUTER_API_KEY"})`
   - `ctx.note_rows("tagged", 0)`
   - return.
3. `templates = store.read_parquet(tpl.KEY, "templates.parquet").select("template_id", "template")`.
4. `memo_path = store.path(MEMO_KEY, MEMO_FILE)` and
   `memo = load_memo(memo_path, ctx.progress)`.
5. Tag, and always save the memo:
   ```python
   try:
       outcome = tag_templates(templates["template"].to_list(),
                               self.model_factory(ctx.settings), memo,
                               ctx.settings.tag_model, log=ctx.progress, sleep=self.sleep)
   finally:
       save_memo(memo_path, memo)
   ```
6. `frame = tags_frame(templates, outcome.tags)` and `store.write_parquet(KEY, FILE, frame)`.
7. Write `meta.json` with these keys: `available: True`, `model`, `prompt_version`,
   `templates`, `tagged`, `untagged`, `calls`, `aborted`, and `failures`. `failures`
   holds the first `MAX_FAILURES_KEPT` items of
   `{"template_id", "reason"}` for untagged rows, in `template_id` order.
8. `ctx.note_rows` for `"tagged"`, `"untagged"` and `"model_calls"`.
9. Log `f"tagged {tagged} of {n} templates, {calls} model call(s)"`. If any templates
   are untagged, also log ``"run `logdash build --only tags --force` to retry untagged templates"``.

`src/logdash/analyses/__init__.py`: add `"tags"` after `"templates"` in `BUILTIN`.

**Step 9: `README.md`**

- Configuration table, two new rows:
  - `LOGDASH_TAG_MODEL` | `anthropic/claude-haiku-5.5` | OpenRouter model for
    semantic template tags (it must support structured outputs).
  - `OPENROUTER_API_KEY` | (unset) | enables the `tags` stage. Without it the stage is
    skipped.
- Below the table: "Variables can also be set in `.env` in the working directory; the
  real environment wins. Only `logdash build` calls the model; `serve` builds every
  other stale stage but leaves `tags` as it is."
- "Other commands" gets one line:
  `uv run logdash build --only tags --force   # retry untagged templates (answers are memoised)`.

Tests in commit 3: the `test_tags.py` rows from section 5. Then run the quality gate
from board.md.

## 5. Tests

**Stubs.**

- `StubModel(answers: dict[str, list[Tag | Exception]] | None = None)`. Its
  `tag(text)` pops the next answer for `text` and raises it if it is an exception.
  With no answer left, it returns `Tag(False, "other", False, f"summary of
  {text[:20]}")`. It appends `text` to `self.calls`.
- Integration helper: build `Templates()` and
  `Tags(model_factory=lambda s: stub, sleep=lambda _: None)` into the session `built`
  cache, with `replace(built.settings, openrouter_api_key="sk-test", tag_model=<unique
  per test>)`.
- A function fixture removes `built.store.module_dir("tags")` and
  `module_dir("tags_memo")` before and after each such test.

| Commit | File: test | Proves | AC |
|---|---|---|---|
| 1 | `test_config.py`: `test_tag_model_from_env_and_default` | `from_env({})` gives `DEFAULT_TAG_MODEL`; `LOGDASH_TAG_MODEL=x` gives `x`; an empty value gives the default | AC3 |
| 1 | `test_config.py`: `test_key_from_env_and_dotenv` (`DOTENV_PATH` monkeypatched to a tmp file, env vars set with `monkeypatch.setenv`) | the key is read from `.env`; `os.environ` overrides `.env`; a missing `.env` is fine; `tagging_enabled` follows the key; `"sk-secret" not in repr(settings)` | AC4 |
| 1 | `test_config.py`: `test_read_dotenv_parsing` | comments, blank lines, `export `, both quote styles, a line without `=` | AC4 |
| 1 | `test_registry.py`: `test_pages_excludes_pageless` and `test_reserved_memo_key` | `pages()` drops `page = False` analyses but `ordered()` keeps them; key `tags_memo` raises `DuplicateKeyError` | AC6 |
| 1 | `test_build_pipeline.py`: `test_model_stage_skipped_without_allow_model` (a `CountLevels` subclass with `calls_model = True`, plus a dependent) | with `allow_model=False` the model stage is `skipped`, `calls == 0`, it is not in the manifest, and the dependent is still built; a later default `run_build` builds it (`built`) and rebuilds the dependent; when it is fresh, `allow_model=False` reports `fresh` | AC6 |
| 1 | `test_build.py`: `test_serve_builds_without_model` (`logdash.cli.run_build` monkeypatched to record kwargs; `logdash.app.main.create_app` monkeypatched to raise `FileNotFoundError`) | `main(["serve", …])` returns 2 and called `run_build` with `allow_model=False`; `--no-build` does not call it | AC6 |
| 2 | `test_tagging.py`: `test_payload_requests_strict_json_schema` | `payload("t")` has `response_format.type == "json_schema"`, `strict is True`, `schema == JSON_SCHEMA`, `provider == {"require_parameters": True}`, `model` equal to the given ID, and `set(payload) == {"model", "max_tokens", "messages", "response_format", "provider"}` (no `temperature`) | AC2, AC3 |
| 2 | `test_tagging.py`: `test_tag_parses_reply_content` (`post_json` monkeypatched) | returns the `Tag`; the headers carry `Bearer k` | AC2 |
| 2 | `test_tagging.py`: `test_reply_without_usable_content_is_invalid` (parametrised: `{}`, `{"error": {...}}`, `{"choices": []}`, content `None`, `finish_reason: "length"`) | each raises `InvalidAnswer` | AC2 |
| 2 | `test_tagging.py`: `test_parse_tag_rejects` (parametrised: non-JSON, a list, a missing key, the string `"true"`, an unknown component, an empty summary) | each raises `InvalidAnswer` | AC2 |
| 2 | `test_tagging.py`: `test_parse_tag_normalises` | `" Network "` becomes `network`; a multi-line summary becomes one line; 300 characters are cut to `SUMMARY_CHARS` ending in `…`; extra keys are ignored | AC1, AC2 |
| 2 | `test_tagging.py`: `test_post_json_maps_errors` (parametrised; `urlopen` monkeypatched in the module; it calls the **real** `post_json`, imported at module top before the autouse patch) | 429 and 503 give `TransientError`; 401, 400 and 404 give `FatalError`; `URLError` gives `FatalError`; `TimeoutError` gives `TransientError`; a non-JSON body gives `InvalidAnswer`; a JSON body is decoded; no message contains the key | AC2 |
| 2 | `test_tagging.py`: `test_invalid_answer_is_retried` | `InvalidAnswer` then a tag: the text is tagged and `calls == 2` | AC2 |
| 2 | `test_tagging.py`: `test_transient_backs_off` | `TransientError` then a tag: the recorded sleeps are `[BACKOFF_S]` | AC2 |
| 2 | `test_tagging.py`: `test_fatal_error_stops_calling` (`workers=1`, 3 texts) | `calls == 1`; all 3 texts failed; `aborted` is set | AC2 |
| 2 | `test_tagging.py`: `test_unexpected_exception_is_untagged` | `RuntimeError`: the text failed, with no retry and nothing raised | AC2 |
| 2 | `test_tagging.py`: `test_memo_hits_make_no_calls` | a pre-filled memo gives 0 calls; duplicate texts cause one call | AC5 |
| 2 | `test_tagging.py`: `test_memo_roundtrip_and_corrupt_file` | save then load gives the same tags; a garbage file gives `{}` and one log line | AC5 |
| 3 | `test_tags.py`: `test_build_writes_one_row_per_template` | same ids as `templates.parquet`; `TAGS_SCHEMA` dtypes; all rows tagged; components in `COMPONENTS`; no `\n` in summaries; manifest `tagged == n`; `read_tags` returns the frame; `read_meta()["available"]` is true | AC1 |
| 3 | `test_tags.py`: `test_persistent_invalid_answer_is_untagged_and_build_succeeds` | one text always fails with `InvalidAnswer`: `run_build` returns; that row has `tagged == False` and null tags; meta `untagged == 1`; the failure reason names `InvalidAnswer`; the stub was called `MAX_ATTEMPTS` times for it | AC2 |
| 3 | `test_tags.py`: `test_model_change_retags` | another `tag_model` makes the stage rebuild and the stub be called again | AC3, AC5 |
| 3 | `test_tags.py`: `test_no_key_skips_and_records_absence` (the factory raises if called) | the build succeeds; the factory is not called; no `tags.parquet`; `read_tags(store) is None`; `read_meta(store)["available"] is False`; `stage_statuses` says `fresh` | AC4 |
| 3 | `test_tags.py`: `test_adding_key_triggers_tagging` | without a key, then with one: the stage is rebuilt and tagged; `"sk-test"` appears in neither `manifest.json` nor `meta.json` | AC4 |
| 3 | `test_tags.py`: `test_rebuild_with_same_texts_makes_no_calls` | after a first build, `run_build(..., only="tags", force=True)` leaves `len(stub.calls)` unchanged, gives `meta.calls == 0` and the same tags | AC5 |
| 3 | `test_tags.py`: `test_serve_build_skips_tags` | with a key and a stale stage, `run_build(..., allow_model=False)` reports `skipped` and the stub is not called | AC6 |
| 3 | `test_tags.py`: `test_dashboard_has_no_tags_page` | a registry with a page analysis and `Tags(factory that raises)`: `create_app` registers no `/tags`; `navbar_links` and `home_layout` have no `/tags`; the factory is never called; `Tags().layout(store)` raises `RuntimeError`; `register_callbacks` returns `None` | AC6 |
| – | existing `test_cli_build_status_serve` | still passes with `tags` among the built-ins and no key (the skip is fast) | AC4 |

AC7: the autouse shield makes every network call and every read of the real `.env`
impossible in tests. The quality gate enforces 90 % coverage.

Manual check, not required for review: with a real key,
`uv run logdash build --only tags` tags about 281 templates. A second run reports
`0 model call(s)`.

## 6. Open questions

Each question has a default; none of them blocks the plan.

1. **Model ID spelling.** PR #34 says `anthropic/claude-haiku-5-5`. OpenRouter's
   canonical ID is `anthropic/claude-haiku-5.5`; the hyphenated form is not in
   `/api/v1/models`. *Default: `anthropic/claude-haiku-5.5`.* It also serves as the
   memo key, so a stable spelling matters.
2. **Reasoning.** Haiku 5.5 supports `reasoning`, and its default effort on OpenRouter
   is not documented per model. *Default: send no `reasoning` parameter.*
   `max_tokens = 1000` bounds the cost, and a cut-off answer is an `InvalidAnswer`. If
   the run shows many `answer cut off` failures, add `"reasoning": {"effort": "low"}`
   (supported by every endpoint) and bump `PROMPT_VERSION`.
3. **Dependents of a skipped stage under serve** (relevant from #32 on). *Default:*
   they build against the tags last cached. The next `logdash build` rebuilds both.
   This is recorded in ADR001.

## 7. Changes from previous version

The human decisions are in the comment on PR #34 (avart, 2026-10-22).

| Decision | How v2 addresses it |
|---|---|
| 1. Naming: plans `-v<n>.md`, ADRs in `adrs/` | v1 renamed to `plans/2026-10-22-tagging-stage-v1.md` and the ADR moved to `adrs/` in a rename-only commit; this file is `-v2.md`. |
| 2. Serve never calls the model; only explicit `logdash build` tags | New analysis attribute `calls_model`; `run_build(allow_model=False)` skips such stages; `cmd_serve` passes it (step 3). Tests: `test_model_stage_skipped_without_allow_model`, `test_serve_builds_without_model`, `test_serve_build_skips_tags`. README states it. ADR001, decision 1, rewritten. v1 open question 1 removed. |
| 3. Default model `anthropic/claude-haiku-5-5`; check strict JSON schema with `require_parameters` | Checked against OpenRouter's endpoints API (section 2): structured outputs are supported by the Anthropic, Vertex and Azure routes; Bedrock routes are excluded by `require_parameters`; **`temperature` is supported by no route**. So the default is `anthropic/claude-haiku-5.5` (canonical spelling, open question 1), `temperature` is removed from the request, and a test pins the request's keys. `max_tokens` and a `finish_reason == "length"` check were added. ADR001, decision 5, states the parameter rule. |
| 4. No automatic retry of untagged templates | Unchanged. The log line and README name `logdash build --only tags --force`. |
| Settled conventions (commit 0949cd1) | Steps grouped into three commits. Test shield split so that each part lands with the code it protects. Every AC is mapped to tests. Open questions now carry defaults. ADR in `adrs/` with `Status: proposed`. |

Other changes:

- `openrouter_factory` raises `FatalError` instead of using `assert`.
- The failure cap moved from `runner.py` to `tags.py`, where `meta.json` is written.
