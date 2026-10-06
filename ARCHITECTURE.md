# CompreDef - Project Architecture

## Goal
Build an Anki 2.1+ Python add-on named "CompreDef" that automatically generates definitions for Japanese vocabulary cards strictly tailored to the user's known vocabulary and kanji levels.

## Core Philosophy: Comprehensible Definitions
CompreDef uses the user's local JSON dictionaries paired with **Kanji Matrix Scoring**:

1. **User-Configured Dictionaries**: Every dictionary contributes its
   candidates; the set is never rewritten by the add-on.
2. **Comprehension wins**: Each candidate is scored against the
   learner's interval-weighted knowledge (see Picker algorithm
   below); the most comprehensible definition wins, regardless of
   which dictionary it came from (pure argmax — the v1.2 不公平 fix:
   an ordering fluke must never beat a strictly better definition).
3. **Maximal Fallback**: If nothing is fully known, the
   highest-density definition still wins (never nothing when a
   dictionary has the word).

## Dictionary Picker algorithm (the value of this add-on)

The dictionary set says WHERE to look; the picker (`picker.py`, self-contained —
depends only on `scoring`/`models`/`utils`) decides WHICH single
definition the learner reads. Pipeline per word:

1. **Collect** every dictionary's entries (`lookup_by_path`, or the
   Yomitan bridge slices).
2. **Filter** cross-reference titles (`is_reference_title`: short
   punctuation-less titles, pipe-separated child lists — never real
   prose, which always ends in 。？！). Filtered titles lose unless
   they are the only candidate.
3. **Score** each survivor against the learner's interval-weighted
   knowledge and **rank** by comprehension density (below).

### Learner knowledge

```
point(x) = 0                                   if x unknown
point(x) = min(1, max_interval(x) / 365)       otherwise
```

Mastered = a card interval ≥ 365 days (full point); younger intervals
count proportionally. Kanji points come from first fields; vocab
points ONLY from multi-kanji compounds (kana-only words are
inflection-hostile, single kanji are already covered by kanji points).

### Comprehension density (v1.3, default `DensityPicker`)

Scoring runs on cleaned text: boilerplate sections (thesaurus lists,
part-of-speech tags — see below) are stripped, then the defined
headword itself is removed (looking a word up proves it unknown — its
self-mentions earn nothing), and only then are kanji/compounds
counted.

For a definition with kanji occurrences K (|K| = kanji_count) and
distinct compounds W:

```
kanji_density  = Σ point(k) / |K|          (fraction known, 0..1)
vocab_density  = Σ point(w) / |W|          (0 when W is empty)
comprehension  = kanji_density + vocab_density
```

Kana-only definitions (|K| = 0) hold no kanji signal: comprehension 0
(neutral — never wins on merit, so a kana gloss can't beat real prose).

Compounds are maximal kanji runs of length ≥ 2 — deliberately not a
morphological analyzer (MeCab is an explicit non-goal: heavy native
dependency, version drift). Runs split at any kana/punctuation, so
okurigana inflections (偏る → 偏) stay out of vocab by construction;
adjacent compounds with no separator can glue (rare in prose, and
synonym lists are stripped first). Deterministic on every machine.

Ranking, best first: highest comprehension, then most kanji.
Exact ties keep encounter order (stable sort / first-best scan), so a
fixed input always yields the same winner; shuffling only matters when
two definitions tie exactly.

Why density, not raw sums: raw sums (v1.2 `LegacySumPicker`, still
available via the `picker_strategy` config key for A/B) grow with
length, so a 20-paragraph 5%-known definition always beat a succinct
90%-known one. Density measures the fraction the learner can actually
read.

### Swapping the method

New picking ideas subclass `picker.PickerStrategy` (one method:
`rank_key(result)`) and become active via
`get_active_strategy()` / the `picker_strategy` config key. Nothing
outside `picker.py` changes; `engine.py` only delegates. Candidate
gathering (`collect_dictionary_candidates`, per-path isolation so one
corrupt dictionary never kills the run) lives in `picker.py` too —
`engine.DefinitionGenerator` keeps only Anki/config wiring (source
selection, Yomitan fallbacks, plain-text mode).

## Architecture Structure

The system is organized into a layered architecture to separate data access, scoring, and orchestration:

```text
UI / Anki Integration (gui.py, editor_browser.py)
        ↓
Orchestration (core.py, engine.py)
        ↓
Scoring & Filtering (scoring.py)
        ↓
Dictionary Provider Interface (provider.py -> DictionaryProvider)
        ↓
Implementation (provider.py -> LocalSQLiteProvider)
```

### Key Modules
- `scope.py`: Deck-based Scope — the deck selection driving the
  learner-KNOWLEDGE snapshot only (scoring weights). Deck names
  (subdecks included); a note counts when ANY of its cards sits in a
  scoped deck; empty scope is fail-closed for knowledge (empty
  weights). Fresh installs auto-preselect Japanese decks once
  (`suggest_japanese_decks`: name keywords + kana content sampling).
  Generation NEVER consults Scope — it is governed by the global CD
  toggle (`tab_generate`) + field mapping (explicit `targets` or
  auto-inference).
- `gui.py`, `editor_browser.py`: Anki-specific UI and hook logic. The
  config GUI offers a compact Scope row + deck picker dialog; per-type
  field mappings ("targets") are kept for all types, with
  auto-inference fallback for unmapped ones. Generation paths resolve
  fields per note via `resolve_fields_for_note` (mapping only, no Scope
  gate). Editor toolbar toggle (Chinese-Support style, `toggleable`
  "CD" button mirroring `tab_generate`, active class synced on note
  load), Tab-to-Generate (legacy-editor unfocus hook, fill-empty
  only), Browser bulk actions (the deliberate overwrite path), and the
  Learner Knowledge dialog (non-modal, payload-cached) live here.
- `core.py`: Application wiring and singleton management.
- `engine.py`: Implements definition generation (pure
  argmax over comprehension density via `picker.py`, order-independent;
  thin delegates `_pick_best` / `_filter_valid_entries` kept for
  compatibility).
- `scoring.py`: Interval-weighted kanji/vocab scoring (`ivl/365`
  capped at 1.0), raw sums AND length-normalized densities, plus
  reference-title filtering.
- `picker.py`: The self-contained picking strategies
  (`DensityPicker` default, `LegacySumPicker` for A/B) with
  rank/pick/filter over scored candidates; the ONLY module that
  decides which definition wins.
- `provider.py`: Defines the `DictionaryProvider` interface and the
  current SQLite-backed implementation.
- `renderer.py`: Renders Yomitan structured content to HTML (ruby,
  `data-sc-*`, per-dictionary scoped CSS).
- `utils.py`: Shared text cleaning, dictionary discovery utilities,
  and the Qt-free config-merge helper (`merge_type_targets`).
- `anki.py`: Safe Anki database interaction for the learner-knowledge
  snapshot — first field of every in-scope note; mastered means a
  card interval ≥ 365 days, seen means any positive interval.
  Session-cached with generation counter; on-demand diagnostics and
  the snapshot spec live in `debug/`.
- `yomitan.py`, `yomitan_installer.py`: Optional Yomitan-API
  dictionary source — localhost bridge install/repair, keepalive
  against service-worker suspension, anti-zombie shutdown.
- `models.py`: Shared data structures (e.g., `DictionaryEntry`).

## Install-Time Indexing
Indexing happens exactly ONCE per dictionary during installation via the GUI. This builds a persistent SQLite index in `user_files/cache/dictionaries.db`. Lookups are pure SQL queries, ensuring the UI never freezes during generation.

## Future Extensibility
The `DictionaryProvider` interface allows replacing the `LocalSQLiteProvider` with a `YomitanApiProvider` (or any other source) without modifying the scoring or generation logic in `engine.py`.
