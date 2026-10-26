### 1. Goal

The chat assistant gets its tool layer. It consists of a registry of tools in the OpenAI/OpenRouter `tools` format, a `Toolbox` that runs a tool by name and returns a small JSON-ready dict or a structured error, a UTC time-window helper with its rule as text, and the first two tools, `count_events` and `explorer_link`. No model is called and there is no UI.

Acceptance criteria (from the issue):
- [ ] `src/logdash/chat/tools.py` defines a tool registry. Each tool has a name, a one-line description and a JSON schema for its arguments. `tool_specs()` returns all of them in the OpenAI/OpenRouter `tools` format (`{"type": "function", "function": {"name", "description", "parameters"}}`). `Toolbox(store).call(name, arguments)` takes the arguments as a dict or a JSON string and returns a JSON-serialisable dict (`json.dumps(..., allow_nan=False)` succeeds). This story registers `count_events` and `explorer_link`.
- [ ] Bad input returns `{"error": {"code", "argument", "message", ...}}` and never raises: an unknown tool, arguments that are not a JSON object, an unknown argument name, a wrong type, an unknown host, service, level or template, a malformed or reversed time window, a window outside the data span, an out-of-range limit. Unknown hosts and services come with a short list of suggestions. A missing cache stage also returns an error.
- [ ] `src/logdash/chat/window.py` resolves time arguments (`start`, `end`, `last_minutes`) in UTC against the end of the data span, not the wall clock. It understands `now`, `today`, clock times such as `14:20` and ISO timestamps, rounds windows outwards to whole minutes and clips them to the data span. A constant holds the rule as one paragraph of text that the next story's system prompt can quote.
- [ ] `count_events` counts events for a window and optional host, service, level and template filters, with an optional `group_by` (host, service or level) capped at a constant number of groups. It reads the pre-aggregated tables when one of them covers the filters. Otherwise it reads `events`, always with an hour-partition pushdown and only for a window of at most a constant length (default 120 minutes). A longer window returns an error that names the limit. The result holds the total, the groups, the source it read and the resolved window.
- [ ] Every result that names a host, template or window carries Explorer deep links built with `page_url("/explorer", ...)` from the keys in `LINK_KEYS`. `explorer_link` validates its filters and returns such a link.
- [ ] Every SQL statement the tools run goes through one method that records it. A test runs every registered tool with representative arguments, records the statements, and fails if a statement on `events` lacks the `hour IN (...)` pushdown. The test is written so that the later stories only add their tools' cases.
- [ ] `count_events` and `explorer_link` run in under 300 ms on the real cache with their default arguments (`pytest -m slow`).
- [ ] Unit tests on the synthetic cache cover both tools, the window helper, every error path and the caps. Coverage stays at or above 90 %.

### 2. Depends on

None. #44 (Ready) and #47 call this interface. #56 and #57 add tools to it.

### 3. Context

- `src/logdash/cache/store.py:82` `CacheStore.connect()` returns an in-memory DuckDB connection with an `events` view over `events/hour=*/*.parquet` (hive partition column `hour`, `'00'`..`'23'`, string). It sets `TimeZone = 'UTC'` and raises `FileNotFoundError` when `events/` is missing. `store.path(key, name)`, `store.exists(key, name)`, `store.read_json(key, name)` and `store.read_parquet(key, name)` are the file helpers. A missing JSON file raises `FileNotFoundError` from `Path.read_text`.
- `src/logdash/ingest/normalize.py:7` `LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL", "UNKNOWN")`. `level` is never NULL. `host` and `program` can be NULL. `ts` is a naive `TIMESTAMP` in UTC. `hour` is `lpad(date_part('hour', ts))` (line 103). The data covers one day (real cache: 2023-05-21 00:01:10 to 23:59:07 UTC).
- `src/logdash/analyses/temporal.py:71` `write_rate_tables` writes `temporal/rate_60.parquet` (`bin TIMESTAMP, level, program, n BIGINT`, where a NULL `program` becomes `'(missing)'`) and `temporal/host_60.parquet` (`bin, host, n`, where a NULL `host` becomes `'(missing)'`). `bin` is `time_bucket(INTERVAL '60 seconds', ts)`, which gives whole UTC minutes. `MISSING = "(missing)"` is at line 43 and `KEY = "temporal"` at line 40.
- `src/logdash/analyses/temporal.py:733-736` writes `temporal/meta.json` with `ts_min`, `ts_max` (strings such as `"2023-05-21 00:01:10.665000"`, from `json.dumps(default=str)`), `services` and `hosts`. Both lists include `"(missing)"`. The real cache has 162 hosts plus `(missing)` and 40 services plus `(missing)`.
- `src/logdash/analyses/templates.py:202-217` writes `templates/templates.parquet` (`template_id`, `template`, ...; ids 1..281 on the real cache), `templates/assignments.parquet` (`masked_hash`, `template_id`), `templates/template_counts.parquet` (`template_id, bin, program, n`) and `templates/template_counts_host.parquet` (`template_id, bin, host, n`). Both count tables come from `events JOIN assignments USING (masked_hash)` with 1-minute bins and `coalesce(..., '(missing)')`.
- `src/logdash/analyses/explorer.py:39` `LINK_KEYS = ("template", "host", "service", "level", "start", "end", "request_id", "q")`. `hour_values(start, end)` at line 102 returns the hour partitions of a range shorter than 24 h, or `None`. `build_query` at line 171 shows the template filter as `masked_hash IN (SELECT masked_hash FROM read_parquet('<path>') WHERE template_id = ?)` with the path's quotes doubled. `merge_link` at line 121 shows how the Explorer reads link keys. `start` and `end` go into `Filters` as strings, and `end` is exclusive (`app/filters.py:114`). `link_from_query` is at line 355.
- `src/logdash/analyses/temporal.py:684` `explorer_href` formats link times as `dt.replace(microsecond=0).isoformat(sep=" ")`, so `YYYY-MM-DD HH:MM:SS`.
- `src/logdash/app/filters.py:175` `page_url(path, **params)` drops `None` and `""` values and urlencodes the rest in keyword order.
- `tests/conftest.py:44` `built` is the session cache with `events`. `build_with(built, Temporal(), Templates())` adds stages (pattern in `tests/unit/test_explorer.py:22`). `tests/synthetic.py`: data from 2023-05-21 00:01:10 UTC for 6 hours. It has six hosts (`HOSTS`, one with a `.xerces.lan` suffix that ingest strips), the services `neutron-openvswitch-agent`, `neutron-l3-agent`, `haproxy`, `libvirt`, `nova-api` and more, 12 malformed rows with a NULL host, and 600 ERROR lines of `ERROR_SERVICE` on `ERROR_HOST` from `truth.error_start`.
- `tests/benchmark/test_latency.py:53-79`: the `store` fixture on `./cache`, `measure(what, case, budget, fn)` and `CALLBACK_BUDGET_S = 0.3`.
- Interface #44 expects (comment on this issue and the #44 plan): `logdash.chat.tools.tool_specs()`, `Toolbox(store).call(name, arguments: str | Mapping[str, Any]) -> dict`, the error codes `unknown_tool` and `bad_arguments`, `WINDOW_RULE` in `logdash.chat.window`, and Explorer links as strings starting with `/explorer`. The #44 plan also uses `data_span(store)` from `logdash.chat.window` when it exists and returns UTC-aware `(ts_min, ts_max)`. This plan provides it.

Measured on the real cache (4 threads, best of 3): `store.connect()` 11 ms. Whole-day sums on `rate_60`, `host_60` and `template_counts_host` 1 to 5 ms. `events` with a 2 h pushdown: level and host grouped 4 ms, template join grouped by level 27 ms, host grouped by service over 3 partitions 19 ms.

### 4. Design

**Package.** New package `src/logdash/chat/` with `__init__.py`, `errors.py` (the error exception and codes), `window.py` (span and time rule) and `tools.py` (registry, `Toolbox`, catalog, JSON helper, the two tools). `errors.py` is separate so that `window.py` can raise tool errors without importing `tools.py`.

**Errors as an exception inside, a dict outside.** Validation code raises `ToolError(code, message, argument=..., hint=..., **extra)`. `Toolbox.call` catches it and returns `exc.result()`. `call` also turns `FileNotFoundError` into `unavailable`. Every other exception is a bug and propagates, as #43 decided and #44 relies on. The codes are `unknown_tool`, `bad_arguments`, `bad_argument`, `window_too_long` and `unavailable`, taken from the #43 notes. A window outside the span is a `bad_argument` on `start` or `end`, so the code list does not grow.

**Argument validation from the schema.** Each tool declares JSON-schema properties. A small validator (`check_arguments`) enforces the subset the tools use: `additionalProperties: false`, the types `string`, `integer` and `boolean`, `enum`, `minimum`, `maximum`, `maxLength` and `required`. The schema the model sees is then the same schema that is enforced. Rejected: the `jsonschema` package (a new dependency, and its messages are not written for a model) and hand-written checks in every tool (the checks would drift from the schema). A `null` value and a string that is empty after stripping count as "not given", because models often send `null` for optional arguments.

**Time windows.** `resolve_window` turns `start`, `end` and `last_minutes` into a `Window` with naive-UTC `start` and an exclusive `end`. Both are whole minutes, rounded outwards and clipped to the span. The span is `[floor_minute(ts_min), floor_minute(ts_max) + 1 min)`, and `now` is its end. With minute-aligned windows, `bin >= start AND bin < end` on the 1-minute tables and `ts >= start AND ts < end` on `events` select the same events, so every source gives the same count. Without `start` and `last_minutes`, the window starts at the beginning of the data. A whole-day question such as "how many errors today" then works from the aggregates. Rejected: a default of the last hour, which would quietly answer a different question than the one asked about "today".

**Source choice.** `count_events` collects the dimensions it needs (the given filters plus `group_by`) and takes the first entry of `AGGREGATES` whose columns cover them, in this order: `rate_60` (level, service), `host_60` (host), `template_counts` (template, service), `template_counts_host` (template, host). The template tables can only be chosen when a template filter is set, because any smaller set is covered earlier. So they never count "events that have a template" in place of "all events". If no table covers the dimensions, the tool reads `events` with `hour IN (...)` and the window bounds, and only if the window is at most `MAX_EVENTS_MINUTES = 120` minutes. Otherwise it returns `window_too_long`.

**All SQL in one place.** Aggregates are read with DuckDB (`read_parquet('<path>')`) and not with polars, so that every query of every tool goes through `Toolbox.sql`. That method appends the statement to `Toolbox.statements`, which #47 can show in its trace. The test also wraps the connection in a recording proxy, so a statement that bypasses `Toolbox.sql` shows up as a mismatch. Rejected: polars for the aggregates. It would be equally fast, but then there would be two query paths and only one of them recorded.

**Links.** `explorer_url(window, selection, request_id=None, text=None)` maps values onto `LINK_KEYS` (`text` becomes `q`) and calls `page_url("/explorer", ...)`. Times are `YYYY-MM-DD HH:MM:SS`, and `end` is exclusive like the Explorer's filter. A group whose value is `(missing)` gets no link, because the Explorer cannot filter on NULL.

**Catalog.** Hosts and services come from `temporal/meta.json` without `(missing)`. They are matched without regard to case, and the result uses the data's spelling. An unknown value returns up to `MAX_SUGGESTIONS = 5` known values that contain the text (case-insensitive), topped up with `difflib.get_close_matches`. Levels are matched against `LEVELS` without regard to case. Template ids are checked against `templates/templates.parquet`, which is read only when a template is given.

**One Toolbox per question.** `Toolbox` opens its DuckDB connection on the first query and caches the span and catalog. It is not thread-safe. #44 builds one per question and #47 one per request. `close()` closes a connection the toolbox opened itself.

### 5. Steps

#### Commit 1: errors and time windows (steps 1–3)

1. New `src/logdash/chat/__init__.py`, which holds only a docstring: `"""Bounded read-only query tools for the chat assistant (issue #43)."""`

2. New `src/logdash/chat/errors.py`:
   ```python
   UNKNOWN_TOOL = "unknown_tool"
   BAD_ARGUMENTS = "bad_arguments"   # arguments are not a JSON object
   BAD_ARGUMENT = "bad_argument"     # one argument is wrong
   WINDOW_TOO_LONG = "window_too_long"
   UNAVAILABLE = "unavailable"       # a cache stage is missing

   class ToolError(Exception):
       """Bad tool input; ``Toolbox.call`` returns ``result()`` instead of raising."""

       def __init__(self, code: str, message: str, *, argument: str | None = None,
                    hint: str | None = None, **extra: Any) -> None: ...
       def result(self) -> dict[str, Any]:
           # {"error": {"code": code, "argument": argument, "message": message,
           #            "hint": hint (only when not None), **extra}}

   def is_error(result: Mapping[str, Any]) -> bool:
       """Whether a tool result is an error result."""
       return isinstance(result.get("error"), dict)
   ```
   `super().__init__(message)` so that `str(exc)` is the message. Store `code`, `message`, `argument`, `hint` and `extra` as attributes.

3. New `src/logdash/chat/window.py`:
   ```python
   WINDOW_RULE = (
       "All times are UTC. Relative times count from the end of the data, not from the "
       "current clock: now is the end of the data, and today is 00:00 UTC on the day the "
       "data ends. A clock time such as 14:20 is that time on the day the data ends. ISO "
       "timestamps such as 2023-05-21 14:20 are read as UTC, and ones with an offset such as "
       "2023-05-21T16:20+02:00 are converted to UTC. last_minutes=N is the N minutes before "
       "end; end defaults to now and start to the beginning of the data, and start and "
       "last_minutes cannot be combined. Every window is widened to whole minutes, cut to "
       "the data span, and excludes its end."
   )
   TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
   TIME_HINT = "use now, today, a clock time such as 14:20, or an ISO timestamp such as 2023-05-21 14:20 (UTC)"
   _CLOCK = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$")
   MINUTE = timedelta(minutes=1)

   def data_span(store: CacheStore) -> tuple[datetime, datetime]:
       """First and last event time from ``temporal/meta.json`` as UTC-aware datetimes."""
   ```
   - `data_span` reads `store.read_json(temporal.KEY, "meta.json")` (import `KEY` from `logdash.analyses.temporal`). A missing file raises `FileNotFoundError`, which is passed on. For each of `ts_min` and `ts_max`, if the value is not a non-empty string or `datetime.fromisoformat` raises `ValueError`, raise `FileNotFoundError("temporal/meta.json has no data span")`. Otherwise parse it and return it with `.replace(tzinfo=UTC)` if naive or `.astimezone(UTC)` if aware.
   - `format_time(dt: datetime) -> str`: `dt.strftime(TIME_FORMAT)`.
   - `floor_minute(dt)`: `dt.replace(second=0, microsecond=0)`. `ceil_minute(dt)`: `dt` if it is already a whole minute, otherwise `floor_minute(dt) + MINUTE`.
   - ```python
     @dataclass(frozen=True)
     class Window:
         start: datetime   # naive UTC, whole minute, inclusive
         end: datetime     # naive UTC, whole minute, exclusive
         clipped: bool = False
         @property
         def minutes(self) -> int: return int((self.end - self.start).total_seconds() // 60)
         def as_dict(self) -> dict[str, Any]:
             return {"start": format_time(self.start), "end": format_time(self.end),
                     "minutes": self.minutes, "clipped": self.clipped}
     ```
   - `parse_time_value(text: str, *, argument: str, day: date, now: datetime) -> datetime` returns naive UTC. Work on `value = text.strip()` and `low = value.lower()`:
     1. `low == "now"`: `now`.
     2. `low == "today"`: `datetime.combine(day, time())`.
     3. `_CLOCK.match(value)`: `datetime.combine(day, time(h, m, s or 0))`.
     4. Otherwise `datetime.fromisoformat(value)`. If the result is aware, use `.astimezone(UTC).replace(tzinfo=None)`. On `ValueError`, raise `ToolError(BAD_ARGUMENT, f"cannot read {argument} '{text}' as a time", argument=argument, hint=TIME_HINT)`.
   - ```python
     def resolve_window(span: tuple[datetime, datetime], *, start: str | None = None,
                        end: str | None = None, last_minutes: int | None = None) -> Window:
     ```
     `span` is what `data_span` returns. Convert both values to naive UTC (`.astimezone(UTC).replace(tzinfo=None)` when aware). Then `first = floor_minute(ts_min)`, `stop = floor_minute(ts_max) + MINUTE`, `day = ts_max.date()` and `now = stop`.
     1. If `start is not None and last_minutes is not None`: raise `ToolError(BAD_ARGUMENT, "give start or last_minutes, not both", argument="last_minutes", hint="drop start, or give start and end")`.
     2. `hi = parse_time_value(end, argument="end", ...) if end is not None else now`.
     3. `lo = hi - timedelta(minutes=last_minutes)` if `last_minutes is not None`, else `parse_time_value(start, argument="start", ...)` if `start is not None`, else `first`.
     4. If `hi <= lo`: raise `ToolError(BAD_ARGUMENT, f"the window end {format_time(hi)} is not after its start {format_time(lo)}", argument="end")`.
     5. `lo, hi = floor_minute(lo), ceil_minute(hi)`.
     6. If `hi <= first or lo >= stop`: raise `ToolError(BAD_ARGUMENT, f"the window {format_time(lo)} to {format_time(hi)} lies outside the data, which runs from {format_time(first)} to {format_time(stop)} UTC", argument="end" if hi <= first else "start", hint="choose a window inside the data span")`.
     7. Return `Window(max(lo, first), min(hi, stop), clipped=lo < first or hi > stop)`.
     The function does no range check on `last_minutes`. The schema does that (step 4).

#### Commit 2: registry, Toolbox and the two tools (steps 4–6)

4. New `src/logdash/chat/tools.py`, framework part. Constants:
   ```python
   MAX_EVENTS_MINUTES = 120   # longest window a query on events may cover
   MAX_LAST_MINUTES = 1440
   MAX_GROUPS = 20            # groups returned by count_events
   MAX_SUGGESTIONS = 5
   MAX_TEXT_LENGTH = 200      # request_id and text arguments
   TEMPLATE_CHARS = 200       # template text in results
   EXPLORER = "/explorer"
   ```
   - Registry:
     ```python
     ToolFn = Callable[["Toolbox", dict[str, Any]], dict[str, Any]]

     @dataclass(frozen=True)
     class Tool:
         name: str
         description: str
         parameters: dict[str, Any]
         run: ToolFn

     TOOLS: dict[str, Tool] = {}

     def register_tool(name: str, description: str, properties: dict[str, dict[str, Any]],
                       required: Sequence[str] = ()) -> Callable[[ToolFn], ToolFn]:
     ```
     The decorator builds `parameters = {"type": "object", "properties": properties, "additionalProperties": False}` and adds `"required": list(required)` only when it is not empty. It raises `ValueError` if the name is already registered, the description contains `"\n"`, or a property type is not in `("string", "integer", "boolean")`. Then it stores `Tool(...)` and returns the function unchanged.
   - `tool_specs() -> list[dict[str, Any]]`: `[{"type": "function", "function": {"name": t.name, "description": t.description, "parameters": t.parameters}} for t in TOOLS.values()]`, in registration order. Return deep copies (`copy.deepcopy`), so that a caller cannot change the registry.
   - Shared schema fragments that later stories reuse:
     ```python
     WINDOW_PROPERTIES = {
         "start": {"type": "string", "description": "Window start: now, today, a UTC clock time such as 14:20, or an ISO timestamp. Default: the start of the data."},
         "end": {"type": "string", "description": "Window end (exclusive), in the same forms as start. Default: now, the end of the data."},
         "last_minutes": {"type": "integer", "minimum": 1, "maximum": MAX_LAST_MINUTES, "description": "The N minutes before end. Do not combine with start."},
     }
     FILTER_PROPERTIES = {
         "host": {"type": "string", "description": "One host name, spelled as in the data."},
         "service": {"type": "string", "description": "One service (program name) such as nova-compute, spelled as in the data."},
         "level": {"type": "string", "description": "One log level: DEBUG, INFO, WARNING, ERROR, CRITICAL or UNKNOWN."},
         "template": {"type": "integer", "minimum": 0, "description": "One template id."},
     }
     ```
   - `parse_arguments(arguments: object) -> dict[str, Any]`. A `Mapping` becomes `dict(arguments)`. A `str` is stripped; an empty string gives `{}`; otherwise `json.loads`. `json.JSONDecodeError` raises `ToolError(BAD_ARGUMENTS, f"arguments are not valid JSON ({exc.msg})", hint=ARGS_HINT)`. A decoded value that is not a dict, and any other input type, raise `ToolError(BAD_ARGUMENTS, "arguments must be a JSON object", hint=ARGS_HINT)`. `ARGS_HINT = 'send the arguments as one JSON object, for example {"last_minutes": 60}'`.
   - `check_arguments(parameters: dict[str, Any], raw: dict[str, Any]) -> dict[str, Any]`, for each `name, value` in `raw.items()`:
     - If the name is not in `properties`: `ToolError(BAD_ARGUMENT, f"unknown argument '{name}'", argument=name, suggestions=sorted(properties))`.
     - A `None` value is skipped.
     - `string`: anything but `str` is a type error. Strip the value and skip it if it is now empty. `maxLength` gives `f"{name} is longer than {n} characters"`. `enum` gives `f"{name} must be one of {', '.join(enum)}, not '{value}'"` with `suggestions=list(enum)`.
     - `integer`: a `bool`, or anything that is neither an `int` nor a `float` with `is_integer()`, is a type error. Then `value = int(value)`. If it is below `minimum` or above `maximum`: `f"{name} must be between {minimum} and {maximum}, not {value}"`, or `f"{name} must be at least {minimum}, not {value}"` when there is no maximum.
     - `boolean`: anything but `bool` is a type error.
     - The type error is `ToolError(BAD_ARGUMENT, f"{name} must be {EXPECTED[kind]}, not {json_type(value)}", argument=name)`, with `EXPECTED = {"string": "a string", "integer": "an integer", "boolean": "true or false"}`. `json_type` maps `bool` to `"a boolean"`, `int` or `float` to `"a number"`, `str` to `"a string"`, `list` or `tuple` to `"an array"`, `dict` to `"an object"`, and anything else to `type(value).__name__`.
     - After the loop, each name in `required` that is not in the output gives `ToolError(BAD_ARGUMENT, f"missing argument '{name}'", argument=name)`.
   - `to_json(value: Any) -> Any`: `None`, `bool`, `int` and `str` are returned as they are. A `float` becomes `None` if it is not `math.isfinite`. A `datetime` becomes `format_time(value)` and a `date` its `isoformat()`. A `Mapping` becomes `{str(k): to_json(v)}`, a `list` or `tuple` a list, and a `numpy.generic` `to_json(value.item())`. Anything else raises `TypeError(f"not JSON-ready: {type(value).__name__}")`, because that is a bug.
   - `require(store: CacheStore, key: str, name: str) -> Path`: `store.path(key, name)` if it exists, otherwise `ToolError(UNAVAILABLE, f"the cache has no {key}/{name}; run `logdash build`")`.
   - ```python
     @dataclass(frozen=True)
     class Selection:
         host: str | None = None
         service: str | None = None
         level: str | None = None
         template: int | None = None
         def dimensions(self) -> set[str]: ...   # names of the fields that are not None
         def as_dict(self) -> dict[str, Any]: ...  # the non-None fields, in field order
     ```
   - `Catalog(store)`, all lazy through `functools.cached_property`:
     - `meta`: `store.read_json(temporal.KEY, "meta.json")`.
     - `hosts` and `services`: sorted tuples of `meta["hosts"]` and `meta["services"]` without `temporal.MISSING`.
     - `templates`: `dict[int, str]` from `pl.read_parquet(require(store, "templates", "templates.parquet"), columns=["template_id", "template"])`.
     - `host(value: str) -> str` and `service(value: str) -> str` call `_known(argument, value, known)`. It builds `{k.lower(): k for k in known}` and returns the match for `value.lower()`. Otherwise it raises `ToolError(BAD_ARGUMENT, f"unknown {argument} '{value}'", argument=argument, hint=hint, suggestions=suggest(value, known))`. The hint is `f"the data has {len(known)} {argument}s; pick one of the suggestions or check the spelling"` when there are suggestions, and `f"the data has {len(known)} {argument}s and none looks like '{value}'"` when there are none.
     - `suggest(value: str, known: Sequence[str]) -> list[str]` (module function): the known values that contain `value.lower()` (compared in lower case), in sorted order. If there are fewer than `MAX_SUGGESTIONS`, add `difflib.get_close_matches(value, known, n=MAX_SUGGESTIONS, cutoff=0.6)` that are not already listed. Cut the list to `MAX_SUGGESTIONS`.
     - `level(value: str) -> str`: `value.upper()` if it is in `LEVELS`, otherwise `ToolError(BAD_ARGUMENT, f"unknown level '{value}'", argument="level", suggestions=list(LEVELS))`.
     - `template(value: int) -> int`: the value if it is in `templates`, otherwise `ToolError(BAD_ARGUMENT, f"unknown template {value}", argument="template", hint=f"template ids run from {min(ids)} to {max(ids)}")`.
   - `class Toolbox`:
     ```python
     def __init__(self, store: CacheStore, con: duckdb.DuckDBPyConnection | None = None) -> None
     statements: list[str]          # every SQL statement run, in order
     def call(self, name: str, arguments: str | Mapping[str, Any]) -> dict[str, Any]
     def sql(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]
     def parquet(self, key: str, name: str) -> str
     span: cached_property -> tuple[datetime, datetime]   # data_span(self.store)
     catalog: cached_property -> Catalog
     def window(self, args: Mapping[str, Any]) -> Window
     def selection(self, args: Mapping[str, Any]) -> Selection
     def events_clause(self, window: Window) -> tuple[str, list[Any]]
     def close(self) -> None
     ```
     - `call`: if `name` is not in `TOOLS`, return `ToolError(UNKNOWN_TOOL, f"unknown tool '{name}'", suggestions=list(TOOLS)).result()`. Otherwise `args = check_arguments(tool.parameters, parse_arguments(arguments))`, then `return to_json(tool.run(self, args))`. `except ToolError as exc: return to_json(exc.result())`. `except FileNotFoundError as exc: return ToolError(UNAVAILABLE, missing_message(self.store, exc)).result()`. `missing_message` gives `f"the cache has no {rel}; run `logdash build`"`, where `rel` is `exc.filename` relative to `store.root` (or only its file name if it is not under the root). When `exc.filename` is `None`, it gives `f"a cache stage is missing ({exc}); run `logdash build`"`.
     - `sql`: append `sql` to `self.statements`. If there is no connection yet, raise `ToolError(UNAVAILABLE, "the cache has no events; run `logdash build`")` when `self.store.events_dir` is missing, and otherwise open `self.store.connect()`. Return `con.execute(sql, list(params)).fetchall()`. This is the only place that runs SQL.
     - `parquet(key, name)`: `str(require(self.store, key, name)).replace("'", "''")`, for use inside `read_parquet('...')`.
     - `window(args)`: `resolve_window(self.span, start=args.get("start"), end=args.get("end"), last_minutes=args.get("last_minutes"))`.
     - `selection(args)`: run each given filter through the catalog (`host`, `service`, `level`, `template`) and return a `Selection`.
     - `events_clause(window)`: `hours = hour_values(window.start, window.end)`. If `hours is None` or `window.minutes > MAX_EVENTS_MINUTES`, raise `ValueError("events window too long")`, because callers check first and this is a bug guard. Return `(f"hour IN ({', '.join('?' for _ in hours)}) AND ts >= ? AND ts < ?", [*hours, window.start, window.end])`.
     - `close()`: close the connection only if the toolbox opened it, then set it to `None`.
   - `explorer_url(window: Window, selection: Selection, *, request_id: str | None = None, text: str | None = None) -> str`: build `values = {"template": str(t) if t is not None else None, "host": ..., "service": ..., "level": ..., "start": format_time(window.start), "end": format_time(window.end), "request_id": request_id, "q": text}` and return `page_url(EXPLORER, **{k: values.get(k) for k in LINK_KEYS})`.

5. `count_events` in `tools.py`:
   ```python
   @dataclass(frozen=True)
   class Source:
       key: str
       file: str
       columns: dict[str, str]   # dimension -> column
       @property
       def label(self) -> str: return f"{self.key}/{self.file}"

   AGGREGATES: tuple[Source, ...] = (
       Source("temporal", "rate_60.parquet", {"level": "level", "service": "program"}),
       Source("temporal", "host_60.parquet", {"host": "host"}),
       Source("templates", "template_counts.parquet", {"template": "template_id", "service": "program"}),
       Source("templates", "template_counts_host.parquet", {"template": "template_id", "host": "host"}),
   )
   EVENTS_FILTERS = {"host": "host = ?", "service": "program = ?", "level": "level = ?"}
   EVENTS_GROUPS = {"host": f"coalesce(host, '{MISSING}')", "service": f"coalesce(program, '{MISSING}')", "level": "level"}
   GROUP_BY = ("host", "service", "level")

   def pick_source(needed: set[str]) -> Source | None:
       return next((s for s in AGGREGATES if needed <= s.columns.keys()), None)
   ```
   Registration: `register_tool("count_events", "Count log events in a time window, optionally filtered by host, service, level and template and split by host, service or level.", {**WINDOW_PROPERTIES, **FILTER_PROPERTIES, "group_by": {"type": "string", "enum": list(GROUP_BY), "description": f"Split the count by host, service or level; at most {MAX_GROUPS} groups, largest first."}})`.

   Body of `count_events(box, args)`:
   1. `window = box.window(args)`, `sel = box.selection(args)` and `group = args.get("group_by")`.
   2. `needed = sel.dimensions() | ({group} if group else set())`, then `source = pick_source(needed)`.
   3. Aggregate path: `path = box.parquet(source.key, source.file)`, `where = ["bin >= ?", "bin < ?"]` and `params = [window.start, window.end]`. For each dimension in `("host", "service", "level", "template")` whose value is set, add `f"{source.columns[dim]} = ?"` and the value. With a group: `SELECT {source.columns[group]} AS grp, sum(n)::BIGINT AS n FROM read_parquet('{path}') WHERE {' AND '.join(where)} GROUP BY 1`. Without one: `SELECT sum(n)::BIGINT AS n FROM read_parquet('{path}') WHERE ...`. `label = source.label`.
   4. Events path (`source is None`): if `window.minutes > MAX_EVENTS_MINUTES`, raise `ToolError(WINDOW_TOO_LONG, f"counting by {', '.join(sorted(needed))} together needs the events table, which allows a window of at most {MAX_EVENTS_MINUTES} minutes; this window has {window.minutes}", argument="start", hint=f"narrow the window, for example last_minutes={MAX_EVENTS_MINUTES}, or drop a filter or the group_by", limit=MAX_EVENTS_MINUTES, minutes=window.minutes)`. Otherwise `clause, params = box.events_clause(window)` and `where = [clause]`. Add `EVENTS_FILTERS[dim]` and the value for host, service and level. For a template, add `f"masked_hash IN (SELECT masked_hash FROM read_parquet('{box.parquet('templates', 'assignments.parquet')}') WHERE template_id = ?)"` and the id. With a group: `SELECT {EVENTS_GROUPS[group]} AS grp, count(*) AS n FROM events WHERE ... GROUP BY 1`. Without one: `SELECT count(*) AS n FROM events WHERE ...`. `label = "events"`.
   5. `rows = box.sql(sql, params)`. Without a group, `total = int(rows[0][0] or 0)`. With a group, `pairs = sorted(((str(g), int(n)) for g, n in rows if n), key=lambda p: (-p[1], p[0]))` and `total = sum(n for _, n in pairs)`.
   6. Result, keys in this order:
      ```python
      {"total": total, "window": window.as_dict(), "filters": sel.as_dict(), "source": label,
       "link": explorer_url(window, sel)}
      ```
      With a template, add `"template_text": truncate(box.catalog.templates[sel.template], TEMPLATE_CHARS)`, where `truncate(text, n)` returns `text` if `len(text) <= n` and otherwise `text[: n - 1] + "…"`. With a group, add `"group_by": group`, `"groups": [...]` (the first `MAX_GROUPS` pairs, each `{group: value, "count": n, "link": explorer_url(window, replace(sel, **{group: value}))}`, with no `"link"` key when `value == MISSING`), `"groups_total": len(pairs)` and `"groups_truncated": len(pairs) > MAX_GROUPS`.

6. `explorer_link` in `tools.py`: `register_tool("explorer_link", "Build a link to the log explorer page that shows the raw lines for a window and filters.", {**WINDOW_PROPERTIES, **FILTER_PROPERTIES, "request_id": {"type": "string", "maxLength": MAX_TEXT_LENGTH, "description": "One request id, such as req-…"}, "text": {"type": "string", "maxLength": MAX_TEXT_LENGTH, "description": "Case-insensitive text the payload must contain."}})`. Body: `window = box.window(args)` and `sel = box.selection(args)`. Return `{"link": explorer_url(window, sel, request_id=args.get("request_id"), text=args.get("text")), "window": window.as_dict(), "filters": {**sel.as_dict(), **{k: args[k] for k in ("request_id", "text") if k in args}}}`. The tool runs no SQL. It always includes the resolved window, which is the whole span by default, so the link shows what the window rule says.

#### Commit 3: latency benchmark (step 7)

7. `tests/benchmark/test_latency.py`: add `test_chat_tools(store)` before `test_zz_print_table`. `host = next(h for h in store.read_json("temporal", "meta.json")["hosts"] if h != "(missing)")`. Cases (name, tool, args):
   - `"count_events, defaults"`: `count_events`, `{}`
   - `"count_events, by host"`: `count_events`, `{"group_by": "host"}`
   - `"count_events, ERROR on a host, 60 min"`: `count_events`, `{"level": "ERROR", "host": host, "last_minutes": 60}` (events path)
   - `"count_events, template 1 by level, 120 min"`: `count_events`, `{"template": 1, "group_by": "level", "last_minutes": 120}` (events path, the largest template)
   - `"explorer_link, defaults"`: `explorer_link`, `{}`

   Each case is `measure("chat.Toolbox.call", case, CALLBACK_BUDGET_S, run)`, where `run` builds a new `Toolbox(store)` (so the connection cost is in every timing), calls the tool, closes the toolbox in `finally`, asserts `"error" not in result` and returns the result.

#### Commit 4: ADR and plan file (steps 8–9)

8. `adrs/ADR<nnn>-chat-query-tools.md`. `<nnn>` is one more than the highest ADR number on `main` when you commit: ADR004 today, but #44 and #58 also add or renumber ADRs, so check. Sections:
   - **Status**: accepted (issue #55, parent #43). Binds #56, #57, #44 and #47.
   - **Context**: the chat model may only read the logs through a fixed set of tools. Questions name hosts, services and times loosely, and the `events` table is 38.8 M rows.
   - **Decision**: (1) tools live in `logdash.chat.tools`, registered with `register_tool` with a JSON schema that `check_arguments` enforces, and there is no free-form SQL; (2) bad input returns `{"error": {"code", "argument", "message", "hint"?, ...}}` with the codes `unknown_tool`, `bad_arguments`, `bad_argument`, `window_too_long` and `unavailable`, while other exceptions are bugs and propagate; (3) time arguments follow `WINDOW_RULE` (UTC, relative to the end of the data, whole minutes, clipped, exclusive end); (4) tools read pre-aggregated tables first, and a query on `events` always has `hour IN (...)` and a window of at most `MAX_EVENTS_MINUTES` = 120 (a payload text search at most 60, #56); (5) all SQL goes through `Toolbox.sql`, which records it, and every new tool adds cases to `PUSHDOWN_CASES` in `tests/unit/test_chat_tools.py`; (6) results pass through `to_json`, and links are built with `explorer_url` from `LINK_KEYS`.
   - **Consequences**: whole-day counts cost a few milliseconds. Some filter combinations (host with level or service) only work for windows up to 2 h, and the model gets an error that names the limit. Adding a tool means adding its pushdown cases.
9. `plans/2026-10-26-chat-query-tools-v1.md`: this plan, verbatim.

### 6. Tests

New `tests/unit/test_chat_window.py`. These are pure tests with `SPAN = (datetime(2023, 5, 21, 0, 1, 10, 665000, tzinfo=UTC), datetime(2023, 5, 21, 23, 59, 7, 815000, tzinfo=UTC))` and no store.
- `test_default_window_is_whole_span`: `resolve_window(SPAN)` gives `00:01:00` to `2023-05-22 00:00:00`, with `clipped` False and `minutes == 1439` (criterion 3).
- `test_now_and_last_minutes`: `last_minutes=60` gives `23:00` to `00:00` on the next day. `end="12:00", last_minutes=30` gives `11:30` to `12:00`. `end="now"` equals the default end (criterion 3).
- `test_today_is_clipped`: `start="today"` gives start `00:01:00` with `clipped` True (criterion 3).
- `test_clock_times`: `"14:20"`, `"4:05"` and `"14:20:30"` (start floors to `14:20`), and `" NOW "` matched without regard to case (criterion 3).
- `test_iso_timestamps`: naive `"2023-05-21 14:20"`, `"2023-05-21T14:20:00Z"` and `"2023-05-21T16:20+02:00"` all give `14:20` (criterion 3).
- `test_rounds_outwards`: start `10:00:30` and end `10:05:01` give `10:00` to `10:06` (criterion 3).
- `test_clips_end`: `start="23:30", end="2023-05-22 02:00"` gives end `2023-05-22 00:00` with `clipped` True (criterion 3).
- `test_window_errors` (parametrised): `start` with `last_minutes`, giving argument `last_minutes`; `start="yesterday"` and `end="25:00"`, giving `bad_argument` on that argument with `hint`; a reversed window (`start="12:00", end="11:00"`) and an empty one (`start="12:00", end="12:00"`), giving argument `end`; a window after the data (`start="2023-05-23 00:00", end="2023-05-23 01:00"`), giving argument `start`; a window before the data (`end="2023-05-20 10:00", last_minutes=10`), giving argument `end`. Each asserts `exc.result()["error"]["code"] == "bad_argument"` (criteria 2 and 3).
- `test_window_rule_is_one_paragraph`: `"\n" not in WINDOW_RULE`, and the rule contains `"UTC"`, `"now"`, `"today"`, `"14:20"`, `"last_minutes"` and `"whole minutes"` (criterion 3).
- `test_data_span`: a `CacheStore(tmp_path)` with `temporal/meta.json` written by `store.write_json` gives UTC-aware values. A missing file raises `FileNotFoundError`, and so does `{"ts_min": None, "ts_max": "None"}`.

New `tests/unit/test_chat_tools.py`. The module fixture `box_store` runs `build_with(built, Temporal(), Templates())` and returns `built.store`. The `box` fixture yields a `Toolbox(store)` and closes it afterwards. Helper `truth(store, sql, params)` runs SQL on its own `store.connect()` for expected values. Constants come from `tests.synthetic`: `HOST = strip_host_domain(SPIKE_HOST)`, `ERROR_HOST` (stripped), `ERROR_SERVICE[0]`, and `TEMPLATE`, the smallest id in `templates.parquet`.
- `test_specs_format`: the names are `{"count_events", "explorer_link"}` and each spec has `type == "function"`, a one-line description and `parameters["type"] == "object"` with `additionalProperties` False. `tool_specs()` returns copies: changing one does not change `TOOLS` (criterion 1).
- `test_register_tool_rejects`: a duplicate name, a description with a newline and a property type `"array"` each raise `ValueError` (criterion 1).
- `test_call_accepts_dict_string_and_empty`: `call("count_events", {})`, `call("count_events", "{}")` and `call("count_events", "")` give equal results (criterion 1).
- `test_total_matches_events`: the default total equals `SELECT count(*) FROM events`, with source `temporal/rate_60.parquet` and the window over the whole span (criterion 4).
- `test_group_by_level`: the level groups sum to the total and are sorted by count, largest first. `level="error"` gives `filters["level"] == "ERROR"` and equals the SQL count of ERROR lines (criteria 4 and 2).
- `test_sources`: host gives `temporal/host_60.parquet`, template gives `templates/template_counts.parquet`, and template with host gives `templates/template_counts_host.parquet`. Each total equals the matching SQL on `events` (template through the `assignments.parquet` join). `template_text` is present and at most `TEMPLATE_CHARS` long (criterion 4).
- `test_aggregates_match_events` (parametrised over `{"level": "INFO"}`, `{"service": ERROR_SERVICE}`, `{"host": HOST, "group_by": "host"}`, `{"template": TEMPLATE, "group_by": "service"}` and `{"template": TEMPLATE, "host": HOST}`, each with `start="02:00", end="03:00"`): the result with the real `AGGREGATES` and the result with `monkeypatch.setattr(tools, "AGGREGATES", ())` have the same `total` and `groups`, and the second has source `events`. This proves that minute-aligned windows count the same rows from every source (criterion 4, Risks).
- `test_events_path`: `level="ERROR", host=ERROR_HOST` with a 60-minute window around `truth.error_start` gives source `events` and a total equal to the SQL count (at least 600). The same filters over the whole span give `window_too_long` with `limit == 120`, argument `start` and the text "120" in the message (criterion 4).
- `test_group_cap`: with `monkeypatch.setattr(tools, "MAX_GROUPS", 2)` and `group_by="host"`, there are 2 groups, `groups_truncated` is True and `groups_total` equals the SQL `count(DISTINCT coalesce(host, '(missing)'))`. A `(missing)` group has no `link` (criterion 4).
- `test_links`: for the result link and every group link, the URL starts with `/explorer?`, every key from `parse_qs` is in `LINK_KEYS`, and `merge_link(Filters(), link_from_query({k: v[0] for ...}))` gives `start` and `end` that `parse_time` maps to `window.start` and `window.end`, plus the host, level and template that were asked for (criterion 5).
- `test_explorer_link`: all arguments, including `request_id` and `text="Error"` (becomes `q=Error`), give a link with those keys. An unknown host returns an error with suggestions. `{}` gives a link with the whole span (criterion 5).
- `test_errors` (parametrised, each asserting the code, the argument and a non-empty message, and that nothing raises):
  - `("nope", {})` gives `unknown_tool`, with `count_events` among the suggestions.
  - `"{not json"` and `"[1, 2]"` give `bad_arguments`.
  - `{"hosts": "x"}` gives `bad_argument` on `hosts`, with `host` among the suggestions.
  - `{"last_minutes": "60"}`, `{"last_minutes": True}` and `{"template": 1.5}` give type errors.
  - `{"last_minutes": 0}` and `{"last_minutes": 5000}` give range errors, and `{"group_by": "program"}` gives an enum error.
  - `{"host": "eselda"}` gives suggestions that are all synthetic hosts, at most 5.
  - `{"service": "neutron"}` gives suggestions that include `neutron-l3-agent` and `neutron-openvswitch-agent`.
  - `{"host": "zzzz"}` gives empty suggestions and the "none looks like" hint.
  - `{"level": "fatal"}` gives `LEVELS` as suggestions, and `{"template": 99999}` gives an id range hint.
  - Malformed, reversed and outside-span windows, through `call`.
  - `explorer_link` with `text` of 201 characters gives a `maxLength` error.

  This covers criterion 2.
- `test_check_arguments_boolean_and_required`: `check_arguments` on a hand-made schema with a `boolean` and a `required` property accepts `True`, rejects `"yes"`, and reports a missing required argument. Integral floats become `int` (criterion 2).
- `test_unavailable`: three cases (criterion 2).
  - An empty `CacheStore(tmp_path)` gives `unavailable` from `count_events` and `explorer_link`, with no absolute path in the message.
  - With only `temporal/` copied (`shutil.copytree(store.module_dir("temporal"), ...)`), `{"template": TEMPLATE}` gives `unavailable` naming `templates/templates.parquet`.
  - With `temporal/` and `templates/` copied but no `events/`, `{"level": "ERROR", "host": HOST, "last_minutes": 30}` gives `unavailable` naming events.
- `test_results_are_json`: every successful and error result in this module passes `json.dumps(result, allow_nan=False)` (a helper `assert_json` used in the tests above). `to_json` maps `datetime`, `nan` (to `None`), `np.int64(3)` (to `3`) and tuples correctly, and raises `TypeError` on `object()` (criterion 1).
- `test_events_clause_guard`: `box.events_clause` raises `ValueError` for a 3-hour `Window` (Risks).
- `test_close`: after a call that ran SQL, `close()` closes the toolbox's own connection, and a toolbox given a `con` does not close it.
- **Pushdown test** (criterion 6):
  ```python
  # Add the cases of every new tool here; test_every_tool_has_cases fails otherwise.
  PUSHDOWN_CASES: dict[str, list[dict[str, Any]]] = {
      "count_events": [{}, {"group_by": "service"}, {"host": HOST, "group_by": "host"},
                       {"template": TEMPLATE}, {"template": TEMPLATE, "host": HOST},
                       {"level": "ERROR", "host": HOST, "start": "02:00", "end": "03:00"},
                       {"template": TEMPLATE, "group_by": "level", "last_minutes": 120},
                       {"service": ERROR_SERVICE, "host": HOST, "last_minutes": 30}],
      "explorer_link": [{}, {"host": HOST, "text": "error", "last_minutes": 60}],
  }
  ```
  `test_every_tool_has_cases`: `set(PUSHDOWN_CASES) == set(tools.TOOLS)`. `test_events_queries_have_pushdown`: a `RecordingConnection` proxy (`execute(sql, params=None)` records and delegates, and `__getattr__` delegates) wraps `store.connect()` and is passed as `Toolbox(store, con=cast(Any, proxy))`. Every case is run and must return no error. Then the test asserts that `proxy.seen == box.statements` (nothing bypassed `Toolbox.sql`), that at least one statement matches `re.compile(r"\bfrom\s+events\b", re.I)` (so the test cannot pass with no events queries), and that every matching statement contains `"hour IN ("`.

Changed `tests/benchmark/test_latency.py` (step 7) covers criterion 7.

Criterion 8 is covered by the quality gate (coverage of at least 90 %).

### 7. Risks

- **Different counts from different sources.** If a window were not minute-aligned, `rate_60` bins would include up to a minute more than `ts`-based `events` counts. The guard is that `resolve_window` always rounds outwards before any source is read. `test_aggregates_match_events` compares every source with `events` on the same window.
- **Template tables only count templated lines.** `template_counts*` comes from the join with `assignments` and leaves out lines with no payload. `pick_source` reaches those tables only when a template filter is set (see Design). `test_sources` checks the totals against SQL.
- **Full scans of `events`.** `events_clause` is the only way to build an `events` filter. It refuses windows over 120 minutes or 24 h. The pushdown test fails on any `FROM events` without `hour IN (`.
- **Latency.** The worst case is a template join over 120 minutes for template 1 (the largest, 27 ms for 2 h in the measurement above), plus 11 ms for the connection. The slow benchmark measures it, with a new connection on every call.
- **UTC handling.** Offsets are converted with `astimezone(UTC)`, naive input is read as UTC, and DuckDB runs with `TimeZone = 'UTC'` on naive `TIMESTAMP` columns. Parameters are naive datetimes. Tests cover `Z` and `+02:00`.
- **Midnight at the end of the data.** `now` is `2023-05-22 00:00` on the real cache, but `today` and clock times use `ts_max.date()`, so `"14:20"` means 14:20 on the day of the data. `hour_values(23:00, 24:00)` gives `["23"]`. `test_now_and_last_minutes` covers this.
- **NULL hosts and services.** Aggregates store them as `(missing)`, and `events` grouping uses the same `coalesce`. `(missing)` is not a valid filter value (it is removed from the catalog), and its group has no link.
- **162 hosts.** `group_by="host"` returns the 20 largest groups with `groups_total`, so the result stays small. Suggestions are capped at 5.
- **Empty results.** A window with no matching events gives `total` 0 and `groups` `[]` (`sum` returns NULL, which `or 0` handles).
- **A missing stage.** `require` and the `events_dir` check turn missing files into `unavailable`. `call` maps any other `FileNotFoundError` (for example from `read_json`) to `unavailable` with a path relative to the cache, so the model never sees an absolute path.
- **SQL injection.** Values are always `?` parameters. Paths are interpolated with doubled quotes, as in the Explorer. Column names come from constants only.
- **Thread safety.** One DuckDB connection per `Toolbox`, as documented in the class docstring: "Not thread-safe; use one Toolbox per question."

### 8. Docs

- New ADR (step 8). It binds #56, #57, #44 and #47.
- No README change. The "Ask the logs" README section comes with #44, when there is something to run.
- No new `LOGDASH_*` settings. The caps are module constants, as the #43 notes decided.

### 9. Open questions

All of these are non-blocking. The plan uses the stated default.
1. **Default window.** Without `start` and `last_minutes`, the window is the whole data span. A question about a whole day then works from the aggregates, and narrower filters get `window_too_long` with a hint.
2. **Case-insensitive host and service matching.** The result uses the data's spelling. Hosts in the data are lower case, and models sometimes write them in upper case.
3. **`explorer_link` always carries `start` and `end`**, which is the whole span when no time is given. The link then shows exactly the window the result describes.
4. **Integral floats such as `60.0` are accepted as integers**, because some models send JSON numbers that way.
5. **`template` has `minimum` 0** in the schema, and the catalog checks that the id exists (ids are 1..281 on the real cache).

