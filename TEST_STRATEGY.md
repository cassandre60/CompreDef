# CompreDef — Test Strategy

Answers: *how do we mechanically prove QUALITY.md stays true?*

```
QUALITY.md (WHAT must hold)
    ↓
TEST_STRATEGY.md (HOW it is verified)
    ↓
tests / linters / smoke scripts / CI
```

## The rings (cheap → expensive)

Every behavior in this repo is verified in exactly one ring — the
cheapest ring that can observe it. A new check goes in the innermost
ring that can see the behavior; it moves outward only when it needs
more of the real world (DB, Qt, network, collection).

| Ring | What | Runs where / when | Cost |
|---|---|---|---|
| **Ring 0 — units** (`tests/test_units.py`) | Every PURE function called in isolation with plain parameters (strings, dicts, lists, tmp files): text helpers, scorer edges, scope config/name math, dataclass contracts, renderer nodes, candidate filter, mastery math, reading normalization, error-state roundtrips. No Anki, no collection, no DB, no Qt, no network. | GitHub Actions (`tests` workflow) on every push/PR — never on the dev machine (user directive 2026-10-06). | milliseconds |
| **Ring 1 — regressions** (`tests/test_regression.py`) | Incident-named tests against fakes (stub `aqt`/collection/DB): decision matrices, snapshots, scoring algorithms, hook wiring, static guards. A bug fixed here earns a permanent test named after the incident. | GitHub Actions (`tests` workflow) on every push/PR — never on the dev machine. Green CI = gate. | ~2 s |
| **Ring 2 — debug tooling** (`debug/`) | On-demand, symptom-driven: `sanity_knowledge.py` (snapshot sanity, Anki stubbed), `smoke_dialog.py` (headless offscreen Learner-Knowledge window: tabs, sorting, Browser search — needs local Anki + collection), `console_snippets.md` (live-collection Debug Console recipes). Never in CI. | After touching the area they cover | seconds–minutes |
| **Ring 3 — real-collection verification** | Offscreen runs of dialog + scoring against the real 57k-note collection (ad-hoc heredocs following the `smoke_dialog.py` pattern). Catches environment-specific issues hermetic fixtures cannot. | Before releases touching scoring/knowledge | minutes |
| **Ring 4 — release pipeline** (`./scripts/ci.sh`) | Units + regressions → commit → push → version bump → GitHub Release → watched AnkiWeb upload → local auto-install. | Every finished session | minutes |

While iterating, push early and let CI verify (`gh run watch
--exit-status`): local suite runs are banned by user directive —
CI is the gate. `tiny test → push → CI green → release`.

## Per-function coverage map

Ring 0 covers a function **iff** it is pure (plain in/out, stdlib
only). Everything needing Anki/DB/Qt/network lives in Ring 1+ with
fakes. No function is tested in two places — the map below is the
single source of truth; keep it current when adding functions.

- `utils.py` — Ring 0: `normalize_reading`, `resolve_dictionary_paths`,
  `is_zip_dictionary`, `is_directory_dictionary`,
  `find_dictionary_folders` (+ `extract_base_text` edges). Ring 1:
  `extract_clean_word`, `parse_furigana_field`, `merge_type_targets`
  (matrices need richer fixtures than plain params allow).
- `scoring.py` — Ring 0: `calculate_kanji_score` edges,
  density fields (raw sums kept alongside), headword exclusion,
  boilerplate stripping, scoring-text sharing. Ring 1:
  `is_reference_title`, `extract_kanji_words`, `score_definition`
  (spec-level behavior with realistic prose), `test_picker_audit_strict`
  (frozen full rankings over the real 11-note deck fixture).
- `picker.py` — Ring 0: `rank_definitions` key edges incl. the
  density-beats-length case + legacy-strategy contrast. Ring 1:
  strategy/rank/`pick_best` agreement, frozen winners,
  order-independence, human-grade agreement (`test_picker_audit_strict`,
  `test_v12_scoring_algorithm` §7). Candidate gathering
  (`collect_dictionary_candidates`) is covered through `generate` paths;
  engine keeps only thin delegates.
- `scope.py` — Ring 0: `get_scope_decks`, `expand_scope_names`,
  `missing_scope_decks`, `is_scope_empty`, `_note_type_name` (bare
  stand-in notes), `is_japanese_deck_name` (name heuristic). Ring 1:
  every `col`-backed path (`note_in_scope`, `implied_note_types`,
  `resolve_deck_for_note`, `suggest_japanese_decks`,
  `maybe_auto_init_scope`, …) via the fake collection.
- `models.py` — Ring 0: dataclass construction, defaults,
  frozen-ness.
- `renderer.py` — Ring 0: `_style_to_css`, `_extract_plain_text_node`,
  `render_yomitan_definition_text`. Ring 1:
  `render_structured_content_node`,
  `render_yomitan_definition_html` (need full-template context).
- `engine.py` — Ring 0: `_filter_valid_entries`. Ring 1: `_pick_best`
  (thin delegate to `picker.pick_best`) + `DefinitionGenerator.generate`
  (need entries + points dicts), `test_picker_audit_strict` (rank/`_pick_best`
  agreement, frozen winners, order-independence on captured real data).
- `anki.py` — Ring 0: `_maturity_points`, `_first_field_text`
  (minimal `aqt` stub for the import only — never touched). Ring 1:
  every snapshot/totals/summary path (needs the fake DB).
- `yomitan.py` — Ring 0: `_normalize_reading` (+ agreement with the
  `utils` twin — the two copies must never drift), error
  set/get/clear roundtrip. Ring 1: split/strip/glossary/pick paths;
  bridge/network paths are mocked or self-skipping.
- `core.py`, `provider.py`, `generator.py`, `db_utils.py` —
  singletons/SQLite/thin wrappers by design; Ring 1 only
  (integration through public entry points).
- `editor_browser.py` — Ring 1 only (needs the `aqt` stub + FakeNote
  stand-ins for hooks, scope, and inference paths).
- `gui.py`, `__init__.py` — not unit-testable (Qt); covered by
  Ring 2 (`smoke_dialog.py`) + `test_qt_enum_compat` /
  `test_no_undefined_names` static guards in Ring 1.

## Requirement → enforcement map

| QUALITY rule | Enforced by |
|---|---|
| Q-K1 first-field-only | `test_kanji_extraction_correctness` (+ S2/S3 twins in `sanity_knowledge.py`) |
| Q-K2 no type gating | `test_kanji_extraction_correctness` (multi-layout notes) |
| Q-K3 mastered ≥ 365d | `test_kanji_extraction_correctness` (182.5/100d notes seen-not-mastered), summary/totals tests |
| Q-K4 scope-bounded, fail-closed | `test_scope_deck_filtering`, knowledge empty-scope cases (generation bypasses Scope by design; `test_multi_note_type_targeting` §6 pins mapping-without-scope) |
| Q-K5 session snapshot | `test_snapshot_waits_for_open_collection`, `test_sync_reset_is_thread_safe`, `test_dialog_payload_cached_per_generation` |
| Q-D1 native wrapper only | Code review + `test_db_connections_are_closed`; `debug/README.md` S1 AST scan |
| Q-D2 schema-proof SQL | `test_knowledge_survives_new_schema` (rejects legacy `models` table), S1 AST scan |
| Q-D3 visible failure | `test_indexing_failure_reported`, `_warn_db_error` paths |
| Q-D4 graceful degradation | Empty-scope / missing-field / malformed-row cases across suite |
| Q-G1 never overwrite | Tab decision matrix (`test_tab_generate_decisions`), editor fill-empty guard (toggle/Tab never overwrite; Browser bulk is the explicit overwrite path) |
| Q-G2 density argmax | `test_order_independent_argmax`, `test_v12_scoring_algorithm` (§7 density), `test_disabled_dictionaries_skipped`, `test_picker_audit_strict` (frozen winners + picker/rank agreement + order-independence + human grades on the real-deck fixture) |
| Q-G3 kana never scores | `test_scoring_ignores_furigana`, `test_parse_furigana_field_formats`, `test_reference_title_filtering`, `test_extract_clean_word_formats`, `test_reading_disambiguates_homographs` |
| Q-G4 non-blocking UI | `test_sync_reset_is_thread_safe` (taskman main-thread rule), dialog `run_in_background` paths |
| Q-G5 rendering fidelity | `test_structured_content_html_fidelity`, `test_renderer_version_invalidates_cache`, `test_data_sc_attribute_names`, `test_zip_folder_parity`, Yomitan split/pick tests, real-dictionary smoke |
| Q-C1 Qt agnostic | `test_qt_enum_compat` (scoped enums), `aqt.qt`-only imports |
| Q-C2 package identity | `scripts/build.sh` `[3/4]` manifest verification |
| Q-C3 renderer version bump | `test_renderer_version_invalidates_cache` |
| Q-C4 dual-context imports | `test_package_relative_imports` |
| Q-P1 suite green gate | `build.sh`/`ci.sh` refuse to proceed on any FAIL |
| Q-P2 history/comments/hints | `test_no_undefined_names_in_shipped_modules`, review |

## Regression discipline (the debugging branch)

```
bug discovered → diagnose → fix → add regression test → verify → commit
```

Every fixed production incident earns a permanent test named after
the incident (see `tests/test_regression.py` header map). A fix
without its regression test is incomplete.

## Fixture policy

- Default: tiny synthetic fixtures (3-entry Yomitan dicts, stub
  collection) — hermetic, millisecond-fast, no network.
- Real dictionaries (`/home/mohamed/Desktop/Dicts`, Yomitan bridge)
  are used ONLY by self-skipping smoke tests; `yomitan_fallback`
  stays disabled under test so live services can never leak in.
- Heavy scans (GROUP BY over 100k real rows) are banned from the
  suite — synthetic equivalents cover the same branches. The single
  exception is FROZEN data: `tests/fixtures/picker_audit.json` captures
  a one-time offline export (11 real words, 37 local + live-Yomitan
  candidates, the user's real knowledge snapshot) so Ring 1 can pin
  strict winners hermetically — the live export itself lives in
  `debug/audit_picker.py` and never runs in CI.
