# CompreDef — Product Definition

Authoritative source for *what CompreDef is supposed to do*. Code
implements this document; tests enforce it; architecture serves it.
(See AGENTS.md for the authority hierarchy.)

## Problem

A beginner/intermediate learner looking up a word in a Japanese–Japanese
(国語) dictionary (大辞林, 大辞泉, 広辞苑, …) gets a definition written
with obscure literary vocabulary and unlearned kanji — which requires
looking up *those* words, ad infinitum (the **infinite lookup loop**).
Children's dictionaries (例解学習国語, …) use simple language but lack
coverage of advanced terms. No single dictionary fits a learner at every
level, and the right dictionary *changes as the learner grows*.

## Target users

Anki users studying Japanese (beginner through advanced) who mine
vocabulary into Anki and want each card's definition to be readable
*at their current level* — without hand-picking dictionaries per word.

## Core user journeys

1. **Generate while adding/mining.** User flips the CD toolbar
   toggle ON once; typing a word (e.g. 不公平) into the Expression
   field then fills the Definition field on Tab — for any card, no
   deck setup. Browser bulk generation covers hundreds of notes at
   once (and is the deliberate overwrite path).
2. **Set up dictionaries once.** User installs their dictionaries;
   every future generation scores every dictionary's definitions and
   returns the most readable one automatically.
3. **Scope their decks.** User picks which decks count as "their
   Japanese" for knowledge/scoring (fresh installs pre-select
   Japanese decks automatically); generation itself never needs Scope.
4. **Inspect what the add-on thinks they know.** Learner Knowledge
   dialog: mastered/seen kanji + vocab counts, full lists, provenance
   search into the Browser.

## Functional requirements

- **One scoring logic for every definition:** all dictionaries'
  candidates are scored with comprehension density and the highest
  score wins (maximal fallback included — never nothing when a
  dictionary has the word).
- **Kanji-matrix scoring:** comprehension is measured in kanji —
  interval-weighted mastery (`ivl/365`, capped at 1.0) over kanji
  occurrences plus multi-kanji compounds; kana-only words are never
  scored (inflection-hostile); cross-reference titles lose to prose.
- **Learner knowledge:** mastered = kanji/vocab on mature notes
  (any card interval ≥ 365 days); seen = any positive interval.
  Knowledge comes ONLY from first fields of in-scope notes —
  definitions, examples, readings never count (else CompreDef's own
  output would mark unknown kanji known).
- **Install-time indexing:** dictionaries are parsed ONCE at install
  into `user_files/cache/dictionaries.db`; generation is pure SQLite
  lookups and never parses files (never freezes Anki).
- **Dictionary sources:** local Yomitan dictionaries (folder or `.zip`,
  identical output) AND the Yomitan browser API as an optional source.
- **Yomitan-fidelity rendering:** structured content renders as native
  Yomitan HTML (`<ruby>`, `data-sc-*`, scoped per-dictionary CSS).

## UX requirements

- Never overwrite user content automatically (Tab and the toolbar
  toggle fill only empty fields; Browser bulk is the explicit
  overwrite path; bulk never touches unmapped types silently).
- Never freeze the UI (heavy work on background threads).
- Fail closed and loud: empty Scope ⇒ empty knowledge + visible
  guidance (generation still works); DB/schema problems ⇒ console
  + tooltip, never a silent empty result.
- Cross-version: PyQt5/PyQt6 imports via `aqt.qt`; schema-proof SQL
  (`notes`/`cards` only, never the legacy `models` table).

## Constraints

- Runs inside Anki's embedded Python; entry point is `__init__.py`.
- No network except the localhost Yomitan bridge (user-opted-in).
- Ships no dictionaries; respects Yomitan formats as-is.
- AnkiWeb listing 1619602654; releases flow GitHub Release → AnkiWeb.

## Non-goals

- Not a dictionary and not a card scheduler — selection/ranking only.
- No cloud sync, accounts, collaboration, or analytics.
- No MeCab morphological analysis (readings come from note fields).

## Important assumptions

- First-field convention: word/expression/front holds the term.
- One-year interval ≈ mastery (user decision, `_MATURE_IVL_DAYS`).
- The user curates the dictionary set; CompreDef never changes it.

## Open questions

- None formally open; evolution is user-driven per session
  (see IMPLEMENTATION_PLAN.md).
