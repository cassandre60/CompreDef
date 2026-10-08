# CompreDef
 
Anki add-on for automatically generating Japanese vocabulary definitions strictly tailored to your known vocabulary and kanji levels.
 
**[Download from AnkiWeb](https://ankiweb.net/shared/info/1619602654)**
 
## Core Concept: One Score for Every Definition

CompreDef frees you from the circular lookup trap of Japanese monolingual dictionaries by scoring **every** definition from **every** dictionary you install — and returning the most readable one:

- Every dictionary contributes its candidates for the word.
- Each candidate is scored with comprehension density: the fraction of
  its kanji and compounds you already know (from your mature Anki cards).
- The highest score wins, regardless of which dictionary it came from.

**Recommended setup:** install the richest dictionaries you can almost read (e.g. 三省堂国語辞典, 大辞泉) alongside simpler ones (e.g. 小学館例解学習国語). You get the most readable definition available, and as your knowledge grows, more words naturally resolve to the richer sources.

> The score counts kanji and multi-kanji compounds; kana is treated as known — so succinct explanations of known kanji beat long walls of unknown ones.

> For an in-depth mathematical specification and flowcharts, see the [Algorithm Specification Wiki (WIKI.md)](WIKI.md).

---

## Features

- **Dictionary Management**: Add, remove, and reorder dictionaries using Move Up/Down buttons or native Drag-and-Drop.
- **Folder Auto-Scanner**: Point CompreDef at a folder containing multiple unzipped Yomitan dictionaries to auto-detect and add all of them.
- **ZIP Archive Support**: Add Yomitan dictionaries directly as `.zip` files for zero-disk footprint and instant loading.
- **Auto-Matching Fields**: Automatically detects and maps your Target Word (`Expression`, `Word`) and `Definition` fields.
- **Card Editor Button**: One-click definition generation directly inside Anki's card editor toolbar.
- **Bulk Generation**: Generate definitions for hundreds of selected cards at once from the Anki Browser (via `Edit -> Generate CompreDef Definitions...` or `Ctrl+Shift+D`).
- **Independent Disk Caching**: Each dictionary is parsed once and cached in `user_files/cache/dictionaries.db`, enabling instant (0.08ms) B-tree lookups and instant reordering without re-parsing.
- **100% Faithful Yomitan HTML**: Renders rich Yomitan structured-content with `<ruby>`, `data-sc-*` attributes, inline CSS, and `用例` blocks directly into Anki notes.

---

## Installation

1. Download or build `CompreDef.ankiaddon` (see **Testing & Local Installation** below).
2. In Anki: **Tools → Add-ons → Install from file...** → select it → restart Anki.
3. Configure your Note Type and Dictionaries under **Tools → Add-ons → CompreDef → Config**.

---

## Configuration

1. **Note Types & Field Mappings**: Check every note type CompreDef should
   generate definitions for (e.g., `Japanese`, `Mining`, `Animecards`).
   Select a type's row to map its fields — each type keeps its **own**
   Word / Reading / Definition fields, auto-matched from the type's
   schema. Unchecked types are ignored by generation.
2. **Dictionaries**:
   - Click **Add Zip Archive...** to select a Yomitan `.zip` file.
   - Click **Add Folder...** to select an unzipped dictionary folder.
   - Click **Scan Folder...** to select a parent folder containing multiple dictionaries (both `.zip` files and subfolders).

---

## Testing & Local Installation

Run the regression suite (no Anki/PyQt needed — the Anki API is stubbed automatically; real-dictionary smoke tests self-skip if the dictionaries are absent):

```bash
python3 tests/test_regression.py
```

Build the installable package to test the current code in Anki:

```bash
./scripts/build.sh        # → dist/CompreDef.ankiaddon
```

Then install it: **Anki → Tools → Add-ons → Install from file...** → select `dist/CompreDef.ankiaddon` → restart Anki.

Prefer a live checkout while developing? Symlink the repo instead of installing the package:

```bash
ln -sfn /path/to/CompreDef ~/.local/share/Anki2/addons21/CompreDef
```

For test + commit + push + full release in one go:
```bash
./scripts/ci.sh          # → GitHub Release + AnkiWeb upload via CI
```
For a full release with an explicit version:
```bash
./scripts/release.sh [vX.Y.Z]
```

**Testing a released change (no manual installs):**
1. Restart Anki — the add-on update check fires.
2. Anki detects the new version → "Update All" / auto-installs (~1s download).
3. Restart Anki again — the new code is active.
4. Test the feature.
 
The regression suite verifies (among others) that definitions stay **rich Yomitan HTML** (never plain text), furigana readings never pollute kanji scoring, every definition is ranked with one density logic regardless of dictionary order, cross-reference titles lose to real definitions, `.zip` archives produce byte-identical output to their unzipped folders, and Tab-to-Generate never overwrites an existing definition.

---

## Architecture & Code Structure

```
CompreDef/
├── __init__.py         # Add-on entry point & hook registration
├── gui.py              # Config dialog, Scope picker, Learner Knowledge window
├── editor_browser.py   # Editor button, Tab-to-Generate, Browser bulk actions
├── core.py / engine.py # Wiring/singletons; definition generation
├── picker.py         # Self-contained picking strategies (density argmax)
├── scoring.py          # Interval-weighted kanji/vocab scoring + filters
├── anki.py             # Learner-knowledge snapshot (native DB wrapper only)
├── scope.py            # Deck Scope (drives generation + knowledge)
├── provider.py         # DictionaryProvider interface + SQLite implementation
├── renderer.py / models.py / utils.py  # Yomitan HTML, data types, helpers
├── parser.py           # Compat layer over provider/renderer/utils
├── yomitan.py / yomitan_installer.py    # Optional Yomitan-API source + bridge
├── PRODUCT.md / MVP.md / ARCHITECTURE.md / QUALITY.md  # What / scope / how / invariants
├── TEST_STRATEGY.md / IMPLEMENTATION_PLAN.md / AGENTS.md  # Verification / work / agent ops
├── tests/
│   └── test_regression.py  # Fundamental regression suite (run before committing)
├── debug/              # On-demand diagnostics (never shipped, never in CI)
├── icons/              # UI toolbar icons (compredef.svg)
├── config.json         # Default configuration settings
└── WIKI.md             # In-depth algorithm & architecture wiki
```

---

## Development & Safety Rules
 
- **CI/CD**: Use `./scripts/ci.sh` for testing and pushing, and `./scripts/release.sh` for tagged releases.
- **AnkiWeb Publishing**: Upon creating a GitHub Release, the `danny900714/upload-anki-addon` action automatically updates the [AnkiWeb listing (1619602654)](https://ankiweb.net/shared/info/1619602654). Ensure `ANKI_WEB_USERNAME` and `ANKI_WEB_PASSWORD` secrets are configured in the repository.
- **Native DB Access Only**: Never open `collection.anki2` with raw sqlite3. CompreDef strictly uses `mw.col.db` to prevent database locks.
- **Non-Blocking Concurrency**: All dictionary parsing and scoring operations execute in background threads using `mw.taskman.run_in_background()`.
- **PyQt Compatibility**: Imports use `aqt.qt` for multi-version Qt compatibility.
- **SQLite Caching**: Dictionary lookups use indexed B-tree tables in `user_files/cache/dictionaries.db` for ~0.08ms performance with 0MB RAM footprint.

---

## Support

Building small, useful software — if this project saves you time, consider [buying me a coffee](https://buymeacoffee.com/cassandre60) to help keep it maintained.

<a href="https://buymeacoffee.com/cassandre60"><img src="https://cdn.buymeacoffee.com/buttons/v2/default-yellow.png" width="150" alt="Buy Me A Coffee"></a>

## License

GNU General Public License v3 or later.
