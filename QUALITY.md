# CompreDef — Quality Rules (Invariants)

Answers: *what properties must remain true?*
`TEST_STRATEGY.md` answers how each one is mechanically verified.

## Knowledge correctness

- **Q-K1 — First-field-only extraction, all note types.** Only the
  FIRST field of a note contributes kanji/vocab — never definitions,
  examples, readings, or other fields (CompreDef's own output must
  never mark unknown kanji known).
- **Q-K2 — No note-type gating.** Notes are never filtered by model
  for knowledge purposes.
- **Q-K3 — Mastered means mature.** Mastered = on a note with a card
  interval ≥ 365 days (`_MATURE_IVL_DAYS`, single source of truth).
  Seen = any strictly positive interval.
- **Q-K4 — Scope-bounded knowledge, generation-independent.**
  Only cards in Scope decks (subdecks included) count for knowledge;
  empty scope is fail-closed for knowledge (empty weights, visible
  guidance) while generation still works everywhere via the CD toggle.
  Out-of-scope decks never leak into knowledge. Fresh installs
  auto-preselect Japanese decks once (name + kana heuristic); a
  deliberate user Clear is never re-seeded.
- **Q-K5 — Session snapshot.** Builds once per session (async at
  startup), reused for all generations; note edits/writes never
  rebuild it; explicit reset rebuilds exactly once; never snapshot
  before the collection opens (`mw.col is None` ⇒ abort, stay
  not-ready).

## Data access

- **Q-D1 — Native DB wrapper only.** NEVER open `collection.anki2`
  with an external `sqlite3` connection (locks/corruption). Only
  `mw.col.db.*` plus the public `mw.col.models` API.
- **Q-D2 — Schema-proof SQL.** Raw SQL names only `notes` and
  `cards`. Never the legacy `models` table (renamed `notetypes` in
  Anki 23.10+ — the v1.0.5 silent-empty-set incident).
- **Q-D3 — Visible failure.** DB/schema problems surface via console
  log (+ one tooltip per session). A silent empty result is a bug,
  never an acceptable "beginner collection" state.
- **Q-D4 — Graceful degradation.** Missing config, unknown types/
  fields/mids, malformed rows ⇒ empty result, never raise.

## Generation behavior

- **Q-G1 — Never overwrite automatically.** Tab and the toolbar
  toggle fill ONLY an empty definition field; unmapped types are
  skipped, not guessed. Browser bulk is the deliberate regenerate
  path and MAY overwrite the notes the user selected.
- **Q-G2 — Density argmax.** Dictionaries are collected,
  never reordered; the winner is the highest comprehension DENSITY
  (known-kanji fraction + known-compound fraction, v1.3 — raw sums
  favored length); ties break toward most kanji, then (title, text);
  order of evaluation must not change the winner. The strategy lives
  in `picker.py` and is swappable without touching other modules.
- **Q-G3 — Kana never scores.** Kana-only words are not vocab
  candidates (inflection mismatch); furigana `<rt>` never pollutes
  kanji scores; reference titles lose to real prose.
- **Q-G4 — Non-blocking UI.** Parsing, indexing, matrix builds, and
  LLM/API calls run on background threads (`run_in_background`);
  editor callbacks never freeze typing.
- **Q-G5 — Rendering fidelity.** Definitions stay rich Yomitan HTML
  (ruby, `data-sc-*`, per-dictionary scoped CSS, sanitized style
  blocks) — never collapsed to plain text; ZIP ≡ folder output,
  byte-identical.

## Compatibility & packaging

- **Q-C1 — Qt backend agnostic.** All UI imports from `aqt.qt`;
  scoped Qt enums only (PyQt6-safe).
- **Q-C2 — Package identity stable.** `manifest.json` package stays
  `1619602654` (AnkiWeb ID — install folder + listing button).
- **Q-C3 — Cache invalidation on render change.** Any intentional
  rendering-behavior change bumps `RENDERER_VERSION` in `parser.py`
  in the same commit, with test expectations updated.
- **Q-C4 — Dual-context imports.** Every sibling import works both
  as a relative import (Anki package load) and absolute import
  (test harness) — never `ModuleNotFoundError` in either context.

## Process

- **Q-P1 — Suite green before commit.** `tests/test_regression.py`
  exit 0 is the gate; any FAIL is fixed first, never committed over.
- **Q-P2 — History explains why.** Comments/docstrings explain the
  *why* (incident IDs: v1.0.5, v1.0.10/11, v1.1.4, …); type hints on
  all functions; no dead code or user-specific hardcodes shipped.
