"""
editor_browser.py - Editor toolbar toggle, Tab-to-Generate and Browser bulk
generation for CompreDef.

Injects UI elements into Anki using `aqt.gui_hooks`:
- Card Editor toolbar TOGGLE (Chinese-Support style): a single
  toggleable "CD" button whose active state is the global auto-generate
  switch. When ON, leaving the word field (Tab / click-away) fills an
  empty definition — never overwrites. Clicking ON also tries the
  current note immediately. Deck Scope is never consulted for
  generation; Scope bounds only learner knowledge (scoring weights).
- Tab-to-Generate: leaving the configured word field auto-fills the
  definition field when it is empty.
- Browser Edit menu & context menu items to bulk generate definitions
  for selected notes (the deliberate regenerate path — MAY overwrite,
  unlike Tab/the toggle).

Tab-to-Generate stability contract (this feature was removed in a1a92a3
after it froze Anki and lost definitions; it is back ONLY because every
historical failure mode is now structurally fixed):
1. Generation itself is pure SQLite lookups (install-time indexing, see
   parser.py) — the old first-use freeze came from parsing dictionary
   files inside the unfocus path, which can never happen anymore.
2. The hook returns `changed` UNTOUCHED so it never triggers the legacy
   editor's "reload after filter" race (the old lost-definition bug).
   Persistence is done by the generation path itself, which updates the
   note BEFORE refreshing the editor (see _apply_definition_to_editor).
3. Single-flight guard: rapid Tab-Tab-Tab never stacks duplicate jobs.
4. Opt-out via config ("tab_generate": false) for users who prefer
   explicit-only workflows.
5. v1.2.1: unmapped note types fall back to field auto-inference — the
   SAME mapping resolver the toolbar button uses. Previously Tab stayed
   silent for any type missing from `targets` while the button worked
   (the "does not always fire" report). Mirrors how the official
   Japanese Support add-on's focus-lost hook (reading.py onFocusLost)
   stays tolerant: it never requires per-type configuration up front.

Anki 26.x compatibility (verified against installed 26.08.1 source):
- Two editor generations coexist: the Svelte `NewEditor` (Add window /
  Edit Current) which has NO `.note` attribute (only `.nid`) and fires no
  unfocus hook, and the legacy `Editor` (Browser / legacy mode) which
  carries `.note` and fires `editor_did_unfocus_field` on blur. Tab-to-
  Generate uses the unfocus hook, so it is active wherever that hook
  fires (the legacy editor; on this user's Anki the Svelte experiment is
  disabled so ALL editors are legacy). No JS injection, no bridge
  monkeypatching — those were the fragile parts of the old approach.
- `run_in_background` executes the task then calls on_done on the MAIN
  thread, so note edits in on_done are safe.
"""

import json
import os
import traceback
from typing import Any, Dict, List, Optional

from aqt import mw, gui_hooks
from aqt.browser import Browser
from aqt.qt import QMenu, QKeySequence
from aqt.utils import tooltip

from .core import get_generator
from .utils import parse_furigana_field, extract_clean_word, resolve_dictionary_paths, field_is_effectively_empty

# Dual-context sibling import (see core.py for why both forms are needed).
if __package__:
    from .scope import note_in_scope as _scope_note_in_scope
else:
    from scope import note_in_scope as _scope_note_in_scope


def _get_addon_name() -> str:
    """
    Safely retrieves the root Anki add-on name for config persistence.

    Returns the correct root addon name instead of the submodule name
    to ensure config.json is saved under the correct key.
    """
    if hasattr(mw, 'addonManager'):
        root_name = mw.addonManager.addonFromModule(__name__)
        if root_name:
            return root_name
    # Fallback to first part of module name
    return __name__.split('.')[0]


def _get_addon_config() -> Dict[str, Any]:
    """
    Retrieves current add-on configuration dictionary using correct root addon name.
    """
    if not mw or not mw.addonManager:
        return {}
    addon_name = _get_addon_name()
    return mw.addonManager.getConfig(addon_name) or {}


def _extract_reading_text(note, word_field: str, reading_field: str) -> str:
    """
    Extracts the word's kana reading from the note for homograph resolution.

    Priority: a dedicated reading/furigana field if configured, else the
    word field itself (Expression fields often embed furigana markup like
    '先[ま]ず'). Returns '' when neither carries usable reading info —
    the generator then falls back to reading-agnostic lookup.
    """
    # Dedicated reading field first (explicit user configuration wins)
    if reading_field and reading_field in note:
        parsed = parse_furigana_field(note[reading_field])
        if parsed:
            return parsed

    # Fall back to the word field (may embed 先[ま]ず / ruby markup)
    if word_field and word_field in note:
        parsed = parse_furigana_field(note[word_field])
        if parsed:
            return parsed

    return ""


def _get_note_type_name(note) -> str:
    """Returns a note's notetype name via the non-deprecated API."""
    try:
        nt = note.note_type()  # anki 2.1.50+; 'note.model()' is deprecated
        if nt:
            return str(nt.get("name", ""))
    except Exception:
        pass
    return ""


def _note_in_scope(note, config: Dict[str, Any], editor: Any = None) -> bool:
    """
    Scope check kept for diagnostics and the knowledge snapshot's
    universe — generation paths NO LONGER call it (Scope bounds only
    knowledge, never eligibility). Never raises.
    """
    try:
        return bool(_scope_note_in_scope(note, config, editor=editor))
    except Exception:
        print(f"CompreDef: scope check failed:\n{traceback.format_exc()}")
        return False


def _infer_field_mapping(note) -> Optional[Dict[str, str]]:
    """
    Auto-infers word/reading/definition field names from the note's
    actual fields. Used as a fallback when a note is inside the Scope
    but has no explicit entry in `targets` — picking a deck should be
    enough to enable generation (the user said 'no need to add each
    note type'). Reuses the same keyword heuristics as the GUI.
    """
    try:
        field_names = list(note.keys())
    except Exception:
        return None
    if not field_names:
        return None
    # Inline lightweight version of gui's keyword match to avoid import
    # cycles; keeps editor_browser independent from gui.
    _WORD_KW = ["word", "expression", "kanji", "front"]
    _READ_KW = ["furigana", "reading", "kana", "hiragana", "katakana",
                "yomi", "読み"]
    _READ_KW2 = ["expression", "word", "front"]
    _DEF_KW = ["definition", "meaning", "glossary", "translation",
               "explanation", "sense", "desc"]

    def _best(fields: List[str], kws: List[str]) -> Optional[str]:
        lows = [f.lower().replace("_", " ").replace("-", " ") for f in fields]
        for kw in kws:
            for i, fl in enumerate(lows):
                if fl == kw.lower():
                    return fields[i]
            for i, fl in enumerate(lows):
                if kw.lower() in fl:
                    return fields[i]
        return None

    word = _best(field_names, _WORD_KW)
    # Prefer dedicated reading field, else word field itself
    reading = _best(field_names, _READ_KW) or (word or "")
    remaining = [f for f in field_names if f != word]
    definition = _best(remaining, _DEF_KW) or _best(field_names, _DEF_KW)
    if not word or not definition or word == definition:
        return None
    return {"word_field": word, "reading_field": reading or "",
            "definition_field": definition}


def _resolve_mapping_for_inscope_note(note, config: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """Mapping resolution for any note (Scope no longer gates generation).

    Returns the field mapping or None when no usable mapping exists
    (explicit + inferred both failed). The name is kept for backward
    compatibility (tests import it indirectly via resolve_fields_for_note).
    """
    targets = config.get("targets")
    if isinstance(targets, dict) and targets:
        mapping = targets.get(_get_note_type_name(note))
        if isinstance(mapping, dict):
            resolved = {
                "word_field": str(mapping.get("word_field", "") or ""),
                "reading_field": str(mapping.get("reading_field", "") or ""),
                "definition_field": str(mapping.get("definition_field", "") or ""),
            }
            if resolved["word_field"] and resolved["definition_field"]:
                return resolved
            # Incomplete mapping → try inference.
            return _infer_field_mapping(note)
        # No entry for this type → infer.
        return _infer_field_mapping(note)

    # Legacy single-type config: applies only to that one type; any
    # other type falls back to inference (no Scope involved).
    legacy_type = str(config.get("note_type", "") or "").strip()
    if legacy_type and _get_note_type_name(note) != legacy_type:
        return _infer_field_mapping(note)
    resolved = {
        "word_field": str(config.get("word_field", "") or ""),
        "reading_field": str(config.get("reading_field", "") or ""),
        "definition_field": str(config.get("definition_field", "") or ""),
    }
    if resolved["word_field"] and resolved["definition_field"]:
        return resolved
    return _infer_field_mapping(note)


def resolve_fields_for_note(note, config: Dict[str, Any]) -> Optional[Dict[str, str]]:
    """
    Returns {'word_field', 'reading_field', 'definition_field'} for this
    note, or None when no usable mapping exists.

    Field mapping comes from the note's own type (multi-type 'targets'
    when configured, legacy single-type otherwise), falling back to
    auto-inference — picking a deck is never required. Deck Scope does
    NOT gate generation: it bounds only the learner-knowledge snapshot
    (kanji/vocab scoring weights). Generation itself is governed by the
    global CD toggle (`tab_generate`) + Tab, Chinese-Support style.
    """
    return _resolve_mapping_for_inscope_note(note, config)


def _resolve_editor_note(editor) -> Optional[Any]:
    """
    Returns the current Note object from either editor generation.

    NewEditor (Svelte) exposes only `.nid` (no `.note`), so we fetch the
    note from the collection. Legacy Editor carries `.note` directly.
    """
    note = getattr(editor, "note", None)
    if note is not None:
        return note
    nid = getattr(editor, "nid", None)
    if nid is not None and mw and mw.col:
        try:
            return mw.col.get_note(nid)
        except Exception:
            return None
    return None


# ---------------------------------------------------------------------------
# Single-note generation (editor toolbar button + Tab-to-Generate)
# ---------------------------------------------------------------------------

_generation_in_flight = set()  # note ids currently being generated


def _offer_mapping_fix(note, editor) -> None:
    """
    Mapping-failure dialog: the note's type could not be mapped to
    word/definition fields, so generation cannot proceed. Offers a
    one-click jump to the Fields tab. Deck Scope is never the cause —
    generation bypasses Scope by design (Scope bounds only knowledge).
    """
    type_name = _get_note_type_name(note)
    fields = list(note.keys()) if note is not None else []
    msg = (
        f"This note's type could not be mapped to word/definition "
        f"fields:\n\nNote type: {type_name}\n"
        f"Fields: {', '.join(fields[:8])}{' …' if len(fields) > 8 else ''}\n\n"
        f"Map it under Tools → CompreDef Configuration → Fields."
    )
    try:
        from aqt.utils import askUserDialog  # type: ignore
        diag = askUserDialog(
            msg, ["Open Configuration…", "Cancel"],
            parent=editor.parentWindow if editor is not None else None,
            title="CompreDef — field mapping needed",
        )
        if diag.run() == "Open Configuration…":
            try:
                from .gui import show_config_dialog
            except Exception:
                from gui import show_config_dialog  # type: ignore
            # Fields tab is index 1 (Scope=0, Fields=1).
            show_config_dialog(initial_tab=1)
    except Exception:
        tooltip(msg, parent=editor.parentWindow if editor else None)


# Backwards-compatibility alias: older code/tests referenced the
# Scope quick-fix by name. Generation no longer consults Scope, so it
# always shows the mapping dialog.
_offer_add_to_scope = _offer_mapping_fix


def on_editor_generate_definition(editor) -> None:
    """
    Fill-empty generation for the current note (toolbar toggle ON path
    and Tab-to-Generate share it, so the two can never diverge).

    Extracts the target word from the configured field and updates the
    definition field asynchronously — but ONLY when the definition field
    is empty. Existing content is never overwritten (Chinese-Support
    rule): clear the field first to regenerate, or use Browser bulk for
    deliberate overwrites. The heavy dictionary work is a set of SQLite
    SELECTs against pre-built indexes (see parser.py) — it never parses
    dictionary files. Deck Scope is not consulted (knowledge-only).
    """
    note = _resolve_editor_note(editor)
    if note is None:
        tooltip("No note selected in editor.", parent=editor.parentWindow)
        return
    # Attach the editor so scope/deck resolution can use the Add
    # window's DeckChooser for unsaved notes (id 0, no cards yet).
    try:
        note._cd_editor = editor  # type: ignore[attr-defined]
    except Exception:
        pass

    config = _get_addon_config()
    dictionaries = config.get("dictionaries", [])
    disabled_dictionaries = config.get("disabled_dictionaries", [])
    dictionary_folder = config.get("dictionary_folder", "")

    # Field mapping comes from the note's own type (multi-type 'targets'
    # when configured, legacy single-type otherwise), with auto-inference
    # as fallback. Unmappable types get the mapping dialog — Scope is
    # never the cause (generation bypasses it by design).
    fields = resolve_fields_for_note(note, config)
    if fields is None:
        _offer_mapping_fix(note, editor)
        return
    word_field = fields["word_field"]
    reading_field = fields["reading_field"]
    def_field = fields["definition_field"]

    # Check field presence in note
    if word_field not in note:
        tooltip(f"Target word field '{word_field}' not found on current note.", parent=editor.parentWindow)
        return

    if def_field not in note:
        tooltip(f"Definition field '{def_field}' not found on current note.", parent=editor.parentWindow)
        return

    # Never overwrite: Tab and the toolbar toggle fill EMPTY definitions
    # only (Chinese-Support rule). Emptiness must count "<br>" as empty but
    # treat embedded media as content — otherwise "<br><img ...>" (a real
    # user image) would look blank and get overwritten.
    try:
        if not field_is_effectively_empty(note[def_field]):
            tooltip(
                "CompreDef: definition already filled — left untouched. "
                "Clear it first to regenerate (or use Browser bulk).",
                parent=editor.parentWindow,
            )
            return
    except Exception:
        pass

    # Anki note fields frequently carry HTML wrappers (<div>, <span>) and
    # furigana markup (先[ま]ず / <ruby>先<rt>ま</rt></ruby>ず). The raw
    # string almost never equals the dictionary term, which made lookups
    # return nothing — clean it before the SQLite lookup.
    word_text = extract_clean_word(note[word_field])
    if not word_text:
        tooltip(f"Field '{word_field}' is empty.", parent=editor.parentWindow)
        return

    # Early validation: without any dictionary configured nothing can be generated
    # When Yomitan is the selected source, local dictionaries are not required.
    # When local is selected but Yomitan fallback is enabled (default), we still
    # allow generation — engine will try Yomitan as fail-safe.
    _src = str(config.get("dictionary_source") or "local").strip().lower()
    _is_yomitan_src = _src in ("yomitan", "yomitan_api", "api")
    _yomitan_fallback_enabled = config.get("yomitan_fallback") is not False
    if not _is_yomitan_src and not dictionaries and not dictionary_folder and not _yomitan_fallback_enabled:
        tooltip(
            "CompreDef: No dictionaries configured.\nSet them under Tools -> Add-ons -> CompreDef -> Config.",
            parent=editor.parentWindow,
        )
        return

    # Single-flight guard: never stack duplicate generations for the same
    # note (double-fired hooks previously burned 2x CPU/RAM).
    nid_key = getattr(note, "id", None) or id(note)
    if nid_key in _generation_in_flight:
        return
    _generation_in_flight.add(nid_key)

    tooltip("CompreDef: Generating definition...", parent=editor.parentWindow)

    # Resolve the word's reading (dedicated field or embedded furigana)
    # so homographs like 先ず(まず) vs 先ず(せんず) pick the right entry.
    reading_text = _extract_reading_text(note, word_field, reading_field)

    def task() -> Optional[str]:
        return get_generator().generate(
            word_text,
            dictionary_paths=resolve_dictionary_paths(dictionaries, dictionary_folder, disabled_dictionaries),
            reading=reading_text,
        )

    def on_done(future) -> None:
        # Always release the in-flight lock, even on failure
        _generation_in_flight.discard(nid_key)
        try:
            definition_result = future.result()
            if not definition_result:
                # When Yomitan is the selected source, surface the actual
                # bridge error instead of the generic "No definition found"
                _is_yomitan = str(config.get("dictionary_source") or "local").strip().lower() in ("yomitan","yomitan_api","api")
                if _is_yomitan:
                    try:
                        if __package__:
                            from .yomitan import get_last_yomitan_error
                        else:
                            from yomitan import get_last_yomitan_error
                        err = get_last_yomitan_error()
                        if err:
                            print(f"CompreDef: Yomitan lookup failed for '{word_text}': {err}")
                            tooltip(
                                f"CompreDef: Yomitan not reachable.\n{err}\n"
                                f"Fix: In CompreDef config → Dictionary Source → Yomitan API →\n"
                                f"click 'Install / Repair Bridge', restart browser, and enable\n"
                                f"Yomitan → Settings → Advanced → General → Enable Yomitan API.\n"
                                f"Or switch back to Local dictionaries.",
                                parent=editor.parentWindow,
                            )
                            return
                    except Exception:
                        pass
                tooltip(
                    f"CompreDef: No definition found for '{word_text}'." + (" (Yomitan mode — is Yomitan running?)" if _is_yomitan else ""),
                    parent=editor.parentWindow,
                )
                return

            _apply_definition_to_editor(editor, note, def_field, definition_result)
            tooltip(f"Generated definition for '{word_text}'!", parent=editor.parentWindow)
        except Exception:
            # Loud, diagnosable failure — never silently swallow (the bulk
            # path's old bare `except: continue` hid real bugs for months).
            print(f"CompreDef: generation failed for word '{word_text}' "
                  f"(note {nid_key}):\n{traceback.format_exc()}")
            tooltip(
                f"CompreDef: generation failed for '{word_text}' — "
                f"see Anki's debug console (Ctrl+Shift+;) for details.",
                parent=editor.parentWindow,
            )

    mw.taskman.run_in_background(task, on_done)


def _apply_definition_to_editor(editor, note, def_field: str, definition_html: str) -> None:
    """
    Persists the generated definition into the note and refreshes the editor.

    Ordering contract (fixes the 'definition disappears' bug):
    1. Write the field on the Note object.
    2. Persist to the collection FIRST (update_note).
    3. THEN refresh the editor UI. A reload can never discard the change
       because it is already durably stored.

    Refresh contract (fixes the 'rendered field not updating' bug):
    prefer the editor's OWN reload entry points. On the legacy editor,
    loadNoteKeepingFocus() is exactly what Anki itself runs after a
    changed unfocus hook (saveSession + full field/meta state +
    focusField + triggerChanges), so BOTH the rendered field AND any
    HTML-source view refresh together — the same reason Japanese
    Support's furigana Tab works flawlessly in one go. The old bare
    setFields() eval only updated the field stores, leaving rendered
    Svelte components stale until the next focus event. The raw eval
    survives only as the last resort for editor generations exposing
    no reload method.
    """
    note[def_field] = definition_html

    # Persist existing notes immediately. New (unsaved) notes in the Add
    # window are written by Anki itself when the user confirms the add —
    # there is no collection row yet, so update_note would only raise a
    # noisy (but harmless) traceback on every Add-window Tab. The
    # in-memory object assigned above is what the Add window saves.
    note_id = getattr(note, "id", 0)
    if note_id and mw and mw.col:
        try:
            mw.col.update_note(note)
        except Exception:
            # Log but don't fail - the note object is already modified
            print(f"CompreDef: update_note failed for note {getattr(note, 'id', 'unknown')}:\n{traceback.format_exc()}")

    # Refresh the visible editor. run_in_background's on_done runs on the
    # main thread, so touching Qt/the webview here is thread-safe. Each
    # mechanism is attempted in preference order with its own guard, so
    # a closed editor (or a missing method) falls through silently
    # instead of raising.
    refreshed = False
    for method_name in ("loadNoteKeepingFocus", "loadNote"):
        try:
            method = getattr(editor, method_name, None)
            if callable(method):
                method()
                refreshed = True
                break
        except Exception:
            continue
    if not refreshed:
        # NewEditor (Svelte): reload by note id — but ONLY for saved
        # notes. reloadNote() re-fetches from the collection, which an
        # unsaved Add-window note (no row yet) would not survive.
        try:
            reload_method = getattr(editor, "reload_note", None)
            if note_id and callable(reload_method):
                reload_method()
                refreshed = True
        except Exception:
            pass
    if not refreshed:
        # Last resort: raw field-store update. triggerChanges() is
        # included so Svelte field components re-render (bare setFields
        # alone left the rendered view stale — the reported bug).
        try:
            names = list(note.keys())
            values = [note[name] for name in names]
            editor.web.eval(
                f"setFields({json.dumps(names)}, {json.dumps(values)});"
                f"triggerChanges();"
            )
        except Exception:
            print(f"CompreDef: editor refresh failed:\n{traceback.format_exc()}")


# ---------------------------------------------------------------------------
# Tab-to-Generate (automatic generation on word-field unfocus)
# ---------------------------------------------------------------------------

# Live editor registry: the unfocus hook gives us (note, field index) but NOT
# the editor instance, and the old implementation guessed it by walking
# topLevelWidgets() — which could grab the WRONG editor when several windows
# are open. Instead we track editors as they load notes via
# editor_did_load_note (fires for both editor generations) and match them to
# blurred notes in the unfocus hook (identity first, then note id).
_live_editors: List[Any] = []


def _register_editor(editor) -> None:
    """
    Hook callback for `editor_did_load_note`: keeps a weak registry of live
    editors so Tab-to-Generate can map a blurred note back to its editor.

    Anki does not fire a symmetric 'editor closed' hook, so the registry is
    pruned lazily: destroyed Qt objects are filtered out on each load
    (via `sip.isdeleted`, imported inside a try/except because the test
    stub has no Qt bindings).
    """
    # Import locally: tests stub aqt, where the Qt/sip bindings may be absent.
    try:
        from aqt.qt import sip  # type: ignore[attr-defined]

        def _gone(obj) -> bool:
            return sip.isdeleted(obj)

        alive = [e for e in _live_editors if not _gone(e)]
        if _live_editors and len(alive) != len(_live_editors):
            _live_editors[:] = alive
    except Exception:
        # No sip available (test stub): keep everything; the registry is
        # only a lookup aid, never a correctness requirement.
        pass
    if editor not in _live_editors:
        _live_editors.append(editor)
    # New/loaded note: the toolbar toggle must show the CURRENT global
    # state (buttons are created once per editor with a stale-at birth
    # tooltip/class). Sync is best-effort — test fakes have no webview.
    try:
        _sync_toggle_visual(editor)
    except Exception:
        pass


def _find_editor_for_note(note) -> Optional[Any]:
    """
    Returns a live editor currently editing `note`, if any.

    Identity match first: the legacy editor keeps the SAME Note object the
    blur hook hands us, so object identity is exact — vital in the Add
    window where unsaved notes all share id 0 and an id-only match could
    pick a different open Add window. The id fallback covers any editor
    generation that resolves notes through the collection.
    For unsaved notes (id=0), we also check for the temporary _cd_editor
    attachment set during processing.
    """
    # Pass 1: exact object identity (legacy editors).
    for editor in reversed(_live_editors):  # most recently loaded wins
        if getattr(editor, "note", None) is note:
            return editor
    # Pass 2: match by note id (collection-resolved notes, same id).
    target_nid = getattr(note, "id", None)
    # For unsaved notes, also check for our temporary attachment
    if target_nid == 0:
        for editor in reversed(_live_editors):
            if getattr(editor, "_cd_editor", None) is note:
                return editor
    if not target_nid:  # 0/None = unsaved note; identity pass already failed
        return None
    for editor in reversed(_live_editors):
        candidate = _resolve_editor_note(editor)
        if candidate is not None and getattr(candidate, "id", None) == target_nid:
            return editor
    return None


def _tab_generate_enabled(config: Dict[str, Any]) -> bool:
    """
    Resolves whether Tab-to-Generate is active for this config.

    Defaults to ON (this was the feature's historical behaviour when it
    worked); explicitly set to False to disable. Kept as a separate pure
    function so the regression suite can exercise the decision matrix.
    """
    return bool(config.get("tab_generate", True))


def _should_auto_generate(note, unfocused_field: str, config: Dict[str, Any],
                          editor: Any = None) -> bool:
    """
    Pure decision function for Tab-to-Generate — returns True when leaving
    `unfocused_field` on `note` should kick off automatic generation.

    Conditions (all must hold):
    - The feature is enabled in config (the global CD toggle).
    - The unfocused field IS the word field — from the note type's
      explicit `targets` entry, its legacy config, or auto-inference
      (same resolver as the toolbar button; v1.2.1).
    - The definition field exists and is empty (never overwrite existing
      content — regeneration stays available via Browser bulk, or by
      clearing the field first).

    Deck Scope is deliberately NOT consulted: Scope bounds only the
    learner-knowledge snapshot (scoring weights), never whether a card
    may generate.
    """
    if not _tab_generate_enabled(config):
        return False

    # Multi-type mode: only fire when the note's type is a configured
    # target AND its own word/definition fields are involved. When the
    # type has no explicit entry, fall back to the same auto-inference
    # the toolbar button uses — otherwise Tab stayed silent for every
    # unmapped type while the button worked (the "not always working"
    # report; Japanese Support's focus-lost hook is equally tolerant:
    # any source field can trigger it).
    targets = config.get("targets")
    if isinstance(targets, dict) and targets:
        mapping = targets.get(_get_note_type_name(note))
        if isinstance(mapping, dict):
            word_field = str(mapping.get("word_field", "") or "")
            def_field = str(mapping.get("definition_field", "") or "")
        else:
            inferred = _infer_field_mapping(note)
            if not inferred:
                return False
            word_field = inferred["word_field"]
            def_field = inferred["definition_field"]
    else:
        # Legacy single-type config
        legacy_type = str(config.get("note_type", "") or "").strip()
        if legacy_type and _get_note_type_name(note) != legacy_type:
            # Same tolerance as the button path: a note of a different
            # type still generates when fields can be inferred
            # (resolve_fields_for_note does exactly this).
            inferred = _infer_field_mapping(note)
            if not inferred:
                return False
            word_field = inferred["word_field"]
            def_field = inferred["definition_field"]
        else:
            word_field = config.get("word_field", "")
            def_field = config.get("definition_field", "")
            if not word_field or not def_field:
                # Empty legacy config: inference is the only chance left
                # (matches resolve_fields_for_note's fallback).
                inferred = _infer_field_mapping(note)
                if inferred:
                    word_field = inferred["word_field"]
                    def_field = inferred["definition_field"]

    if not word_field or not def_field or word_field == def_field:
        return False
    if unfocused_field != word_field:
        return False
    if def_field not in note:
        return False

    # Only auto-fill EMPTY definition fields. A never-edited field in the
    # legacy editor ships as "<br>" / "<div><br></div>" HTML rather than
    # "" — plain .strip() sees it as non-empty and blocks generation.
    # field_is_effectively_empty treats "<br>"-only HTML as empty but
    # keeps media (<img> etc.) as content, so an existing user image is
    # never silently overwritten (お好み焼き bug). This was the Browser
    # bug: 不公平 in Expression + Tab left Definition as "<br>", so Tab
    # silently did nothing despite the setting.
    return field_is_effectively_empty(note[def_field])


def on_field_unfocus(changed: bool, note, current_field_index: int) -> bool:
    """
    Hook callback for `editor_did_unfocus_field` — the Tab-to-Generate seam.

    Fired by the legacy editor when a field loses focus (Tab, click-away,
    window switch). If the blurred field is the configured word field and
    the definition field is empty, generation starts in the background.

    CRITICAL: returns `changed` UNTOUCHED. The legacy editor reloads the
    note when any filter returns True, which raced with our background
    write and deleted freshly generated definitions (historical bug).
    Returning the input unchanged leaves the reload decision to Anki;
    our own persistence path (update_note BEFORE editor refresh) is what
    makes the definition survive.
    """
    try:
        if not mw or not note:
            return changed

        config = _get_addon_config()
        if not _should_auto_generate(
            note, _field_name_at(note, current_field_index), config,
            editor=_find_editor_for_note(note),
        ):
            return changed

        editor = _find_editor_for_note(note)
        if editor is None:
            # No live editor for this note (e.g. programmatic blur) —
            # do nothing rather than guess at a window like the old code.
            return changed

        # Reuse the exact same generation path as the toolbar toggle
        # (validation, fill-empty guard, single-flight guard, background
        # thread, safe persistence) so Tab and toggle can never diverge
        # in behaviour.
        try:
            note._cd_editor = editor  # type: ignore[attr-defined]
        except Exception:
            pass
        on_editor_generate_definition(editor)
    except Exception:
        # A hook failure must never break editing; log loudly and move on.
        print(f"CompreDef: Tab-to-Generate unfocus hook failed:\n{traceback.format_exc()}")
    return changed


def _field_name_at(note, index: int) -> str:
    """
    Resolves the field name for a field ordinal via the non-deprecated
    note API. Returns '' for out-of-range indices (hook can fire during
    notetype switches with a stale index).

    Uses mw.col.models.field_names(note.note_type()) as the single
    source of truth — the same ordered list the legacy editor's
    onBridgeCmd uses when it saves note.fields[ord] = mungeHTML(txt)
    before firing the hook. Falls back to note.keys() for the test stub
    (which has no collection/models) and for any odd note that lacks a
    note_type. This matches the Japanese Support pattern:
    `fields = mw.col.models.field_names(note.note_type())`.
    """
    # Preferred: ordered names from the note type (editor's own order).
    try:
        nt = note.note_type()  # type: ignore[attr-defined]
        if nt:
            try:
                from aqt import mw as _mw  # local import: test stub has no Qt
                if _mw and getattr(_mw, "col", None) is not None:
                    models = getattr(_mw.col, "models", None)
                    if models is not None and hasattr(models, "field_names"):
                        names = models.field_names(nt)  # type: ignore[union-attr]
                        if 0 <= index < len(names):
                            return str(names[index])
            except Exception:
                pass
            # Fallback: flds list inside the note-type dict itself.
            flds = nt.get("flds") if isinstance(nt, dict) else None
            if isinstance(flds, list) and 0 <= index < len(flds):
                fld = flds[index]
                if isinstance(fld, dict):
                    return str(fld.get("name", ""))
                return str(fld)
    except Exception:
        pass
    try:
        names = list(note.keys())  # type: ignore[attr-defined]
        if 0 <= index < len(names):
            return names[index]
    except Exception:
        pass
    return ""


# ---------------------------------------------------------------------------
# Editor toolbar toggle (Chinese-Support style)
# ---------------------------------------------------------------------------

_TOGGLE_BUTTON_ID = "compredef_editor_btn"


def _toggle_tip(enabled: bool) -> str:
    """Tooltip text for the toolbar toggle in each state."""
    if enabled:
        return ("CompreDef auto-generate ON — leaving the word field (Tab) "
                "fills an empty Definition (never overwrites). "
                "Click to turn OFF.")
    return ("CompreDef auto-generate OFF — Tab does nothing. "
            "Click to turn ON (Tab then fills empty Definitions).")


def _set_toggle_visual(editor, enabled: bool) -> None:
    """Reflects the toggle state on one editor's toolbar button.

    Copied from Chinese Support 3's `updateButton`: the pressed (blue)
    look comes from Anki's own theme variables
    (`--button-primary-*`, the same blue as native toggles) — nothing
    is hard-coded and the label text ("CD" + icon) is never rewritten.
    Needed because raw `.anki-addon-button` elements have no `.active`
    CSS rule in Anki, so the UI-flipped `active` class alone is
    invisible; these inline variables are what make ON blue (and
    readable in dark mode too). Never raises (headless/test editors
    have no webview).
    """
    try:
        web = getattr(editor, "web", None)
        if web is None or not hasattr(web, "eval"):
            return
        grab = (f'document.getElementById({json.dumps(_TOGGLE_BUTTON_ID)})')
        if enabled:
            web.eval(
                f'{grab}.classList.add("active");'
                f'{grab}.style.setProperty("--button-bg", "var(--button-primary-bg)");'
                f'{grab}.style.setProperty("--button-gradient-start", "var(--button-primary-gradient-start)");'
                f'{grab}.style.setProperty("--button-gradient-end", "var(--button-primary-gradient-end)");'
                f'{grab}.title={json.dumps(_toggle_tip(True))};'
            )
        else:
            web.eval(
                f'{grab}.classList.remove("active");'
                f'{grab}.style.setProperty("--button-bg", "");'
                f'{grab}.style.setProperty("--button-gradient-start", "");'
                f'{grab}.style.setProperty("--button-gradient-end", "");'
                f'{grab}.title={json.dumps(_toggle_tip(False))};'
            )
    except Exception:
        pass


def _sync_toggle_visual(editor) -> None:
    """Sets one editor's button to the current config state."""
    try:
        cfg = _get_addon_config()
    except Exception:
        cfg = {}
    try:
        _set_toggle_visual(editor, _tab_generate_enabled(cfg))
    except Exception:
        pass


def on_toggle_cd_button(editor) -> None:
    """
    Toolbar toggle callback (Chinese-Support `toggleButtonClick`).

    Flips the global `tab_generate` flag, persists it, syncs the visual
    state on every live editor, and — when turning ON — immediately
    attempts a fill-empty generation for the current note so a single
    click both enables and fills (never overwrites: clear the field
    first to regenerate, or use Browser bulk).
    """
    try:
        addon = _get_addon_name()
        cfg = mw.addonManager.getConfig(addon) or {} if mw else {}
    except Exception:
        cfg, addon = {}, None
    new_state = not _tab_generate_enabled(cfg)
    try:
        if mw and addon:
            cfg["tab_generate"] = new_state
            mw.addonManager.writeConfig(addon, cfg)
    except Exception:
        print(f"CompreDef: toggle persist failed:\n{traceback.format_exc()}")
    for ed in list(_live_editors):
        _set_toggle_visual(ed, new_state)
    _set_toggle_visual(editor, new_state)
    try:
        tooltip(
            f"CompreDef auto-generate {'ON' if new_state else 'OFF'}"
            + (" — Tab now fills empty Definitions." if new_state else "."),
            parent=editor.parentWindow if editor is not None else None,
        )
    except Exception:
        pass
    if new_state:
        try:
            on_editor_generate_definition(editor)
        except Exception:
            print(f"CompreDef: toggle-on generate failed:\n{traceback.format_exc()}")


def add_editor_button(buttons: List[str], editor) -> None:
    """
    Hook callback to append the CompreDef toggle to the editor toolbar.

    Chinese-Support style: a single `toggleable` button whose `active`
    class shows the global auto-generate state. Clicking flips the
    state (persisted `tab_generate`); Tab fills empty definitions while
    ON. `editor_did_init_buttons` fires for BOTH editor generations and
    `addButton` exists on both, so one registration covers everything.
    """
    icon_path = os.path.join(os.path.dirname(__file__), "icons", "compredef.svg")
    try:
        enabled = _tab_generate_enabled(_get_addon_config())
    except Exception:
        enabled = True

    btn = editor.addButton(
        icon=icon_path if os.path.exists(icon_path) else None,
        cmd="compredef_toggle_autogen",
        func=lambda ed: on_toggle_cd_button(ed),
        tip=_toggle_tip(enabled),
        label="CD",
        id=_TOGGLE_BUTTON_ID,
        toggleable=True,
    )
    buttons.append(btn)


# ---------------------------------------------------------------------------
# Browser bulk generation (explicit menu action)
# ---------------------------------------------------------------------------

def on_bulk_generate_definitions(browser: Browser) -> None:
    """
    Action callback triggered from Browser Edit menu or Context menu.

    Processes all selected notes in a background thread, updating definition
    fields. This is the DELIBERATE regenerate path: unlike Tab and the
    editor toggle (which never touch a filled definition), bulk MAY
    overwrite existing definitions for the notes the user selected.
    Failures are logged with note id, word and traceback, reported
    to the user in the summary, and never abort the remaining notes.
    """
    # selected_notes() is the modern name; selectedNotes() the legacy one
    if hasattr(browser, "selected_notes"):
        nids = list(browser.selected_notes())
    else:
        nids = list(browser.selectedNotes())
    if not nids:
        tooltip("No notes selected.", parent=browser)
        return

    config = _get_addon_config()
    dictionaries = config.get("dictionaries", [])
    disabled_dictionaries = config.get("disabled_dictionaries", [])
    dictionary_folder = config.get("dictionary_folder", "")

    # Early validation: without any dictionary configured nothing can be generated
    _src2 = str(config.get("dictionary_source") or "local").strip().lower()
    _is_yomitan_src2 = _src2 in ("yomitan", "yomitan_api", "api")
    _yomitan_fallback_enabled2 = config.get("yomitan_fallback") is not False
    if not _is_yomitan_src2 and not dictionaries and not dictionary_folder and not _yomitan_fallback_enabled2:
        tooltip(
            "CompreDef: No dictionaries configured.\nSet them under Tools -> Add-ons -> CompreDef -> Config.",
            parent=browser,
        )
        return

    tooltip(
        f"CompreDef: Generating definitions for {len(nids)} note(s)...",
        parent=browser,
    )

    def task() -> tuple:
        """
        Background task across all selected note IDs.

        Returns (updated_count, skipped_count, failures) where failures is a
        list of (note_id, word, error_message) for the user-facing summary.
        """
        updated_count = 0
        skipped_count = 0
        failures: List[tuple] = []

        for nid in nids:
            word_text = ""
            try:
                note = mw.col.get_note(nid)

                # Field mapping per note type (multi-type 'targets' or
                # legacy single-type). Unconfigured types are skipped
                # silently in bulk — the selection may span many types.
                fields = resolve_fields_for_note(note, config)
                if fields is None:
                    skipped_count += 1
                    continue
                word_field = fields["word_field"]
                reading_field = fields["reading_field"]
                def_field = fields["definition_field"]

                if word_field not in note or def_field not in note:
                    continue

                # Strip HTML wrappers and furigana markup so the dictionary
                # term matches (same fix as the editor button path).
                word_text = extract_clean_word(note[word_field])
                if not word_text:
                    continue

                # Per-note reading resolution (dedicated field or embedded
                # furigana) so homographs resolve correctly in bulk too.
                reading_text = _extract_reading_text(
                    note, word_field, reading_field
                )

                # Generate definition (pure SQLite lookups — no indexing
                # ever happens here).
                definition_result = get_generator().generate(
                    word_text,
                    dictionary_paths=resolve_dictionary_paths(dictionaries, dictionary_folder, disabled_dictionaries),
                    reading=reading_text,
                )
                if not definition_result:
                    skipped_count += 1
                    continue

                # Persist BEFORE any UI refresh, so the definition survives.
                note[def_field] = definition_result
                mw.col.update_note(note)
                updated_count += 1
            except Exception:
                # Log loudly and keep going: one bad note must not kill the
                # batch, but the failure must be visible and diagnosable.
                err = traceback.format_exc()
                print(f"CompreDef: bulk generation FAILED for note {nid} "
                      f"(word '{word_text}'):\n{err}")
                failures.append((nid, word_text, err.splitlines()[-1] if err else "unknown error"))
                continue

        # Learner knowledge is a per-session snapshot: bulk definition
        # writes must NOT invalidate it (no repeated full collection scan).

        return updated_count, skipped_count, failures

    def on_done(future) -> None:
        try:
            updated_count, skipped_count, failures = future.result()
            # Refresh browser view to reflect updated note fields
            if hasattr(browser, "search"):
                browser.search()

            parts = [f"generated: {updated_count}"]
            if skipped_count:
                parts.append(f"no definition found: {skipped_count}")
            if failures:
                parts.append(f"FAILED: {len(failures)} (see console for details)")
                for nid, word, err in failures[:5]:  # console gets full tracebacks
                    print(f"CompreDef: note {nid} word '{word}': {err}")
            tooltip(
                f"CompreDef: {', '.join(parts)}",
                parent=browser,
            )
        except Exception:
            print(f"CompreDef: bulk generation crashed:\n{traceback.format_exc()}")
            tooltip("CompreDef: bulk generation crashed — see console.", parent=browser)

    mw.taskman.run_in_background(task, on_done)


def setup_browser_menu(browser: Browser) -> None:
    """
    Hook callback to append bulk edit option under Browser's Edit menu.

    Target hook: `gui_hooks.browser_menus_did_init`.
    """
    menu: QMenu = browser.form.menuEdit
    menu.addSeparator()

    action = menu.addAction("Generate CompreDef Definitions...")
    action.setShortcut(QKeySequence("Ctrl+Shift+D"))
    action.triggered.connect(lambda _, b=browser: on_bulk_generate_definitions(b))


def setup_browser_context_menu(browser: Browser, menu: QMenu) -> None:
    """
    Hook callback to append bulk edit option to Browser's right-click context menu.

    Target hook: `gui_hooks.browser_will_show_context_menu`.
    """
    action = menu.addAction("Generate CompreDef Definitions")
    action.triggered.connect(lambda _, b=browser: on_bulk_generate_definitions(b))


def setup_editor_browser_hooks() -> None:
    """
    Registers editor toolbar and browser menu hooks with Anki.

    Tab-to-Generate is registered via `editor_did_unfocus_field` + the
    `editor_did_load_note` registry. There is deliberately NO JS key
    listener and NO webview bridge monkeypatching: the old Tab feature
    relied on both, they never fired on the Svelte editor, and the
    unfocus hook already covers every Tab/click-away path natively.
    """
    gui_hooks.editor_did_init_buttons.append(add_editor_button)
    gui_hooks.browser_menus_did_init.append(setup_browser_menu)
    gui_hooks.browser_will_show_context_menu.append(setup_browser_context_menu)
    # Tab-to-Generate: map blurred notes back to their editor, and react
    # to word-field unfocus (returns `changed` untouched — see on_field_unfocus).
    gui_hooks.editor_did_load_note.append(_register_editor)
    gui_hooks.editor_did_unfocus_field.append(on_field_unfocus)
