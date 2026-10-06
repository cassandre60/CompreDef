#!/usr/bin/env python3
"""
tests/test_units.py — Ring 0: isolated unit tests for every PURE
building block in the codebase.

Each test calls ONE function with plain parameters (strings, dicts,
lists, tmp files) — no Anki, no collection, no database, no Qt, no
network. If a function passes here, it is safe to use as a component
inside larger code. Run it directly:

    python3 tests/test_units.py

Exit code 0 = all green, 1 = failure (names printed). Runs in
milliseconds; runs BEFORE tests/test_regression.py in the pipeline.

Coverage contract (no duplication with the regression suite — each
pure function is pinned in exactly ONE place):
- COVERED HERE: every pure function with NO isolated coverage in
  test_regression.py (see each test's docstring for the gap it fills).
  rank_definitions key edges live here; its end-to-end agreement with
  engine._pick_best on real captured data lives there
  (test_picker_audit_strict).
- COVERED THERE (referenced, not repeated): extract_base_text,
  extract_clean_word, parse_furigana_field, merge_type_targets
  (utils); is_reference_title, extract_kanji_words, score_definition
  (scoring); _pick_best (engine); split_glossary_by_dictionary,
  strip_style_blocks (yomitan); _should_auto_generate and friends
  (editor_browser, via FakeNote stand-ins).
- NOT UNIT-TESTABLE (need Anki/DB/Qt/network by design): gui.py,
  __init__.py, core.py singletons, provider.py SQLite paths,
  generator.py thin wrapper, db_utils.py re-export shim,
  anki.py snapshot builders, scope.py col-backed paths,
  yomitan.py bridge/network paths, editor_browser.py UI paths.
"""
import dataclasses
import os
import sys
import types
import zipfile

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO_ROOT)

# anki.py does `from aqt import mw` at module top; everything else
# under test here is stdlib-only. A minimal stub keeps Ring 0 free of
# any Anki install (mw is never touched — these are pure functions).
sys.modules.setdefault("aqt", types.SimpleNamespace(mw=None))

import anki as compredef_anki
import engine as compredef_engine
import models as compredef_models
import picker as compredef_picker
import renderer as compredef_renderer
import scope as compredef_scope
import scoring as compredef_scoring
import utils as compredef_utils
import yomitan as compredef_yomitan

RESULTS = {"pass": 0, "fail": 0, "failed_names": []}


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
# utils.py — text helpers with no isolated coverage yet
# ---------------------------------------------------------------------------

def test_normalize_reading() -> None:
    """utils.normalize_reading: katakana->hiragana + strip decorations.

    Gap: only covered indirectly through parse_furigana_field matrices;
    this pins the primitive itself (yomitan.py carries a mirror copy —
    see test_yomitan_normalize_reading; both must agree).
    """
    n = compredef_utils.normalize_reading
    check("unit: katakana maps to hiragana", n("マズ") == "まず")
    check("unit: mixed kana normalizes", n("せん-ず") == "せんず")
    check("unit: decorations stripped", n("ま ず") == "まず")
    check("unit: empty stays empty", n("") == "")
    check("unit: kanji passes through", n("行く") == "行く")


def test_extract_base_text_edges() -> None:
    """utils.extract_base_text: degenerate inputs (gap: only HTML blobs
    are covered in the regression suite)."""
    b = compredef_utils.extract_base_text
    check("unit: None -> empty", b(None) == "")
    check("unit: empty -> empty", b("") == "")
    check("unit: entities unescaped", b("&lt;食&gt;") == "<食>")


def test_resolve_dictionary_paths() -> None:
    """utils.resolve_dictionary_paths: pure list shaping (gap: only covered
    implicitly inside full generation runs)."""
    r = compredef_utils.resolve_dictionary_paths
    check("unit: all-empty -> no dictionaries", r(None, "", None) == [])
    check("unit: blanks stripped, order kept",
          r(["/b ", "", "  ", "/a"], "", None) == ["/b", "/a"])
    check("unit: non-list dictionaries ignored",
          r("notalist", "", None) == [])


def test_resolve_dictionary_paths_disabled(tmp_root: str) -> None:
    """Disabled-path filtering uses realpath comparison (gap: the
    disabled-dictionaries branch has no isolated test)."""
    keep = os.path.join(tmp_root, "keep.txt")
    drop = os.path.join(tmp_root, "drop.txt")
    for p in (keep, drop):
        with open(p, "w") as f:
            f.write("x")
    got = compredef_utils.resolve_dictionary_paths([keep, drop], "", [drop])
    check("unit: disabled path filtered out", got == [keep], f"got {got}")
    got2 = compredef_utils.resolve_dictionary_paths([keep], "", [drop])
    check("unit: unrelated disabled entry keeps the set",
          got2 == [keep], f"got {got2}")


def test_is_zip_and_directory_dictionary(tmp_root: str) -> None:
    """utils.is_zip/is_directory_dictionary: filesystem predicates
    (gap: only exercised incidentally via full installs)."""
    zpath = os.path.join(tmp_root, "d.zip")
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("index.json", '{"title": "T", "format": 3}')
    check("unit: zip with index.json detected",
          compredef_utils.is_zip_dictionary(zpath) is True)
    notzip = os.path.join(tmp_root, "plain.txt")
    with open(notzip, "w") as f:
        f.write("hi")
    check("unit: plain file is not a zip dictionary",
          compredef_utils.is_zip_dictionary(notzip) is False)
    check("unit: missing path is not a zip dictionary",
          compredef_utils.is_zip_dictionary(
              os.path.join(tmp_root, "nope.zip")) is False)
    ddir = os.path.join(tmp_root, "ddir")
    os.makedirs(ddir)
    with open(os.path.join(ddir, "index.json"), "w") as f:
        f.write("{}")
    check("unit: folder with index.json detected",
          compredef_utils.is_directory_dictionary(ddir) is True)
    emptydir = os.path.join(tmp_root, "empty")
    os.makedirs(emptydir)
    check("unit: empty folder is not a dictionary",
          compredef_utils.is_directory_dictionary(emptydir) is False)
    check("unit: missing path is not a dictionary",
          compredef_utils.is_directory_dictionary(
              os.path.join(tmp_root, "nope")) is False)


def test_find_dictionary_folders(tmp_root: str) -> None:
    """utils.find_dictionary_folders: discovery + title dedup (gap:
    only covered implicitly via folder-based generation)."""
    f = compredef_utils.find_dictionary_folders
    d1 = os.path.join(tmp_root, "dictA")
    os.makedirs(d1)
    with open(os.path.join(d1, "index.json"), "w") as f1:
        f1.write("{}")
    check("unit: single folder resolves to itself",
          f(d1) == [os.path.realpath(d1)], f"got {f(d1)}")
    # Parent scan finds direct children only (folders + zips, no
    # recursion — one level, like Anki's own folder picker).
    parent = os.path.join(tmp_root, "parent")
    for sub in ("dictA", "dictB"):
        os.makedirs(os.path.join(parent, sub))
        with open(os.path.join(parent, sub, "index.json"), "w") as fh:
            fh.write("{}")
    zpath = os.path.join(parent, "dictC.zip")
    with zipfile.ZipFile(zpath, "w") as z:
        z.writestr("index.json", "{}")
    found = f(parent)
    check("unit: parent scan finds folder dicts and zips",
          len(found) == 3, f"got {found}")
    # Nested grandchildren are NOT scanned (single level by design).
    os.makedirs(os.path.join(parent, "dictA", "nested"))
    with open(os.path.join(parent, "dictA", "nested",
                           "index.json"), "w") as fh:
        fh.write("{}")
    check("unit: scan stays one level deep",
          len(f(parent)) == 3, f"got {f(parent)}")
    check("unit: missing path finds nothing",
          f(os.path.join(tmp_root, "nope")) == [])


# ---------------------------------------------------------------------------
# scoring.py — direct scorer edges
# ---------------------------------------------------------------------------

def test_calculate_kanji_score_edges() -> None:
    """scoring.calculate_kanji_score: boundary contracts (gap: only
    covered through the generator wrapper with fixed fixtures)."""
    s = compredef_scoring.calculate_kanji_score
    check("unit: empty text scores 1.0 (vacuous)",
          s("", set()).score == 1.0)
    check("unit: kana-only text scores 1.0 (nothing to know)",
          s("ひらがな", set()).score == 1.0)
    check("unit: all-known scores 1.0",
          s("漢字", {"漢", "字"}).score == 1.0)
    check("unit: none-known scores 0.0",
          s("漢字", set()).score == 0.0)
    check("unit: half-known scores 0.5",
          s("漢字", {"漢"}).score == 0.5)


# ---------------------------------------------------------------------------
# scope.py — pure config/name helpers (no collection needed)
# ---------------------------------------------------------------------------

def test_get_scope_decks() -> None:
    """scope.get_scope_decks: config parsing (gap: only covered via
    integration through note_in_scope)."""
    g = compredef_scope.get_scope_decks
    check("unit: None config -> []", g(None) == [])
    check("unit: non-dict config -> []", g("x") == [])
    check("unit: missing key -> []", g({}) == [])
    check("unit: non-list value -> []", g({"scope_decks": "A"}) == [])
    check("unit: dedups + strips, keeps order",
          g({"scope_decks": ["B ", "A", "B", "", "  "]}) == ["B", "A"])
    check("unit: empty list -> []", g({"scope_decks": []}) == [])


def test_expand_scope_names() -> None:
    """scope.expand_scope_names: subdeck expansion (gap: only covered
    via integration; the AB-vs-A::B prefix guard deserves pinning)."""
    e = compredef_scope.expand_scope_names
    check("unit: exact + children expand",
          e(["A", "A::B", "AB"], ["A"]) == {"A", "A::B"})
    check("unit: prefix lookalike AB excluded",
          "AB" not in e(["A", "A::B", "AB"], ["A"]))
    check("unit: missing deck matches nothing",
          e(["A"], ["Klingon"]) == set())
    check("unit: empty scope expands to nothing",
          e(["A", "B"], []) == set())


def test_missing_scope_decks() -> None:
    """scope.missing_scope_decks: renamed/deleted detection (gap: only
    one integration case exists)."""
    m = compredef_scope.missing_scope_decks
    check("unit: reports only unmatched, in scope order",
          m(["A"], ["A", "B", "C"]) == ["B", "C"])
    check("unit: all matched -> []", m(["A"], ["A"]) == [])


def test_is_scope_empty() -> None:
    """scope.is_scope_empty: fail-closed predicate (gap: no direct
    test)."""
    ie = compredef_scope.is_scope_empty
    check("unit: empty config is empty scope",
          ie({}) is True)
    check("unit: empty list is empty scope",
          ie({"scope_decks": []}) is True)
    check("unit: one deck is not empty",
          ie({"scope_decks": ["A"]}) is False)


def test_is_japanese_deck_name() -> None:
    """scope.is_japanese_deck_name: fresh-install guess, name pass (pure)."""
    f = compredef_scope.is_japanese_deck_name
    check("unit: Japanese name matches",
          f("Japanese") is True)
    check("unit: 日本語 name matches",
          f("My 日本語 deck") is True)
    check("unit: keyword Tango matches",
          f("Tango N5") is True)
    check("unit: French name misses",
          f("French") is False)
    check("unit: empty/None miss",
          f("") is False and f(None) is False)


def test_note_type_name() -> None:
    """scope._note_type_name: tolerant name extraction with a bare
    stand-in (gap: only covered via full FakeNote fixtures)."""
    import types as _types
    f = compredef_scope._note_type_name
    check("unit: name extracted",
          f(_types.SimpleNamespace(
              note_type=lambda: {"name": "X"})) == "X")
    check("unit: empty dict -> ''",
          f(_types.SimpleNamespace(note_type=dict)) == "")
    check("unit: None type -> ''",
          f(_types.SimpleNamespace(note_type=lambda: None)) == "")

    def _boom():
        raise RuntimeError("nope")

    check("unit: raising note_type -> ''",
          f(_types.SimpleNamespace(note_type=_boom)) == "")
    check("unit: missing note_type attr -> ''",
          f(_types.SimpleNamespace()) == "")


# ---------------------------------------------------------------------------
# models.py — dataclass contracts
# ---------------------------------------------------------------------------

def test_models_dataclasses() -> None:
    """models: construction, defaults, frozen-ness (gap: dataclasses
    are only built incidentally inside engine tests)."""
    e = compredef_models.DictionaryEntry(
        word="学校", reading="がっこう", definition="まなびや",
        dictionary_title="T", dictionary_path="/p")
    check("unit: entry fields echo",
          (e.word, e.reading, e.definition, e.dictionary_title,
           e.dictionary_path) == ("学校", "がっこう", "まなびや", "T", "/p"))
    try:
        e.word = "x"  # type: ignore[misc]
        frozen = False
    except dataclasses.FrozenInstanceError:
        frozen = True
    check("unit: DictionaryEntry is frozen", frozen)
    r = compredef_models.ScoringResult(definition="d", score=0.5,
                                       is_perfect=False)
    check("unit: ScoringResult defaults are zero",
          (r.kanji_score, r.vocab_score, r.total_score, r.kanji_count)
          == (0.0, 0.0, 0.0, 0))
    check("unit: ScoringResult keeps values",
          compredef_models.ScoringResult(
              definition="d", score=0.5, is_perfect=False,
              kanji_score=1.0, vocab_score=2.0, total_score=3.0,
              kanji_count=4).total_score == 3.0)


# ---------------------------------------------------------------------------
# renderer.py — pure node rendering
# ---------------------------------------------------------------------------

def test_style_to_css() -> None:
    """renderer._style_to_css: camelCase->kebab, em-suffix, lists
    (gap: only covered inside full-HTML renders)."""
    s = compredef_renderer._style_to_css
    check("unit: plain props join", s({"color": "red"}) == "color: red")
    check("unit: camelCase kebabbed",
          s({"fontSize": "12px"}) == "font-size: 12px")
    check("unit: numeric margin gets em",
          s({"marginTop": 5}) == "margin-top: 5em")
    check("unit: numeric non-size stays raw",
          s({"zIndex": 3}) == "z-index: 3")
    check("unit: list values join with space",
          s({"margin": ["1px", "2px"]}) == "margin: 1px 2px")
    check("unit: None values skipped",
          s({"color": None, "x": "y"}) == "x: y")
    check("unit: non-dict -> empty", s("nope") == "")


def test_extract_plain_text_node() -> None:
    """renderer._extract_plain_text_node: tree walk incl. rt/rp/img/br
    rules (gap: only the HTML renderer is covered in regression)."""
    p = compredef_renderer._extract_plain_text_node
    check("unit: string passes through", p("x") == "x")
    check("unit: list concatenates", p(["a", "b"]) == "ab")
    check("unit: rt readings skipped",
          p({"tag": "ruby", "content": ["先", {"tag": "rt",
                                              "content": "せん"}]}) == "先")
    check("unit: img keeps alt text",
          p({"tag": "img", "alt": "pic"}) == "pic")
    check("unit: br becomes newline",
          p({"tag": "br"}) == "\n")
    check("unit: dict without content -> empty",
          p({"tag": "span"}) == "")
    check("unit: non-node -> empty", p(123) == "")


def test_render_definition_text() -> None:
    """renderer.render_yomitan_definition_text: plain-text path
    (gap: regression covers the HTML renderer, not this one)."""
    r = compredef_renderer.render_yomitan_definition_text
    check("unit: string stripped", r("  x  ") == "x")
    check("unit: text-type unwrapped",
          r({"type": "text", "text": " y "}) == "y")
    check("unit: structured content joined",
          r({"type": "structured-content",
             "content": ["a", {"tag": "br"}, "b"]}) == "a\nb")
    check("unit: unknown shape -> empty", r({"foo": 1}) == "")


# ---------------------------------------------------------------------------
# engine.py — pure candidate filtering
# ---------------------------------------------------------------------------

def test_filter_valid_entries() -> None:
    """engine._filter_valid_entries: reference-title filter shared by
    every scoring path (gap: only covered end-to-end via _pick_best)."""
    E = compredef_models.DictionaryEntry

    def _e(defn):
        return E(word="w", reading="", definition=defn,
                 dictionary_title="T", dictionary_path="/p")

    real = _e("夜があけて、太陽がのぼる時。また、その時刻。")
    ref = _e("会社更生法")
    check("unit: mixed list drops only the reference",
          compredef_engine._filter_valid_entries([ref, real]) == [real])
    check("unit: lone reference is still allowed (better than nothing)",
          compredef_engine._filter_valid_entries([ref]) == [ref])
    check("unit: empty in -> empty out",
          compredef_engine._filter_valid_entries([]) == [])


# ---------------------------------------------------------------------------
# anki.py — pure math/string helpers (aqt stubbed, never touched)
# ---------------------------------------------------------------------------

def test_maturity_points() -> None:
    """anki._maturity_points: ivl/365 capped at 1.0 (gap: only covered
    indirectly through snapshot point values)."""
    m = compredef_anki._maturity_points
    check("unit: zero -> 0.0", m(0) == 0.0)
    check("unit: negative -> 0.0", m(-5) == 0.0)
    check("unit: garbage -> 0.0", m("abc") == 0.0)
    check("unit: None -> 0.0", m(None) == 0.0)
    check("unit: one year -> exactly 1.0", m(365) == 1.0)
    check("unit: over a year caps at 1.0", m(400) == 1.0)
    check("unit: half year -> 0.5",
          abs(m(182.5) - 0.5) < 1e-9, f"got {m(182.5)}")


def test_first_field_text() -> None:
    """anki._first_field_text: \\x1f split + strip (gap: only covered
    inside full snapshot builds)."""
    f = compredef_anki._first_field_text
    check("unit: empty -> empty", f("") == "")
    check("unit: None -> empty", f(None) == "")
    check("unit: non-string -> empty", f(123) == "")
    check("unit: no separator passes through stripped",
          f("  x  ") == "x")
    check("unit: splits on first separator only",
          f("a\x1fb\x1fc") == "a")
    check("unit: empty first field -> empty", f("\x1fb") == "")


# ---------------------------------------------------------------------------
# yomitan.py — pure normalization + error-state helpers (no bridge)
# ---------------------------------------------------------------------------

def test_yomitan_normalize_reading() -> None:
    """yomitan._normalize_reading: mirrors utils.normalize_reading
    (separate copy to avoid a circular import — both must agree)."""
    n = compredef_yomitan._normalize_reading
    check("unit: katakana maps to hiragana", n("マズ") == "まず")
    check("unit: empty stays empty", n("") == "")
    check("unit: decorations stripped", n("ま ず") == "まず")
    check("unit: agrees with utils twin on samples",
          all(n(s) == compredef_utils.normalize_reading(s)
              for s in ["マズ", "せん-ず", "ひらがな", ""]))


def test_yomitan_error_state() -> None:
    """yomitan error set/get/clear roundtrip (gap: only covered via
    full bridge-failure paths). No network involved."""
    compredef_yomitan._set_last_error("boom")
    check("unit: set error readable",
          compredef_yomitan.get_last_yomitan_error() == "boom")
    compredef_yomitan.clear_yomitan_cache()
    check("unit: clear resets error to None",
          compredef_yomitan.get_last_yomitan_error() is None)


def test_rank_definitions() -> None:
    """picker.rank_definitions: (score, kanji) ordering (gap: new in
    the picker-audit work; engine._pick_best agreement is pinned in
    Ring 1 on real captured data)."""
    rk = compredef_picker.rank_definitions
    kp = {"公": 1.0, "平": 1.0}
    cands = [("B", "公平だ。"), ("A", "公平だ。"), ("C", "かなだけ。")]
    ranked = rk(cands, kp, {})
    titles = [t for (t, _), _ in ranked]
    check("unit: richer definition ranks first",
          titles[0] in ("A", "B") and titles[-1] == "C",
          f"got {titles}")
    check("unit: exact ties keep encounter order (stable sort)",
          titles[:2] == ["B", "A"], f"got {titles}")
    check("unit: distinct scores order identically when shuffled",
          [t for (t, _), _ in rk([("C", "かなだけ。"), ("A", "公平だ。")],
                                  kp, {})] == ["A", "C"])
    check("unit: empty in -> empty out", rk([], kp, {}) == [])
    # Density (v1.3 default): a succinct fully-known definition beats
    # a long mostly-unknown one — raw sums ranked the reverse
    # (3.0 vs 2.0 for the long one).
    short = "公平だ。"
    long_unknown = "公平な不透明不平等。"
    ranked2 = rk([("long", long_unknown), ("short", short)], kp, {})
    check("unit: succinct known beats long unknown (density)",
          ranked2[0][0][0] == "short",
          f"got {[t for (t, _), _ in ranked2]}")
    # Legacy strategy keeps the old length-favoring order (A/B switch).
    ranked3 = rk([("long", long_unknown), ("short", short)], kp, {},
                 strategy=compredef_picker.LegacySumPicker())
    check("unit: legacy strategy still favors raw mass",
          ranked3[0][0][0] == "long",
          f"got {[t for (t, _), _ in ranked3]}")


def test_scoring_exclusion_and_boilerplate() -> None:
    """scoring.remove_excluded_terms / strip_scoring_boilerplate /
    scoring_base_text (gap: new in the fat-removal work; end-to-end
    refreeze agreement is pinned in Ring 1 on real captured data)."""
    sc = compredef_scoring
    kp = {"不": 1.0, "公": 1.0, "平": 1.0}
    defn = "不公平でないこと。公平だ。"
    plain = sc.score_definition(defn, kp, {})
    excl = sc.score_definition(defn, kp, {}, exclude=("不公平",))
    check("unit: headword mentions earn nothing",
          excl.kanji_count < plain.kanji_count
          and excl.kanji_score < plain.kanji_score,
          f"plain={plain.kanji_score}/{plain.kanji_count} "
          f"excl={excl.kanji_score}/{excl.kanji_count}")
    check("unit: other occurrences of the same kanji still count",
          excl.kanji_score == 2.0 and excl.kanji_count == 2,
          f"got {excl.kanji_score}/{excl.kanji_count}")
    check("unit: single-char exclusion is ignored (too destructive)",
          sc.remove_excluded_terms("公平だ。", ("公",)) == "公平だ。")
    check("unit: empty exclusion is a no-op",
          sc.remove_excluded_terms("公平だ。", ()) == "公平だ。")
    ruigo = ('<div data-sc-meaning="" data-sc-class="C">'
             '<span data-sc-href="$c-ruigo">類語</span>'
             '<a href="?query=X">偏見</a></div>'
             '<span>公平だ。</span>')
    stripped = sc.strip_scoring_boilerplate(ruigo)
    check("unit: thesaurus block stripped for scoring",
          "偏見" not in stripped and "公平" in stripped,
          f"got {stripped!r}")
    hinshi = ('<span data-sc-hinshi="" data-sc-bm="">〘名〙</span>'
              '<span>公平だ。</span>')
    check("unit: part-of-speech tag stripped for scoring",
          "名" not in sc.strip_scoring_boilerplate(hinshi))
    check("unit: untagged text passes through identical",
          sc.strip_scoring_boilerplate("公平だ。") == "公平だ。")
    check("unit: display text untouched (strip is scoring-only)",
          "偏見" in ruigo)
    res = sc.score_definition(ruigo, {"公": 1.0, "平": 1.0, "偏": 1.0,
                                      "見": 1.0}, {})
    check("unit: stripped compounds never enter vocab",
          "偏見" not in sc.extract_kanji_words(
              sc.scoring_base_text(ruigo)),
          f"got {sc.extract_kanji_words(sc.scoring_base_text(ruigo))}")


def main() -> int:
    import shutil as _sh
    import tempfile as _tf
    tmp_root = _tf.mkdtemp(prefix="compredef_units_")
    try:
        test_normalize_reading()
        test_extract_base_text_edges()
        test_resolve_dictionary_paths()
        test_resolve_dictionary_paths_disabled(tmp_root)
        test_is_zip_and_directory_dictionary(tmp_root)
        test_find_dictionary_folders(tmp_root)
        test_calculate_kanji_score_edges()
        test_get_scope_decks()
        test_expand_scope_names()
        test_missing_scope_decks()
        test_is_scope_empty()
        test_is_japanese_deck_name()
        test_note_type_name()
        test_models_dataclasses()
        test_style_to_css()
        test_extract_plain_text_node()
        test_render_definition_text()
        test_filter_valid_entries()
        test_rank_definitions()
        test_scoring_exclusion_and_boilerplate()
        test_maturity_points()
        test_first_field_text()
        test_yomitan_normalize_reading()
        test_yomitan_error_state()
    finally:
        _sh.rmtree(tmp_root, ignore_errors=True)
    print("=" * 70)
    print(f"UNIT RESULT: {RESULTS['pass']}/{RESULTS['pass'] + RESULTS['fail']} "
          f"passed, {RESULTS['fail']} failed")
    if RESULTS["failed_names"]:
        print("\nFAILED TESTS:")
        for name in RESULTS["failed_names"]:
            print(f"  - {name}")
    print("=" * 70)
    return 0 if RESULTS["fail"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
