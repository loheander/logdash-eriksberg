# ADR001: Language model calls happen only in explicit builds, memoised by content

Status: proposed (story #28, parent issue #24)

## Context

Issue #24 tags every log template with a language model via OpenRouter. More features
built on language models will follow (the chat assistant and anomaly triage named in
#24). The dashboard must stay fast (under 300 ms per interaction) and must work
offline and without a key. Model calls cost money and take seconds. The build pipeline
(`src/logdash/build.py`) rebuilds a stage whenever its inputs' fingerprint changes. It
also wipes the stage's directory before `compute` (`_build_analysis` calls
`CacheStore.reset_module`). Every fingerprint includes the data-file fingerprint, so
any change to the data rebuilds every stage. `logdash serve` builds stale stages before
serving unless `--no-build` is given.

## Decision

1. **Model calls only in `Analysis.compute`, and only on `logdash build`.** Layouts and
   callbacks read the cache only. A stage that calls a model sets
   `calls_model = True`. `run_build(..., allow_model=False)` skips such stages; it
   neither builds nor records them. `logdash serve` always passes
   `allow_model=False`, so starting the dashboard never calls a model. A dependent
   stage built in that run reads whatever the skipped stage last cached. The next
   `logdash build` rebuilds the skipped stage, and its dependents are rebuilt in the
   same run.
2. **Stages without a page.** An analysis may set the class attribute `page = False`.
   `Registry.pages()` lists only analyses with a page, and the app (pages, navigation,
   home cards) uses `pages()`. The build still uses `ordered()`. A model stage is such
   a page-less analysis, so it is fingerprinted, built, shown in `logdash status` and
   depended on like any other stage.
3. **Content-addressed memo outside the stage directory.** Model answers are stored in
   `cache/<stage>_memo/memo.parquet`, keyed by
   `digest_of([prompt_version, model_id, input_text])`. The stage directory is reset
   on every rebuild, but the memo is not, so a rebuild caused by anything other than
   new input text makes no calls. Only valid answers are stored. Failures are retried
   on the next forced build of the stage.
4. **Secrets never enter fingerprints, the manifest or `repr`.** A stage's fingerprint
   includes the model ID and whether a key is present (a boolean), never the key.
5. **Structured output, validated locally.** Requests use OpenRouter's
   `response_format: json_schema` with `strict: true` and
   `provider.require_parameters: true`. With `require_parameters`, OpenRouter routes
   only to providers that support **every** parameter sent. A request therefore sends
   only parameters that the model's structured-output providers support. For example,
   no provider of `anthropic/claude-haiku-5.5` accepts `temperature`. Every answer is
   still validated in Python. An invalid answer is retried and then recorded as
   missing, and it never fails the build.
6. **No SDK dependency.** The OpenRouter client is a small stdlib (`urllib.request`)
   function. Its single network seam (`post_json`) is replaced in tests.

## Consequences

- Without a key the stage records "absent" quickly and the build succeeds. Adding a
  key later changes the fingerprint, so the next build tags.
- After the data or templates change, the tags stay stale until someone runs
  `logdash build`. `serve` alone never refreshes them.
- Changing the model ID or the prompt version re-tags everything. Both are meant to.
- `cache/` gains a memo directory that `reset_module` never touches. Deleting it is
  the way to force fresh answers.
- Later model features reuse the pattern: a page-less stage with `calls_model = True`,
  a memo keyed by input content, and readers for pages.
