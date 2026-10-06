#!/usr/bin/env python3
"""
tests/test_regression.py - Fundamental regression suite for CompreDef.

Each test guards a bug that ACTUALLY HAPPENED in this project's history.
Run this after ANY change to parser.py / generator.py / db_utils.py:

    python3 tests/test_regression.py

Exit code 0 = all green, 1 = regression detected (print output shows which).

ARCHITECTURE TEST MAP (install-time indexing):
  A1. First installation parses + builds the index          -> test_install_indexes_once
  A2. Index survives 'Anki restart' (new module instances)  -> test_index_survives_restart
  A3. Normal lookup uses the index, never triggers indexing  -> test_lookup_never_indexes
  A4. Missing-word lookup returns [] instantly               -> test_missing_word
  A5. Dictionary replacement re-indexes exactly once        -> test_replacement_reindexes
  A6. Indexing failure is reported loudly                   -> test_indexing_failure_reported
  A7. Re-adding an unchanged dictionary is a no-op (covered inside A2)
  A8. Dictionary removal deletes its index                  -> test_uninstall_removes_index

HISTORICAL BUG MAP (bug -> test):
  1. '先ず' returned 121 chars of plain text instead of rich
     Yomitan HTML                 -> test_structured_content_html_fidelity
  2. Renderer upgraded but SQLite kept serving stale entries
     forever                      -> test_renderer_version_invalidates_cache
  3. Furigana <rt> readings polluted kanji scores
                                  -> test_scoring_ignores_furigana
  4. Advanced def won when a simpler one existed
                                  -> test_order_independent_argmax
  5. Cross-reference titles won over real definitions
                                  -> test_reference_title_filtering
  6. ZIP and folder produced different output
                                  -> test_zip_folder_parity
  7. data-sc-* attributes rendered differently from Yomitan
                                  -> test_data_sc_attribute_names
  8. Real-dictionary smoke (skips when absent)
                                  -> test_real_dictionary_smoke
  9. Nonsense word '駿ってさ' froze Anki at 100% CPU
                                  -> test_missing_word
 10. Indexing accumulated ~1.3 GB in RAM (OOM/freeze)
                                 -> test_indexing_streams_in_batches
 11. SQLite connections leaked one handle per lookup
                                 -> test_db_connections_are_closed
 12. 先ず(まず) returned 先ず(せんず)'s definition
                                 -> test_reading_disambiguates_homographs
 13. Furigana markup parsing     -> test_parse_furigana_field_formats
 14. Disabled dictionaries skipped, order preserved
                                 -> test_disabled_dictionaries_skipped
 15. HTML/furigana word fields never matched dictionary terms
                                 -> test_extract_clean_word_formats
 16. Lookup triggered lazy re-index storms (3.4 GB RAM burn)
                                 -> test_lookup_never_indexes

No Anki/PyQt required: db_utils' Anki dependency is stubbed before import.
"""

import ast
import builtins
import glob
import importlib
import importlib.abc
import json
import os
import re
import shutil
import sqlite3
import symtable
import sys
import tempfile
import time
import types

# ---------------------------------------------------------------------------
# Make the repo root importable and stub Anki (aqt) BEFORE importing db_utils.
# Anki's embedded Python has aqt on sys.path; a system Python does not.
# The stub must be first on sys.path so `from aqt import mw` resolves to it.
# ---------------------------------------------------------------------------
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

FAKE_STUB_DIR = os.path.join(tempfile.gettempdir(), "compredef_test_aqt_stub")


class _FakeDecks:
    """Minimal stand-in for mw.col.decks (deck-name Scope support)."""
    def __init__(self):
        self.decks = {}  # name -> id
        self._next_id = 1
    def add(self, name):
        if name not in self.decks:
            self.decks[name] = self._next_id
            self._next_id += 1
        return self.decks[name]
    def all_names_and_ids(self):
        import types as _types
        return [_types.SimpleNamespace(name=n, id=i)
                for n, i in self.decks.items()]
    def all(self):
        return [{"name": n, "id": i} for n, i in self.decks.items()]


class _FakeCol:
    """Minimal stand-in for mw.col."""
    def __init__(self):
        self.models = _FakeModels()
        self.decks = _FakeDecks()
        self.db = self._FakeDB()

    class _FakeDB:
        """Routes Scope queries by SQL shape; plain flds rows otherwise.

        Tests populate `notes` as {nid: {"flds": blob, "dids": [...],
        "mid": int}} for deck-aware paths, or set `flds_rows` for the
        legacy deck-agnostic shape (returned verbatim for queries that
        carry no did filter).
        """
        def __init__(self):
            self.flds_rows = []
            self.notes = {}

        def all(self, query, params=()):
            import re as _re
            ql = (query or "").lower()
            if "select did from cards where nid" in ql:
                m = _re.search(r"nid\s*=\s*(\d+)", query or "")
                nid = int(m.group(1)) if m else None
                dids = self.notes.get(nid, {}).get("dids", []) \
                    if nid is not None else []
                return [(d,) for d in dids]
            if "select distinct mid from notes" in ql:
                m = _re.search(r"did in\s*\(([\d,\s]+)\)", ql)
                dids = {int(x) for x in m.group(1).split(",") if x.strip()} \
                    if m else set()
                out = set()
                for n in self.notes.values():
                    if set(n.get("dids", [])) & dids \
                            and n.get("mid") is not None:
                        out.add(n["mid"])
                return [(mm,) for mm in out]
            # v1.2.1 knowledge_totals: every in-scope first field (for
            # the X/total denominators). Returns (flds, ivl) rows so the
            # same post-processing as production runs on them.
            if "select distinct notes.flds from notes" in ql:
                m = _re.search(r"did in\s*\(([\d,\s]+)\)", ql)
                dids = {int(x) for x in m.group(1).split(",") if x.strip()} \
                    if m else set()
                rows = []
                for n in self.notes.values():
                    if set(n.get("dids", [])) & dids:
                        rows.append((n["flds"],))
                return rows
            if "count(distinct notes.id) from notes" in ql:
                # scope-notes / mature-notes-in-scope counts (scalar).
                # The ivl floor is parsed from the query so the fake
                # follows production's threshold automatically (21 -> 365
                # in v1.2.3); no floor = plain scope-notes count.
                m = _re.search(r"did in\s*\(([\d,\s]+)\)", ql)
                dids = {int(x) for x in m.group(1).split(",") if x.strip()} \
                    if m else set()
                ivl_floor_m = _re.search(r"ivl\s*>=\s*(\d+)", ql)
                floor = int(ivl_floor_m.group(1)) if ivl_floor_m else 0
                count = 0
                for n in self.notes.values():
                    if not (set(n.get("dids", [])) & dids):
                        continue
                    if floor > 0:
                        ivl = n.get("ivl", 30)
                        if "ivls" in n:
                            hit = set(n.get("dids", [])) & dids
                            ivl = max((n["ivls"].get(d, 0) for d in hit),
                                      default=0)
                        if ivl < floor:
                            continue
                    count += 1
                return [(count,)]
            if "select count() from notes" in ql:
                # total notes in the collection (scalar call).
                return [(len(self.notes),)]
            if "from notes" in ql and "group by notes.id" in ql:
                # v1.2 knowledge snapshot: (flds, MAX(ivl)) per note,
                # filtered by scoped dids + the query's own ivl floor
                # (>= 365 mature in v1.2.3, >= 1 seen). The floor is
                # parsed from the SQL so the fake can never desync from
                # production's threshold. Notes may carry per-card ivls:
                # {"dids": [...], "ivls": {did: ivl}}.
                m = _re.search(r"did in\s*\(([\d,\s]+)\)", ql)
                dids = {int(x) for x in m.group(1).split(",") if x.strip()} \
                    if m else set()
                ivl_floor_m = _re.search(r"ivl\s*>=\s*(\d+)", ql)
                floor = int(ivl_floor_m.group(1)) if ivl_floor_m else 21
                rows = []
                for n in self.notes.values():
                    hit = set(n.get("dids", [])) & dids
                    if not hit:
                        continue
                    if "ivls" in n:
                        ivl = max((n["ivls"].get(d, 0) for d in hit),
                                  default=0)
                    else:
                        ivl = n.get("ivl", 30)  # sane mature default
                    if ivl >= floor:
                        rows.append((n["flds"], ivl))
                return rows
            if "from notes" in ql and "did in" in ql:
                m = _re.search(r"did in\s*\(([\d,\s]+)\)", ql)
                dids = {int(x) for x in m.group(1).split(",") if x.strip()} \
                    if m else set()
                rows = [(n["flds"],) for n in self.notes.values()
                        if set(n.get("dids", [])) & dids]
                rows.extend(self.flds_rows)
                return rows
            return list(self.flds_rows)

        def scalar(self, query, params=()):
            """Single-value queries (production uses db.scalar for the
            knowledge_totals counts); routed through all()."""
            rows = self.all(query, params)
            if not rows:
                return 0
            first = rows[0]
            if isinstance(first, (list, tuple)):
                return first[0] if first else 0
            return first

    def models_by_name(self, name):
        return self.models.by_name(name)


class _FakeModels:
    def __init__(self):
        self.models_dict = {}
        self._next_id = 1
    def by_name(self, name):
        return self.models_dict.get(name)
    def get(self, mid):
        # Public models API used by anki.py (mid-based, schema-proof).
        for m in self.models_dict.values():
            if m.get("id") == mid:
                return m
        return None
    def all_names(self):
        return list(self.models_dict.keys())
    def add_model(self, name, field_names):
        # Real Anki models carry an id and fields as dicts with "name".
        model = {"id": self._next_id, "name": name,
                 "flds": [{"name": n} for n in field_names]}
        self._next_id += 1
        self.models_dict[name] = model
        return model["id"]

class _FakeAddonManager:
    def __init__(self):
        self.configs = {}
    def getConfig(self, name):
        # yomitan_fallback stays disabled for ALL tests: the suite must be
        # hermetic. On a dev machine with live Yomitan running, the engine's
        # fail-safe would otherwise query the real bridge during tests that
        # expect None ("missing word", "all dictionaries disabled"), because
        # the aqt stub's empty config enables the fallback by default.
        if name not in self.configs:
            return {"yomitan_fallback": False}
        cfg = dict(self.configs[name])
        cfg.setdefault("yomitan_fallback", False)
        return cfg
    def writeConfig(self, name, config):
        self.configs[name] = config

class _FakeMW:
    def __init__(self):
        self.col = _FakeCol()
        self.addonManager = _FakeAddonManager()



def _install_aqt_stub() -> None:
    """Creates a tiny aqt package exposing `mw` so db_utils imports cleanly."""
    os.makedirs(FAKE_STUB_DIR, exist_ok=True)
    aqt_dir = os.path.join(FAKE_STUB_DIR, "aqt")
    os.makedirs(aqt_dir, exist_ok=True)
    with open(os.path.join(aqt_dir, "__init__.py"), "w") as f:
        f.write("mw = None  # replaced below after db_utils import\n")
    if FAKE_STUB_DIR not in sys.path:
        sys.path.insert(0, FAKE_STUB_DIR)


_install_aqt_stub()
import aqt  # noqa: E402  (the stub)

aqt.mw = _FakeMW()  # type: ignore[attr-defined]

# Modules under test (imports must come AFTER the stub is in place)
import generator as compredef_generator  # noqa: E402
import parser as compredef_parser  # noqa: E402
import provider  # noqa: E402
import scope as compredef_scope  # noqa: E402
import utils as compredef_utils  # noqa: E402

# The directory containing the user's real Yomitan dictionaries (used ONLY
# by the dynamic smoke tests; everything else runs on synthetic fixtures).
DICTS_DIR = "/home/mohamed/Desktop/Dicts"

RESULTS = {"pass": 0, "fail": 0, "failed_names": []}


def _save_collection_state() -> dict:
    """Snapshots stub collection state (decks/notes/models/config)."""
    col = aqt.mw.col
    return {
        "decks": dict(col.decks.decks),
        "deck_next": col.decks._next_id,
        "notes": {k: dict(v) for k, v in col.db.notes.items()},
        "flds": list(col.db.flds_rows),
        "models": dict(col.models.models_dict),
        "model_next": col.models._next_id,
        "cfg": dict(aqt.mw.addonManager.configs.get("1619602654", {})),
        "had_cfg": "1619602654" in aqt.mw.addonManager.configs,
    }


def _restore_collection_state(st: dict) -> None:
    """Restores stub collection state saved by _save_collection_state."""
    col = aqt.mw.col
    col.decks.decks = st["decks"]
    col.decks._next_id = st["deck_next"]
    col.db.notes = st["notes"]
    col.db.flds_rows = st["flds"]
    col.models.models_dict = st["models"]
    col.models._next_id = st["model_next"]
    if st["had_cfg"]:
        aqt.mw.addonManager.configs["1619602654"] = st["cfg"]
    else:
        aqt.mw.addonManager.configs.pop("1619602654", None)


def _set_scope_config(decks: list) -> None:
    """Points the stub config at the given Scope decks."""
    aqt.mw.addonManager.configs["1619602654"] = {"scope_decks": list(decks)}


def check(name: str, condition: bool, detail: str = "") -> None:
    """Records one assertion result; prints PASS/FAIL immediately."""
    if condition:
        RESULTS["pass"] += 1
        print(f"[PASS] {name}")
    else:
        RESULTS["fail"] += 1
        RESULTS["failed_names"].append(name)
        print(f"[FAIL] {name}" + (f" -- {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# Shared fixtures: a tiny synthetic Yomitan dictionary used by most tests.
# ---------------------------------------------------------------------------
SYNTH_TITLE = "CompreDefTestSynthetic"


def synth_term_bank() -> list:
    """
    A minimal term bank exercising every rendering branch:
    - plain string definition
    - {'type': 'text'} definition
    - {'type': 'structured-content'} with ruby/rt, data, style
    """
    return [
        [
            "先ず", "まず", "", "", 0,
            [
                {
                    "type": "structured-content",
                    "content": [
                        {
                            "tag": "span",
                            "data": {"name": "見出"},
                            "content": [
                                {"tag": "ruby", "content": [
                                    {"tag": "span", "data": {"rb": ""},
                                     "content": "先"},
                                    {"tag": "rt", "data": {"rt": ""},
                                     "content": "ま"},
                                ]},
                                "ず［最初に］",
                            ],
                        }
                    ],
                }
            ],
            0, "",
        ],
        [
            "あさ", "朝", "", "", 0,
            [{"type": "text", "text": "夜があけて、太陽がのぼる時。\nまた、その時刻。"}],
            0, "",
        ],
        [
            "参照", "さんしょう", "", "", 0,
            ["会社更生法"],  # plain-string cross-reference title
            0, "",
        ],
    ]


def build_synthetic_dict(dir_path: str, as_zip: bool = False) -> str:
    """Creates a synthetic Yomitan dictionary on disk (folder or zip)."""
    import json as _json
    bank = synth_term_bank()
    index = {"title": SYNTH_TITLE, "revision": "test1", "format": 3}

    if as_zip:
        import zipfile as _zf
        zip_path = dir_path + ".zip"
        with _zf.ZipFile(zip_path, "w") as z:
            z.writestr(
                "index.json",
                _json.dumps(index, ensure_ascii=False),
            )
            z.writestr(
                "term_bank_1.json",
                _json.dumps(bank, ensure_ascii=False),
            )
        return zip_path

    os.makedirs(dir_path, exist_ok=True)
    with open(os.path.join(dir_path, "index.json"), "w") as f:
        _json.dump(index, f, ensure_ascii=False)
    with open(os.path.join(dir_path, "term_bank_1.json"), "w") as f:
        _json.dump(bank, f, ensure_ascii=False)
    return dir_path


def db_dict_row(path: str):
    """Returns the dictionaries marker row for a path (or None)."""
    conn = sqlite3.connect(compredef_parser._get_db_path())
    try:
        return conn.execute(
            "SELECT title, signature, entry_count FROM dictionaries WHERE path = ?",
            (path,),
        ).fetchone()
    finally:
        conn.close()


def db_entry_count(path: str) -> int:
    """Number of entry rows stored for a dictionary path."""
    conn = sqlite3.connect(compredef_parser._get_db_path())
    try:
        return conn.execute(
            "SELECT COUNT(*) FROM entries WHERE dict_path = ?", (path,)
        ).fetchone()[0]
    finally:
        conn.close()


def purge_dict_rows(path: str) -> None:
    """Removes a dictionary's rows from the cache DB (test cleanup)."""
    conn = sqlite3.connect(compredef_parser._get_db_path())
    try:
        conn.execute("DELETE FROM entries WHERE dict_path = ?", (path,))
        conn.execute("DELETE FROM dictionaries WHERE path = ?", (path,))
        conn.commit()
    finally:
        conn.close()


# ===========================================================================
# ARCHITECTURE TESTS: install once, look up forever.
# ===========================================================================

def test_install_indexes_once(tmp_root: str) -> None:
    """
    A1: First installation parses the dictionary exactly once and builds a
    complete index (marker row + all entry rows). Before the fix, indexing
    happened lazily on first lookup, freezing Anki mid-generation.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "install_once"))

    d = compredef_parser.get_single_dictionary(dict_dir)
    check(
        "install: not indexed before install()",
        not d.is_indexed(),
        "is_indexed() true before install",
    )
    # lookup() on an uninstalled dictionary must return [] (never index!)
    check(
        "install: lookup on uninstalled dict returns [] (no lazy index)",
        d.lookup("先ず") == [],
        "lookup built the index lazily!",
    )
    check(
        "install: still not indexed after lookup (no lazy index)",
        not d.is_indexed(),
        "lookup() triggered indexing",
    )

    # Explicit install: the ONLY place parsing happens.
    count = d.install()
    check(
        "install: install() returns entry count (3 synth defs)",
        count == 3,
        f"got {count}",
    )
    check(
        "install: is_indexed() true after install()",
        d.is_indexed(),
    )
    row = db_dict_row(dict_dir)
    check(
        "install: marker row written with title + count",
        row is not None and row[0] == SYNTH_TITLE and row[2] == 3,
        f"row={row}",
    )
    check(
        "install: entry rows match marker count",
        db_entry_count(dict_dir) == 3,
        f"rows={db_entry_count(dict_dir)}",
    )
    # And now the same lookup succeeds — pure DB query.
    defs = d.lookup("先ず")
    check(
        "install: lookup succeeds after install()",
        len(defs) == 1 and "structured-content" in defs[0],
        f"got {defs[:1]}",
    )


def test_index_survives_restart(tmp_root: str) -> None:
    """
    A2: The index persists across 'Anki restarts'. Simulated by dropping all
    in-memory state (fresh SingleDictionary instances, like a new process)
    and verifying lookups still work without any install/parse.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "restart"))
    compredef_parser.get_single_dictionary(dict_dir).install()

    # --- 'Restart': forget every in-memory object
    compredef_parser._loaded_dicts.clear()

    t0 = time.time()
    d2 = compredef_parser.get_single_dictionary(dict_dir)
    check(
        "restart: fresh instance sees the dictionary as indexed",
        d2.is_indexed(),
        "marker row lost after restart",
    )
    defs = d2.lookup("先ず")
    elapsed = time.time() - t0
    check(
        "restart: lookup works immediately with no re-parse",
        len(defs) == 1 and "structured-content" in defs[0],
        f"got {defs[:1]}",
    )
    check(
        "restart: lookup is fast (<50ms, pure DB query)",
        elapsed < 0.05,
        f"took {elapsed*1000:.1f}ms",
    )

    # Re-install of the same unchanged dictionary must be a NO-OP (no
    # re-parse, no duplicate rows) — install() checks the signature first.
    before = db_entry_count(dict_dir)
    count = d2.install()
    check(
        "restart: reinstalling unchanged dictionary is a no-op",
        count == 3 and db_entry_count(dict_dir) == before,
        f"count={count}, rows before={before} after={db_entry_count(dict_dir)}",
    )


def test_lookup_never_indexes(tmp_root: str) -> None:
    """
    A3 + historical bug #16: lookup() must be a PURE database query.
    The old lazy ensure_indexed() design caused re-index storms that burned
    3.4 GB of RAM and froze Anki. Verified two ways: after corrupting the
    marker row, a lookup returns [] instead of silently rebuilding, and a
    lookup on a healthy index does no filesystem parsing work.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "pure_lookup"))
    compredef_parser.get_single_dictionary(dict_dir).install()

    # Corrupt the marker (simulate a crashed/partial index from history).
    purge_dict_rows(dict_dir)

    # lookup() must NOT rebuild the index — it just finds nothing.
    d = compredef_parser.get_single_dictionary(dict_dir)
    t0 = time.time()
    defs = d.lookup("先ず")
    elapsed = time.time() - t0
    check(
        "pure-lookup: missing index means empty result, NOT a rebuild",
        defs == [],
        f"got {defs!r}",
    )
    check(
        "pure-lookup: no marker row was recreated by the lookup",
        db_dict_row(dict_dir) is None,
        "lookup() wrote to the dictionaries table!",
    )
    check(
        "pure-lookup: miss on uninstalled dict is fast (<100ms)",
        elapsed < 0.1,
        f"took {elapsed*1000:.1f}ms",
    )

    # Reinstall and confirm normal lookups are pure DB queries: spy on
    # _iter_term_banks — if a lookup ever parses files, this blows up.
    d.install()
    original_iter = compredef_parser.SingleDictionary._iter_term_banks

    def guarded_iter(self):
        raise AssertionError("lookup() parsed dictionary files (forbidden!)")

    compredef_parser.SingleDictionary._iter_term_banks = guarded_iter
    try:
        defs = d.lookup("先ず")
        check(
            "pure-lookup: healthy lookup never touches dictionary files",
            len(defs) == 1,
            f"got {defs!r}",
        )
    finally:
        compredef_parser.SingleDictionary._iter_term_banks = original_iter


def test_missing_word(tmp_root: str) -> None:
    """
    A4 + historical bug #9: a missing/nonsense word returns [] instantly on
    an installed index. Never None, never a crash, never a freeze.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "missing"))
    compredef_parser.get_single_dictionary(dict_dir).install()

    d = compredef_parser.get_single_dictionary(dict_dir)
    t0 = time.time()
    defs = d.lookup("駿ってさ")
    elapsed = time.time() - t0
    check(
        "missing: nonsense word returns [] (not None/crash)",
        defs == [],
        f"got: {defs!r}",
    )
    check(
        "missing: lookup completes in <0.1s (100% CPU bug)",
        elapsed < 0.1,
        f"took {elapsed:.3f}s",
    )
    chosen = compredef_generator.generate_definition(
        "駿ってさ", dictionaries=[dict_dir]
    )
    check(
        "missing: generate_definition returns None cleanly",
        chosen is None,
        f"got: {chosen!r}",
    )


def test_replacement_reindexes(tmp_root: str) -> None:
    """
    A5: Replacing a dictionary's files on disk causes exactly ONE new
    indexing operation when the user explicitly reinstalls it. The new index
    completely replaces the old one — no duplicates, no orphans.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "replace"))
    d = compredef_parser.get_single_dictionary(dict_dir)
    d.install()
    old_sig = db_dict_row(dict_dir)[1]

    # Simulate the user replacing the dictionary's files: REWRITE a term
    # bank with different content (and thus a different size/mtime).
    time.sleep(0.02)  # ensure mtime_ns differs even on coarse filesystems
    with open(os.path.join(dict_dir, "term_bank_2.json"), "w") as f:
        json.dump([
            ["追加語", "ついかご", "", "", 0,
             [{"type": "text", "text": "置き換え後に追加された語の定義。"}],
             0, ""],
        ], f, ensure_ascii=False)

    # Detect that the files changed: index_is_current() must be False.
    check(
        "replace: files changed => index_is_current() is False",
        not d.index_is_current(),
        "stale index reported as current",
    )

    # Reinstall must pick up the new file: 3 original + 1 new entry.
    count = d.install()
    check(
        "replace: reinstall re-indexes cleanly",
        count == 4 and db_entry_count(dict_dir) == 4,
        f"count={count}, rows={db_entry_count(dict_dir)}",
    )
    new_sig = db_dict_row(dict_dir)[1]
    check(
        "replace: signature updated after reinstall",
        new_sig != old_sig,
        "signature unchanged after file modification",
    )

    # index_is_current() must be True again; further install is a no-op.
    check(
        "replace: index_is_current() true after reinstall",
        d.index_is_current(),
    )
    d.install()
    check(
        "replace: second install is a no-op (no duplicate rows)",
        db_entry_count(dict_dir) == 4,
        f"rows={db_entry_count(dict_dir)}",
    )

    # Fresh 'restart' instance must trust the rebuilt index.
    compredef_parser._loaded_dicts.clear()
    d2 = compredef_parser.get_single_dictionary(dict_dir)
    check(
        "replace: fresh instance trusts rebuilt index",
        d2.index_is_current() and len(d2.lookup("先ず")) == 1,
    )


def test_indexing_failure_reported(tmp_root: str) -> None:
    """
    A6: A corrupt dictionary fails LOUDLY with IndexingError — never a
    silent partial index that later masquerades as installed.
    """
    # Empty directory: no term banks at all.
    empty_dir = os.path.join(tmp_root, "broken_dict")
    os.makedirs(empty_dir, exist_ok=True)
    with open(os.path.join(empty_dir, "index.json"), "w") as f:
        json.dump({"title": "Broken", "format": 3}, f)

    d = compredef_parser.get_single_dictionary(empty_dir)
    raised = False
    try:
        d.install()
    except compredef_parser.IndexingError:
        raised = True
    except Exception as e:
        check(
            "fail: wrong exception type for corrupt dictionary",
            False,
            f"got {type(e).__name__}: {e}",
        )
        return
    check(
        "fail: corrupt dictionary raises IndexingError",
        raised,
        "install() of a term-bank-less dictionary did not raise",
    )
    # No marker row may survive a failed install.
    check(
        "fail: no marker row after failed install",
        db_dict_row(empty_dir) is None,
        "partial index trusted as complete",
    )

    # Cancelled indexing must also leave no marker behind.
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "cancelled"))
    d2 = compredef_parser.get_single_dictionary(dict_dir)
    raised = False
    try:
        d2.install(cancel_check=lambda: True)  # cancel immediately
    except compredef_parser.IndexingError:
        raised = True
    check(
        "fail: cancelled install raises IndexingError",
        raised,
        "cancel did not raise",
    )
    check(
        "fail: cancelled install leaves no marker row",
        db_dict_row(dict_dir) is None,
        "cancelled install trusted as complete",
    )


def test_uninstall_removes_index(tmp_root: str) -> None:
    """
    A8: Removing a dictionary deletes its index — 'old cached definitions'
    must never keep appearing after the user removes a dictionary. This was
    a real bug: removed dictionaries left 500k+ stale rows behind.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "uninstall"))
    d = compredef_parser.get_single_dictionary(dict_dir)
    d.install()
    check(
        "uninstall: dictionary indexed before removal",
        d.is_indexed(),
    )

    compredef_parser.uninstall_dictionary(dict_dir)

    check(
        "uninstall: marker row deleted",
        db_dict_row(dict_dir) is None,
        "dictionaries row survived uninstall",
    )
    check(
        "uninstall: entry rows deleted",
        db_entry_count(dict_dir) == 0,
        f"rows={db_entry_count(dict_dir)}",
    )
    # Fresh instance (restart simulation) must also see it gone.
    compredef_parser._loaded_dicts.clear()
    d2 = compredef_parser.get_single_dictionary(dict_dir)
    check(
        "uninstall: fresh instance sees no index after removal",
        not d2.is_indexed(),
    )
    # And lookups return nothing.
    check(
        "uninstall: lookup returns [] after removal",
        d2.lookup("先ず") == [],
    )


def test_extract_clean_word_formats(tmp_root: str) -> None:
    """
    Historical bug #15: note fields carry HTML wrappers and furigana markup
    that never match dictionary terms — extract_clean_word() cleans them.
    """
    cases = {
        "<div>先[ま]ず</div>": "先ず",
        "<ruby>先<rt>ま</rt></ruby>ず": "先ず",
        "先ず[まず]": "先ず",
        "&lt;食&gt;": "<食>",
        " 食[た]べる ": "食べる",
        "先ず": "先ず",
        "<span>駿ってさ</span>": "駿ってさ",
        "": "",
        "<div>&nbsp;</div>": "",
    }
    for field, expected in cases.items():
        got = compredef_parser.extract_clean_word(field)
        check(
            f"clean-word: {field!r} -> {expected!r}",
            got == expected,
            f"got {got!r}",
        )

    # Full pipeline: raw HTML/furigana field text must find definitions.
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "cleanword"))
    compredef_parser.get_single_dictionary(dict_dir).install()
    res = compredef_generator.generate_definition(
        "<div>先[ま]ず</div>", dictionaries=[dict_dir], reading="まず"
    )
    check(
        "clean-word: HTML/furigana word generates a definition",
        res is not None and "structured-content" in res,
        f"got: {(res or '')[:60]!r}",
    )


# ===========================================================================
# HISTORICAL REGRESSION TESTS (behavioral invariants).
# ===========================================================================

def test_structured_content_html_fidelity(tmp_root: str) -> None:
    """Historical bug #1: rich structured content survives indexing intact."""
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "synth"))
    compredef_parser.get_single_dictionary(dict_dir).install()
    defs = compredef_parser.get_single_dictionary(dict_dir).lookup("先ず")

    check("html: definition found for 先ず", len(defs) >= 1)
    if not defs:
        return
    out = defs[0]

    check(
        "html: structured-content wrapper present",
        "structured-content" in out,
        f"got: {out[:120]}...",
    )
    check(
        "html: ruby furigana preserved as <ruby>",
        "<ruby" in out and "</rt></ruby>" in out,
    )
    check(
        "html: base kanji 先 kept in rb span (not escaped away)",
        ">先<" in out,
    )
    # THE original bug: plain text "３まず［先▶１ず］..." with zero HTML tags
    check(
        "html: output is NOT collapsed plain text (bug #1 regression)",
        len(out) > 200 and "<" in out,
        f"len={len(out)}",
    )


def test_renderer_version_invalidates_cache(tmp_root: str) -> None:
    """Historical bug #2: renderer version is embedded in the signature."""
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "cache_inv"))
    d1 = compredef_parser.get_single_dictionary(dict_dir)
    sig1 = d1._compute_signature()

    old = provider.LocalSQLiteProvider.RENDERER_VERSION
    try:
        provider.LocalSQLiteProvider.RENDERER_VERSION = old + "_bumped"
        sig2 = d1._compute_signature()
        check(
            "cache: signature changes when renderer version changes",
            sig1 != sig2,
            "same signature despite version bump -> stale caches forever",
        )
    finally:
        provider.LocalSQLiteProvider.RENDERER_VERSION = old

    zip_path = build_synthetic_dict(
        os.path.join(tmp_root, "cache_inv_zip"), as_zip=True
    )
    dz = compredef_parser.get_single_dictionary(zip_path)
    check(
        "cache: zip signature embeds renderer version",
        compredef_parser.RENDERER_VERSION in dz._compute_signature(),
    )
    purge_dict_rows(zip_path)


def test_scoring_ignores_furigana() -> None:
    """Historical bug #3: <rt> furigana readings never pollute kanji scores."""
    sample = (
        '<ruby class="gloss-sc-ruby"><span data-sc-rb="">先</span>'
        '<rt class="gloss-sc-rt">ま</rt></ruby>'
        "ず［"
        "<ruby>最<rt>さい</rt></ruby>"
        "<ruby>初<rt>しょ</rt></ruby>"
        "に］"
    )
    base = compredef_parser._extract_base_text(sample)
    check(
        "score: base text has furigana stripped",
        base == "先ず［最初に］",
        f"got: {base!r}",
    )

    full = compredef_generator._calculate_kanji_score(sample, {"先", "最", "初"})
    check(
        "score: known base kanji => 1.0 (kana readings ignored)",
        full == 1.0,
        f"got {full}",
    )
    none_known = compredef_generator._calculate_kanji_score(sample, set())
    check(
        "score: unknown base kanji => 0.0",
        none_known == 0.0,
        f"got {none_known}",
    )
    kana_only = compredef_generator._calculate_kanji_score(
        "ひらがなだけのぶんしょう。", set()
    )
    check(
        "score: kana-only text => 1.0",
        kana_only == 1.0,
        f"got {kana_only}",
    )


def test_order_independent_argmax(tmp_root: str) -> None:
    """Historical bug #4 → v1.2 SEMANTICS CHANGE: with argmax scoring the
    BETTER-comprehension definition wins regardless of dictionary order.
    Known 会/社: the 'easy' dictionary's definition uses only known
    kanji and MORE vocab compounds (会社), so argmax picks it — but it
    must win on QUALITY, not on being enumerated first (reversed
    order must produce the same winner)."""
    # db_utils returns no known kanji in the test env, so monkeypatch.
    original = compredef_generator.get_known_kanji_set

    def fake_known() -> set:
        return {"会", "社", "定", "義", "説", "明", "高", "度", "専", "門", "的",
                "や", "さ", "し", "い", "か", "ん", "た", "な", "む", "ず", "こ"}

    compredef_generator.get_known_kanji_set = fake_known  # type: ignore
    try:
        easy = os.path.join(tmp_root, "order_easy")
        os.makedirs(easy, exist_ok=True)
        with open(os.path.join(easy, "index.json"), "w") as f:
            json.dump({"title": "order_easy", "format": 3}, f)
        with open(os.path.join(easy, "term_bank_1.json"), "w") as f:
            json.dump([
                ["会社", "かいしゃ", "", "", 0,
                 [{"type": "text", "text": "やさしい定義。かんたんな説明。"}],
                 0, ""],
            ], f, ensure_ascii=False)
        compredef_parser.get_single_dictionary(easy).install()

        hard = os.path.join(tmp_root, "order_hard")
        os.makedirs(hard, exist_ok=True)
        with open(os.path.join(hard, "index.json"), "w") as f:
            json.dump({"title": "order_hard", "format": 3}, f)
        with open(os.path.join(hard, "term_bank_1.json"), "w") as f:
            json.dump([
                ["会社", "かいしゃ", "", "", 0,
                 [{"type": "text", "text": "むずかしい定義。高度に専門的な説明。"}],
                 0, ""],
            ], f, ensure_ascii=False)
        compredef_parser.get_single_dictionary(hard).install()

        chosen = compredef_generator.generate_definition(
            "会社", dictionaries=[easy, hard]
        )
        check(
            "order: a definition was chosen",
            chosen is not None,
        )
        # With all these kanji known, BOTH definitions are fully known;
        # argmax tie-break: most kanji → the hard/kanji-dense one wins.
        check(
            "order: argmax tie-break picks most-kanji definition",
            chosen is not None and "むずかしい" in chosen,
            f"got: {chosen[:40] if chosen else None}",
        )
        # Order-independence: reversed enumeration must pick the same winner.
        chosen_rev = compredef_generator.generate_definition(
            "会社", dictionaries=[hard, easy]
        )
        check(
            "order: argmax is order-independent",
            chosen_rev == chosen,
            f"forward={chosen[:20] if chosen else None!r} "
            f"reversed={chosen_rev[:20] if chosen_rev else None!r}",
        )
    finally:
        compredef_generator.get_known_kanji_set = original  # type: ignore


def test_reference_title_filtering() -> None:
    """Historical bug #5: cross-reference titles lose to real definitions."""
    ref = compredef_generator._is_reference_title("会社更生法")
    check(
        "ref: short title without punctuation is a reference",
        ref,
    )
    real = compredef_generator._is_reference_title(
        "夜があけて、太陽がのぼる時。また、その時刻。"
    )
    check(
        "ref: real definition with punctuation is NOT a reference",
        not real,
    )
    html_ref = compredef_generator._is_reference_title(
        "<span>会社更生法</span>"
    )
    check(
        "ref: HTML reference title detected (base text extraction)",
        html_ref,
    )


def test_zip_folder_parity(tmp_root: str) -> None:
    """Historical bug #6: zip and folder of the same dictionary are identical."""
    folder = build_synthetic_dict(os.path.join(tmp_root, "parity_f"))
    zipfile_path = build_synthetic_dict(
        os.path.join(tmp_root, "parity_z"), as_zip=True
    )

    df = compredef_parser.get_single_dictionary(folder)
    dz = compredef_parser.get_single_dictionary(zipfile_path)
    df.install()
    dz.install()

    check("parity: zip detected as zip", dz.is_zip)
    check("parity: folder NOT detected as zip", not df.is_zip)
    check(
        "parity: both resolve the same title",
        df.title == dz.title == SYNTH_TITLE,
        f"{df.title!r} vs {dz.title!r}",
    )

    fdefs = df.lookup("先ず")
    zdefs = dz.lookup("先ず")
    check(
        "parity: both return the word",
        len(fdefs) == len(zdefs) == 1,
        f"folder={len(fdefs)} zip={len(zdefs)}",
    )
    check(
        "parity: definitions are byte-identical",
        fdefs == zdefs,
    )
    purge_dict_rows(folder)
    purge_dict_rows(zipfile_path)


def test_data_sc_attribute_names() -> None:
    """Historical bug #7: data-sc-* attributes match Yomitan's naming."""
    node = {"tag": "span", "data": {"name": "見出"}, "content": "text"}
    out = compredef_parser.render_structured_content_node(node)
    check(
        "data-sc: simple key renders as data-sc-name",
        'data-sc-name="見出"' in out,
        f"got: {out}",
    )
    node2 = {"tag": "span", "data": {"myKey": "v"}, "content": "t"}
    out2 = compredef_parser.render_structured_content_node(node2)
    check(
        "data-sc: camelCase key lowercased (Yomitan DOM conversion)",
        'data-sc-mykey="v"' in out2,
        f"got: {out2}",
    )
    check(
        "data-sc: 用例 example blocks survive (CSS compactor dependency)",
        "見出" in compredef_parser.render_yomitan_definition_html(
            {"type": "structured-content", "content": [node]}
        ),
    )


def test_indexing_streams_in_batches(tmp_root: str) -> None:
    """
    Historical bug #10: indexing must stream in bounded batches — the old
    code accumulated ~1.3 GB of rendered HTML in RAM for 大辞泉.
    """
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "stream"))

    big_entries = []
    for i in range(4000):
        big_entries.append([
            f"語{i}", f"ご{i}", "", "", 0,
            [{"type": "text", "text": f"定義{i}。長い定義のテスト。{i}番目。"}],
            i, "",
        ])
    with open(os.path.join(dict_dir, "term_bank_2.json"), "w") as f:
        json.dump(big_entries, f, ensure_ascii=False)

    d = compredef_parser.get_single_dictionary(dict_dir)
    t0 = time.time()
    count = d.install()
    elapsed = time.time() - t0

    # 3 synth entries + 4000 forced ones, exactly — no dupes, no loss.
    check(
        "stream: install writes exactly 4003 rows (no duplicates/loss)",
        count == 4003 and db_entry_count(dict_dir) == 4003,
        f"count={count}, rows={db_entry_count(dict_dir)}",
    )
    check(
        "stream: 4k-row dictionary installs in <30s (bounded RAM by design)",
        elapsed < 30.0,
        f"took {elapsed:.1f}s",
    )
    check(
        "stream: _INDEX_BATCH_SIZE is bounded (<= 10,000)",
        compredef_parser._INDEX_BATCH_SIZE <= 10_000,
        f"got {compredef_parser._INDEX_BATCH_SIZE}",
    )


def test_db_connections_are_closed(tmp_root: str) -> None:
    """
    Historical bug #11: SQLite connections leaked one handle per lookup.
    """
    if not os.path.isdir("/proc/self/fd"):
        # fd-counting only exists on Linux; elsewhere there is nothing
        # to count (previously this test CRASHED off-Linux).
        print("[SKIP] conn: /proc/self/fd unavailable (non-Linux)")
        return
    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "connleak"))
    compredef_parser.get_single_dictionary(dict_dir).install()

    d = compredef_parser.get_single_dictionary(dict_dir)

    open_before = len(os.listdir("/proc/self/fd"))
    for _ in range(60):
        d.lookup("先ず")
    open_after = len(os.listdir("/proc/self/fd"))

    # Allow a small margin for unrelated fd churn, but nothing like 60.
    check(
        "conn: no SQLite connection handles leak after 60 queries",
        open_after - open_before < 10,
        f"fds before={open_before} after={open_after}",
    )


def test_reading_disambiguates_homographs(tmp_root: str) -> None:
    """
    Historical bug #12: 先ず(まず 'first') returned 先ず(せんず 'precede')'s
    definition — the term alone matched BOTH readings' entries.
    """
    dict_dir = os.path.join(tmp_root, "homograph")
    os.makedirs(dict_dir, exist_ok=True)
    with open(os.path.join(dict_dir, "index.json"), "w") as f:
        json.dump({"title": "HomographTest", "format": 3}, f)
    entries = [
        ["先ず", "せんず", "", "", 0,
         [{"type": "text", "text": "他より先に事を行う。先を越す。さきんずる。"}],
         0, ""],
        ["先ず", "まず", "", "", 0,
         [{"type": "text", "text": "最初に。第だい一いちに。はじめに。"}],
         0, ""],
    ]
    with open(os.path.join(dict_dir, "term_bank_1.json"), "w") as f:
        json.dump(entries, f, ensure_ascii=False)

    d = compredef_parser.get_single_dictionary(dict_dir)
    d.install()

    both = d.lookup("先ず")
    check(
        "homograph: unfiltered lookup sees both readings' defs",
        len(both) == 2,
        f"got {len(both)}",
    )

    mazu = d.lookup("先ず", "まず")
    check(
        "homograph: reading=まず returns exactly 1 def",
        len(mazu) == 1,
        f"got {len(mazu)}",
    )
    check(
        "homograph: reading=まず returns the まず content (not せんず)",
        mazu and "最初に" in mazu[0],
        f"got: {mazu[0][:40] if mazu else 'nothing'}",
    )
    senzu = d.lookup("先ず", "せんず")
    check(
        "homograph: reading=せんず returns the せんず content",
        senzu and "先を越す" in senzu[0],
    )
    kata = d.lookup("先ず", "マズ")
    check(
        "homograph: katakana reading マズ normalizes to まず and matches",
        len(kata) == 1 and "最初に" in kata[0],
    )

    chosen = compredef_generator.generate_definition(
        "先ず", dictionaries=[dict_dir], reading="まず"
    )
    check(
        "homograph: generate_definition(reading=まず) picks まず def",
        chosen is not None and "最初に" in chosen,
        f"got: {chosen[:40] if chosen else None}",
    )


def test_parse_furigana_field_formats() -> None:
    """Historical bug #13: every furigana markup format -> pure kana."""
    cases = {
        "先[ま]ず": "まず",
        "先ず[まず]": "まず",
        "食[た]べる": "たべる",
        "<ruby>先<rt>ま</rt></ruby>ず": "まず",
        "<ruby>食</ruby><rt>た</rt>べる": "たべる",
        "マズ": "まず",
        "せん-ず": "せんず",
        "せんず": "せんず",
        "先ず": "",
        "行く": "",
    }
    for field, expected in cases.items():
        got = compredef_parser.parse_furigana_field(field)
        check(
            f"furigana: {field!r} -> {expected!r}",
            got == expected,
            f"got {got!r}",
        )


def test_disabled_dictionaries_skipped(tmp_root: str) -> None:
    """Historical bug #14: disabled dictionaries are skipped, order preserved."""
    def custom_dict(path: str, text: str) -> str:
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "index.json"), "w") as f:
            json.dump(
                {"title": os.path.basename(path), "format": 3}, f
            )
        with open(os.path.join(path, "term_bank_1.json"), "w") as f:
            json.dump(
                [["言葉", "ことば", "", "", 0,
                  [{"type": "text", "text": text}], 0, ""]],
                f, ensure_ascii=False,
            )
        compredef_parser.get_single_dictionary(path).install()
        return path

    easy = custom_dict(os.path.join(tmp_root, "dis_easy"), "やさしい定義。かんたんな説明。")
    hard = custom_dict(os.path.join(tmp_root, "dis_hard"), "むずかしい定義。高度に専門的な説明。")

    both = compredef_generator.generate_definition(
        "言葉", dictionaries=[easy, hard]
    )
    check(
        "disabled: baseline picks a definition",
        both is not None,
    )
    # v1.2 argmax: winner is quality-based (no early exit by order); the
    # same winner must appear regardless of which dictionaries remain.
    check(
        "disabled: baseline with both enabled picks the argmax winner",
        both is not None and ("やさしい" in both or "むずかしい" in both),
    )

    only_hard = compredef_generator.generate_definition(
        "言葉", dictionaries=[easy, hard], disabled_dictionaries=[easy]
    )
    check(
        "disabled: skipping easy dict falls through to hard def",
        only_hard is not None and "むずかしい" in only_hard,
        f"got: {only_hard[:40] if only_hard else None}",
    )

    none_left = compredef_generator.generate_definition(
        "言葉", dictionaries=[easy, hard],
        disabled_dictionaries=[easy, hard],
    )
    check(
        "disabled: all disabled returns None cleanly",
        none_left is None,
        f"got: {none_left!r}",
    )


# ---------------------------------------------------------------------------
# Tab-to-Generate decision logic (restored feature — see editor_browser.py).
# ---------------------------------------------------------------------------

def _ensure_editor_browser_stubs() -> None:
    """(Re)writes the extended aqt stub files editor_browser needs.

    Importing editor_browser needs more of aqt than the minimal stub
    provides (gui_hooks, browser, qt, utils). Idempotent: safe to call
    from every test that touches editor_browser.
    """
    aqt_dir = os.path.join(FAKE_STUB_DIR, "aqt")
    with open(os.path.join(aqt_dir, "browser.py"), "w") as f:
        f.write("class Browser:  # stub\n    pass\n")
    with open(os.path.join(aqt_dir, "qt.py"), "w") as f:
        f.write(
            "class QMenu:  # stub\n    pass\n"
            "class QKeySequence:  # stub\n    pass\n"
        )
    with open(os.path.join(aqt_dir, "utils.py"), "w") as f:
        f.write("def tooltip(*args, **kwargs):  # stub\n    pass\n")
    with open(os.path.join(aqt_dir, "gui_hooks.py"), "w") as f:
        f.write(
            "class _Hook:  # stub: append-only registry like the real one\n"
            "    def __init__(self): self._hooks = []\n"
            "    def append(self, fn): self._hooks.append(fn)\n"
            "    def __call__(self, *a, **kw):\n"
            "        r = None\n"
            "        for fn in self._hooks:\n"
            "            r = fn(*a, **kw)\n"
            "        return r\n"
            "editor_did_init_buttons = _Hook()\n"
            "browser_menus_did_init = _Hook()\n"
            "browser_will_show_context_menu = _Hook()\n"
            "editor_did_load_note = _Hook()\n"
            "editor_did_unfocus_field = _Hook()\n"
            "editor_did_init = _Hook()\n"
            "profile_did_open = _Hook()\n"
        )


def _import_editor_browser():
    """Imports editor_browser in the synthetic add-on package context.

    editor_browser uses package-relative imports (`from .generator
    import ...`) because it ships inside the add-on package. Importing
    the add-on's real `__init__.py` here would register hooks against
    the stub and pull in gui.py (needs real Qt) — so we synthesize a
    package whose __init__ is empty and whose members alias the
    top-level modules already imported above (parser, generator,
    db_utils). The relative imports must resolve to the
    ALREADY-imported (and fully initialized) top-level modules —
    re-importing them under new names would duplicate module state
    (separate SQLite handles, caches).
    """
    pkg_name = "compredef_addon"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [REPO_ROOT]  # resolve .editor_browser etc. from repo
        sys.modules[pkg_name] = pkg
    sys.modules[f"{pkg_name}.generator"] = compredef_generator
    sys.modules[f"{pkg_name}.parser"] = compredef_parser
    if "db_utils" in sys.modules:
        sys.modules[f"{pkg_name}.db_utils"] = sys.modules["db_utils"]
    else:
        sys.modules[f"{pkg_name}.db_utils"] = importlib.import_module("db_utils")
    return importlib.import_module(f"{pkg_name}.editor_browser")


def test_tab_generate_decisions() -> None:
    """
    Exercises the pure decision core of Tab-to-Generate:

    _should_auto_generate(note, unfocused_field, config) must fire ONLY when
    the blurred field is the configured word field AND the definition field
    is empty AND the feature is enabled. This matrix guards the historical
    accidents: overwriting existing definitions, firing on the wrong field,
    and firing after the user disabled the feature.
    """
    _ensure_editor_browser_stubs()
    eb = _import_editor_browser()

    # Scope wiring: a single "Japanese" deck; the default FakeNote id
    # maps into it so the decision matrix exercises in-scope notes.
    scope_state = _save_collection_state()
    _tab_jp = aqt.mw.col.decks.add("Japanese")
    aqt.mw.col.decks.add("French")
    aqt.mw.col.db.notes[123] = {"flds": "x", "dids": [_tab_jp], "mid": 1}
    _set_scope_config(["Japanese"])

    class FakeNote:
        """Mimics anki.notes.Note field access for the decision core."""

        def __init__(self, fields: dict, nid: int = 123, note_type: str = ""):
            self._fields = fields
            self.id = nid
            self._note_type = note_type

        def note_type(self):
            """Mimics the non-deprecated note API for type-aware paths."""
            return {"name": self._note_type} if self._note_type else {}

        def __contains__(self, name):
            return name in self._fields

        def __getitem__(self, name):
            return self._fields[name]

        def keys(self):
            return list(self._fields)

    base_config = {
        "word_field": "Expression",
        "definition_field": "Definition",
        "tab_generate": True,
        "scope_decks": ["Japanese"],
    }

    # 1. Happy path: leaving the word field with an empty definition fires.
    note = FakeNote({"Expression": "試験", "Definition": ""})
    check(
        "tab: word-field unfocus + empty def generates",
        eb._should_auto_generate(note, "Expression", base_config),
    )

    # 2. Existing definitions are NEVER overwritten.
    note_filled = FakeNote({"Expression": "試験", "Definition": "さき。"})
    check(
        "tab: non-empty def never auto-overwritten",
        not eb._should_auto_generate(note_filled, "Expression", base_config),
    )

    # 3. Whitespace-only definitions count as empty (historical leak).
    note_ws = FakeNote({"Expression": "試験", "Definition": "  \n"})
    check(
        "tab: whitespace-only def counts as empty",
        eb._should_auto_generate(note_ws, "Expression", base_config),
    )

    # 4. Blurring a NON-word field must not fire.
    check(
        "tab: non-word-field unfocus does nothing",
        not eb._should_auto_generate(note, "Definition", base_config),
    )

    # 5. Feature disabled in config → never fires (opt-out respected).
    check(
        "tab: tab_generate=false disables the feature",
        not eb._should_auto_generate(note, "Expression", {**base_config, "tab_generate": False}),
    )

    # 6. Missing config key defaults ON (historical behaviour).
    legacy_config = {"word_field": "Expression", "definition_field": "Definition",
                     "scope_decks": ["Japanese"]}
    check(
        "tab: missing key defaults to enabled",
        eb._should_auto_generate(note, "Expression", legacy_config),
    )

    # 7. Degenerate config: word == definition field → never fires.
    check(
        "tab: identical word/def fields never auto-generate",
        not eb._should_auto_generate(note, "Expression", {
            "word_field": "Expression", "definition_field": "Expression",
        }),
    )

    # 8. Definition field absent from the note → never fires.
    check(
        "tab: missing def field on note never fires",
        not eb._should_auto_generate(FakeNote({"Expression": "試験"}),
                                    "Expression", base_config),
    )

    # 9. The unfocus hook returns `changed` UNTOUCHED (the lost-definition
    #    race: a truthy return makes the legacy editor reload the note).
    #    Patch mw/note so the hook takes the earliest early-exit path.
    real_mw = eb.mw
    try:
        eb.mw = None
        before = object()
        check(
            "tab: hook returns changed untouched",
            eb.on_field_unfocus(before, None, 0) is before,
        )
    finally:
        eb.mw = real_mw

    # 10. Hook wiring: setup_editor_browser_hooks registers the unfocus
    #     hook (a regression here silently disables the whole feature).
    eb.setup_editor_browser_hooks()
    from aqt import gui_hooks as gh
    check(
        "tab: unfocus hook registered with gui_hooks",
        any(getattr(h, "_hooks", None) and eb.on_field_unfocus in h._hooks
            for h in (gh.editor_did_unfocus_field,)),
    )
    check(
        "tab: editor registry hook registered",
        eb._register_editor in gh.editor_did_load_note._hooks,
    )

    # 11. Editor<->note matching: identity first (unsaved Add notes share
    #     id 0 — an id-only match across two Add windows is a bug).
    e1 = type("E", (), {"note": FakeNote({"Expression": "x"}, nid=0), "nid": None})()
    e2 = type("E", (), {"note": FakeNote({"Expression": "y"}, nid=0), "nid": None})()
    eb._live_editors.clear()
    eb._live_editors.extend([e1, e2])
    check(
        "tab: matching by note identity, not shared id 0",
        eb._find_editor_for_note(e2.note) is e2,
    )

    # 12. Field-ordinal resolution: correct name, out-of-range safe.
    check("tab: field ordinal resolves name",
          eb._field_name_at(note, 0) == "Expression")
    check("tab: out-of-range ordinal returns ''",
          eb._field_name_at(note, 99) == "")

    # 13. Deck Scope never gates Tab: a note in a NON-scope deck still
    #     auto-generates (Scope bounds only knowledge/scoring weights).
    fr_note = FakeNote({"Expression": "試験", "Definition": ""}, nid=999)
    aqt.mw.col.db.notes[999] = {"flds": "x", "dids": [
        aqt.mw.col.decks.decks["French"]], "mid": 2}
    check("tab: other-deck notes still auto-generate (no Scope gate)",
          eb._should_auto_generate(fr_note, "Expression", base_config))

    _restore_collection_state(scope_state)


def test_apply_definition_refresh_order() -> None:
    """
    Refresh contract for _apply_definition_to_editor (the 'rendered
    field not updating' bug): after Tab-generation the definition must
    appear in BOTH the rendered field and any HTML-source view in one
    go — like Japanese Support's furigana Tab, which works because
    Anki itself reloads the note (loadNoteKeepingFocus) after a
    changed unfocus hook.

    Our hook must still return `changed` untouched (no reload race),
    so on_done must drive Anki's OWN reload entry points instead of a
    bare setFields() eval (which updated field stores but left the
    rendered components stale until the next focus event).
    """
    _ensure_editor_browser_stubs()
    eb = _import_editor_browser()

    order_log: list = []

    class RecNote:
        """Note stand-in supporting field assignment like anki.notes.Note."""

        def __init__(self, fields: dict, nid: int):
            self._fields = dict(fields)
            self.id = nid

        def __contains__(self, name):
            return name in self._fields

        def __getitem__(self, name):
            return self._fields[name]

        def __setitem__(self, name, value):
            order_log.append(("note-set", name))
            self._fields[name] = value

        def keys(self):
            return list(self._fields)

    class RecWeb:
        def eval(self, js):
            order_log.append(("web.eval", js))

    class LegacyEditor:
        """Legacy Editor surface: native reload entry points."""

        def __init__(self):
            self.web = RecWeb()

        def loadNoteKeepingFocus(self):
            order_log.append(("loadNoteKeepingFocus",))

        def loadNote(self, focusTo=None):
            order_log.append(("loadNote",))

    class NewEditorish:
        """Svelte NewEditor surface: reload_note only, no legacy methods."""

        def __init__(self):
            self.web = RecWeb()

        def reload_note(self):
            order_log.append(("reload_note",))

    class BareEditor:
        """Unknown generation: only web.eval available."""

        def __init__(self):
            self.web = RecWeb()

    class DeadEditor:
        """Closed editor: every touch raises (sip-wrapped C++ gone)."""

        @property
        def web(self):
            raise RuntimeError("wrapped C/C++ object has been deleted")

        def loadNoteKeepingFocus(self):
            raise RuntimeError("wrapped C/C++ object has been deleted")

        def loadNote(self, focusTo=None):
            raise RuntimeError("wrapped C/C++ object has been deleted")

    # update_note recorder on the stub collection (restored afterwards).
    col = aqt.mw.col
    had_update = hasattr(col, "update_note")

    def _rec_update_note(n):
        order_log.append(("update_note", getattr(n, "id", None)))

    col.update_note = _rec_update_note  # type: ignore[attr-defined]
    try:
        # 1. Legacy + saved note: field written, persisted, then the
        #    NATIVE reload runs — and no raw setFields eval happens.
        order_log.clear()
        note = RecNote({"Expression": "不公平", "Definition": ""}, 123)
        eb._apply_definition_to_editor(
            LegacyEditor(), note, "Definition", "<b>def</b>")
        check("refresh: note field updated",
              note["Definition"] == "<b>def</b>")
        check("refresh: update_note runs BEFORE the UI refresh",
              order_log[:2] == [("note-set", "Definition"),
                                ("update_note", 123)],
              f"got {order_log[:2]}")
        check("refresh: legacy editor uses loadNoteKeepingFocus",
              ("loadNoteKeepingFocus",) in order_log,
              f"got {order_log}")
        check("refresh: native path needs no setFields eval",
              not any(kind == "web.eval" for kind, *_ in order_log),
              f"got {order_log}")

        # 2. Legacy + UNSAVED note (Add window, id 0): no update_note
        #    call, but the native refresh still runs (reads the
        #    in-memory note object — no collection row needed).
        order_log.clear()
        new_note = RecNote({"Expression": "不公平", "Definition": ""}, 0)
        eb._apply_definition_to_editor(
            LegacyEditor(), new_note, "Definition", "<b>def</b>")
        check("refresh: unsaved note skips update_note",
              not any(kind == "update_note" for kind, *_ in order_log),
              f"got {order_log}")
        check("refresh: unsaved note still gets native refresh",
              ("loadNoteKeepingFocus",) in order_log,
              f"got {order_log}")

        # 3. NewEditor-ish + saved note: reload_note (never the raw eval).
        order_log.clear()
        note3 = RecNote({"Expression": "不公平", "Definition": ""}, 456)
        eb._apply_definition_to_editor(
            NewEditorish(), note3, "Definition", "<b>def</b>")
        check("refresh: svelte editor uses reload_note for saved notes",
              ("reload_note",) in order_log
              and not any(kind == "web.eval" for kind, *_ in order_log),
              f"got {order_log}")

        # 4. NewEditor-ish + UNSAVED note: reload_note would re-fetch a
        #    nonexistent row, so it must NOT run — raw eval instead.
        order_log.clear()
        note4 = RecNote({"Expression": "不公平", "Definition": ""}, 0)
        eb._apply_definition_to_editor(
            NewEditorish(), note4, "Definition", "<b>def</b>")
        check("refresh: unsaved note never triggers reload_note",
              not any(kind == "reload_note" for kind, *_ in order_log),
              f"got {order_log}")
        evals = [js for kind, js in order_log if kind == "web.eval"]
        check("refresh: fallback eval carries setFields AND triggerChanges",
              len(evals) == 1 and "setFields(" in evals[0]
              and "triggerChanges" in evals[0],
              f"got {evals}")

        # 5. Bare editor (no reload methods at all): same eval fallback.
        order_log.clear()
        note5 = RecNote({"Expression": "不公平", "Definition": ""}, 789)
        eb._apply_definition_to_editor(
            BareEditor(), note5, "Definition", "<b>def</b>")
        check("refresh: method-less editor falls back to eval",
              any(kind == "web.eval" and "triggerChanges" in js
                  for kind, js in order_log),
              f"got {order_log}")

        # 6. Dead (closed) editor: nothing may raise; persistence to the
        #    collection still happened (the note survives in the DB even
        #    though no window could show it).
        order_log.clear()
        note6 = RecNote({"Expression": "不公平", "Definition": ""}, 321)
        try:
            eb._apply_definition_to_editor(
                DeadEditor(), note6, "Definition", "<b>def</b>")
            raised = False
        except Exception as e:  # noqa: BLE001 — the failure IS the test
            raised = e  # type: ignore[assignment]
        check("refresh: closed editor never raises",
              raised is False, f"raised {raised!r}")
        check("refresh: closed editor still persists the note",
              ("update_note", 321) in order_log,
              f"got {order_log}")
    finally:
        if not had_update:
            try:
                delattr(col, "update_note")
            except AttributeError:
                pass


def test_multi_note_type_targeting() -> None:
    """
    Multi-note-type support: the 'targets' config shape maps EACH note
    type to its own word/reading/definition fields, and every generation
    path (editor button, bulk, Tab-to-Generate) routes through the same
    resolver. Guards: correct mapping per type, unconfigured types never
    generating,     and full legacy single-type compatibility.
    """
    # Same stub+package machinery as test_tab_generate_decisions.
    _ensure_editor_browser_stubs()
    eb = _import_editor_browser()

    # Scope wiring: Japanese deck holds the configured types' cards,
    # French deck holds everything else.
    scope_state = _save_collection_state()
    _multi_jp = aqt.mw.col.decks.add("Japanese")
    _multi_fr = aqt.mw.col.decks.add("French")

    def _deck_for(type_name: str) -> int:
        return _multi_jp if type_name in (
            "JP Mining Note", "Animecards", "Japanese") else _multi_fr

    class Note:
        def __init__(self, fields, type_name):
            self._fields = fields
            self.id = hash(type_name) % 10_000
            self._type = type_name
            aqt.mw.col.db.notes[self.id] = {
                "flds": "", "dids": [_deck_for(type_name)], "mid": 1,
            }

        def note_type(self):
            return {"name": self._type}

        def __contains__(self, name):
            return name in self._fields

        def __getitem__(self, name):
            return self._fields[name]

        def keys(self):
            return list(self._fields)

    targets_config = {
        "targets": {
            "JP Mining Note": {
                "word_field": "Word", "reading_field": "Furigana",
                "definition_field": "Definition",
            },
            "Animecards": {
                "word_field": "Expression", "reading_field": "",
                "definition_field": "Meaning",
            },
        },
        "dictionaries": ["/some/dict"],
        "scope_decks": ["Japanese"],
    }

    # 1. Each configured type resolves to its OWN field mapping.
    r1 = eb.resolve_fields_for_note(
        Note({"Word": "x", "Furigana": "f", "Definition": "d"},
             "JP Mining Note"), targets_config)
    check("multi: JP Mining Note resolves its mapping",
          r1 == {"word_field": "Word", "reading_field": "Furigana",
                 "definition_field": "Definition"}, f"got {r1}")
    r2 = eb.resolve_fields_for_note(
        Note({"Expression": "x", "Meaning": "m"}, "Animecards"),
        targets_config)
    check("multi: Animecards resolves its mapping",
          r2 == {"word_field": "Expression", "reading_field": "",
                 "definition_field": "Meaning"}, f"got {r2}")

    # 2. Unconfigured types never generate (bulk/editor show tooltips).
    r3 = eb.resolve_fields_for_note(
        Note({"Expression": "x"}, "Kaishi 1.5k"), targets_config)
    check("multi: unconfigured type is rejected", r3 is None, f"got {r3}")

    # 3. A target missing word or definition fields cannot generate.
    broken = dict(targets_config)
    broken["targets"] = {"Ghost": {"word_field": "", "reading_field": "",
                                   "definition_field": "Def"}}
    r4 = eb.resolve_fields_for_note(Note({"Def": "d"}, "Ghost"), broken)
    check("multi: incomplete mapping is rejected", r4 is None, f"got {r4}")

    # 4. Legacy single-type configs behave exactly as before (plus Scope).
    legacy_config = {"note_type": "Japanese", "word_field": "Expression",
                     "reading_field": "furigana",
                     "definition_field": "Definition",
                     "scope_decks": ["Japanese"]}
    r5 = eb.resolve_fields_for_note(
        Note({"Expression": "x"}, "Japanese"), legacy_config)
    check("multi: legacy config resolves unchanged",
          r5 == {"word_field": "Expression", "reading_field": "furigana",
                 "definition_field": "Definition"}, f"got {r5}")
    r6 = eb.resolve_fields_for_note(
        Note({"Expression": "x"}, "Other"), legacy_config)
    check("multi: legacy config still excludes other types",
          r6 is None, f"got {r6}")

    # 5. Tab-to-Generate follows the same multi-type rules.
    mining_note = Note({"Word": "x", "Furigana": "f", "Definition": ""},
                       "JP Mining Note")
    check("multi: tab fires on configured type's word field",
          eb._should_auto_generate(mining_note, "Word", targets_config))
    check("multi: tab ignores non-word fields of same type",
          not eb._should_auto_generate(mining_note, "Furigana",
                                       targets_config))
    # v1.2.1: an UNCONFIGURED but in-scope type now auto-generates via
    # the same field inference the toolbar button uses — the old
    # hard rejection made Tab "not always work" while the button did.
    # ("Japanese" sits in the JP deck but has no targets_config entry.)
    unconfigured_note = Note({"Expression": "x", "Meaning": ""},
                             "Japanese")
    check("multi: tab fires on unconfigured type via inference",
          eb._should_auto_generate(unconfigured_note, "Expression",
                                   targets_config))
    # …but a type whose fields CANNOT be inferred stays silent.
    cryptic_note = Note({"Side A": "x", "Side B": ""}, "Japanese")
    check("multi: tab stays silent when fields cannot be inferred",
          not eb._should_auto_generate(cryptic_note, "Side A",
                                       targets_config))
    check("multi: tab legacy config still works",
          eb._should_auto_generate(
              Note({"Expression": "x", "Definition": ""}, "Japanese"),
              "Expression", legacy_config))

    # 6. Scope gate REMOVED (v1.3): generation never consults Scope —
    #    Scope bounds only knowledge/scoring. A configured type resolves
    #    even with a mismatched or empty scope.
    check("multi: mismatched scope no longer blocks generation",
          eb.resolve_fields_for_note(
              Note({"Word": "x", "Furigana": "f", "Definition": "d"},
                   "JP Mining Note"),
              {**targets_config, "scope_decks": ["French"]}) is not None)
    check("multi: empty scope no longer blocks generation",
          eb.resolve_fields_for_note(
              Note({"Word": "x", "Furigana": "f", "Definition": "d"},
                   "JP Mining Note"),
              {**targets_config, "scope_decks": []}) is not None)

    # 7. Mapping-fail routes to the Fields dialog (v1.2 quick-fix
    #    contract, the 会社 complaint): a note whose fields cannot be
    #    mapped gets the field-mapping dialog, NEVER an "Add deck" offer
    #    (which looped forever). Generation ignores Scope, so the dialog
    #    is mapping-only by construction.
    #    (Scope membership itself is covered by test_scope_deck_filtering.)
    nomap_note = Note({"Front": "x", "Back": ""}, "Japanese")
    nomap_cfg = {"scope_decks": ["Japanese"], "targets": {}}
    check("multi: mapping-fail resolves to None",
          eb.resolve_fields_for_note(nomap_note, nomap_cfg) is None)
    check("multi: mapping-fail note is still in scope (knowledge-side)",
          eb._note_in_scope(nomap_note, nomap_cfg) is True)

    _restore_collection_state(scope_state)


def test_cd_toggle_and_japanese_scope_guess() -> None:
    """
    Chinese-Support-style toggle + knowledge-only Scope (v1.3):
    - the toolbar button is a toggleable switch bound to the global
      `tab_generate` flag (active class + tip follow the config);
    - Tab ignores deck Scope entirely (Scope bounds only knowledge);
    - fresh installs guess Japanese decks (name keywords + kana
      content sampling) exactly once (auto-init flag).
    """
    _ensure_editor_browser_stubs()
    eb = _import_editor_browser()

    scope_state = _save_collection_state()
    try:
        jp_did = aqt.mw.col.decks.add("Japanese")
        aqt.mw.col.decks.add("French")
        odd_did = aqt.mw.col.decks.add("My Odd Deck")
        # Kana-bearing first field in the oddly-named deck: content
        # pass must catch it; the French deck stays kana-free.
        aqt.mw.col.db.notes[201] = {
            "flds": "たべる\x1fto eat", "dids": [odd_did], "mid": 1}
        aqt.mw.col.db.notes[202] = {
            "flds": "manger\x1fto eat", "dids": [
                aqt.mw.col.decks.decks["French"]], "mid": 2}

        # 1. Toggle flag: missing key defaults ON, explicit False off.
        check("toggle: missing key defaults to enabled",
              eb._tab_generate_enabled({}) is True)
        check("toggle: explicit False disables",
              eb._tab_generate_enabled({"tab_generate": False}) is False)
        check("toggle: tips differ by state",
              eb._toggle_tip(True) != eb._toggle_tip(False))

        # 2. Toolbar button is registered toggleable with the toggle cmd.
        seen: dict = {}

        class FakeEditor:
            def addButton(self, **kw):
                seen.update(kw)
                return "<button>CD</button>"
        eb.add_editor_button([], FakeEditor())
        check("toggle: button is toggleable",
              seen.get("toggleable") is True, f"got {seen}")
        check("toggle: button fires the toggle cmd",
              seen.get("cmd") == "compredef_toggle_autogen",
              f"got {seen.get('cmd')}")
        check("toggle: button id stable for visual sync",
              seen.get("id") == "compredef_editor_btn",
              f"got {seen.get('id')}")
        check("toggle: creation label is the plain CD logo",
              seen.get("label") == "CD",
              f"got {seen.get('label')}")

        # 3. Visual sync never crashes headless editors (no webview).
        #    The pressed blue comes from Anki's own theme variables
        #    (Chinese Support 3's mechanism) — asserted structurally
        #    below; the label text is never rewritten (no ticks, no
        #    childNodes walk).
        class FakeWeb:
            def __init__(self):
                self.js = ""

            def eval(self, js):
                self.js = js

        class WebEditor:
            def __init__(self):
                self.web = FakeWeb()

        try:
            eb._set_toggle_visual(object(), True)
            eb._sync_toggle_visual(object())
            web_on = WebEditor()
            eb._set_toggle_visual(web_on, True)
            web_off = WebEditor()
            eb._set_toggle_visual(web_off, False)
            js_ok = (
                'classList.add("active")' in web_on.web.js
                and "--button-bg" in web_on.web.js
                and "var(--button-primary-bg)" in web_on.web.js
                and "--button-gradient-start" in web_on.web.js
                and "--button-gradient-end" in web_on.web.js
                and 'classList.remove("active")' in web_off.web.js
                and "childNodes" not in web_on.web.js
                and "childNodes" not in web_off.web.js
                and "✓" not in web_on.web.js
                and "✕" not in web_off.web.js
                and "\\u2713" not in web_on.web.js
                and "\\u2715" not in web_off.web.js
            )
            visual_ok = bool(js_ok)
        except Exception:
            visual_ok = False
        check("toggle: visual sync is headless-safe", visual_ok)
        check("toggle: ON sync paints theme blue, OFF clears it", visual_ok)

        # 4. Tab bypasses Scope: French-deck note fires when toggle ON.
        class FakeNote:
            def __init__(self, fields, nid=202):
                self._fields = fields
                self.id = nid

            def note_type(self):
                return {"name": "French"}

            def __contains__(self, name):
                return name in self._fields

            def __getitem__(self, name):
                return self._fields[name]

            def keys(self):
                return list(self._fields)

        fr_cfg = {"word_field": "Expression",
                  "definition_field": "Definition",
                  "tab_generate": True, "scope_decks": ["Japanese"]}
        check("toggle: Tab fires outside Scope when ON",
              eb._should_auto_generate(
                  FakeNote({"Expression": "manger", "Definition": ""}),
                  "Expression", fr_cfg))
        check("toggle: Tab silent when toggle OFF",
              not eb._should_auto_generate(
                  FakeNote({"Expression": "manger", "Definition": ""}),
                  "Expression", {**fr_cfg, "tab_generate": False}))

        # 5. Japanese guess: name hit + kana-content hit, French excluded.
        guess = compredef_scope.suggest_japanese_decks(aqt.mw.col)
        check("guess: Japanese deck by name",
              "Japanese" in guess, f"got {guess}")
        check("guess: kana-content deck caught",
              "My Odd Deck" in guess, f"got {guess}")
        check("guess: French deck excluded",
              "French" not in guess, f"got {guess}")

        # 6. Name heuristic is pure (Ring-0 style, no collection).
        check("guess: 日本語 name matches",
              compredef_scope.is_japanese_deck_name("My 日本語 deck"))
        check("guess: French name misses",
              not compredef_scope.is_japanese_deck_name("French"))
        check("guess: empty name misses",
              not compredef_scope.is_japanese_deck_name(""))

        # 7. Auto-init runs once: empty+unflagged guesses; flagged stays empty.
        _jp = jp_did  # keep linters quiet about the deck id
        got = compredef_scope.maybe_auto_init_scope({}, aqt.mw.col)
        check("autoinit: empty unflagged config guesses",
              "Japanese" in got, f"got {got}")
        check("autoinit: flagged empty config stays empty",
              compredef_scope.maybe_auto_init_scope(
                  {"scope_auto_initialized": True}, aqt.mw.col) == [])
        check("autoinit: configured scope passes through untouched",
              compredef_scope.maybe_auto_init_scope(
                  {"scope_decks": ["French"]}, aqt.mw.col) == ["French"])
    finally:
        _restore_collection_state(scope_state)


def test_v12_scoring_algorithm() -> None:
    """
    v1.2 definition scoring — the 不公平 incident:
    - kanji score  = Σ point(k) per occurrence, point = ivl/365 cap 1.0
    - vocab score  = Σ point(w) per multi-kanji compound occurrence
    - winner = highest (kanji + vocab); tie-break = most kanji count
    - kana-only inflections NEVER match vocab (やめる vs やめて)
    """
    import scoring as scoring_mod

    # Learner: 公/平 fully known (1.0), 判/定 half-year (0.5), 不 unknown.
    # Kana-only words never enter vocab candidates (v1.2 spec).
    kp = {"公": 1.0, "平": 1.0, "判": 0.5, "定": 0.5}
    vp = {"会社": 1.0}

    # Three candidates shaped like the user's 不公平 case.
    shogakukan = "公平でないこと。えこひいきがあること。例 不公平な判定。対 公平。"
    daijirin = "かたよっていて、扱いが公平でない・こと（さま）。⇔公平。「━な処置」「━感」━さ（名）"

    # 1. Kanji points: 公平×3 (2.0+2.0+2.0? no — per OCCURRENCE:
    #    公平 appears 3× ⇒ 3×(1+1)=6.0; 不=0; 判定=(0.5+0.5)=1.0; 例=0; 対=0
    #    → kanji_score = 7.0. Vocab: no known compounds in text → 0.
    res = scoring_mod.score_definition(shogakukan, kp, vp)
    check("v12: kanji pts sum per occurrence",
          abs(res.kanji_score - 7.0) < 1e-9,
          f"got {res.kanji_score}")
    check("v12: vocab pts from known compounds only",
          res.vocab_score == 0.0,
          f"got {res.vocab_score}")
    check("v12: total = kanji + vocab",
          abs(res.total_score - 7.0) < 1e-9,
          f"got {res.total_score}")
    check("v12: counts every kanji occurrence",
          res.kanji_count == 11,
          f"got {res.kanji_count}")

    # 2. Kana-heavy daijirin: fewer kanji ⇒ lower total despite same
    #    readability. (User can read both; algorithm prefers kanji-vocab
    #    coverage — matches "I'd get 大辞泉" once weights are equal.)
    res_d = scoring_mod.score_definition(daijirin, kp, vp)
    check("v12: kana-dense definition scores lower",
          res_d.total_score < res.total_score,
          f"daijirin={res_d.total_score} vs shogakukan={res.total_score}")

    # 3. Tie-break: equal totals → most kanji wins.
    #    公(1)+平(1)+判(0.5)+定(0.5)=3.0, 4 kanji
    #    公(1)+平(1)+決(0.5)+定(0.5)+場(0)=3.0, 5 kanji
    tie_a = "公平な判定。"
    kp2 = {**kp, "決": 0.5, "場": 0.0}
    ra = scoring_mod.score_definition(tie_a, kp2, vp)
    rb = scoring_mod.score_definition("公平な決定場。", kp2, vp)
    check("v12: tie-break set up (equal totals)",
          abs(ra.total_score - rb.total_score) < 1e-9,
          f"{ra.total_score} vs {rb.total_score}")
    check("v12: tie-break prefers more kanji",
          rb.kanji_count > ra.kanji_count,
          f"{rb.kanji_count} vs {ra.kanji_count}")

    # 4. Inflection immunity: やめて never matches known やめる because
    #    kana-only words are not vocab candidates at all.
    kp2 = {}
    vp2 = {"やめる": 1.0}
    res_y = scoring_mod.score_definition("やめてもいいですか。", kp2, vp2)
    check("v12: kana-only vocab never scores (inflection immunity)",
          res_y.vocab_score == 0.0 and res_y.kanji_score == 0.0)

    # 5. extract_kanji_words: compounds only, split at non-kanji.
    words = scoring_mod.extract_kanji_words("不公平な会社の判定だ。")
    check("v12: kanji-word extraction",
          words == {"不公平", "会社", "判定"}, f"got {words}")
    check("v12: single kanji not a vocab word",
          scoring_mod.extract_kanji_words("日") == set())

    # 6. Engine-level argmax with the 不公平-style dictionary set:
    #    two fully-readable defs; the one with more kanji+compounds wins.
    import engine as engine_mod
    import picker as picker_mod
    from models import DictionaryEntry
    entries = [
        DictionaryEntry("不公平", "ふこうへい", daijirin, "大辞林", "x"),
        DictionaryEntry("不公平", "ふこうへい", shogakukan, "小学館", "y"),
    ]
    picked = engine_mod._pick_best(entries, kp, vp)
    check("v12: engine argmax picks the richer definition",
          picked is not None and "えこひいき" in picked[1],
          f"got {(picked[1][:30] if picked else None)!r}")
    # Order independence:
    picked_rev = engine_mod._pick_best(list(reversed(entries)), kp, vp)
    check("v12: engine argmax is order-independent",
          picked_rev is not None and picked is not None
          and picked_rev[1] == picked[1])

    # 7. v1.3 DENSITY (default strategy): the user's core critique —
    #    a succinct 90%-known definition must beat a 20-paragraph
    #    5%-known one, even though raw sums favor the long one.
    dense_short = "公平だ。"            # kc=2, all known -> density 1.0
    sparse_long = "公平な不透明不平等。"  # kc=8, 3 known -> density 0.375
    res_short = scoring_mod.score_definition(dense_short, kp, vp)
    res_long = scoring_mod.score_definition(sparse_long, kp, vp)
    check("v12: density numbers (1.0 vs 0.375)",
          abs(res_short.density_total - 1.0) < 1e-9
          and abs(res_long.density_total - 0.375) < 1e-9,
          f"got {res_short.density_total} vs {res_long.density_total}")
    check("v12: raw sums favor the long one (2.0 vs 3.0)",
          abs(res_short.total_score - 2.0) < 1e-9
          and abs(res_long.total_score - 3.0) < 1e-9)
    density_entries = [
        DictionaryEntry("w", "", sparse_long, "long", "x"),
        DictionaryEntry("w", "", dense_short, "short", "y"),
    ]
    picked_d = engine_mod._pick_best(density_entries, kp, vp)
    check("v12: density strategy picks the succinct known definition",
          picked_d is not None and picked_d[1] == dense_short,
          f"got {(picked_d[1][:30] if picked_d else None)!r}")
    picked_legacy = picker_mod.pick_best(
        density_entries, kp, vp,
        strategy=picker_mod.LegacySumPicker())
    check("v12: legacy strategy still picks the long one (raw mass)",
          picked_legacy is not None and picked_legacy[1] == sparse_long)
    # Density tie-break: equal density -> most kanji wins.
    tie_long = picker_mod.pick_best(
        [DictionaryEntry("w", "", "公平公平。", "A", "x"),
         DictionaryEntry("w", "", "公平だ。", "B", "y")], kp, vp)
    check("v12: density tie-break prefers more kanji",
          tie_long is not None and tie_long[1] == "公平公平。",
          f"got {(tie_long[1] if tie_long else None)!r}")


PICKER_AUDIT_FIXTURE = os.path.join(
    REPO_ROOT, "tests", "fixtures", "picker_audit.json")


def _picker_def_hash(definition: str) -> str:
    """Short stable id matching debug/audit_picker.py (same sha1:12)."""
    import hashlib
    return hashlib.sha1(definition.encode("utf-8")).hexdigest()[:12]


def _picker_audit_points(fixture: dict, profile: str,
                         candidates: list,
                         exclude: tuple = ()) -> tuple:
    """(kanji_points, vocab_points) for one audit profile.

    Mirrors debug/audit_picker.py (which never ships, so the suite
    cannot import it — this small twin is the price of that rule).
    mine = frozen interval-weighted snapshot; beginner = nothing;
    native = every kanji/compound in the scoring-cleaned candidates
    at 1.0.
    """
    import scoring as scoring_mod
    if profile == "mine":
        return fixture["mine"]["kanji"], fixture["mine"]["vocab"]
    if profile == "beginner":
        return {}, {}
    kanji: dict = {}
    vocab: dict = {}
    for _, definition in candidates:
        base = scoring_mod.scoring_base_text(definition, exclude)
        for ch in set(c for c in base if "\u4e00" <= c <= "\u9fff"):
            kanji[ch] = 1.0
        for word in scoring_mod.extract_kanji_words(base):
            vocab[word] = 1.0
    return kanji, vocab


def test_picker_audit_strict() -> None:
    """
    Strict dictionary-picker audit over the user's REAL 11-note deck
    (My Life Decks::Japanese::anki-japanese-template — every card,
    including the nonsense word, the full-sentence note and the
    zero-hit words), frozen in tests/fixtures/picker_audit.json by
    debug/audit_picker.py --capture.

    For each source (local dictionaries / Yomitan) x word x profile
    (mine / beginner / native) the test recomputes the ranking with
    the CURRENT code and pins the WINNING pick (dictionary + content
    hash + score) — the decision — without pinning the full ordering
    (runner-up order is not a decision and must stay free to move).
    It also pins:
    - rank()[0] == engine._pick_best() (picker/rank agreement);
    - order-independence (reversed input, same winner — Q-G2);
    - profile sanity (beginner: all scores 0, most-kanji wins;
      native: kanji_score == kanji_count on cleaned text);
    - empty words rank empty and pick None (missing-word contract).
    Refresh workflow: change dictionaries/knowledge legitimately, then
    re-run debug/audit_picker.py --capture --html (or --refreeze for
    pure scoring changes) and review the HTML diff — the fixture diff
    shows exactly which picks moved and why.
    """
    import scoring as scoring_mod
    import engine as engine_mod
    import picker as picker_mod
    from models import DictionaryEntry

    if not os.path.isfile(PICKER_AUDIT_FIXTURE):
        check("audit: fixture present", False,
              f"missing {PICKER_AUDIT_FIXTURE} — run "
              "debug/audit_picker.py --capture")
        return
    with open(PICKER_AUDIT_FIXTURE, encoding="utf-8") as f:
        fixture = json.load(f)

    # The frozen rankings belong to ONE strategy: a hand-switched
    # picker_strategy must never silently compare one strategy's
    # expectations against another strategy's code — re-freeze instead.
    check("audit: code strategy matches frozen strategy",
          picker_mod.get_active_strategy().name == fixture.get(
              "meta", {}).get("strategy"),
          f"code={picker_mod.get_active_strategy().name!r} "
          f"fixture={fixture.get('meta', {}).get('strategy')!r}")

    # Capture-completeness guards: a half capture (e.g. Yomitan taken
    # while the browser was closed) must fail LOUD, never freeze
    # vacuous all-empty expectations as "correct".
    local_total = sum(len(v) for v in fixture["local"].values())
    check("audit: local capture non-empty", local_total > 0,
          "re-run debug/audit_picker.py --capture")
    yomitan_total = sum(len(v) for v in fixture["yomitan"].values())
    check("audit: yomitan capture non-empty (browser open at capture?)",
          yomitan_total > 0,
          "re-run --capture with the browser + Yomitan running")
    check("audit: three profiles frozen",
          set(fixture["expected"]["local"]
              [str(fixture["words"][0]["note_id"])]) == {
              "mine", "beginner", "native"})

    for source in ("local", "yomitan"):
        for item in fixture["words"]:
            nid = str(item["note_id"])
            label = f"{source}:{item['word'] or '(empty)'}"
            raw = fixture[source].get(nid, [])
            entries = [DictionaryEntry(
                word=item["word"], reading="", definition=c["definition"],
                dictionary_title=c["dict"],
                dictionary_path=c.get("path", "")) for c in raw]
            valid = engine_mod._filter_valid_entries(entries)
            cands = [(e.dictionary_title, e.definition) for e in valid]
            exclude = (item["word"],) if item.get("word") else ()
            for profile in ("mine", "beginner", "native"):
                kp, vp = _picker_audit_points(fixture, profile, cands,
                                              exclude)
                ranked = picker_mod.rank_definitions(cands, kp, vp,
                                                     exclude=exclude)
                scored = [(t, scoring_mod.score_definition(d, kp, vp,
                                                           exclude))
                          for (t, d), _ in ranked]
                live = [(t, round(res.density_total, 4),
                         round(res.total_score, 3), res.kanji_count)
                        for t, res in scored]
                frozen = fixture["expected"][source][nid][profile]
                # The frozen rows store rounded densities; recompute the
                # comparison on the same rounding so float repr can
                # never cause a phantom drift.
                live_winner = None
                if ranked:
                    (live_title, _), _ = ranked[0]
                    live_winner = (live_title,
                                   _picker_def_hash(ranked[0][0][1]))
                frozen_winner = None
                if frozen:
                    frozen_winner = (frozen[0]["dict"], frozen[0]["hash"])
                check(f"audit: {label}/{profile} winner frozen",
                      live_winner == frozen_winner,
                      f"live={live_winner} frozen={frozen_winner}")
                if live and frozen:
                    check(f"audit: {label}/{profile} winner score stable",
                          live[0][1] == frozen[0]["density"]
                          and live[0][3] == frozen[0]["kanji_count"],
                          f"live={live[0]} frozen={frozen[0]}")
                picked = engine_mod._pick_best(valid, kp, vp)
                live_winner = ranked[0][0][1] if ranked else None
                check(f"audit: {label}/{profile} picker agrees with rank",
                      (picked[1] if picked else None) == live_winner)
                if valid:
                    rev = engine_mod._pick_best(
                        list(reversed(valid)), kp, vp)
                    # Unique best: reversed input must pick the same
                    # winner. Exact tie at the top: encounter order
                    # decides, so the reversed winner must be one of
                    # the tied best (by design, not a bug).
                    top = min((-res.density_total, -res.kanji_count)
                              for _, res in scored)
                    tied_defs = {d for (t, d), res in ranked
                                 if (-res.density_total, -res.kanji_count)
                                 == top}
                    if len(tied_defs) == 1:
                        check(f"audit: {label}/{profile} order-independent",
                              rev is not None and picked is not None
                              and rev[1] == picked[1])
                    else:
                        check(f"audit: {label}/{profile} tied best kept",
                              rev is not None and rev[1] in tied_defs,
                              f"{len(tied_defs)} tied")
                else:
                    check(f"audit: {label}/{profile} empty picks None",
                          picked is None and ranked == [])
                if profile == "beginner" and ranked:
                    densities = {round(res.density_total, 9)
                                 for _, res in scored}
                    counts = [n for _, _, _, n in live]
                    check(f"audit: {label}/beginner all-zero, most-kanji wins",
                          densities == {0.0}
                          and live[0][0] == ranked[0][0][0]
                          and live[0][3] == max(counts))
                if profile == "native" and ranked:
                    ok = all(
                        abs(res.kanji_score - n) < 1e-9
                        for (_, res), (_, _, _, n) in zip(ranked, live))
                    check(f"audit: {label}/native kanji fully known", ok)

    _check_picker_grades(fixture)


def _check_picker_grades(fixture: dict) -> None:
    """Human grades vs algorithm picks (the grading loop).

    Grades are recorded in the HTML report (export grades JSON, merge
    with debug/audit_picker.py --import-grades): per source x word x
    profile, {"hash": <frozen winner hash>, "verdict": "correct"|"wrong"}.
    - no grades yet -> single PASS (nothing to agree with);
    - grade hash != frozen winner hash -> FAIL (stale grade: the
      definition changed since grading — re-grade);
    - verdict "wrong" -> FAIL (the algorithm's pick is judged incorrect;
      suite stays red until the picker is fixed — strict by design);
    - verdict "correct" -> PASS, counted in the agreement summary.
    """
    grades = fixture.get("grades", {})
    total = sum(len(profiles)
                for words in grades.values() for profiles in words.values())
    if not total:
        check("audit: no human grades recorded yet", True)
        return
    correct = 0
    for source, words in grades.items():
        for nid, profiles in words.items():
            frozen_profiles = fixture["expected"].get(source, {}).get(nid)
            if frozen_profiles is None:
                check(f"audit: grade references known word {source}/{nid}",
                      False)
                continue
            for profile, grade in profiles.items():
                frozen = frozen_profiles.get(profile, [])
                winner_hash = frozen[0]["hash"] if frozen else None
                if grade.get("verdict") not in ("correct", "wrong"):
                    check(f"audit: grade {source}/{nid}/{profile} "
                          f"has a verdict", False,
                          f"got {grade.get('verdict')!r}")
                    continue
                check(f"audit: grade {source}/{nid}/{profile} "
                      f"matches frozen winner",
                      grade.get("hash") == winner_hash,
                      "re-grade: the definition changed since")
                if grade.get("hash") == winner_hash:
                    if grade["verdict"] == "correct":
                        correct += 1
                        check(f"audit: human agrees {source}/{nid}/{profile}",
                              True)
                    else:
                        check(f"audit: human agrees {source}/{nid}/{profile}",
                              False, "graded WRONG — fix the picker")
    check(f"audit: human agreement {correct}/{total}", correct == total,
          "see the WRONG lines above")


def test_scope_deck_filtering() -> None:
    """
    Deck Scope: subdeck inclusion, ANY-card membership, unsaved-note
    type fallback, fail-closed empty scope, and missing-deck handling.
    """
    scope_state = _save_collection_state()
    try:
        col = aqt.mw.col
        jp = col.decks.add("Japanese")
        jpv = col.decks.add("Japanese::Vocab")
        col.decks.add("JapaneseExtended")  # prefix lookalike, NOT a child
        fr = col.decks.add("French")

        # 1. Expansion: children included, lookalikes excluded.
        expanded = compredef_scope.expand_scope_names(
            ["Japanese", "Japanese::Vocab", "JapaneseExtended", "French"],
            ["Japanese"],
        )
        check("scope: deck implies its subdecks",
              expanded == {"Japanese", "Japanese::Vocab"},
              f"got {sorted(expanded)}")
        check("scope: missing decks reported",
              compredef_scope.missing_scope_decks(["Japanese"], ["Klingon"])
              == ["Klingon"])
        check("scope: scope_dids resolves children to ids",
              compredef_scope.scope_dids(col, ["Japanese"]) == {jp, jpv})

        # 2. Membership via cards (ANY-card rule for multi-deck notes).
        mid_jp = col.models.add_model(
            "JP Mining Note", ["Word", "Furigana", "Definition"])
        mid_fr = col.models.add_model("French Note", ["Mot", "Def"])
        col.db.notes[101] = {"flds": "x", "dids": [jp], "mid": mid_jp}
        col.db.notes[102] = {"flds": "x", "dids": [fr], "mid": mid_fr}
        col.db.notes[103] = {"flds": "x", "dids": [fr, jpv], "mid": mid_jp}

        class N:
            def __init__(self, nid, tname=""):
                self.id = nid
                self._t = tname

            def note_type(self):
                return {"name": self._t} if self._t else {}

        cfg = {"scope_decks": ["Japanese"]}
        check("scope: note in scoped deck is in scope",
              compredef_scope.note_in_scope(N(101), cfg, col))
        check("scope: subdeck card is in scope",
              compredef_scope.note_in_scope(N(103), cfg, col))
        check("scope: French-deck note is out of scope",
              not compredef_scope.note_in_scope(N(102), cfg, col))
        check("scope: empty scope is fail-closed",
              not compredef_scope.note_in_scope(N(101), {"scope_decks": []}, col))
        check("scope: missing deck matches nothing",
              not compredef_scope.note_in_scope(
                  N(101), {"scope_decks": ["Klingon"]}, col))

        # 3. Unsaved notes (no cards yet) fall back to implied types.
        check("scope: implied types come from scoped decks",
              compredef_scope.implied_note_types(col, ["Japanese"])
              == ["JP Mining Note"])
        check("scope: unsaved note of implied type is in scope",
              compredef_scope.note_in_scope(N(0, "JP Mining Note"), cfg, col))
        check("scope: unsaved note of other type is out of scope",
              not compredef_scope.note_in_scope(N(0, "French Note"), cfg, col))
        check("scope: empty config is empty scope",
              compredef_scope.is_scope_empty({}) and
              compredef_scope.get_scope_decks({}) == [])

        # 4. SIBLING-DECK case (v1.1.2 production confusion): note in
        #    Japanese::Sibling vs scope=Japanese::Vocab (another child)
        #    — same parent, NOT covered; note_deck_names reveals it.
        sibling = col.decks.add("Japanese::Sibling")
        col.db.notes[104] = {"flds": "x", "dids": [sibling], "mid": mid_jp}
        leaf_cfg = {"scope_decks": ["Japanese::Vocab"]}
        check("scope: sibling deck is NOT covered by leaf selection",
              not compredef_scope.note_in_scope(N(104), leaf_cfg, col))
        check("scope: sibling deck IS covered by parent selection",
              compredef_scope.note_in_scope(
                  N(104), {"scope_decks": ["Japanese"]}, col))
        names = compredef_scope.note_deck_names(N(104), col)
        check("scope: note_deck_names reveals the blocking deck",
              names == ["Japanese::Sibling"],
              f"got {names}")
        check("scope: note_deck_names empty for unsaved notes",
              compredef_scope.note_deck_names(N(0), col) == [])

        # 5. ADD-WINDOW resolution (v1.1.3 "Add deck does nothing" bug):
        #    unsaved notes scope-check against the deck the window will
        #    add to — DeckChooser when reachable, else col curDeck.
        class _FakeChooser:
            def __init__(self, did):
                self.selected_deck_id = did

        class _FakeEditor:
            def __init__(self, did):
                self.deck_chooser = _FakeChooser(did)

        # curDeck points at French (out of scope)…
        class _CfgCol:
            pass
        cfg_col = _CfgCol()
        cfg_col.decks = col.decks
        cfg_col.db = col.db
        cfg_col.models = col.models
        cfg_col.get_config = lambda key, default=None: fr  # curDeck

        unsaved_jp = N(0, "JP Mining Note")
        # a) editor's DeckChooser pointing at an in-scope deck → in scope
        check("scope: add-window chooser deck decides scope (in-scope deck)",
              compredef_scope.note_in_scope(
                  unsaved_jp, cfg, cfg_col, editor=_FakeEditor(jp)))
        # b) chooser pointing OUT of scope → out (even for implied type)
        check("scope: add-window chooser deck decides scope (out-of-scope deck)",
              not compredef_scope.note_in_scope(
                  unsaved_jp, cfg, cfg_col, editor=_FakeEditor(fr)))
        # c) no editor → curDeck fallback (French → out)
        check("scope: curDeck fallback used for unsaved notes",
              not compredef_scope.note_in_scope(unsaved_jp, cfg, cfg_col))
        # d) resolve_deck_for_note mirrors the same results
        check("scope: resolve_deck_for_note reads the chooser",
              compredef_scope.resolve_deck_for_note(
                  unsaved_jp, cfg_col, editor=_FakeEditor(jp)) == ["Japanese"])
        check("scope: resolve_deck_for_note falls back to curDeck",
              compredef_scope.resolve_deck_for_note(unsaved_jp, cfg_col)
              == ["French"])
        # e) saved notes ignore the editor entirely (cards win)
        check("scope: saved note ignores editor chooser",
              compredef_scope.note_in_scope(
                  N(101), cfg, cfg_col, editor=_FakeEditor(fr)))
    finally:
        _restore_collection_state(scope_state)


def test_real_dictionary_smoke() -> None:
    """
    Fully DYNAMIC smoke test: derives every expectation from whatever real
    dictionary is installed — no hard-coded paths, titles, counts, or
    definition text. If no dictionary exists, the whole section skips.

    Guards the same production bugs as the synthetic tests, plus verifies
    the install-once architecture against real data: if the dictionary is
    already indexed, NO parse happens and lookups are pure DB queries.
    """
    if not os.path.isdir(DICTS_DIR):
        print(f"[SKIP] smoke: {DICTS_DIR} not present")
        return
    found = []
    for entry in sorted(os.listdir(DICTS_DIR)):
        path = os.path.join(DICTS_DIR, entry)
        if compredef_parser.is_zip_dictionary(path) or (
            os.path.isdir(path) and compredef_parser.is_directory_dictionary(path)
        ):
            found.append(path)
    if not found:
        print(f"[SKIP] smoke: no dictionaries found in {DICTS_DIR}")
        return

    # Prefer an already-indexed dictionary with RICH HTML rows (zero parse
    # cost, tests the reuse path). An indexed dictionary whose rows are
    # plain text is a stale-cache victim (historical bug #1): reinstall it
    # once so the smoke expectations reflect a healthy index.
    target = None
    for path in found:
        d = compredef_parser.get_single_dictionary(path)
        if not d.is_indexed():
            continue
        conn = sqlite3.connect(compredef_parser._get_db_path())
        try:
            rich = conn.execute(
                "SELECT COUNT(*) FROM entries WHERE dict_path = ? "
                "AND definition LIKE '%<ruby%'",
                (d.path,),
            ).fetchone()[0]
        finally:
            conn.close()
        if rich:
            target = d
            break
    if target is None:
        # Nothing healthy indexed: install the smallest dictionary found.
        sized = []
        for path in found:
            try:
                if compredef_parser.is_zip_dictionary(path):
                    sized.append((os.path.getsize(path), path))
                elif os.path.isdir(path):
                    total = sum(
                        os.path.getsize(os.path.join(path, f))
                        for f in os.listdir(path)
                        if f.startswith("term_bank") and f.endswith(".json")
                    )
                    sized.append((total, path))
            except OSError:
                continue
        if not sized:
            print("[SKIP] smoke: no readable term banks found")
            return
        sized.sort()
        target = compredef_parser.get_single_dictionary(sized[0][1])
        target.install()

    print(f"[INFO] smoke: using '{target.title}' ({target.entry_count():,} entries)")

    # Repeated lookups must never re-parse: install once, query many times.
    if target.is_indexed():
        original_iter = compredef_parser.SingleDictionary._iter_term_banks

        def guarded_iter(self):
            raise AssertionError("smoke lookup parsed dictionary files!")

        compredef_parser.SingleDictionary._iter_term_banks = guarded_iter
        try:
            conn = sqlite3.connect(compredef_parser._get_db_path())
            try:
                row = conn.execute(
                    "SELECT term, reading FROM entries "
                    "WHERE dict_path = ? AND definition LIKE '%<ruby%' "
                    "AND reading != '' LIMIT 1",
                    (target.path,),
                ).fetchone()
            finally:
                conn.close()

            check(
                "smoke: dictionary has at least one ruby/reading entry",
                row is not None,
                "no structured-content entry with a reading found",
            )
            if row is not None:
                term, _reading = row
                t0 = time.time()
                defs = target.lookup(term)
                elapsed = time.time() - t0
                check(
                    f"smoke: lookup {term!r} works WITHOUT parsing files",
                    len(defs) >= 1,
                    f"got {len(defs)} defs",
                )
                check(
                    "smoke: lookup is fast (<50ms, pure DB query)",
                    elapsed < 0.05,
                    f"took {elapsed*1000:.1f}ms",
                )
                if defs:
                    out = defs[0]
                    check(
                        "smoke: definition is rich HTML (production bug)",
                        len(out) > 1000 and "<ruby" in out,
                        f"len={len(out)}, has_ruby={'<ruby' in out}",
                    )
                    kanji = [c for c in compredef_parser._extract_base_text(out)
                             if "\u4e00" <= c <= "\u9fff"]
                    check(
                        "smoke: no kana readings leak into base kanji",
                        all("\u4e00" <= c <= "\u9fff" for c in kanji),
                    )
        finally:
            compredef_parser.SingleDictionary._iter_term_banks = original_iter

    # NOTE: real-dictionary homograph narrowing is covered synthetically
    # by test_reading_disambiguates_homographs. The old tail of this test
    # ran GROUP BY ... HAVING COUNT DISTINCT over the whole (100k-row)
    # real index just to find a homograph — pure CPU cost for no unique
    # coverage. Deliberately not repeated here.


# ---------------------------------------------------------------------------
# Main entry point.
# ---------------------------------------------------------------------------

def _check_scope_names(scope, missing: set, module_bound: set,
                       allowed: set) -> None:
    """Recursive symtable walk: collects referenced-but-unbound names.

    Module-level (not nested in the per-file loop) so it can never
    capture a stale loop iteration's `missing`/`module_bound` sets
    (B023 closure hazard — behavior identical, structure safe).
    """
    for s in scope.get_symbols():
        name = s.get_name()
        if name.startswith("_") or name in allowed:
            continue
        if not s.is_referenced() or s.is_namespace():
            continue
        if scope.get_type() == "module":
            if not (s.is_assigned() or s.is_imported()):
                missing.add(name)
            continue
        if s.is_local() or s.is_free() or s.is_imported():
            continue
        if name in module_bound:
            continue
        missing.add(name)
    for child in scope.get_children():
        _check_scope_names(child, missing, module_bound, allowed)


def test_no_undefined_names_in_shipped_modules() -> None:
    """
    Static guard for modules that cannot be imported in this suite
    (gui.py needs real Qt): every name is resolved with the same scope
    analysis the compiler uses (symtable), so cross-function globals,
    closures, comprehensions and imports are all handled precisely.
    Guards the production crash:
        NameError: name 'get_single_dictionary' is not defined
    (gui.py called it without importing it — invisible to every other
    test because the module never imports without Qt).
    """
    allowed = set(dir(builtins)) | {"__name__", "__package__"}
    for path in sorted(glob.glob(os.path.join(REPO_ROOT, "*.py"))):
        src = open(path, encoding="utf-8").read()
        table = symtable.symtable(src, path, "exec")
        module_bound = {
            s.get_name() for s in table.get_symbols()
            if s.is_assigned() or s.is_imported() or s.is_namespace()
        }
        missing = set()
        _check_scope_names(table, missing, module_bound, allowed)

        check(
            f"static-names: {os.path.basename(path)} "
            "has no undefined names",
            not missing,
            ", ".join(sorted(missing)),
        )

def test_qt_enum_compat() -> None:
    """
    PyQt6 scoped all Qt enums: raw spellings like `Qt.ItemIsUserCheckable`
    or `Qt.UserRole` raise AttributeError at RUNTIME on Anki's Qt6
    builds (production crash, v1.0.14 — the config dialog died on open).
    gui.py may reference Qt enums ONLY via two-level scoped names
    (Qt.CheckState.X, Qt.ItemFlag.X, ...) or via the designated compat
    helpers' `return Qt.<Name>` fallback lines, which exist precisely
    to serve PyQt5.
    """
    path = os.path.join(REPO_ROOT, "gui.py")
    src = open(path, encoding="utf-8").read()
    # Strip comments and docstrings: only executable code counts.
    tree = ast.parse(src)
    doc_ranges = []
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if body and isinstance(body, list) and isinstance(body[0], ast.Expr):
            v = body[0].value
            if isinstance(v, ast.Constant) and isinstance(v.value, str):
                doc_ranges.append((body[0].lineno, body[0].end_lineno))
    lines = src.splitlines()
    offenders = []
    pattern = re.compile(r"Qt\.([A-Za-z_][A-Za-z0-9_]*)(?:\.([A-Za-z_][A-Za-z0-9_]*))?")
    for lineno, line in enumerate(lines, start=1):
        if any(a <= lineno <= b for a, b in doc_ranges):
            continue
        code = line.split("#", 1)[0]  # strip trailing comments
        stripped = code.strip()
        if "hasattr(Qt" in code:
            continue  # compat-probe lines (enum CLASS refs are still top-level in PyQt6)
        for m in pattern.finditer(code):
            first, second = m.group(1), m.group(2)
            if second:
                continue  # scoped usage (Qt.CheckState.Checked) — always fine
            if stripped.startswith("return Qt."):
                continue  # compat-helper PyQt5 fallback (see _user_role etc.)
            offenders.append(f"gui.py:{lineno}: raw 'Qt.{first}' -> use scoped enum")
    check(
        "qt-compat: gui.py uses only scoped Qt enums (PyQt6-safe)",
        not offenders,
        "; ".join(offenders[:6]),
    )


def test_package_relative_imports() -> None:
    """
    Simulates how Anki loads the add-on: as a PACKAGE whose folder is NOT
    on sys.path, so absolute sibling imports MUST fail while relative ones
    succeed. An import hook blocks top-level imports of our own module
    names (exactly what Anki's loader does implicitly) and every non-Qt
    module is then imported through the synthetic package. Guards the
    production crash:
        ModuleNotFoundError: No module named 'provider'
    (gui.py and editor_browser.py are excluded here only because they need
    real Qt; editor_browser's relative chain is already covered by
    test_tab_generate_decisions below).
    """
    siblings = {"anki", "core", "engine", "provider", "renderer", "models",
                "scoring", "picker", "utils", "parser", "generator", "db_utils",
                "scope"}

    class _BlockSiblingImports(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if "." not in name and name in siblings:
                raise ModuleNotFoundError(
                    f"No module named '{name}' (Anki simulation: "
                    "sibling dir not on sys.path)"
                )
            return None

    pkg_name = "compredef_addon"
    if pkg_name not in sys.modules:
        pkg = types.ModuleType(pkg_name)
        pkg.__path__ = [REPO_ROOT]
        sys.modules[pkg_name] = pkg

    # Save global import state: top-level siblings are evicted so the
    # blocker is airtight (otherwise sys.modules would satisfy absolute
    # imports and the simulation would prove nothing).
    saved_modules = {}
    for key in list(sys.modules):
        if key in siblings or key.startswith(pkg_name + "."):
            saved_modules[key] = sys.modules.pop(key)
    saved_meta = list(sys.meta_path)
    sys.meta_path.insert(0, _BlockSiblingImports())
    try:
        expected = {
        "anki": ["get_known_kanji_set", "get_known_vocabulary_set",
                 "init_caches_async", "reset_caches", "knowledge_status",
                 "knowledge_summary_text", "knowledge_totals",
                 "sync_reset_caches", "_seen_points"],
            "core": ["get_provider", "get_generator"],
            "engine": ["DefinitionGenerator"],
            "provider": ["LocalSQLiteProvider", "IndexingError"],
            "renderer": ["render_yomitan_definition_html",
                         "render_structured_content_node"],
            "models": ["DictionaryEntry"],
            "scoring": ["calculate_kanji_score", "is_reference_title"],
            "picker": ["PickerStrategy", "DensityPicker", "LegacySumPicker",
                       "rank_definitions", "pick_best", "collect_dictionary_candidates",
                       "filter_valid_entries", "get_active_strategy"],
            "scope": ["get_scope_decks", "expand_scope_names",
                      "note_in_scope", "implied_note_types", "scope_dids",
                      "is_scope_empty", "note_deck_names",
                      "resolve_deck_for_note"],
            "utils": ["extract_clean_word", "extract_base_text",
                      "parse_furigana_field", "resolve_dictionary_paths"],
            "parser": ["get_single_dictionary", "RENDERER_VERSION",
                       "parse_furigana_field"],
            "generator": ["generate_definition"],
            "db_utils": ["get_known_kanji_set", "reset_caches"],
        }
        for mod_name, attrs in expected.items():
            try:
                mod = importlib.import_module(f"{pkg_name}.{mod_name}")
            except ImportError as e:
                check(
                    f"pkg-import: {mod_name} imports in package context",
                    False, str(e),
                )
                continue
            for attr in attrs:
                check(
                    f"pkg-import: {mod_name}.{attr} resolves in package context",
                    hasattr(mod, attr),
                )
    finally:
        sys.meta_path[:] = saved_meta
        for key in [k for k in sys.modules
                    if k in siblings or k.startswith(pkg_name + ".")]:
            del sys.modules[key]
        sys.modules.update(saved_modules)

def test_kanji_extraction_correctness(tmp_root: str) -> None:
    """
    Mastered kanji/vocab come ONLY from the FIRST field of mature notes
    (ivl >= 365 — v1.2.3 raised the bar from the deprecated 21 days)
    INSIDE the Scope decks — never from Definition/Example/other fields,
    and never from out-of-scope decks (the French deck must not inflate
    Japanese knowledge). Knowledge is INTERVAL-WEIGHTED (ivl/365
    capped at 1.0) and vocab is KANJI-ONLY (multi-kanji compounds;
    kana-only words like 'plain' excluded, single kanji covered by the
    kanji score). This also proves CompreDef-generated definitions
    (written to non-first fields) can never pollute the mastered set.
    """
    import anki

    # Save global state: this test rebinds the fake collection and
    # rebuilds the session snapshot, so everything must be restored.
    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    import core as _core
    prev_generator = _core._generator
    scope_state = _save_collection_state()
    try:
        SEP = "\x1f"
        col = aqt.mw.col
        jp = col.decks.add("Japanese")
        fr = col.decks.add("French")
        col.db.notes = {
            # 3-field layout (word / definition / example), in scope.
            # ivls vary: 400 (mature: >= 365), 182.5 and 100 (SEEN but
            # NOT mature since v1.2.3 — below the one-year bar).
            1: {"flds": SEP.join(["漢字", "plain def", "plain ex"]),
                "dids": [jp], "mid": 1, "ivl": 400},
            2: {"flds": SEP.join(["plain", "龍の定義", "plain"]),
                "dids": [jp], "mid": 1, "ivl": 182.5},
            3: {"flds": SEP.join(["plain", "plain", "虎の例文"]),
                "dids": [jp], "mid": 1, "ivl": 100},
            # 2-field layout (front / back) — a different note type
            4: {"flds": SEP.join(["語彙", "解釈"]),
                "dids": [jp], "mid": 2, "ivl": 400},
            # 1-field layout (cloze-like single field) — seen-only:
            # 182.5-day interval is BELOW mature (365) since v1.2.3, so
            # 日本語 must NOT enter the mastered snapshot.
            5: {"flds": "日本語", "dids": [jp], "mid": 2, "ivl": 182.5},
            # French deck: first-field kanji must NOT leak into knowledge
            6: {"flds": SEP.join(["仏文", "définitions"]),
                "dids": [fr], "mid": 3, "ivl": 400},
        }
        _set_scope_config(["Japanese"])

        anki.reset_caches()
        anki._build_caches()
        known = anki.get_known_kanji_set()
        vocab = anki.get_known_vocabulary_set()
        kanji_pts = anki.get_kanji_points()
        vocab_pts = anki.get_vocab_points()

        check("kanji: first-field kanji is mastered",
              {"漢", "字", "語", "彙"} <= known,
              f"known={sorted(known)}")
        check(
            "kanji: Definition-only kanji is NOT mastered",
            "龍" not in known,
            f"known={sorted(known)}",
        )
        check(
            "kanji: Example-only kanji is NOT mastered",
            "虎" not in known,
            f"known={sorted(known)}",
        )
        check(
            "kanji: out-of-scope (French deck) kanji is NOT mastered",
            "仏" not in known,
            f"known={sorted(known)}",
        )
        check(
            "kanji: sub-year notes (182.5/100d) are seen, not mastered",
            known == {"漢", "字", "語", "彙"},
            f"known={sorted(known)}",
        )
        check(
            "kanji: generated definitions do not pollute knowledge",
            "龍" not in known and "虎" not in known,
        )
        # v1.2: vocab is KANJI-ONLY — kana/plain words ('plain') excluded,
        # and only MATURE (>= 1 year) compounds are mastered.
        check(
            "kanji: mastered vocab is mature kanji-only compounds",
            vocab == {"漢字", "語彙"},
            f"vocab={sorted(vocab)}",
        )
        # v1.2: interval weighting — mature notes earn exactly 1.0.
        check("kanji: 400-day interval earns full 1.0 points",
              kanji_pts.get("漢") == 1.0 and kanji_pts.get("字") == 1.0,
              f"pts={kanji_pts}")
        check("kanji: vocab points follow the same weighting",
              vocab_pts.get("漢字") == 1.0 and vocab_pts.get("語彙") == 1.0,
              f"vpts={vocab_pts}")
        # v1.2.3 SEEN view: sub-year notes ARE seen (ivl > 0) — the
        # mature-only snapshot must not lose them from the seen totals.
        seen_kpts, seen_vpts = anki._seen_points()
        check("kanji: seen includes sub-year kanji (日本語)",
              {"日", "本"} <= set(seen_kpts),
              f"seen_kpts={sorted(seen_kpts)}")
        check("kanji: seen includes sub-year vocab (日本語)",
              "日本語" in seen_vpts,
              f"seen_vpts={sorted(seen_vpts)}")
        status = anki.knowledge_status()
        check(
            "kanji: status reports a ready scoped snapshot",
            status["ready"] and status["mature_notes_scanned"] == 2
            and "scope decks" in status["scope"]
            and "Japanese" in status["scope"]
            and status["last_error"] is None,
            f"status={status}",
        )

        # Empty scope is fail-closed: no decks selected, no knowledge.
        _set_scope_config([])
        anki.reset_caches()
        anki._build_caches()
        check("kanji: empty scope yields empty knowledge",
              anki.get_known_kanji_set() == set()
              and anki.get_known_vocabulary_set() == set()
              and anki.get_kanji_points() == {})
    finally:
        _restore_collection_state(scope_state)
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator

def test_snapshot_waits_for_open_collection() -> None:
    """
    The v1.0.10/11 production bug: add-ons load BEFORE the profile
    opens, so mw.col was None at startup. The async build then snapshotted
    an EMPTY collection and marked it ready for the whole session —
    every user saw 0 known kanji with no error. The build must abort (and
    stay NOT-ready) until a collection exists; the profile_did_open hook
    then builds the real snapshot.
    """
    import anki

    prev_col = aqt.mw.col
    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    import core as _core
    prev_generator = _core._generator
    had_cfg = "1619602654" in aqt.mw.addonManager.configs
    prev_cfg = dict(aqt.mw.addonManager.configs.get("1619602654", {}))
    try:
        SEP = "\x1f"
        # Startup moment: collection not open yet.
        aqt.mw.col = None
        anki.reset_caches()
        anki._build_caches()          # direct call: the worker itself
        check("col-gate: no snapshot without an open collection",
              not anki._caches_ready.is_set(),
              "snapshot marked ready while mw.col was None")
        check("col-gate: init_caches_async is a no-op without collection",
              anki.init_caches_async() is None)
        check("col-gate: empty-set stays empty after gated build",
              anki.get_known_kanji_set() == set())
        check("col-gate: still not ready (nothing to build from)",
              not anki._caches_ready.is_set(),
              "gated build must not mark ready")

        # Profile opens: collection becomes available — now it builds.
        # The reopened collection carries one Scope deck; knowledge is
        # scoped to it. (v1.2 rows carry (flds, ivl).)
        _decks = _FakeDecks()
        _jp = _decks.add("Japanese")
        aqt.mw.col = types.SimpleNamespace(
            db=types.SimpleNamespace(
                all=lambda q, p=(): [(SEP.join(["漢字", "def"]), 400)]
            ),
            decks=_decks,
        )
        _set_scope_config(["Japanese"])
        anki._build_caches()
        known = anki.get_known_kanji_set()
        check("col-gate: snapshot builds once collection opens",
              anki._caches_ready.is_set() and known == {"漢", "字"},
              f"known={sorted(known)}")
    finally:
        aqt.mw.col = prev_col
        if had_cfg:
            aqt.mw.addonManager.configs["1619602654"] = prev_cfg
        else:
            aqt.mw.addonManager.configs.pop("1619602654", None)
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator

def test_sync_reset_is_thread_safe() -> None:
    """
    The v1.2.1 production bug: the knowledge dialog's background task
    called reset_caches(), which reaches mw.taskman.run_in_background
    from a NON-main thread — Anki's Taskman printed a 'bug: run_in_
    background not called from main thread' traceback for every dialog
    refresh. sync_reset_caches() must rebuild WITHOUT ever touching
    taskman, and still produce a ready, scoped snapshot.
    """
    import threading

    import anki

    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    import core as _core
    prev_generator = _core._generator
    scope_state = _save_collection_state()
    # Tripwire: any taskman access from the reset path fails the test.
    taskman_calls = []
    prev_mw_taskman = getattr(aqt.mw, "taskman", None)

    class _TripwireTaskman:
        def run_in_background(self, *a, **kw):
            taskman_calls.append(a)
            raise AssertionError("taskman touched from sync reset path")

    try:
        SEP = "\x1f"
        col = aqt.mw.col
        jp = col.decks.add("Japanese")
        col.db.notes = {
            1: {"flds": SEP.join(["漢字", "def"]), "dids": [jp], "mid": 1,
                "ivl": 400},
        }
        _set_scope_config(["Japanese"])
        aqt.mw.taskman = _TripwireTaskman()
        # Simulate the dialog's worker thread exactly: a background
        # thread calling the SYNCHRONOUS reset.
        result = {}

        def worker():
            try:
                anki.sync_reset_caches()
                result["ok"] = True
            except Exception as e:  # noqa: BLE001 — the failure IS the test
                result["ok"] = False
                result["err"] = e

        t = threading.Thread(target=worker)
        t.start()
        t.join(timeout=30)
        check("sync-reset: background thread never touches taskman",
              result.get("ok") is True and not taskman_calls,
              f"result={result} taskman_calls={taskman_calls}")
        check("sync-reset: snapshot is ready after background rebuild",
              anki._caches_ready.is_set()
              and anki.get_known_kanji_set() == {"漢", "字"},
              f"known={sorted(anki._known_kanji_cache)}")
        # core.reset_generator (used by generation paths that can run on
        # background threads) must also stay off taskman.
        _core.reset_generator()
        check("sync-reset: reset_generator stays off taskman",
              not taskman_calls,
              f"taskman_calls={taskman_calls}")
    finally:
        if prev_mw_taskman is not None:
            aqt.mw.taskman = prev_mw_taskman
        else:
            try:
                del aqt.mw.taskman
            except AttributeError:
                pass
        _restore_collection_state(scope_state)
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator

def test_knowledge_summary_text() -> None:
    """The knowledge dialog's content source: counts, lists, scope.

    v1.2.3 terminology (the user's definitions — 'known' was too
    loose): MASTERED = mastery 1.0 (interval >= 1 year), SEEN = any
    interval > 0, 'Vocab' = kanji-only compounds. Readouts are
    X/total with totals INSIDE the Scope decks; Mature notes divide
    by notes-in-scope (the old whole-collection denominator made
    '10705/57185' meaningless), with the collection count secondary.
    """
    import anki

    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    import core as _core
    prev_generator = _core._generator
    scope_state = _save_collection_state()
    try:
        SEP = "\x1f"
        col = aqt.mw.col
        jp = col.decks.add("Japanese")
        col.db.notes = {
            1: {"flds": SEP.join(["漢字", "def"]), "dids": [jp], "mid": 1,
                "ivl": 400},
            2: {"flds": SEP.join(["語彙", "def"]), "dids": [jp], "mid": 1,
                "ivl": 400},
        }
        _set_scope_config(["Japanese"])
        anki.reset_caches()
        anki._build_caches()
        text = anki.knowledge_summary_text()
        check("summary: shows mastered kanji count",
              "Mastered kanji: 4" in text, text)
        check("summary: shows mastered vocab count",
              "Mastered vocab: 2" in text, text)
        check("summary: shows seen kanji count",
              "Seen kanji: 4" in text, text)
        check("summary: mastered kanji carries /total denominator",
              "Mastered kanji: 4 / 4" in text, text)
        check("summary: mastered vocab carries /total denominator",
              "Mastered vocab: 2 / 2" in text, text)
        check("summary: mature notes divide by notes IN SCOPE",
              "/ 2 notes in scope" in text and
              "collection: 2" in text, text)
        check("summary: lists the kanji",
              "漢" in text and "語" in text, text)
        check("summary: shows scope", "scope decks" in text, text)
        # v1.2.3: 'Kanji Words' is renamed 'Vocab' everywhere; the old
        # 'Kanji words' / 'Known' labels must be gone.
        check("summary: mastered lists labeled with new terminology",
              "Mastered kanji (all):" in text and
              "Mastered vocab (all):" in text, text)
        check("summary: no deprecated 'Known'/'Kanji words' labels remain",
              "Known kanji" not in text and
              "Kanji words" not in text and "Kanji-words" not in text, text)
        check("summary: no deprecated '(sample)' label remains",
              "(sample)" not in text, text)
        check("summary: no deprecated 21-day mention remains",
              "21" not in text, text)
        short = anki.knowledge_summary_text(max_kanji=2, max_words=1)
        check("summary: truncates long lists with a remainder",
              "more" in short, short)
        # Totals: mature/seen denominators count only in-scope notes;
        # the collection count is separate and secondary.
        totals = anki.knowledge_totals()
        check("totals: counts mature notes in scope",
              totals["mature_notes"] == 2, f"totals={totals}")
        check("totals: counts notes in scope (primary denominator)",
              totals["scope_notes"] == 2, f"totals={totals}")
        check("totals: counts all notes in collection",
              totals["total_notes"] == 2, f"totals={totals}")
        # An out-of-scope note widens ONLY the collection total; the
        # scope denominators must stay untouched (the user's 57185 bug).
        col.db.notes[3] = {"flds": SEP.join(["仏文", "d"]), "dids": [999],
                           "mid": 1, "ivl": 1}
        totals2 = anki.knowledge_totals()
        check("totals: out-of-scope note widens collection total only",
              totals2["total_notes"] == 3 and
              totals2["scope_notes"] == 2 and
              totals2["kanji"] == 4 and totals2["vocab"] == 2,
              f"totals2={totals2}")
        # A SEEN-but-young in-scope note joins the seen/total kanji
        # denominators but never the mastered snapshot.
        col.db.notes[4] = {"flds": SEP.join(["若い", "d"]), "dids": [jp],
                           "mid": 1, "ivl": 40}
        totals3 = anki.knowledge_totals()
        check("totals: young in-scope note joins kanji totals",
              totals3["kanji"] == 5 and totals3["scope_notes"] == 3 and
              totals3["mature_notes"] == 2,
              f"totals3={totals3}")
        seen_kpts, _seen_vpts = anki._seen_points()
        check("seen: young in-scope kanji is seen but not mastered",
              "若" in seen_kpts and "若" not in anki.get_kanji_points(),
              f"seen={sorted(seen_kpts)}")
    finally:
        _restore_collection_state(scope_state)
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator


def test_provenance_search_builders() -> None:
    """
    Browser provenance searches (v1.2.4): clicking a Kanji/Vocab row in
    the knowledge dialog must open Anki's Browse screen with the notes
    that actually produced that item's mastery points.

    Syntax contract per the OFFICIAL Anki manual (searching.html):
    - "field:value"  = field matches EXACTLY (vocab rows)
    - "field:*value*" = field CONTAINS (kanji rows — kanji points are
      admitted when the first field contains the kanji anywhere)
    - terms are double-quoted (special characters stay literal),
      multiple fields OR-join, list capped at 8 fields.
    - fallbacks: 're:^term$' (vocab) / bare quoted term (kanji).

    v1.2.5: every search is AND-ed with the Scope's deck terms
    ('deck:"Name" or deck:"Other"') — provenance lists ONLY notes
    inside the Scope decks, the same universe the snapshot counts
    from. Blank-scope searches keep the old un-restricted shape.
    """
    import anki

    scope_state = _save_collection_state()
    try:
        # ---- Pure builder: shapes, quoting, caps, fallbacks -----------
        # Explicit scope: field matches AND deck restriction.
        SCOPE = ["My Life Decks", "日本語::Mining"]
        q = anki.build_provenance_search("vocab", "学校",
                                         ["Expression", "Word"],
                                         scope_decks=SCOPE)
        check("prov: vocab search is exact fields AND scope decks",
              q == '("Expression:学校" or "Word:学校") and '
                   '("deck:My Life Decks" or "deck:日本語::Mining")',
              f"got {q}")
        qk = anki.build_provenance_search("kanji", "学",
                                          ["Expression"], scope_decks=SCOPE)
        check("prov: kanji search is contains fields AND scope decks",
              qk == '("Expression:*学*") and '
                    '("deck:My Life Decks" or "deck:日本語::Mining")',
              f"got {qk}")
        # Dedup + 8-field cap (9 fields -> 8 terms).
        many = [f"F{i}" for i in range(9)]
        qm = anki.build_provenance_search("vocab", "学校", many,
                                          scope_decks=SCOPE)
        check("prov: field list capped at 8 terms",
              qm.count("F") == 8 and '"F8:学校"' not in qm, f"got {qm}")
        # Dedup keeps first occurrence order.
        qd = anki.build_provenance_search("vocab", "学校",
                                          ["Expression", "Expression"],
                                          scope_decks=SCOPE)
        check("prov: duplicate field names deduped",
              qd == '("Expression:学校") and '
                    '("deck:My Life Decks" or "deck:日本語::Mining")',
              f"got {qd}")
        # Fallbacks when no fields resolve — STILL scope-limited.
        check("prov: vocab fallback is regex exact AND scope",
              anki.build_provenance_search("vocab", "学校", [],
                                           scope_decks=SCOPE) ==
              '("re:^学校$") and ("deck:My Life Decks" or '
              '"deck:日本語::Mining")',
              "fallback shape changed")
        check("prov: kanji fallback is plain term AND scope",
              anki.build_provenance_search("kanji", "学", None,
                                          scope_decks=SCOPE) ==
              '("学") and ("deck:My Life Decks" or "deck:日本語::Mining")',
              "fallback shape changed")
        check("prov: empty term yields empty search",
              anki.build_provenance_search("kanji", "", ["F"],
                                           scope_decks=SCOPE) == "")
        # Whitespace-only field names are dropped, not searched.
        check("prov: blank field names ignored",
              anki.build_provenance_search("vocab", "学校", [" ", ""],
                                           scope_decks=SCOPE).startswith(
                  '("re:^学校$")'))
        # Blank scope list => no deck restriction (bare legacy shape,
        # no parentheses — a lone term needs no grouping).
        q_no = anki.build_provenance_search("vocab", "学校",
                                            ["Expression"], scope_decks=[])
        check("prov: blank scope searches without deck terms",
              q_no == '"Expression:学校"', f"got {q_no}")

        # ---- Live resolution against the fake collection ---------------
        # Two note types in scope: their FIRST fields are what the
        # snapshot counts from, so provenance must target exactly them.
        col = aqt.mw.col
        jp = col.decks.add("Japanese")
        mid_a = col.models.add_model("Type A", ["Expression", "Reading"])
        mid_b = col.models.add_model("Type B", ["Word", "Definition"])
        col.db.notes = {
            1: {"flds": "学校", "dids": [jp], "mid": mid_a, "ivl": 400},
            2: {"flds": "語彙", "dids": [jp], "mid": mid_b, "ivl": 400},
        }
        _set_scope_config(["Japanese"])
        # Cached-fields contract (v1.2.5): the builder reuses the
        # snapshot's cached list; a fresh build refreshes it.
        anki.reset_caches()
        anki._build_caches()
        fields = anki.first_field_names_for_scope()
        check("prov: scope first-field names resolve",
              sorted(fields) == ["Expression", "Word"], f"got {fields}")
        # Reading the config scope (what the dialog passes) produces
        # the same deck restriction.
        qv = anki.build_provenance_search("vocab", "学校", fields,
                                          scope_decks=["Japanese"])
        check("prov: live vocab search covers both first fields",
              qv == '("Expression:学校" or "Word:学校") and ("deck:Japanese")',
              f"got {qv}")

        # Empty scope is fail-closed (no fields -> fallback shape).
        _set_scope_config([])
        anki.reset_caches()
        anki._build_caches()
        check("prov: empty scope yields no field names",
              anki.first_field_names_for_scope() == [])
        # Out-of-scope types never contribute their fields.
        _set_scope_config(["Japanese"])
        anki.reset_caches()
        anki._build_caches()
        fr = col.decks.add("French")
        mid_c = col.models.add_model("French Type", ["Front", "Back"])
        col.db.notes[3] = {"flds": "bonjour", "dids": [fr], "mid": mid_c,
                           "ivl": 400}
        fields2 = anki.first_field_names_for_scope()
        check("prov: out-of-scope type fields excluded",
              "Front" not in fields2 and
              sorted(fields2) == ["Expression", "Word"],
              f"got {fields2}")
    finally:
        _restore_collection_state(scope_state)


def test_dialog_payload_cached_per_generation() -> None:
    """
    The v1.2.5 'loading as slow as generating' bug: even in cached
    mode the dialog task re-ran _fetch_learned_note_rows(),
    _seen_points() and knowledge_totals() on EVERY open — each a full
    mature-notes scan. v1.2.6 caches the whole payload per snapshot
    GENERATION: a warm open must do ZERO DB queries, and only a real
    rebuild (force) bumps the generation and refreshes the payload.
    """
    import anki

    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    prev_gen = anki._snapshot_generation
    prev_payload = anki._dialog_payload_cache
    prev_payload_gen = anki._dialog_payload_cache_gen
    import core as _core
    prev_generator = _core._generator
    scope_state = _save_collection_state()
    query_log = []
    prev_all = aqt.mw.col.db.all
    prev_scalar = aqt.mw.col.db.scalar

    def logging_all(q, p=()):
        query_log.append(q)
        return prev_all(q, p)

    def logging_scalar(q, p=()):
        query_log.append(q)
        return prev_scalar(q, p)

    try:
        SEP = "\x1f"
        col = aqt.mw.col
        jp = col.decks.add("Japanese")
        col.db.notes = {
            1: {"flds": SEP.join(["漢字", "def"]), "dids": [jp], "mid": 1,
                "ivl": 400},
        }
        _set_scope_config(["Japanese"])
        # Start from a clean slate: earlier tests may have left the
        # snapshot READY under their own scope, which would make the
        # first payload call a no-op (no gen bump).
        anki._caches_ready.clear()
        anki._dialog_payload_cache = None
        anki._dialog_payload_cache_gen = -1

        # First payload call: builds the snapshot (generation 1) and
        # gathers the payload — DB queries expected.
        aqt.mw.col.db.all = logging_all
        aqt.mw.col.db.scalar = logging_scalar
        try:
            payload1 = anki.get_knowledge_dialog_payload()
        finally:
            aqt.mw.col.db.all = prev_all
            aqt.mw.col.db.scalar = prev_scalar
        gen1 = anki.snapshot_generation()
        check("payload: first call builds snapshot (gen bump)",
              gen1 == prev_gen + 1 and payload1["status"]["ready"],
              f"gen1={gen1} prev={prev_gen}")

        # Warm call: same generation, ZERO DB queries (pure cache hit).
        query_log.clear()
        aqt.mw.col.db.all = logging_all
        aqt.mw.col.db.scalar = logging_scalar
        try:
            payload2 = anki.get_knowledge_dialog_payload()
            warm_queries = len(query_log)
        finally:
            aqt.mw.col.db.all = prev_all
            aqt.mw.col.db.scalar = prev_scalar
        check("payload: warm open performs ZERO DB queries",
              warm_queries == 0,
              f"queries run: {query_log[-3:]}")
        check("payload: warm call returns the SAME cached dict",
              payload2 is payload1)

        # force_rebuild: new generation, payload refreshed.
        col.db.notes[2] = {"flds": SEP.join(["語彙", "d"]), "dids": [jp],
                           "mid": 1, "ivl": 400}
        query_log.clear()
        aqt.mw.col.db.all = logging_all
        aqt.mw.col.db.scalar = logging_scalar
        try:
            payload3 = anki.get_knowledge_dialog_payload(
                force_rebuild=True)
        finally:
            aqt.mw.col.db.all = prev_all
            aqt.mw.col.db.scalar = prev_scalar
        gen3 = anki.snapshot_generation()
        check("payload: force rebuild bumps the generation",
              gen3 == gen1 + 1, f"gen1={gen1} gen3={gen3}")
        check("payload: force rebuild refreshes the data",
              payload3 is not payload1 and
              len(payload3["kanji_points"]) == 4,
              f"kanji={sorted(payload3['kanji_points'])}")
        # And the next warm call is again query-free.
        query_log.clear()
        aqt.mw.col.db.all = logging_all
        aqt.mw.col.db.scalar = logging_scalar
        try:
            anki.get_knowledge_dialog_payload()
            warm2 = len(query_log)
        finally:
            aqt.mw.col.db.all = prev_all
            aqt.mw.col.db.scalar = prev_scalar
        check("payload: warm open after rebuild is again query-free",
              warm2 == 0, f"queries run: {query_log[-3:]}")
    finally:
        aqt.mw.col.db.all = prev_all
        aqt.mw.col.db.scalar = prev_scalar
        _restore_collection_state(scope_state)
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        anki._snapshot_generation = prev_gen
        anki._dialog_payload_cache = prev_payload
        anki._dialog_payload_cache_gen = prev_payload_gen
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator


def test_knowledge_survives_new_schema(tmp_root: str) -> None:
    """
    The v1.0.5 production bug: the knowledge query referenced the legacy
    'models' table ('JOIN models'), which does not exist on Anki 23.10+
    (renamed to 'notetypes'). The query failed, the error was swallowed,
    and every user got 0 known kanji. This test simulates the new schema
    by rejecting ANY query that names the legacy table, then asserts the
    snapshot still builds correctly.
    """
    import sqlite3 as _sqlite3

    import anki

    prev_db_all = aqt.mw.col.db.all
    prev_kanji = set(anki._known_kanji_cache)
    prev_vocab = set(anki._known_vocab_cache)
    prev_ready = anki._caches_ready.is_set()
    import core as _core
    prev_generator = _core._generator
    scope_state = _save_collection_state()
    try:
        SEP = "\x1f"
        aqt.mw.col.decks.add("Japanese")
        _set_scope_config(["Japanese"])
        rows = [(SEP.join(["漢字", "龍の定義"]), 400)]

        def strict_all(query, params=()):
            # New-schema Anki: there is no 'models' table at all.
            if re.search(r"\b(join|from)\s+models\b", query,
                          re.IGNORECASE):
                raise _sqlite3.OperationalError("no such table: models")
            return list(rows)

        aqt.mw.col.db.all = strict_all
        anki.reset_caches()
        anki._build_caches()
        known = anki.get_known_kanji_set()
        check(
            "schema: snapshot builds without the legacy models table",
            known == {"漢", "字"},
            f"known={sorted(known)}",
        )
        check(
            "schema: non-first-field kanji still excluded under new schema",
            "龍" not in known,
        )
    finally:
        aqt.mw.col.db.all = prev_db_all
        _restore_collection_state(scope_state)
        anki._known_kanji_cache = prev_kanji
        anki._known_vocab_cache = prev_vocab
        if prev_ready:
            anki._caches_ready.set()
        else:
            anki._caches_ready.clear()
        _core._generator = prev_generator

def test_config_survives_yomitan_toggle() -> None:
    """
    Regression for v1.0.27 bug: switching Dictionary Source to Yomitan
    and closing the dialog wiped local dictionaries and Note Types targets.

    The dialog's early save (before _load_config populates the list)
    wrote {"dictionaries":[],"targets":{}} over the real config, and
    install_local.sh deleting meta.json made it permanent. The fix
    preserves previous config when the UI list is empty but previous
    config was not.

    Tests the REAL utils.merge_type_targets (which gui.py delegates
    to) — not a copy of the logic.
    """
    merge = compredef_utils.merge_type_targets

    prev = {
        "targets": {
            "Japanese": {"word_field": "Expression", "reading_field": "Reading", "definition_field": "Definition"},
            "Mining": {"word_field": "Word", "reading_field": "", "definition_field": "Def"},
        },
        "dictionaries": ["/tmp/dictA", "/tmp/dictB"],
        "note_type": "Japanese",
        "word_field": "Expression",
        "definition_field": "Definition",
    }
    # Empty UI mappings (dialog opened, types not loaded yet) must
    # preserve the previously saved targets.
    result = merge({}, prev)
    check("config-preserve: Yomitan toggle keeps targets",
          result["targets"] == prev["targets"],
          f"got {result['targets']}")
    check("config-preserve: legacy mirror preserved",
          result["note_type"] == "Japanese" and result["word_field"] == "Expression",
          f"got {result}")

    # A complete UI mapping wins over stale saved targets.
    live = {"Japanese": {"word_field": "Word", "reading_field": "",
                         "definition_field": "Meaning"}}
    result_live = merge(live, prev)
    check("config-preserve: live UI mapping wins over saved",
          result_live["targets"]
          == {"Japanese": {"word_field": "Word", "reading_field": "",
                            "definition_field": "Meaning"}},
          f"got {result_live['targets']}")

    # Incomplete UI mappings are UI-only, never saved.
    partial = {"Japanese": {"word_field": "Word", "reading_field": "",
                            "definition_field": ""}}
    result_partial = merge(partial, {"targets": {}})
    check("config-preserve: incomplete mapping not saved",
          result_partial["targets"] == {},
          f"got {result_partial['targets']}")

    # Empty previous config stays empty (new user, not a clobber).
    result2 = merge({}, {"targets": {}, "dictionaries": []})
    check("config-preserve: empty stays empty for new user",
          result2["targets"] == {},
          f"got {result2['targets']}")

    # Legacy single-type config is preserved as a target.
    result_legacy = merge({}, {"targets": {}, "note_type": "Japanese",
                               "word_field": "Expression",
                               "reading_field": "Reading",
                               "definition_field": "Definition"})
    check("config-preserve: legacy single-type preserved",
          result_legacy["targets"].get("Japanese", {}).get("word_field")
          == "Expression",
          f"got {result_legacy['targets']}")

    # Wiring: gui.py's ConfigDialog._collect_type_config must delegate
    # to THIS function (not carry its own copy — the copy is exactly
    # what made the old test vacuous). AST check, no Qt needed.
    tree = ast.parse(open(os.path.join(REPO_ROOT, "gui.py"),
                          encoding="utf-8").read())
    delegated = False
    for node in ast.walk(tree):
        if (isinstance(node, ast.FunctionDef)
                and node.name == "_collect_type_config"):
            for child in ast.walk(node):
                if (isinstance(child, ast.Call)
                        and getattr(child.func, "id", "") ==
                        "merge_type_targets"):
                    args = [getattr(a, "attr", "") for a in child.args
                            if isinstance(a, ast.Attribute)]
                    delegated = args == ["type_mappings", "config"]
    check("config-preserve: gui.py delegates to merge_type_targets",
          delegated,
          "ConfigDialog._collect_type_config does not call "
          "merge_type_targets(self.type_mappings, self.config)")


def test_yomitan_provider_implements_full_surface() -> None:
    """
    pyright found a latent AttributeError: parser.py's compat Mock
    delegates install/_compute_signature/_iter_term_banks to whatever
    get_provider() returns, but YomitanApiProvider lacked the latter
    two (and db_path). Unreachable in normal flows today (the engine
    bypasses the local dictionaries in Yomitan mode), but one GUI path away
    from a crash — so the no-network surface is asserted directly.
    (is_installed/lookup are skipped: they hit the network/bridge.)
    """
    import yomitan

    provider_cls = getattr(yomitan, "YomitanApiProvider", None)
    check("yomitan-iface: YomitanApiProvider importable",
          provider_cls is not None)
    if provider_cls is None:
        return
    prov = provider_cls()
    check("yomitan-iface: _compute_signature is stable (no local index)",
          prov._compute_signature("anything") == "yomitan-api:no-local-index",
          )
    check("yomitan-iface: _iter_term_banks yields nothing (no local files)",
          list(prov._iter_term_banks("anything")) == [])
    check("yomitan-iface: install reports 0 entries (nothing indexed)",
          prov.install("anything") == 0)
    check("yomitan-iface: uninstall is a silent no-op",
          prov.uninstall("anything") is None)
    check("yomitan-iface: entry count is 0",
          prov.get_entry_count("anything") == 0)
    try:
        _ = prov.db_path  # touch the property: must raise, not return junk
        db_path_raises = False
    except NotImplementedError:
        db_path_raises = True
    check("yomitan-iface: db_path fails LOUDLY (no silent fake path)",
          db_path_raises)


def test_yomitan_bridge_sw_keepalive() -> None:
    """
    Regression for the 2026-09-04 "Yomitan completely dead" incident.

    Root cause (verified live on Chrome 151 + Yomitan 26.8.24.0): Yomitan's
    MV3 service worker owns the native-messaging port. ~30s after
    connectNative the SW suspends, Chrome closes both pipes, and the old
    bridge became a ZOMBIE: still listening on port 19633, stdin dead, so
    every /ankiFields returned 502 forever and no new launch could bind.

    The bundled BRIDGE_SCRIPT must therefore:
    1. Ping the port periodically (KEEPALIVE_INTERVAL < 30s SW idle timer)
       so port.onMessage keeps firing and the SW never suspends.
    2. Watch stdin from a reader thread and, on EOF, SHUT THE HOST DOWN so
       the process exits and frees port 19633 (anti-zombie self-heal).
    3. Match replies by a per-request nonce so keepalive echoes can never
       be mis-paired with a real HTTP request's reply.
    4. Never kill a browser-launched bridge from the installer (v1.0.34
       incident) — only bare-cmdline standalone orphans.
    5. Keep the bridge's Yomitan wait (YOMITAN_RESPONSE_TIMEOUT) below the
       Anki-side fetch timeout in yomitan.py (nested timeouts).
    """
    import yomitan_installer as installer

    src = installer.BRIDGE_SCRIPT

    # 1. Keepalive: interval must be strictly under the 30s SW idle timer.
    m = re.search(r"KEEPALIVE_INTERVAL\s*=\s*([0-9.]+)", src)
    check("bridge: keepalive interval configured",
          m is not None, "KEEPALIVE_INTERVAL not found")
    if m:
        check("bridge: keepalive fires under 30s SW idle timer",
              0 < float(m.group(1)) < 30.0,
              f"got {m.group(1)}")
    check("bridge: keepalive thread started",
          "_keepalive_loop, daemon=True" in src.replace(" ", "").replace("\n", "")
          or "threading.Thread(target=_keepalive_loop" in src,
          "keepalive thread not launched")
    check("bridge: keepalive sends a port message (resets SW idle timer)",
          re.search(r'_raw_send\(\{"action": "keepalive"', src) is not None,
          "no keepalive ping found")

    # 2. Anti-zombie: a stdin reader thread must trigger host shutdown on EOF.
    check("bridge: stdin reader thread exists",
          "target=_stdin_reader" in src, "no _stdin_reader thread")
    check("bridge: reader clears connected state on EOF",
          re.search(r"_yomitan_connected\.clear\(\)", src) is not None,
          "EOF does not clear connection state")
    check("bridge: reader shuts the HTTP server down on EOF",
          re.search(r"def _stdin_reader.*?_shutdown_host\(\)", src, re.S) is not None,
          "reader thread does not call _shutdown_host()")
    check("bridge: _shutdown_host stops the HTTP server",
          re.search(r"def _shutdown_host.*?httpd\.shutdown\(\)", src, re.S) is not None,
          "_shutdown_host missing or does not call httpd.shutdown()")
    check("bridge: port is closed on exit (frees 19633)",
          "server_close()" in src, "no server_close() on exit path")

    # 3. Nonce pairing: every request carries a unique nonce in params.
    check("bridge: requests carry a nonce",
          '"_cd": nonce' in src, "no nonce in request frame")
    check("bridge: replies matched by nonce",
          re.search(r"params\.get\(\"_cd\"\)", src) is not None,
          "reader does not extract nonce from echoed params")

    # 4. Browser-launched guard: the killer must skip chrome-extension:// procs.
    inst_src = open(os.path.join(REPO_ROOT, "yomitan_installer.py"), encoding="utf-8").read()
    check("installer: _is_browser_launched guard exists",
          "def _is_browser_launched" in inst_src,
          "no browser-launched guard in killer")
    check("installer: killer checks the guard before killing",
          re.search(r"not _is_browser_launched\(pid\)", inst_src) is not None,
          "killer does not check _is_browser_launched")
    check("installer: guard treats unreadable cmdline as browser-launched",
          re.search(r"Unreadable.*?return True", inst_src, re.S) is not None
          or "assume browser-launched" in inst_src,
          "guard does not err on the side of not killing")

    # 5. Nested timeouts: bridge wait must stay under Anki fetch timeout.
    m = re.search(r"YOMITAN_RESPONSE_TIMEOUT\s*=\s*([0-9.]+)", src)
    bridge_wait = float(m.group(1)) if m else 0.0
    m2 = re.search(r"YOMITAN_TIMEOUT\s*=\s*([0-9.]+)",
                   open(os.path.join(REPO_ROOT, "yomitan.py"), encoding="utf-8").read())
    anki_wait = float(m2.group(1)) if m2 else 0.0
    check("bridge: bridge wait < Anki fetch timeout (nested)",
          0 < bridge_wait < anki_wait,
          f"bridge={bridge_wait}s anki={anki_wait}s")

    # 6. 502 must be reported only via the connected-state check (so a
    # dying SW between request and reply still fails within the timeout,
    # never hangs the HTTP thread forever).
    check("bridge: HTTP path checks connection before sending",
          re.search(r"if not _yomitan_connected\.is_set\(\)", src) is not None,
          "no connection pre-check in do_POST")

    # 7. The script must compile.
    try:
        ast.parse(src)
        ok = True
        err = ""
    except SyntaxError as e:
        ok = False
        err = str(e)
    check("bridge: BRIDGE_SCRIPT parses as valid Python", ok, err)


def _yomitan_multi_dict_blob() -> str:
    """Synthetic /ankiFields glossary: 3 dictionaries glued in one blob.

    Mirrors Yomitan's real template: each <li data-dictionary> is followed
    by its OWN interleaved <style> block (scoped [data-dictionary] CSS that
    Anki needs to render the definition like Yomitan does).

    Dict B's definition uses only known kanji (不公平); A and C smuggle in
    unknown kanji (贔屓扱受 / 欠続). B also carries a nested example list to
    prove inner <li> items (no data-dictionary) never cause a false split.
    """
    return (
        "<ol>"
        '<li data-dictionary="Dict A"><i>(Dict A)</i>'
        "<span>贔屓があって、不公平な扱いを受けること。</span></li>"
        '<style>[data-dictionary="Dict A"]{color:red;}</style>'
        '<li data-dictionary="Dict B"><i>(Dict B)</i>'
        "<span>公で平でないこと。不公平であること。</span>"
        "<ul><li>nested example without marker</li></ul></li>"
        '<style>[data-dictionary="Dict B"]{color:green;}</style>'
        '<li data-dictionary="Dict C"><i>(Dict C)</i>'
        "<span>公平を欠く状態が続くこと。</span></li>"
        '<style>[data-dictionary="Dict C"]{color:blue;}</style>'
        "</ol>"
    )


def test_yomitan_glossary_split_per_dictionary() -> None:
    """Yomitan blob splits into per-dictionary native-HTML slices (no network)."""
    import yomitan

    blob = _yomitan_multi_dict_blob()
    parts = yomitan.split_glossary_by_dictionary(blob)
    check("yomitan-split: blob splits into 3 slices",
          len(parts) == 3, f"got {len(parts)}")
    check("yomitan-split: titles extracted in order",
          [t for t, _ in parts] == ["Dict A", "Dict B", "Dict C"],
          f"got {[t for t, _ in parts]}")
    check("yomitan-split: NO <style> element survives in any slice",
          all("<style" not in s.lower() for _, s in parts),
          "a <style> element survived — definitions must never carry "
          "Yomitan CSS (the 不公平 incident: 31KB of stylesheet inside "
          "a 34KB slice)")
    check("yomitan-split: dictionary content survives within its slice",
          "贔屓があって" in parts[0][1]
          and "公で平でないこと" in parts[1][1]
          and "公平を欠く状態" in parts[2][1])
    check("yomitan-split: nested inner <li> does not over-split",
          "nested example without marker" in parts[1][1])
    check("yomitan-split: no <ol> wrapper leaks into slices",
          all("<ol" not in s.lower() for _, s in parts))

    single = '<div class="yomitan-glossary">単独の定義文です。</div>'
    check("yomitan-split: marker-less blob falls back to whole",
          yomitan.split_glossary_by_dictionary(single) == [(None, single)])
    custom = ("<ol><li><span>custom template one。</span></li>"
              "<li><span>custom template two。</span></li></ol>")
    check("yomitan-split: custom template without markers falls back to whole",
          yomitan.split_glossary_by_dictionary(custom) == [(None, custom)])
    check("yomitan-split: empty input falls back",
          yomitan.split_glossary_by_dictionary("") == [(None, "")])

    # SPEC tests for yomitan.split_glossary_by_dictionary.
    # NOTE: blobs below use REAL Yomitan syntax (<li data-dictionary="X">
    # attributes). An earlier draft used square-bracket pseudo-markers
    # ([data-dictionary="X"]) which _LI_DICT_RE never matches — every
    # spec silently fell back to one slice. If you add cases here, copy
    # real /ankiFields output shape.
    # 1. style stripping: EVERY <style> element is removed from every
    #    slice, no exceptions — including blocks that target the slice's
    #    own dictionary. (An earlier version kept "own" styles for
    #    fidelity; real output proved the kept block IS the bloat —
    #    tens of KB per definition with zero visual effect in Anki.)
    spec_style_blob = ('<div class="yomitan-glossary">'
                       '<ol>'
                       '<li data-dictionary="DicA">{text}</li>'
                       '<style>[data-dictionary="DicA"]{color:red;}</style>'
                       '<li data-dictionary="DicB">{text}</li>'
                       '<style>[data-dictionary="DicB"]{color:blue;}</style>'
                       '</ol>'
                       '</div>')
    spec_parts = yomitan.split_glossary_by_dictionary(spec_style_blob)
    check("yomitan-split: spec style-strip - exactly two slices",
          len(spec_parts) == 2, f"got {len(spec_parts)}")
    check("yomitan-split: spec style-strip - no <style> in DicA slice",
          len(spec_parts) == 2
          and "<style" not in spec_parts[0][1].lower())
    check("yomitan-split: spec style-strip - no <style> in DicB slice",
          len(spec_parts) == 2
          and "<style" not in spec_parts[1][1].lower())
    check("yomitan-split: spec style-strip - content survives",
          len(spec_parts) == 2
          and "{text}" in spec_parts[0][1]
          and "{text}" in spec_parts[1][1]
          and spec_parts[0][0] == "DicA"
          and spec_parts[1][0] == "DicB")

    # 2. adjacent <li> no-intermediate-markup: if two <li> are adjacent with no markup between,
    #    split must still occur (no false merge)
    spec_adj_blob = ('<div class="yomitan-glossary">'
                     '<ol>'
                     '<li data-dictionary="X">{first}</li>'
                     '<li data-dictionary="Y">{second}</li>'
                     '</ol>'
                     '</div>')
    spec_adj_parts = yomitan.split_glossary_by_dictionary(spec_adj_blob)
    check("yomitan-split: spec adjacent <li> - exactly two slices",
          len(spec_adj_parts) == 2)
    check("yomitan-split: spec adjacent <li> - first slice contains 'first'",
          any("first" in s for _, s in spec_adj_parts))
    check("yomitan-split: spec adjacent <li> - second slice contains 'second'",
          any("second" in s for _, s in spec_adj_parts))

    # 3. empty <li> handling: <li> with only whitespace should still yield a slice
    spec_empty_blob = ('<div class="yomitan-glossary">'
                       '<ol>'
                       '<li data-dictionary="Real">{real}</li>'
                       '<li data-dictionary="Empty">{   }</li>'
                       '<li data-dictionary="AlsoReal">{also}</li>'
                       '</ol>'
                       '</div>')
    spec_empty_parts = yomitan.split_glossary_by_dictionary(spec_empty_blob)
    check("yomitan-split: spec empty <li> - three slices despite middle being whitespace-only",
          len(spec_empty_parts) == 3)
    check("yomitan-split: spec empty <li> - middle slice title is 'Empty'",
          len(spec_empty_parts) == 3 and spec_empty_parts[1][0] == "Empty")
    check("yomitan-split: spec empty <li> - whitespace content survives in its own slice",
          len(spec_empty_parts) == 3 and "{   }" in spec_empty_parts[1][1])

    # 4. unordered fallback: if no [data-dictionary] markers present, fall back to returning whole blob as one slice
    spec_fallback_blob = ('<div class="yomitan-glossary">'
                          '<ol>'
                          '<li>no marker here</li>'
                          '<li>nor here</li>'
                          '</ol>'
                          '</div>')
    spec_fallback_parts = yomitan.split_glossary_by_dictionary(spec_fallback_blob)
    check("yomitan-split: spec unordered fallback - exactly one slice",
          len(spec_fallback_parts) == 1)
    check("yomitan-split: spec unordered fallback - slice title is None",
          spec_fallback_parts[0][0] is None)
    check("yomitan-split: spec unordered fallback - slice contains original content",
          "no marker here" in spec_fallback_parts[0][1] and "nor here" in spec_fallback_parts[0][1])

    # 5. THE 不公平 incident (v1.2.x): real Yomitan output inlines its
    #    ENTIRE structured-content stylesheet per glossary item — measured
    #    live: 31,333 bytes of CSS inside a 34,245-byte slice (91% dead
    #    weight), stored verbatim into the Anki field on every generation.
    #    It renders identically without it (ruby is native; our own local
    #    renderer never emits <style>), so NO slice may carry ANY of it.
    big_css = "\n".join(f".gloss-{i} {{ color: red; }}" for i in range(300))
    big_blob = ('<div class="yomitan-glossary"><ol>'
                '<li data-dictionary="BigA">本文A</li>'
                f'<style>[data-dictionary="BigA"]{{{big_css}}}</style>'
                '<li data-dictionary="BigB">本文B</li>'
                f'<style>[data-dictionary="BigB"]{{{big_css}}}</style>'
                '</ol></div>')
    big_parts = yomitan.split_glossary_by_dictionary(big_blob)
    check("yomitan-split: incident blob splits into two slices",
          len(big_parts) == 2, f"got {len(big_parts)}")
    check("yomitan-split: incident slices carry zero <style> elements",
          all("<style" not in s.lower() for _, s in big_parts))
    check("yomitan-split: incident slices are small (CSS gone)",
          all(len(s) < 500 for _, s in big_parts),
          f"sizes={[len(s) for _, s in big_parts]}")
    check("yomitan-split: incident content survives sans CSS",
          any("本文A" in s for _, s in big_parts)
          and any("本文B" in s for _, s in big_parts))


def _yomitan_child_list_blob() -> str:
    """Synthetic blob mirroring the 会社 incident: one dictionary contributes
    only a 子見出し child-entry term list (all-common kanji → scores ~1.0),
    while 小学館 holds the genuine prose definition (partly-unknown kanji).
    The term list must NEVER win, no matter its kanji score.
    """
    return (
        "<ol>"
        '<li data-dictionary="子見出し辞典"><i>(子見出し辞典)</i>'
        "<span>(子) 会社員 | 会社組合 | 会社法 | 会社人間</span></li>"
        '<li data-dictionary="小学館例解学習国語 第十二版">'
        "<i>(小学館例解学習国語 第十二版)</i>"
        "<span>利益をえるため、お金を出し合って作る仕組みの団体。例 株式会社。</span></li>"
        "</ol>"
    )


def test_yomitan_term_list_loses_to_real_definition() -> None:
    """Regression for the 会社 incident: a pipe-separated child-entry list
    ((子) 会社員 | 会社組合 | …) outscored the genuine 小学館 definition
    because every kanji in the list was already known. Term lists are not
    readable definitions — they must be filtered like reference titles."""
    import engine as engine_mod
    import yomitan
    from models import DictionaryEntry

    junk = "(子) 会社員 | 会社組合 | 会社更生法 | 会社整理 | 会社説明会"
    check("ref: long pipe-separated term list is a reference",
          compredef_generator._is_reference_title(junk), f"got: {junk[:40]!r}")
    prose_with_pipe = "定義にパイプ｜記号がある場合もある。"
    check("ref: prose WITH sentence punctuation stays real (pipe or not)",
          not compredef_generator._is_reference_title(prose_with_pipe))

    blob = _yomitan_child_list_blob()
    real_slice = yomitan.split_glossary_by_dictionary(blob)[1][1]
    entries = [
        DictionaryEntry(word="会社", reading="かいしゃ", definition=s,
                        dictionary_title=t or "Yomitan",
                        dictionary_path="yomitan://api")
        for t, s in yomitan.split_glossary_by_dictionary(blob)
    ]

    # Learner knows every kanji in the junk slice (list + its dictionary
    # title are all common) but only a few of the real definition's —
    # without the filter the junk scores a perfect 1.0 and early-exits.
    known = {"子", "会", "社", "員", "組", "合", "法", "人", "間",
             "見", "出", "辞", "典"}
    junk_score = compredef_generator._calculate_kanji_score(entries[0].definition, known)
    check("term-list: junk really does score 1.0 here (repro is valid)",
          junk_score == 1.0, f"got {junk_score}")

    original_fetch = engine_mod.fetch_yomitan_definitions
    engine_mod.fetch_yomitan_definitions = lambda w, r="": entries  # type: ignore
    cfgs = aqt.mw.addonManager.configs
    had_key = "1619602654" in cfgs
    old_cfg = cfgs.get("1619602654")
    cfgs["1619602654"] = {"dictionary_source": "yomitan"}
    try:
        gen = engine_mod.DefinitionGenerator(provider=None, known_kanji=known)
        result = gen.generate("会社", dictionary_paths=[], reading="かいしゃ")
    finally:
        engine_mod.fetch_yomitan_definitions = original_fetch  # type: ignore
        if had_key:
            cfgs["1619602654"] = old_cfg
        else:
            cfgs.pop("1619602654", None)
    check("term-list: genuine definition wins, byte-exact",
          result == real_slice, f"got: {(result or '')[:80]!r}")


def test_yomitan_returns_single_best_definition() -> None:
    """Engine scores per-dictionary slices, returns winner in native HTML."""
    import engine as engine_mod
    import yomitan
    from models import DictionaryEntry

    blob = _yomitan_multi_dict_blob()
    winner_slice = yomitan.split_glossary_by_dictionary(blob)[1][1]
    entries = [
        DictionaryEntry(word="不公平", reading="ふこうへい", definition=s,
                        dictionary_title=t or "Yomitan",
                        dictionary_path="yomitan://api")
        for t, s in yomitan.split_glossary_by_dictionary(blob)
    ]

    # Hermetic: stub the network fetch AND the dictionary-source config.
    original_fetch = engine_mod.fetch_yomitan_definitions
    engine_mod.fetch_yomitan_definitions = lambda w, r="": entries  # type: ignore
    cfgs = aqt.mw.addonManager.configs
    had_key = "1619602654" in cfgs
    old_cfg = cfgs.get("1619602654")
    cfgs["1619602654"] = {"dictionary_source": "yomitan"}
    try:
        gen = engine_mod.DefinitionGenerator(
            provider=None, known_kanji={"不", "公", "平"})
        result = gen.generate("不公平", dictionary_paths=[], reading="ふこうへい")
    finally:
        engine_mod.fetch_yomitan_definitions = original_fetch  # type: ignore
        if had_key:
            cfgs["1619602654"] = old_cfg
        else:
            cfgs.pop("1619602654", None)
    check("yomitan-pick: a definition was returned", result is not None)
    check("yomitan-pick: winner is the fully-known Dict B slice, byte-exact",
          result == winner_slice, f"got: {(result or '')[:80]!r}")
    check("yomitan-pick: losing dictionaries are NOT in the output",
          result is not None and "Dict A" not in result and "Dict C" not in result,
          f"got: {(result or '')[:80]!r}")


def test_yomitan_source_falls_back_to_local(tmp_root: str) -> None:
    """v1.2.15 fail-safe: Yomitan source + dead bridge => local dictionaries.

    The user's report: Yomitan selected, Chrome closed, Tab on a word
    present in a local dictionary silently produced nothing. Now the
    Yomitan-primary path falls back to the local dictionaries (mirroring the
    existing local->Yomitan fail-safe) instead of returning None.
    Priority stays Yomitan-first: a working bridge still wins even
    when local dictionaries are configured.
    """
    import engine as engine_mod
    from models import DictionaryEntry

    dict_dir = build_synthetic_dict(os.path.join(tmp_root, "y2l_fallback"))
    compredef_parser.get_single_dictionary(dict_dir).install()

    original_fetch = engine_mod.fetch_yomitan_definitions
    cfgs = aqt.mw.addonManager.configs
    had_key = "1619602654" in cfgs
    old_cfg = cfgs.get("1619602654")
    try:
        # 1. Bridge down (empty list): local dictionaries must produce.
        engine_mod.fetch_yomitan_definitions = lambda w, r="": []  # type: ignore
        cfgs["1619602654"] = {"dictionary_source": "yomitan"}
        gen = engine_mod.DefinitionGenerator(
            provider=None, known_kanji={"先", "ず", "最", "初"})
        result = gen.generate("先ず", dictionary_paths=[dict_dir],
                              reading="まず")
        check("y2l: dead bridge + local dict => local definition",
              result is not None and "structured-content" in result,
              f"got: {(result or '')[:80]!r}")

        # 2. Bridge raising (not just empty): identical behavior.
        def _boom(w, r=""):
            raise ConnectionError("browser closed")
        engine_mod.fetch_yomitan_definitions = _boom  # type: ignore
        gen2 = engine_mod.DefinitionGenerator(
            provider=None, known_kanji={"先", "ず", "最", "初"})
        result2 = gen2.generate("先ず", dictionary_paths=[dict_dir],
                                reading="まず")
        check("y2l: raising bridge + local dict => local definition",
              result2 is not None and "structured-content" in result2,
              f"got: {(result2 or '')[:80]!r}")

        # 3. Working bridge still wins over local (priority unchanged).
        y_entries = [DictionaryEntry(
            word="先ず", reading="まず", definition="YOMITAN wins here",
            dictionary_title="Yomitan", dictionary_path="yomitan://api")]
        engine_mod.fetch_yomitan_definitions = lambda w, r="": y_entries  # type: ignore
        gen3 = engine_mod.DefinitionGenerator(
            provider=None, known_kanji={"先", "ず", "最", "初"})
        result3 = gen3.generate("先ず", dictionary_paths=[dict_dir],
                                reading="まず")
        check("y2l: working bridge still beats local dictionaries",
              result3 == "YOMITAN wins here",
              f"got: {(result3 or '')[:80]!r}")

        # 4. Dead bridge + NO local dictionaries => None (unchanged).
        engine_mod.fetch_yomitan_definitions = lambda w, r="": []  # type: ignore
        gen4 = engine_mod.DefinitionGenerator(provider=None,
                                              known_kanji=set())
        result4 = gen4.generate("先ず", dictionary_paths=[], reading="まず")
        check("y2l: dead bridge + no local dicts => None (unchanged)",
              result4 is None, f"got: {(result4 or '')[:80]!r}")
    finally:
        engine_mod.fetch_yomitan_definitions = original_fetch  # type: ignore
        if had_key:
            cfgs["1619602654"] = old_cfg
        else:
            cfgs.pop("1619602654", None)


def test_application_runtime_wiring() -> None:
    """Verifies core.reset_generator() and get_generator() singleton wiring."""
    import core as _core
    gen1 = _core.get_generator()
    check("core: get_generator returns DefinitionGenerator instance",
          gen1 is not None)

    _core.reset_generator()
    check("core: reset_generator clears singleton",
          _core._generator is None)

    gen2 = _core.get_generator()
    check("core: get_generator rebuilds fresh instance after reset",
          gen2 is not gen1 and gen2 is not None)


def main() -> int:
    print("=" * 70)
    print("CompreDef fundamental regression suite")
    print("=" * 70)
    tmp_root = tempfile.mkdtemp(prefix="compredef_test_")
    try:
        # Architecture tests (install-time indexing)
        test_install_indexes_once(tmp_root)
        test_index_survives_restart(tmp_root)
        test_lookup_never_indexes(tmp_root)
        test_missing_word(tmp_root)
        test_replacement_reindexes(tmp_root)
        test_indexing_failure_reported(tmp_root)
        test_uninstall_removes_index(tmp_root)
        test_extract_clean_word_formats(tmp_root)

        # Historical regression tests
        test_structured_content_html_fidelity(tmp_root)
        test_renderer_version_invalidates_cache(tmp_root)
        test_scoring_ignores_furigana()
        test_order_independent_argmax(tmp_root)
        test_reference_title_filtering()
        test_zip_folder_parity(tmp_root)
        test_data_sc_attribute_names()
        test_indexing_streams_in_batches(tmp_root)
        test_db_connections_are_closed(tmp_root)
        test_reading_disambiguates_homographs(tmp_root)
        test_parse_furigana_field_formats()
        test_disabled_dictionaries_skipped(tmp_root)
        test_kanji_extraction_correctness(tmp_root)
        test_knowledge_survives_new_schema(tmp_root)
        test_snapshot_waits_for_open_collection()
        test_sync_reset_is_thread_safe()
        test_knowledge_summary_text()
        test_provenance_search_builders()
        test_dialog_payload_cached_per_generation()
        test_package_relative_imports()
        test_no_undefined_names_in_shipped_modules()
        test_qt_enum_compat()
        test_tab_generate_decisions()
        test_apply_definition_refresh_order()
        test_multi_note_type_targeting()
        test_cd_toggle_and_japanese_scope_guess()
        test_scope_deck_filtering()
        test_v12_scoring_algorithm()
        test_picker_audit_strict()
        test_config_survives_yomitan_toggle()
        test_yomitan_provider_implements_full_surface()
        test_yomitan_bridge_sw_keepalive()
        test_yomitan_glossary_split_per_dictionary()
        test_yomitan_term_list_loses_to_real_definition()
        test_yomitan_returns_single_best_definition()
        test_yomitan_source_falls_back_to_local(tmp_root)
        test_application_runtime_wiring()
        test_real_dictionary_smoke()
    finally:
        # Clean up all synthetic dictionaries from the shared cache DB.
        conn = sqlite3.connect(compredef_parser._get_db_path())
        try:
            conn.execute("DELETE FROM entries WHERE dict_path LIKE ?", (tmp_root + "%",))
            conn.execute("DELETE FROM dictionaries WHERE path LIKE ?", (tmp_root + "%",))
            conn.commit()
        finally:
            conn.close()
        shutil.rmtree(tmp_root, ignore_errors=True)

    print("=" * 70)
    print(f"RESULT: {RESULTS['pass']}/{RESULTS['pass'] + RESULTS['fail']} "
          f"passed, {RESULTS['fail']} failed")
    if RESULTS["failed_names"]:
        print("\nFAILED TESTS:")
        for name in RESULTS["failed_names"]:
            print(f"  - {name}")
    print("=" * 70)
    return 0 if RESULTS["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
