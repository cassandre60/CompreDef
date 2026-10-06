# CompreDef - Agent Coding Rules & Best Practices

> **Authority hierarchy (conflicts resolve top-down):**
> `PRODUCT.md` (what to build) → `MVP.md` (current scope) →
> `ARCHITECTURE.md` (technical structure) → `QUALITY.md`
> (invariants that must hold) → `TEST_STRATEGY.md` (how they are
> verified) → `IMPLEMENTATION_PLAN.md` (current work) → this file
> (how to operate in this repo). **Code implements the specs; tests
> enforce them.** Do not widen MVP scope or modify architectural
> constraints without updating the corresponding document first.

Start every task by reading the docs above that it touches; keep
changes consistent with them.

## 1. Environment & Entry Point
- This is an Anki add-on running within Anki's embedded Python environment. 
- The main entry point must be `__init__.py`.

## 2. Database Interaction & The "Kanji Grid" Approach
- **NEVER** attempt to read the `collection.anki2` file using an external `sqlite3` connection. This causes database locks and corruption.
- Exclusively use Anki's native database wrapper: `mw.col.db.execute()` or `mw.col.db.all()`.
- When building the known-vocabulary list or the "Kanji Matrix" (for Mode B), reference the database logic used in the open-source "Kanji Grid" add-on (Anki Web ID: 1610304449) to safely parse fields and `interval` stats natively.

## 3. General Programming Practices & Clean Code
- **Commenting:** Write explicit inline comments explaining the *why* of the logic, especially for database queries, regex matching, and API calls. Include docstrings (`"""..."""`) for all functions and classes.
- **Modularity:** Do not stuff all logic into `__init__.py`. Separate concerns (e.g., `gui.py` for UI, `db_utils.py` for Anki database queries, `parser.py` for MeCab/Dictionary logic).
- **Type Hinting:** Use standard Python type hints (e.g., `def filter_words(words: list[str]) -> bool:`) to ensure code readability and prevent type errors.
- **Error Handling:** Wrap API calls and file reading operations in robust `try...except` blocks. Never let an uncaught exception crash Anki. 

## 4. Hook System & Concurrency
- **Hooks:** Do not overwrite Anki's core UI classes. Use `aqt.gui_hooks` (e.g., `editor_did_init`, `note_will_be_added`) to intercept card creation and inject UI seamlessly.
- **Non-Blocking UI:** Any intensive tasks (parsing massive JSON dictionaries, generating the Kanji Matrix, or making LLM API calls) MUST be executed in a background thread using Anki's `mw.taskman.run_in_background()` with a callback to update the UI on completion.

## 5. PyQt Compatibility
- Anki versions differ in their Qt backend. When importing UI components, always import from `aqt.qt` (e.g., `from aqt.qt import QDialog, QVBoxLayout, QPushButton`) rather than hardcoding `PyQt5` or `PyQt6`.

## 6. Regression Test & Build Mandate
- **Verification runs in CI, never on this machine** (user directive
  2026-10-06): do NOT run `tests/test_units.py`,
  `tests/test_regression.py`, `scripts/build.sh` or `scripts/ci.sh`
  locally. Push the branch and let the `tests` workflow
  (`.github/workflows/tests.yml`) verify; watch it with
  `gh run watch --exit-status`. A green CI run is the gate —
  exit code 0 = safe to release; any FAIL = fix forward, never commit
  over it.
  (Ring 0 isolated unit tests, then the Ring 1 regression suite.)
- The suite guards the project's historical bugs (plain-text definitions replacing rich Yomitan HTML, furigana polluting kanji scores, order-dependent picks, reference-title filtering, ZIP/folder parity, stale SQLite caches, Tab-to-Generate overwrites). Green CI = safe; any FAIL = fix first.
- It runs on both system Python and Anki's bundled Python (no Anki/PyQt required — `aqt` is stubbed automatically). Real-dictionary smoke tests self-skip when the dictionaries are absent.
- If you intentionally change rendering behavior, bump `RENDERER_VERSION` in `parser.py` so users' SQLite caches invalidate cleanly, and update the affected test expectations in the same commit.

## 7. Git Remote & Repository Sync Mandate
- **ALWAYS** push all committed changes to the official GitHub remote (`https://github.com/mansour.com/CompreDef`) as the final step of your work session — NO, correctly: (`https://github.com/mansourvery-hub/CompreDef`):
  `git push origin master` (or `git push origin main`).
- **AnkiWeb**: Official AnkiWeb Add-on Page: [https://ankiweb.net/shared/info/1619602654](https://ankiweb.net/shared/info/1619602654). Releases are automatically uploaded via GitHub Actions.

## 8. End-of-Session Release Pipeline (MANDATORY)
The user tests every change through Anki's native updater — never by manually
installing files. Therefore EVERY code-change session must end with the change
released all the way to AnkiWeb:

```bash
./scripts/ci.sh        # tests → commit → push → version bump (patch +1)
                       #   → GitHub Release (dist/CompreDef.ankiaddon attached)
                       #   → CI uploads to AnkiWeb (workflow watches + verifies)
                       #   → auto local install (for instant single-restart testing)
```

Use `./scripts/ci.sh --no-release` for intermediate work; every *finished*
session must still end with a full `./scripts/ci.sh` (or `./scripts/release.sh vX.Y.Z`).

## 9. Local Auto-Install Mandate (NEW — for immediate testing)
- **ALWAYS** auto-install the freshly built add-on into the local Anki
  profile as the **very last step** of any build/ci/release, **after**
  tests, packaging, and (for ci/release) GitHub + AnkiWeb upload:
  `./scripts/install_local.sh` → unzips `dist/CompreDef.ankiaddon` to
  `~/.local/share/Anki2/addons21/1619602654/` (Linux),
  `~/Library/Application Support/Anki2/addons21/1619602654/` (macOS),
  `%APPDATA%/Anki2/addons21/1619602654` (Windows), preserving `user_files/`.
  The user then only needs **one** Anki restart (not two) to see the change.
- `scripts/build.sh` already does this as `[4/4] Auto local install`.
  `scripts/ci.sh` does it as `[5/5]` and `scripts/release.sh` as `[6/6]` —
  all are non-fatal (build/release still succeed even if local Anki is not
  found). **Future agents must keep this as the final step** — do not
  remove it, and ensure any new pipeline script also ends with
  `./scripts/install_local.sh`.
- The GitHub Actions workflow (`.github/workflows/upload-to-ankiweb.yml`)
  runs on `ubuntu-latest` where no local Anki exists, so it does **not**
  do a local install — the rule above is for **local** agent runs only.

**The user's test loop (memorize this):**
1. Restart Anki → update check fires.
2. Anki detects the new version → "Update All" / auto-installs (~1s download).
3. Restart Anki again.
4. Test the feature.

**Pipeline pieces (know what they do):**
- `scripts/build.sh` — regression suite + package + verify `dist/CompreDef.ankiaddon`. No git side effects. `manifest.json` package MUST stay `1619602654` (the AnkiWeb ID — it makes Anki install into the right folder and enables the native "View Add-on Page" button).
- `scripts/release.sh [vX.Y.Z]` — tests+build → commit → push → tag → GitHub Release → waits for the AnkiWeb upload workflow and FAILS LOUDLY if the upload failed (it once failed silently for months).
- `scripts/ci.sh` — one command for the whole pipeline; auto-bumps the patch version.
- `.github/workflows/upload-to-ankiweb.yml` — triggered by Release published; uses `danny900714/upload-anki-addon@v1.0.0` with `username`/`password`/`addon-id`/`title`/`branches`/`addon-files` inputs. Requires repo secrets `ANKI_WEB_USERNAME` + `ANKI_WEB_PASSWORD` (set via `gh secret set ...`).

**Version rules:** patch bump per release (ci.sh does this); minor/major for
feature/behavioral milestones via `./scripts/release.sh vX.Y.Z`. Keep `VERSION`
in sync (release.sh writes it).
