# Capability tagging for easy-hermes model selection

Status: idea / not implemented. Draft for future work.

## Goal

Let the selector (and downstream Hermes config) prefer models that advertise
specific capabilities — e.g. `tools`, `coding`, `agent`, `vision`, `reasoning` —
instead of ranking on release date / context / provider priority alone.

## Why it is cheap

The data is already in the payloads we fetch. No new network sources are needed;
we reuse the same model records that `extract_release_date` already inspects.

- OpenRouter: `supported_parameters` (e.g. `tools`, `response_format`,
  `structured_outputs`), `architecture.modality`, `architecture.tokenizer`,
  `architecture.instruct_type`.
- Ollama / Ollama Cloud: model `capabilities` / `details` fields.
- NVIDIA NIM: model metadata / `owned_by` + tags.
- Gemini: `supportedGenerationMethods`.
- Novita: OpenAI-style `capabilities`.
- HuggingFace (used for release-date backfill): model card `tags`.

## Proposed design

1. Add `extract_capabilities(m) -> set[str]` in `select-models.py`, analogous to
   `extract_release_date`: check a list of candidate keys, normalize to lowercase
   tokens, return a set. Store as `m["_capabilities"]`.

2. Normalize per provider in each `poll_*/normalize_model` so the common keys above
   are preserved (most already pass through raw fields).

3. Add a capability weight to the ranking matrix (like `release_date`,
   `context_window`, `provider_priority`) and a `--capability KEY` CLI filter
   that keeps only models whose `_capabilities` contains `KEY`.

4. Optional: expose capabilities in `config.yaml` fallback entries so Hermes can
   pick a tool-capable model for agent runs vs. a cheap model for chat.

## Open questions

- Capability vocab: standardize on a small set (`tools`, `vision`, `coding`,
  `reasoning`, `json`, `long_context`) or pass through raw tags? Standardizing
  makes filtering usable but requires per-provider mapping.
- Should capability be a hard filter or a soft weight? Soft weight keeps the pool
  broad; hard filter risks empty results on providers with sparse metadata.
- Release-date backfill uses HuggingFace; capability backfill there is noisier
  (tags are community-authored). Prefer provider-native metadata first.

## Effort

Small-to-medium: ~1 day for extraction + ranking weight + filter + tests.
Provider-specific mapping is the bulk of the work.