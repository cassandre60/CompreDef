# CompreDef — MVP / Current Scope

`PRODUCT.md` answers *what this product is*.
This file answers *what is actually built and shipped right now*.
An agent must not independently widen this scope while coding.

## MVP goal (achieved, shipped as v1.x)

A learner installs the add-on, points it at their Japanese decks and
dictionaries, and every mined word gets the richest definition they
can currently read — automatically, without per-word effort.

## Included capabilities (all shipped, all tested)

- Dictionary management GUI (add folder/ZIP, reorder, enable/disable)
- Install-time indexing → pure-SQLite generation (CD toggle + Tab
  fill-empty, Browser bulk with `Ctrl+Shift+D` as the overwrite path)
- Deck Scope for knowledge (subdecks included, fail-closed for
  weights, Japanese decks pre-selected on fresh installs)
- Multi-type field targets + auto-inference ("no need to add each
  note type")
- Interval-weighted learner knowledge (mastered ≥ 365d / seen > 0d),
  Learner Knowledge dialog (counts, full lists, provenance search)
- Local Yomitan dictionaries AND Yomitan-API source with bridge
  installer, keepalive, and anti-zombie handling
- Yomitan-fidelity HTML rendering (ruby, `data-sc-*`, per-dict CSS
  scoping, style sanitization)

## Excluded capabilities

- Nothing in PRODUCT.md is excluded: the MVP *is* the product as
  defined today.

## Acceptance criteria (every release)

- `python3 tests/test_regression.py` fully green (exit 0).
- Headless dialog smoke (`debug/smoke_dialog.py`) passes locally.
- `./scripts/ci.sh` pipeline completes: package verifies, GitHub
  Release published, AnkiWeb upload workflow succeeds, local
  auto-install lands.

## Known limitations

- Tab-to-Generate fires in the legacy editor (Browser, and Add/Edit
  windows while the Svelte experiment stays off); the Svelte
  `NewEditor` fires no unfocus hook, so Tab is legacy-only by Anki
  design — the toolbar button covers all editors.
- Knowledge is a per-session snapshot (rebuilt on Refresh,
  scope/config change, profile open) — not live per review.

## Deferred features

No formal deferred list. Candidates discussed but never built
(PROMPT.md-era ideas, kept here so they are not forgotten):

- LLM-assisted definition selection/reranking (provider interface
  anticipates alternate sources).
- MeCab-based reading analysis (readings currently come from note
  fields and embedded furigana).

New work enters via IMPLEMENTATION_PLAN.md — never by silent
scope creep inside a coding session.
