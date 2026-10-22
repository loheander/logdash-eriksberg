# Plan: semantic tags, tagging build stage (story #28), version 01

## 1. Story

Story [#28](https://github.com/loheander/logdash-eriksberg/issues/28), parent
[#24](https://github.com/loheander/logdash-eriksberg/issues/24). Feature branch
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

- `src/logdash/build.py:78-136`: `run_build`. A stage is rebuilt when it is forced,
  missing, has a changed fingerprint, or when an upstream stage was rebuilt in the same
  run (`needs_build`, line 107).
- `src/logdash/build.py:34-54`: `compute_fingerprints`. The data fingerprint is part
  of **every** stage, and `setting_names` (read with `getattr`, line 50) adds settings.
  Consequence: the stage alone cannot meet AC5. Any data change or upstream rebuild
  re-runs `compute`, so a text-keyed memo is required.
- `src/logdash/build.py:139-161`: `_build_analysis` calls `store.reset_module(key)`
  (line 149) **before** `compute`, so the stage directory is wiped every time. The
  memo must therefore live outside it.
- `src/logdash/analyses/base.py:20`: `RESERVED_KEYS`. Lines 23-36: the `Analysis`
  protocol (`runtime_checkable`; optional attributes such as `setting_names` are read
  with `getattr`, not declared). Line 107: `Registry.ordered`.
- `src/logdash/analyses/__init__.py:7-17`: `BUILTIN` module list.
- `src/logdash/analyses/templates.py:48` `KEY = "templates"`, and lines 167-181:
  `templates.parquet` with columns `template_id, template, count, first_seen,
  last_seen, n_variants`. Template texts use `<UUID> <IP> <HEX> <PATH> <NUM>`
  (`src/logdash/ingest/masking.py:13-19`) and Drain's `<*>`.
- `src/logdash/analyses/anomalies.py:394-396`: the pattern for reading upstream stages
  inside `compute`: `CacheStore(ctx.out_dir.parent, ctx.settings)`.
- `src/logdash/cache/store.py:53-69`: `write_parquet`, `read_parquet`, `write_json`,
  `read_json`, `path`, `module_dir`.
- `src/logdash/cache/manifest.py:52-55`: `digest_of`, the stable sha256 of a JSON
  value (reused as the memo key).
- `src/logdash/config.py:25-85`: `Settings` (frozen dataclass, positional
  construction in tests: `Settings(data_dir, cache_dir, "1GB", 2)`), `from_env`,
  `analysis_params`.
- `src/logdash/app/main.py:18-32` (`home_layout`) and `:70-77` (`create_app`), and
  `src/logdash/app/shell.py:37-42` (`navbar_links`): these iterate
  `registry.ordered()` to make pages.
- `tests/benchmark/test_latency.py:88`: iterates `REGISTRY.ordered()` and calls
  `layout`.
- `tests/conftest.py`: session `built` fixture and `build_with`.
  `tests/unit/test_build_pipeline.py:11-32`: the stub analysis pattern.
- `.gitignore` already ignores `.env`. The main checkout has a real `.env`, and
  `tests/unit/test_build.py:33-45` runs `main(["build", …])` with all built-ins, so
  **tests must be shielded from the real key** (see step 9).
- HTTP: there is no `requests` or `httpx` direct dependency (`pyproject.toml`). Use
  stdlib `urllib.request`.

## 3. Design

See `adr/ADR001-language-model-calls-at-build-time.md` (committed with this plan).

**Stage.** A new analysis `Tags` (`key = "tags"`, `depends_on = ("templates",)`,
`page = False`) in `src/logdash/analyses/tags.py`, registered through `BUILTIN`. Its
`compute`:

1. If no key: log `skipped (no OPENROUTER_API_KEY)` and write `meta.json` with
   `available: false`. Write no `tags.parquet` and do no reads. Done in milliseconds.
2. Otherwise read `templates.parquet`, load the memo, and tag the unique texts that
   are not in the memo (thread pool, 4 workers), saving the memo in a `finally`.
   Then write `tags.parquet` (one row per template, nulls when untagged) and
   `meta.json`.

**Fingerprint.** `setting_names = ("tag_model", "tagging_enabled")`, where
`tagging_enabled` is a `Settings` property `bool(openrouter_api_key)`. A build without
a key is "fresh" until a key appears, which flips the fingerprint and triggers tagging.
The key itself never enters the fingerprint, the manifest or `repr`.
`version = f"1:{PROMPT_VERSION}"`, so a prompt change rebuilds the stage.

**Memo.** `cache/tags_memo/memo.parquet` (outside the reset directory). It holds only
valid answers, keyed by `digest_of([PROMPT_VERSION, model_id, template_text])`. Same
text means no call (AC5), even if Drain renumbers `template_id`. A new model or prompt
means a new key. Untagged templates are retried on `logdash build --only tags --force`.
`"tags_memo"` is added to `RESERVED_KEYS`.

**Structured output (AC2).** Use OpenRouter chat completions with
`response_format = {"type": "json_schema", "json_schema": {"name": "template_tags",
"strict": true, "schema": JSON_SCHEMA}}`, `provider = {"require_parameters": true}`
(route only to providers that honour the schema) and `temperature = 0`. The answer is
validated locally with `parse_tag`, which raises `InvalidAnswer`.

**Errors and retries.** These never escape `compute`:

| Failure | Classified as | Handling |
|---|---|---|
| answer not JSON / wrong shape / unknown component / no `choices` | `InvalidAnswer` | retry at once, up to 3 attempts in total, then untagged |
| HTTP 408, 429, 5xx, socket timeout | `TransientError` | retry after `sleep(2 * attempt)`, up to 3 attempts, then untagged |
| HTTP 401, 402, 403, other 4xx (bad model ID, unsupported params); `URLError` without HTTP status (offline, DNS) | `FatalError` | stop the run: no further calls; all remaining templates untagged; `meta.aborted` holds the reason |
| any other `Exception` from the model | (other) | untagged with `repr(exc)` as the reason, no retry |

**Readers for later stories.** `read_tags(store) -> pl.DataFrame | None` and
`read_meta(store) -> dict`. Both read only the cache. `None` / `available: false`
means pages hide tag UI.

**No page (AC6).** `Registry.pages()` filters on `getattr(a, "page", True)`, and the
app uses it. `Tags.layout` raises `RuntimeError` (it is never called), and
`register_callbacks` is a no-op. The serve command's optional pre-build (`cmd_serve`
→ `run_build`) is a build, so it may tag if the stage is stale. The Dash app itself
(layouts and callbacks) never does. See open question 1.

**`.env` (AC4).** `config.read_dotenv(path)` is a tiny parser with no new dependency.
`Settings.from_env()` with no argument merges `.env` (cwd) **under** `os.environ`, so
the real environment wins. With an explicit `env` mapping (tests) `.env` is not read.

**Alternatives rejected.**

- *Tag inside `templates.compute`*: couples an expensive, offline-capable stage to the
  network. A failure or a key change would rebuild Drain.
- *Memo in the stage directory / reuse the previous `tags.parquet`*: wiped by
  `reset_module` before `compute`.
- *A page for the tags stage*: showing tags is #29's job. An empty page clutters the
  navigation.
- *Special-case the stage in `build.py` like `events`*: it loses `--only`, `status`
  and dependency handling for free.
- *Free-text answer parsed with regexes*: forbidden by #24 and AC2.
- *`openai` SDK or `python-dotenv`*: new dependencies for about 40 lines of stdlib
  code.
- *Batching several templates per request*: one bad answer would spoil a batch, and
  retries would be coarser. 281 small calls on 4 workers take a few minutes, once.
- *Sending the service or level with the template*: it would change the memo key when
  only counts change (breaking AC5). #24 also asks for meaning "regardless of its log
  level".

## 4. Steps

Each step is one commit (`… (story #28)`). Run the quality gate (board.md) after the
last step.

### Step 1: settings and `.env`

`src/logdash/config.py`:

- Add the constant `DEFAULT_TAG_MODEL = "openai/gpt-4o-mini"` and the module global
  `DOTENV_PATH = Path(".env")`.
- Add `def read_dotenv(path: Path) -> dict[str, str]`. If the file is missing, return
  `{}`. For each line: strip it; skip it if it is empty, starts with `#` or has no
  `=`; remove a leading `export `. Split at the first `=` and strip the key and the
  value. If the value is at least 2 characters long and starts and ends with the same
  `'` or `"`, remove those quotes. There is no interpolation and no inline comments.
- Add two `Settings` fields **after** `novel_fraction` (positional construction must
  keep working):
  ```python
  tag_model: str = DEFAULT_TAG_MODEL  # OpenRouter model ID for semantic tags
  openrouter_api_key: str | None = field(default=None, repr=False)  # never fingerprinted
  ```
  Import `field` from `dataclasses`.
- Add the property `tagging_enabled -> bool`, returning `bool(self.openrouter_api_key)`.
- Change `from_env`: when `env is None`, use
  `env = {**read_dotenv(DOTENV_PATH), **os.environ}`. Read `DOTENV_PATH` at call time
  so that tests can monkeypatch it. Add
  `tag_model=env.get("LOGDASH_TAG_MODEL") or DEFAULT_TAG_MODEL` and
  `openrouter_api_key=env.get("OPENROUTER_API_KEY") or None`.

### Step 2: page-less analyses

- `src/logdash/analyses/base.py`: set
  `RESERVED_KEYS = frozenset({"events", "tags_memo"})` with the comment
  `# tags_memo: memo of model answers, see analyses/tags.py`. In the `Analysis`
  docstring, add: "An analysis without a page sets `page = False`; it is built but gets
  no page, link or card." Add to `Registry`:
  ```python
  def pages(self) -> list[Analysis]:
      """Analyses with a dashboard page (``page`` attribute, default True), in build order."""
      return [a for a in self.ordered() if getattr(a, "page", True)]
  ```
- `src/logdash/app/main.py:26` and `:70`, and `src/logdash/app/shell.py:40`: change
  `registry.ordered()` to `registry.pages()`.
- `tests/benchmark/test_latency.py:88`: change `REGISTRY.ordered()` to `REGISTRY.pages()`.

### Step 3: tag model, schema and prompt

New `src/logdash/tagging/__init__.py` (a docstring only: "Semantic template tags from a
language model; called at build time only, see ADR001.").

New `src/logdash/tagging/model.py`:

```python
COMPONENTS = ("network", "storage", "auth", "scheduling", "configuration", "hypervisor", "other")
PROMPT_VERSION = "1"
SUMMARY_CHARS = 160
TEMPLATE_CHARS = 2000  # longer templates are cut before sending

@dataclass(frozen=True)
class Tag:
    actionable: bool
    component: str
    noise: bool
    summary: str

class InvalidAnswer(ValueError): ...    # retry, then untagged
class TransientError(Exception): ...    # retry with back-off, then untagged
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

- If `content` is a `str`, decode it with `json.loads`. On a `JSONDecodeError`, raise
  `InvalidAnswer`.
- The result must be a dict containing all four required keys. Extra keys are
  ignored.
- `actionable` and `noise` must be `bool`, using `isinstance(v, bool)`. Do not coerce
  strings.
- `component` must be a `str`. Apply `.strip().lower()`; the result must be in
  `COMPONENTS`.
- `summary` must be a `str`. Collapse it to one line with `" ".join(s.split())`. It
  must be non-empty. If it is longer than `SUMMARY_CHARS`, cut it to
  `SUMMARY_CHARS - 1` characters and append `"…"`.

### Step 4: OpenRouter client

New `src/logdash/tagging/openrouter.py`:

```python
ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
TIMEOUT_S = 60.0
TRANSIENT_4XX = frozenset({408, 429})

def post_json(url: str, payload: Mapping[str, Any], headers: Mapping[str, str], timeout: float) -> Any:
    """POST JSON and decode the JSON reply; the only network access (tests replace it)."""

class OpenRouterModel:
    def __init__(self, api_key: str, model_id: str, timeout: float = TIMEOUT_S) -> None: ...
    def payload(self, template: str) -> dict[str, Any]: ...
    def tag(self, template: str) -> Tag: ...
```

- `post_json`: build a `urllib.request.Request(url, data=json.dumps(payload).encode(),
  headers={"Content-Type": "application/json", **headers}, method="POST")` and call
  `urllib.request.urlopen(req, timeout=timeout)` as a context manager. Return
  `json.loads(resp.read())`. Map errors as follows:
  - `urllib.error.HTTPError` with code in `TRANSIENT_4XX` or `>= 500`: raise
    `TransientError(f"HTTP {code}")`.
  - Any other `HTTPError`: raise `FatalError(f"HTTP {code} {reason}")`.
  - `TimeoutError` (which includes `socket.timeout`): raise `TransientError("timeout")`.
  - `URLError`: raise `FatalError(f"cannot reach OpenRouter: {exc.reason}")`.
  - A reply that is not JSON: raise `InvalidAnswer`.

  Catch `HTTPError` before `URLError`, because it is a subclass. **No message may
  contain the key or the headers.**
- `payload`: return
  `{"model": model_id, "temperature": 0, "messages": [{"role": "system", "content":
  SYSTEM_PROMPT}, {"role": "user", "content": user_prompt(template)}],
  "response_format": {"type": "json_schema", "json_schema": {"name": "template_tags",
  "strict": True, "schema": JSON_SCHEMA}}, "provider": {"require_parameters": True}}`.
- `tag`: `body = post_json(ENDPOINT, self.payload(template), {"Authorization": f"Bearer
  {key}", "X-Title": "logdash"}, self.timeout)`. Take
  `body["choices"][0]["message"]["content"]`. If this raises
  `KeyError`/`IndexError`/`TypeError`, or the result is `None`, raise
  `InvalidAnswer("no content in reply")`. OpenRouter can return 200 with an `error`
  object. Return `parse_tag(content)`.
- Store the key in a private attribute `_api_key`. Do not define `__repr__` with the
  key.

### Step 5: runner and memo

New `src/logdash/tagging/runner.py`:

```python
MAX_ATTEMPTS = 3
BACKOFF_S = 2.0
WORKERS = 4
PROGRESS_EVERY = 25
MAX_FAILURES_KEPT = 20  # in meta.json
MEMO_SCHEMA = {"key": pl.String, "actionable": pl.Boolean, "component": pl.String,
               "noise": pl.Boolean, "summary": pl.String}

def memo_key(model_id: str, template: str) -> str:
    return digest_of([PROMPT_VERSION, model_id, template])

def load_memo(path: Path, log: Callable[[str], None] = print) -> dict[str, Tag]:
    """Missing file -> {}; unreadable or wrong schema -> {} and a log line."""

def save_memo(path: Path, memo: Mapping[str, Tag]) -> None:
    """Write sorted by key to path.with_suffix('.tmp') then replace (atomic); mkdir parents."""

@dataclass
class Outcome:
    tags: dict[str, Tag]          # by template text, memo hits included
    failed: dict[str, str]        # template text -> reason
    calls: int = 0                # model.tag invocations
    aborted: str | None = None    # FatalError message that stopped the run

def tag_templates(
    texts: Sequence[str],
    model: TagModel,
    memo: dict[str, Tag],          # mutated: new answers are added under their memo_key
    model_id: str,
    *,
    log: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    workers: int = WORKERS,
) -> Outcome:
```

`tag_templates` behaviour:

- Deduplicate `texts`, keeping their order. Hits are the texts whose
  `memo_key(model_id, t)` is in `memo`. Misses go to
  `concurrent.futures.ThreadPoolExecutor(max_workers=workers)`. Return at once when
  there are no misses: no pool, `calls == 0`.
- For each miss, run `_tag_one(text)`, an inner function, for at most `MAX_ATTEMPTS`
  attempts. Each attempt:
  - If `abort` (a `threading.Event`) is set, record `failed[text] = f"aborted:
    {reason}"` and stop.
  - Increment `calls` under a `threading.Lock`, then call `model.tag(text)`.
  - On success, store under the lock: `memo[key] = tag` and `tags[text] = tag`.
  - On `InvalidAnswer`, retry immediately.
  - On `TransientError`, call `sleep(BACKOFF_S * attempt)` and retry.
  - On `FatalError`, set `aborted = str(exc)` (the first one wins), set `abort`, and
    record the failure.
  - On any other `Exception`, record `failed[text] = repr(exc)` and stop.
  - When the attempts run out, record `failed[text] = f"{type(last).__name__}: {last}"`.
- Every `PROGRESS_EVERY` completed misses, log `f"tagged {done}/{len(misses)}"`.
- `memo` and the result dicts are touched only under the lock.

### Step 6: the stage

New `src/logdash/analyses/tags.py`:

```python
KEY = "tags"
FILE = "tags.parquet"
META = "meta.json"
MEMO_KEY = "tags_memo"
MEMO_FILE = "memo.parquet"
TAGS_SCHEMA = {"template_id": pl.Int64, "template": pl.String, "tagged": pl.Boolean,
               "actionable": pl.Boolean, "component": pl.String, "noise": pl.Boolean,
               "summary": pl.String}
ModelFactory = Callable[[Settings], TagModel]

def openrouter_factory(settings: Settings) -> TagModel:
    assert settings.openrouter_api_key  # compute checks tagging_enabled first
    return OpenRouterModel(settings.openrouter_api_key, settings.tag_model)

def tags_frame(templates: pl.DataFrame, tags: Mapping[str, Tag]) -> pl.DataFrame:
    """One row per template (template_id, template), tag columns null where untagged."""

def read_meta(store: CacheStore) -> dict[str, Any]:
    """meta.json of the stage, or {"available": False, "reason": "not built"} if missing."""

def read_tags(store: CacheStore) -> pl.DataFrame | None:
    """tags.parquet, or None when tags are absent (no key, not built). Reads the cache only."""

@register
class Tags:
    key = KEY
    title = "Semantic tags"
    version = f"1:{PROMPT_VERSION}"
    depends_on: tuple[str, ...] = (templates.KEY,)
    setting_names = ("tag_model", "tagging_enabled")
    page = False

    def __init__(self, model_factory: ModelFactory = openrouter_factory,
                 sleep: Callable[[float], None] = time.sleep) -> None: ...
    def compute(self, ctx: ComputeContext) -> None: ...
    def layout(self, store: CacheStore, **_query: Any) -> Component:
        raise RuntimeError("the tags stage has no page")
    def register_callbacks(self, app: Dash, store: CacheStore) -> None:
        """No page, no callbacks: the dashboard never calls the model."""
```

Import `templates` as `from logdash.analyses import templates as tpl` and use
`tpl.KEY`. Import `Dash` and `Component` only under `TYPE_CHECKING`, so that the stage
does not import Dash at build time.

`compute(ctx)`:

1. `store = CacheStore(ctx.out_dir.parent, ctx.settings)`.
2. If `not ctx.settings.tagging_enabled`:
   - `ctx.progress("skipped (no OPENROUTER_API_KEY)")`;
   - `store.write_json(KEY, META, {"available": False, "reason": "no OPENROUTER_API_KEY"})`;
   - `ctx.note_rows("tagged", 0)`;
   - return.
3. `templates = store.read_parquet(tpl.KEY, "templates.parquet").select("template_id", "template")`.
4. `memo_path = store.path(MEMO_KEY, MEMO_FILE)` and
   `memo = load_memo(memo_path, ctx.progress)`.
5. Run the tagging and always save the memo:
   ```python
   try:
       outcome = tag_templates(templates["template"].to_list(),
                               self.model_factory(ctx.settings), memo,
                               ctx.settings.tag_model, log=ctx.progress, sleep=self.sleep)
   finally:
       save_memo(memo_path, memo)
   ```
6. `frame = tags_frame(templates, outcome.tags)` and
   `store.write_parquet(KEY, FILE, frame)`.
7. Write `meta.json` with: `available: True`, `model`, `prompt_version`, `templates`
   (frame height), `tagged`, `untagged`, `calls`, `aborted`, and `failures`. `failures`
   is a list of `{"template_id", "reason"}` for the first `MAX_FAILURES_KEPT` untagged
   rows, in `template_id` order.
8. Call `ctx.note_rows` for `"tagged"`, `"untagged"` and `"model_calls"`.
9. Log `f"tagged {tagged} of {n} templates, {calls} model call(s)"`. If `untagged`,
   also log `"run `logdash build --only tags --force` to retry untagged templates"`.

`src/logdash/analyses/__init__.py`: insert `"tags"` after `"templates"` in `BUILTIN`.

### Step 7: README

`README.md` configuration table: add two rows.

- `LOGDASH_TAG_MODEL` | `openai/gpt-4o-mini` | OpenRouter model for semantic template
  tags.
- `OPENROUTER_API_KEY` | (unset) | enables the `tags` stage. Without it the stage is
  skipped.

Add one sentence under the table: variables can also be set in `.env` in the working
directory, and the real environment wins. Add one line to "Other commands":
`uv run logdash build --only tags --force   # retry untagged templates (answers are memoised)`.

### Step 8: tests (see section 5)

### Step 9: test shield (commit together with step 8, or before it)

`tests/conftest.py`: add an autouse fixture:

```python
@pytest.fixture(autouse=True)
def _no_model_network(monkeypatch: pytest.MonkeyPatch, tmp_path_factory) -> None:
    """Never read the developer's .env or reach OpenRouter from a test."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("LOGDASH_TAG_MODEL", raising=False)
    monkeypatch.setattr(config, "DOTENV_PATH", tmp_path_factory.getbasetemp() / "no.env")
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("tests must not call OpenRouter")
    monkeypatch.setattr(openrouter, "post_json", refuse)
```

## 5. Tests

New `tests/unit/test_tags.py`.

Helper: `StubModel(answers: dict[str, list[Tag | Exception]] | None = None, default:
Tag | None = None)`. Its `tag(text)` pops the next answer for `text`. When it raises
an exception, the exception propagates. When there is no answer left, it returns
`default`, or a tag derived from the text (component `"other"`, summary
`f"summary of {text[:20]}"`). It records each `text` in `self.calls` (a list).

Integration helper: build `Templates()` and `Tags(model_factory=lambda s: stub,
sleep=lambda _: None)` into the session `built` cache. Use
`dataclasses.replace(built.settings, openrouter_api_key="sk-test",
tag_model=<unique per test>)`. A per-test fixture removes
`built.store.module_dir("tags_memo")` and `module_dir("tags")` before and after the
test.

| Test | Proves | AC |
|---|---|---|
| `test_build_writes_one_row_per_template` | `tags.parquet` has exactly the `template_id`s of `templates.parquet`, columns and dtypes `TAGS_SCHEMA`, all `tagged`, components in `COMPONENTS`, summaries with no `\n`; the manifest rows have `tagged == n`; `read_tags` returns it; `read_meta(...)["available"]` is true | AC1 |
| `test_payload_requests_strict_json_schema` | `OpenRouterModel("k", "m").payload("t")` has `response_format.type == "json_schema"`, `strict is True`, `schema == JSON_SCHEMA`, `provider.require_parameters is True`, `model == "m"` | AC2, AC3 |
| `test_tag_parses_reply_content` | with `post_json` monkeypatched to return a choices/message/content reply: `tag()` returns the `Tag`, and the captured headers carry `Bearer k` | AC2 |
| `test_tag_without_content_is_invalid` (parametrised: `{}`, `{"error": {...}}`, `{"choices": []}`, content `None`) | raises `InvalidAnswer` | AC2 |
| `test_parse_tag_rejects` (parametrised: non-JSON, list, missing key, `"true"` string, unknown component, empty summary) | `InvalidAnswer` | AC2 |
| `test_parse_tag_normalises` | `" Network "` gives `network`; a multi-line summary becomes one line; a 300-character summary is cut to `SUMMARY_CHARS` ending in `…`; extra keys are ignored | AC1, AC2 |
| `test_post_json_maps_errors` (parametrised, `urllib.request.urlopen` monkeypatched in the module, calling the **real** `post_json` imported at module load before the autouse patch) | 429 and 503 give `TransientError`; 401 and 400 give `FatalError`; `URLError` gives `FatalError`; `TimeoutError` gives `TransientError`; a non-JSON body gives `InvalidAnswer`; a valid body is decoded; no error message contains the key | AC2 |
| `test_invalid_answer_is_retried` | the stub answers `InvalidAnswer` then a tag, so the text is tagged and `calls == 2` | AC2 |
| `test_persistent_invalid_answer_is_untagged_and_build_succeeds` (integration) | the stub always raises `InvalidAnswer` for one text: `run_build` returns normally; that row has `tagged == False` and null tag columns; meta `untagged == 1` and `failures[0].reason` mentions `InvalidAnswer`; `calls` for it `== MAX_ATTEMPTS` | AC2 |
| `test_transient_backs_off` | `TransientError` then success; the recorded `sleep` values are `[BACKOFF_S]` | AC2 |
| `test_fatal_error_stops_calling` (`workers=1`) | `FatalError` on the first call: `outcome.calls == 1`, all texts failed, `outcome.aborted` set | AC2 |
| `test_unexpected_exception_is_untagged` | `RuntimeError` from the stub: the text failed, no retry, nothing raised | AC2 |
| `test_model_id_from_env_and_default` (in `tests/unit/test_config.py`) | `from_env({})` gives `DEFAULT_TAG_MODEL`; `LOGDASH_TAG_MODEL=x` gives `x`; an empty value gives the default | AC3 |
| `test_model_change_rebuilds_and_retags` (integration) | building again with another `tag_model` makes the stage rebuild and call the stub again (the memo key includes the model) | AC3, AC5 |
| `test_key_from_env_and_dotenv` (`test_config.py`, with `DOTENV_PATH` monkeypatched to a tmp `.env` and `os.environ` monkeypatched) | the key comes from `.env`; `os.environ` overrides `.env`; a missing `.env` is fine; `repr(Settings(...))` does not contain the key; `tagging_enabled` follows the key | AC4 |
| `test_read_dotenv_parsing` | comments, blank lines, `export `, single and double quotes, lines without `=` | AC4 |
| `test_no_key_skips_and_records_absence` (integration, the factory raises if called) | the build succeeds; the factory is not called; no `tags.parquet`; `read_tags(store) is None`; `read_meta(store)["available"] is False`; `stage_statuses` says `fresh` | AC4 |
| `test_adding_key_triggers_tagging` (integration) | build without a key, then with a key: the stage is rebuilt and tags are written; the key string appears in neither `manifest.json` nor `meta.json` | AC4 |
| `test_rebuild_with_same_texts_makes_no_calls` (integration) | after a first build, `run_build(..., only="tags", force=True)` leaves `len(stub.calls)` unchanged and `meta.calls == 0`, with the same tags | AC5 |
| `test_memo_hit_ignores_template_id` | `tag_templates` with a pre-filled memo makes 0 calls; duplicate texts are called once | AC5 |
| `test_memo_roundtrip_and_corrupt_file` | `save_memo` then `load_memo` returns the same tags; a garbage file returns `{}` and logs a line | AC5 |
| `test_dashboard_has_no_tags_page` | a registry with `Dummy`-like page and `Tags(factory that raises)`: `pages()` excludes `tags`; `create_app(built.store, reg)` registers no `/tags` page, and `navbar_links` and `home_layout` have no `/tags`; the factory is never called; `Tags().layout(store)` raises `RuntimeError`; `register_callbacks` returns `None` | AC6 |
| `test_reserved_memo_key` (in `test_registry.py`) | `Registry().add(<analysis with key "tags_memo">)` raises `DuplicateKeyError` | design |
| (existing) `test_cli_build_status_serve` | still passes with `tags` in the built-ins and no key, which also shows that the default CLI build skips quickly | AC4 |

AC7: the autouse shield makes any accidental network call fail. The gate (`pytest` at
90 %, `ruff`, `mypy src`) must pass.

## 6. Risks and open questions

1. **Serve's pre-build (open).** `logdash serve` runs `run_build` first unless
   `--no-build` is given. This plan treats that as a build: with a key and a stale
   stage, starting serve tags. The Dash app (layouts, callbacks) never calls the model.
   If AC6 means that serve must never call the model even through its pre-build, the
   fix is small: give `run_build` a `skip: set[str]` argument that `cmd_serve` sets to
   `{"tags"}`. Ask the reviewer or human.
2. **Default model ID (open).** `openai/gpt-4o-mini` was chosen because it is cheap and
   supports `json_schema` strict mode on OpenRouter. A human may prefer another. It is
   one constant plus the README row.
3. **Run time with a key.** About 281 calls on 4 workers take roughly 1 to 3 minutes,
   once. After that the memo makes rebuilds free.
4. **Partial failures are not retried automatically.** The stage is "fresh" after a
   run with untagged templates. This is deliberate, to avoid paying for calls on every
   build. The log line and README name the retry command.
5. **`.env` now also feeds `LOGDASH_*`.** This is harmless and documented. The real
   environment still wins.
6. **Provider quirks.** Some OpenRouter providers return 200 with an `error` body, or
   wrap the JSON in code fences despite strict mode. The first case is an
   `InvalidAnswer` and is retried. The second is not handled. If it shows up in
   practice, strip a surrounding code fence in `parse_tag`.
