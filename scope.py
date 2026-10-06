"""
scope.py - Deck-based Scope for CompreDef.

The Scope is the single place where the user picks which part of their
collection CompreDef considers. It is a list of DECK NAMES (stored in
config under ``scope_decks``) and it drives BOTH:

- definition generation eligibility (editor button, bulk action,
  Tab-to-Generate), and
- the learner-knowledge snapshot (known kanji / known vocabulary).

Rules (agreed with the user):

- Deck-only selection: picking a deck implicitly includes every note
  type inside it. There is no separate note-type picker.
- A deck implicitly includes all of its subdecks (``A`` matches ``A``
  and every ``A::*`` deck, mirroring Anki's own ``deck:A`` search).
- A note is in scope when ANY of its cards sits in a scoped deck. (A
  note can own cards in several decks; the user's mental model is
  one-note-one-card, so ANY-match is the least surprising rule.)
- Empty scope is fail-closed: nothing generates and the knowledge
  snapshot is empty. A fresh install therefore starts inert with a
  visible "pick your decks" warning instead of silently scanning the
  French decks too.
- Deck NAMES (not ids) are stored: they survive backup/restore, stay
  readable in config.json, and are trivial to test. A renamed/deleted
  deck simply matches nothing and is reported as missing by the GUI.

Only the ``notes``/``cards`` tables and the public ``col.decks`` /
``col.models`` APIs are used here (never the legacy ``models`` table,
renamed to ``notetypes`` in Anki 23.10+).
"""

from typing import Any, Dict, List, Optional, Set
import re

SCOPE_CONFIG_KEY = "scope_decks"

# Set once the fresh-install Japanese-deck heuristic has run (even when
# it found nothing), so a user who deliberately empties the Scope is
# never re-seeded on the next launch.
SCOPE_AUTOINIT_CONFIG_KEY = "scope_auto_initialized"

_CHILD_SEP = "::"

# Japanese script: hiragana + katakana (+ half-width katakana) + CJK.
# Kana presence is what separates Japanese decks from Chinese ones
# (kanji alone) and Western ones (neither) for the fresh-install guess.
_JAPANESE_RE = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\uff61-\uff9f\u4e00-\u9faf]')
_KANA_RE = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\uff61-\uff9f]')

# Lowercased substrings that mark a deck NAME as Japanese-learner
# material. Deliberately broad: a false positive (e.g. a French deck
# named "Vocab") only adds kana-less notes to the knowledge scan,
# which contribute no kanji points — while a missed Japanese deck
# silently starves scoring. Content sampling below catches oddly
# named decks that carry kana.
_JAPANESE_NAME_KEYWORDS = (
    "japan", "jap ", "japanese", "nihon", "nihongo", "にほん",
    "日本", "jlpt", "kanji", "漢字", "かんじ", "カンジ",
    "kana", "かな", "カナ", "hiragana", "katakana", "ひらがな",
    "カタカナ", "tango", "単語", "たんご", "kaishi", "core ",
    "core2", "core6", "mining", "animecard", "yomitan",
    "jouyou", "じょうよう", "じょう用", "kanken", "expression",
    "furigana",
)


def is_japanese_deck_name(name: str) -> bool:
    """True when a deck NAME looks like Japanese-learner material.

    Pure (no collection/DB): Japanese script anywhere in the name, or
    a case-insensitive keyword hit. Used by the fresh-install Scope
    guess and covered in Ring 0.
    """
    if not name or not isinstance(name, str):
        return False
    if _JAPANESE_RE.search(name):
        return True
    lowered = f" {name.lower()} "
    return any(kw in lowered or kw in name.lower() for kw in _JAPANESE_NAME_KEYWORDS)


def get_scope_decks(config: Optional[Dict[str, Any]]) -> List[str]:
    """Returns the ordered, de-duplicated scope deck names from config."""
    if not isinstance(config, dict):
        return []
    raw = config.get(SCOPE_CONFIG_KEY)
    if not isinstance(raw, list):
        return []
    seen: Set[str] = set()
    out: List[str] = []
    for item in raw:
        name = str(item).strip() if item is not None else ""
        # Skip empties; keep first occurrence order for a stable GUI label.
        if name and name not in seen:
            seen.add(name)
            out.append(name)
    return out


def expand_scope_names(
    all_names: List[str], scope: List[str]
) -> Set[str]:
    """Expands selected decks to include their subdecks.

    ``["A"]`` matches ``A`` plus every ``A::*`` deck, exactly like
    Anki's ``deck:A`` search. Names absent from ``all_names`` (renamed
    or deleted decks) match nothing.
    """
    existing = set(all_names)
    expanded: Set[str] = set()
    for selected in scope:
        prefix = selected + _CHILD_SEP
        for name in existing:
            # Exact match or a true child (prefix guard avoids ``AB``
            # matching selection ``A``).
            if name == selected or name.startswith(prefix):
                expanded.add(name)
    return expanded


def missing_scope_decks(
    all_names: List[str], scope: List[str]
) -> List[str]:
    """Returns selected decks that match nothing (renamed/deleted)."""
    expanded = expand_scope_names(all_names, scope)
    return [s for s in scope if s not in expanded]


def get_all_deck_names(col: Any) -> List[str]:
    """Lists every deck name via the public decks API, cross-version.

    Prefers ``all_names_and_ids()`` (Anki 23.10+); falls back to the
    legacy ``all()`` (list of dicts with a ``name`` key). Returns []
    when the collection (or the API) is unavailable, e.g. headless
    test stubs without decks.
    """
    if col is None:
        return []
    decks = getattr(col, "decks", None)
    if decks is None:
        return []
    # Modern API: list of DeckNameId (objects, dicts or tuples).
    try:
        if hasattr(decks, "all_names_and_ids"):
            out: List[str] = []
            for entry in decks.all_names_and_ids() or []:
                name: Optional[str] = None
                if isinstance(entry, dict):
                    name = entry.get("name")
                elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    # Some versions yield (name, id); others (id, name).
                    # A deck name is the string element.
                    for part in entry:
                        if isinstance(part, str):
                            name = part
                            break
                else:
                    name = getattr(entry, "name", None)
                if name:
                    out.append(str(name))
            return out
    except Exception:
        pass
    # Legacy API: list of {"name": ..., "id": ...} dicts.
    try:
        if hasattr(decks, "all"):
            return [
                str(d.get("name"))
                for d in (decks.all() or [])
                if isinstance(d, dict) and d.get("name")
            ]
    except Exception:
        pass
    return []


def _deck_name_to_did(col: Any) -> Dict[str, int]:
    """Maps deck name -> deck id via the public decks API."""
    mapping: Dict[str, int] = {}
    if col is None:
        return mapping
    decks = getattr(col, "decks", None)
    if decks is None:
        return mapping
    try:
        if hasattr(decks, "all_names_and_ids"):
            for entry in decks.all_names_and_ids() or []:
                name: Optional[str] = None
                did: Optional[int] = None
                if isinstance(entry, dict):
                    name = entry.get("name")
                    did = entry.get("id")
                elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                    for part in entry:
                        if isinstance(part, str) and name is None:
                            name = part
                        elif isinstance(part, int) and did is None:
                            did = part
                else:
                    name = getattr(entry, "name", None)
                    did = getattr(entry, "id", None)
                if name and did is not None:
                    try:
                        mapping[str(name)] = int(did)
                    except (TypeError, ValueError):
                        continue
            return mapping
    except Exception:
        pass
    try:
        if hasattr(decks, "all"):
            for d in decks.all() or []:
                if isinstance(d, dict) and d.get("name") is not None:
                    try:
                        mapping[str(d["name"])] = int(d["id"])
                    except (TypeError, ValueError, KeyError):
                        continue
    except Exception:
        pass
    return mapping


def scope_dids(col: Any, scope: List[str]) -> Set[int]:
    """Resolves scope deck names (plus subdecks) to deck ids."""
    if col is None or not scope:
        return set()
    name_to_did = _deck_name_to_did(col)
    if not name_to_did:
        return set()
    expanded = expand_scope_names(list(name_to_did.keys()), scope)
    return {name_to_did[n] for n in expanded if n in name_to_did}


def note_dids(col: Any, nid: Any) -> List[int]:
    """Returns the deck ids of all cards belonging to note ``nid``."""
    if col is None or nid is None:
        return []
    try:
        nid_int = int(nid)
    except (TypeError, ValueError):
        return []
    if not nid_int:
        # Unsaved Add-window notes have id 0 and no cards yet; the
        # caller falls back to the note-type check for those.
        return []
    try:
        # Integer interpolation (not placeholders): the id is int()-
        # validated above, and placeholder style drifts across Anki
        # versions and test doubles.
        rows = col.db.all(
            "SELECT did FROM cards WHERE nid = %d" % nid_int
        ) or []
    except Exception:
        return []
    dids: List[int] = []
    for row in rows:
        raw = row[0] if isinstance(row, (list, tuple)) else row
        try:
            dids.append(int(raw))
        except (TypeError, ValueError):
            continue
    return dids


def _note_type_name(note: Any) -> str:
    """Best-effort note-type name via the non-deprecated note API."""
    try:
        nt = note.note_type()
        if nt:
            return str(nt.get("name", ""))
    except Exception:
        pass
    return ""


def _did_to_name(col: Any) -> Dict[int, str]:
    """Inverted deck map (did -> name) for membership checks."""
    return {did: name for name, did in _deck_name_to_did(col).items()}


def implied_note_types(col: Any, scope: List[str]) -> List[str]:
    """Lists note-type names that own at least one card in the scope.

    Used by the GUI to show which types a deck selection implies (for
    field mapping) and as the fallback membership rule for unsaved
    notes that have no cards yet. Schema-proof: touches only
    ``notes.mid`` + ``cards.did`` plus the public ``models.get(mid)``
    lookup — never the legacy ``models`` table.
    """
    if col is None or not scope:
        return []
    dids = scope_dids(col, scope)
    if not dids:
        return []
    try:
        # Plain-integer interpolation (see note_dids for why).
        did_list = ",".join(str(int(d)) for d in sorted(dids))
        rows = col.db.all(
            "SELECT DISTINCT mid FROM notes WHERE id IN "
            f"(SELECT nid FROM cards WHERE did IN ({did_list}))"
        ) or []
    except Exception:
        return []
    names: List[str] = []
    seen: Set[str] = set()
    models = getattr(col, "models", None)
    for row in rows:
        raw = row[0] if isinstance(row, (list, tuple)) else row
        try:
            mid = int(raw)
        except (TypeError, ValueError):
            continue
        name = ""
        try:
            model = models.get(mid) if models is not None else None
            if model:
                name = str(model.get("name", ""))
        except Exception:
            name = ""
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return sorted(names)


def is_scope_empty(config: Optional[Dict[str, Any]]) -> bool:
    """True when no scope decks are selected (fail-closed state)."""
    return not get_scope_decks(config)


def note_deck_names(note: Any, col: Any = None) -> List[str]:
    """Returns the human deck names holding this note's cards (for the
    out-of-scope quick-fix: show the user WHICH deck blocked the note).

    Unsaved notes (no cards) yield []. Never raises.
    """
    try:
        if col is None:
            try:
                from aqt import mw  # type: ignore

                col = mw.col if mw is not None else None
            except Exception:
                return []
        if col is None:
            return []
        dids = note_dids(col, getattr(note, "id", None))
        if not dids:
            return []
        names = _did_to_name(col)
        out: List[str] = []
        for d in dids:
            n = names.get(d)
            if n and n not in out:
                out.append(n)
        return out
    except Exception:
        return []


def resolve_deck_for_note(note: Any, col: Any = None, editor: Any = None) -> List[str]:
    """Returns the deck names relevant to a note, Add-window aware.

    Saved notes: their cards' decks (like note_deck_names). Unsaved
    Add-window notes (id 0, no cards): the deck the window will add the
    note to — editor's DeckChooser when reachable, else the
    collection's 'curDeck' (the same default Anki's own Add window
    uses). This lets the Scope gate and quick-fix work BEFORE the first
    save, matching the user's mining workflow (generate while adding).
    Returns [] when nothing is resolvable. Never raises.
    """
    try:
        # Saved note: its cards' decks.
        decks = note_deck_names(note, col)
        if decks:
            return decks
        # Unsaved: editor's deck chooser (Add window).
        if editor is not None:
            chooser = getattr(editor, "deck_chooser", None)
            if chooser is None:
                chooser = getattr(getattr(editor, "parentWindow", None),
                                   "deck_chooser", None)
            did = getattr(chooser, "selected_deck_id", None)
            if did and col is not None:
                did_to_name = _did_to_name(col)
                name = did_to_name.get(int(did))
                if name:
                    return [name]
        # Fallback: collection's current deck (Anki's Add default).
        if col is not None:
            try:
                did = col.get_config("curDeck", default=None)
                if did:
                    did_to_name = _did_to_name(col)
                    name = did_to_name.get(int(did))
                    if name:
                        return [name]
            except Exception:
                pass
        return []
    except Exception:
        return []


def note_in_scope(
    note: Any, config: Optional[Dict[str, Any]], col: Any = None, editor: Any = None
) -> bool:
    """Returns True when ``note`` belongs to the configured scope.

    Membership rule: ANY of the note's cards in a scoped deck (subdecks
    included). Unsaved Add-window notes use the deck the window will
    add the card to (editor's DeckChooser / curDeck), with an
    implied-type fallback. Empty scope is fail-closed (False).
    ``col`` may be passed explicitly (tests); otherwise ``mw.col`` is
    used when available. ``editor`` enables Add-window deck resolution
    for unsaved notes.
    """
    scope = get_scope_decks(config)
    if not scope:
        return False
    if col is None:
        try:
            from aqt import mw  # type: ignore

            col = mw.col if mw is not None else None
        except Exception:
            col = None
    if col is None:
        return False
    expanded = expand_scope_names(get_all_deck_names(col), scope)
    if not expanded:
        # Every selected deck is missing (renamed/deleted): nothing can
        # match, and the GUI surfaces the missing names as a warning.
        return False
    dids = note_dids(col, getattr(note, "id", None))
    if dids:
        names = _did_to_name(col)
        # ANY card in scope is enough (one-note-one-card mental model).
        return any(names.get(d) in expanded for d in dids)
    # No cards (unsaved Add-window note): use the deck the note WILL be
    # added to (editor's DeckChooser, else curDeck) — mining workflows
    # generate while adding, before the first save. Falls back to the
    # implied-type check when neither is resolvable.
    target = resolve_deck_for_note(note, col, editor=editor)
    if target:
        return any(n in expanded for n in target)
    type_name = _note_type_name(note)
    if not type_name:
        return False
    return type_name in set(implied_note_types(col, scope))


# How many first-field samples to read per deck when guessing whether
# an oddly-named deck holds Japanese. Small on purpose: the guess runs
# once on fresh installs inside the config dialog / profile-open path,
# so per-deck cost stays a single LIMITed SELECT.
_SUGGEST_SAMPLE_LIMIT = 20


def _sample_first_fields(col: Any, dids: Set[int], limit: int) -> List[str]:
    """Returns up to `limit` first-field texts for cards in `dids`.

    Schema-proof (notes/cards only). Never raises — [] on any failure.
    """
    if col is None or not dids:
        return []
    try:
        did_list = ",".join(str(int(d)) for d in sorted(dids))
        rows = col.db.all(
            "SELECT notes.flds FROM notes "
            "JOIN cards ON cards.nid = notes.id "
            f"WHERE cards.did IN ({did_list}) "
            f"LIMIT {int(limit)}"
        ) or []
    except Exception:
        return []
    out: List[str] = []
    for row in rows:
        blob = row[0] if isinstance(row, (list, tuple)) else row
        if isinstance(blob, str) and blob:
            out.append(blob.split("\x1f", 1)[0].strip())
    return out


def suggest_japanese_decks(col: Any) -> List[str]:
    """Guesses which decks hold Japanese cards (fresh-install default).

    Two passes, cheapest first:
    1. Name pass (no DB): decks whose NAME looks Japanese
       (see is_japanese_deck_name).
    2. Content pass (one small LIMITed SELECT per remaining deck):
       decks with kana in sampled first fields. Kana is the
       discriminator — Chinese decks carry kanji but no kana, Western
       decks carry neither.

    Returns names in collection order. [] when the collection is
    unavailable or nothing looks Japanese (caller then keeps the
    fail-closed empty scope + warning). Never raises.
    """
    try:
        all_names = get_all_deck_names(col)
    except Exception:
        return []
    if not all_names or col is None:
        return []
    suggested: List[str] = []
    for name in all_names:
        if is_japanese_deck_name(name):
            suggested.append(name)
    if len(suggested) == len(all_names):
        return suggested
    # Content pass for the oddly-named remainder.
    try:
        name_to_did = _deck_name_to_did(col)
    except Exception:
        return suggested
    for name in all_names:
        if name in suggested:
            continue
        try:
            expanded = expand_scope_names(all_names, [name])
            dids = {name_to_did[n] for n in expanded if n in name_to_did}
            if not dids:
                continue
            for text in _sample_first_fields(col, dids, _SUGGEST_SAMPLE_LIMIT):
                if _KANA_RE.search(text):
                    suggested.append(name)
                    break
        except Exception:
            continue
    # Preserve collection order.
    order = {n: i for i, n in enumerate(all_names)}
    return sorted(set(suggested), key=lambda n: order.get(n, 0))


def maybe_auto_init_scope(config: Optional[Dict[str, Any]], col: Any) -> List[str]:
    """Fresh-install Scope default: Japanese decks pre-selected.

    Returns the scope deck list to USE. When the config was never
    auto-initialized AND holds an empty scope, the Japanese guess
    above becomes the scope (persisted by the caller with the
    auto-initialized flag, so a deliberate user Clear is never
    re-seeded). Otherwise returns the configured scope untouched.
    Never raises; never returns None.
    """
    configured = get_scope_decks(config)
    if configured:
        return configured
    if isinstance(config, dict) and config.get(SCOPE_AUTOINIT_CONFIG_KEY):
        return []
    try:
        return suggest_japanese_decks(col)
    except Exception:
        return []
