import re
import html
import os
import zipfile
from typing import Any, Dict, List, Optional

_RT_RE = re.compile(r'<rt\b[^>]*>.*?</rt>', flags=re.DOTALL | re.IGNORECASE)
_RP_RE = re.compile(r'<rp\b[^>]*>.*?</rp>', flags=re.DOTALL | re.IGNORECASE)
_TAG_RE = re.compile(r'<[^>]+>')

def extract_base_text(html_or_text: str) -> str:
    """Extracts visible base text from HTML, stripping ruby furigana (<rt> tags)."""
    if not html_or_text:
        return ""
    no_rt = _RT_RE.sub("", html_or_text)
    no_rp = _RP_RE.sub("", no_rt)
    plain = _TAG_RE.sub("", no_rp)
    return html.unescape(plain).strip()

def field_is_effectively_empty(field_html: str) -> bool:
    """True when a note field has no visible content.

    An untouched legacy-editor field ships as "<br>" / "<div><br></div>",
    and never-edited AnkiWeb templates look like "&nbsp;\n" — those must
    count as empty so Tab-to-Generate can fill them. But a field holding
    an <img>/<audio>/<video>/etc. is NOT empty: stripping every tag (as
    extract_base_text does) makes "<br><img src=...>" look blank and let
    the fill-empty guard overwrite the user's media (お好み焼き bug).
    """
    if not field_html:
        return True
    text = field_html.strip()
    if not text:
        return True
    # Any embedded media/widget element is real user content.
    if re.search(r'<\s*(img|audio|video|source|svg|object|embed|iframe|canvas|picture)\b',
                 text, flags=re.IGNORECASE):
        return False
    # No media: fall back to visible text after removing ruby readings,
    # tags, and the non-breaking-space placeholders editors emit.
    no_rt = _RT_RE.sub("", text)
    no_rp = _RP_RE.sub("", no_rt)
    plain = _TAG_RE.sub("", no_rp)
    plain = html.unescape(plain).replace("\xa0", " ").strip()
    return not plain


def extract_clean_word(field_text: str) -> str:
    """Extracts the clean target word/expression from a note field."""
    if not field_text:
        return ""

    text = field_text.strip()
    if not text:
        return ""

    text = _RT_RE.sub("", text)
    text = _RP_RE.sub("", text)
    text = _TAG_RE.sub("", text).strip()
    text = html.unescape(text).strip()

    if "[" in text or "［" in text:
        s = text.replace("［", "[").replace("］", "]")
        whole = re.fullmatch(r"([^\[\]]+)\[([^\[\]]+)\]", s)
        if whole:
            text = whole.group(1).strip()
        else:
            text = re.sub(r"\[[^\]]*\]", "", s).strip()

    return text

def parse_furigana_field(field_text: str) -> str:
    """Extracts the pure kana reading from a note field."""
    if not field_text:
        return ""
    text = field_text.strip()

    if "<ruby" in text or "<rt" in text:
        def _ruby_sub(match: re.Match) -> str:
            inner = match.group(0)
            rt = re.search(r"<rt\b[^>]*>(.*?)</rt>", inner, flags=re.DOTALL)
            return re.sub(r"<[^>]+>", "", rt.group(1)) if rt else ""
        kana = re.sub(r"<ruby\b[^>]*>.*?</ruby>", _ruby_sub, text, flags=re.DOTALL)
        kana = re.sub(r"<[^>]+>", "", kana)
        return normalize_reading(kana)

    if "[" in text or "［" in text:
        s = text.replace("［", "[").replace("］", "]")
        whole = re.fullmatch(r"([^\[\]]+)\[([^\[\]]+)\]", s)
        if whole:
            return normalize_reading(whole.group(2))
        result: list = []
        tokens = re.split(r"(\[[^\]]*\])", s)
        for part in tokens:
            if part.startswith("[") and part.endswith("]"):
                if result:
                    prev = result[-1]
                    trimmed = re.sub(r"[\u4e00-\u9fff]+$", "", prev)
                    result[-1] = trimmed
                result.append(part[1:-1])
            else:
                result.append(part)
        return normalize_reading("".join(result))

    if re.fullmatch(r"[\u3040-\u30ff\u30fc\s\-・]+", text):
        return normalize_reading(text)
    if not re.search(r"[\u3040-\u30ff]", text):
        return ""
    if re.search(r"[\u4e00-\u9fff]", text):
        return ""
    return normalize_reading(text)

def normalize_reading(reading: str) -> str:
    if not reading: return ""
    out = []
    for ch in reading:
        code = ord(ch)
        if 0x30A1 <= code <= 0x30F6 or 0x30FD <= code <= 0x30FC:
            out.append(chr(code - 0x60))
        else:
            out.append(ch)
    return re.sub(r"[\s\-・.。_ー()()「」【】]", "", "".join(out))

def resolve_dictionary_paths(
    dictionaries: Optional[List[str]],
    dictionary_folder: str,
    disabled_dictionaries: Optional[List[str]],
) -> List[str]:
    """Resolves the ordered dictionary paths from user config."""
    dictionary_paths: List[str] = []

    if dictionaries and isinstance(dictionaries, list):
        dictionary_paths = [str(p).strip() for p in dictionaries if p and str(p).strip()]

    if not dictionary_paths and dictionary_folder:
        dictionary_paths = find_dictionary_folders(dictionary_folder)

    if disabled_dictionaries and dictionary_paths:
        disabled = {os.path.realpath(os.path.expanduser(str(p))) for p in disabled_dictionaries}
        dictionary_paths = [
            p for p in dictionary_paths
            if os.path.realpath(os.path.expanduser(p)) not in disabled
        ]

    return dictionary_paths

def is_zip_dictionary(path: str) -> bool:
    """Checks if path points to a valid Yomitan dictionary zip archive."""
    if not (path.endswith(".zip") and os.path.isfile(path)):
        return False
    try:
        with zipfile.ZipFile(path, "r") as z:
            names = z.namelist()
            return "index.json" in names or any("term_bank" in n and n.endswith(".json") for n in names)
    except Exception:
        return False

def is_directory_dictionary(path: str) -> bool:
    """Checks if path points to an unzipped Yomitan dictionary directory."""
    if not os.path.isdir(path):
        return False
    try:
        names = os.listdir(path)
        return "index.json" in names or any(n.startswith("term_bank") and n.endswith(".json") for n in names)
    except Exception:
        return False

def find_dictionary_folders(parent_or_dict_path: str) -> List[str]:
    """Discovers all dictionary archives (.zip) and unzipped folders in a path."""
    if not parent_or_dict_path:
        return []

    norm = os.path.realpath(os.path.expanduser(parent_or_dict_path))

    if is_zip_dictionary(norm) or is_directory_dictionary(norm):
        return [norm]

    if not os.path.isdir(norm):
        return []

    found: List[str] = []
    seen_titles: set = set()

    def get_simple_title(p):
        return os.path.basename(p.rstrip("/\\"))

    for entry in sorted(os.listdir(norm)):
        sub = os.path.join(norm, entry)
        if is_directory_dictionary(sub):
            title = get_simple_title(sub)
            if title not in seen_titles:
                found.append(sub)
                seen_titles.add(title)

    for entry in sorted(os.listdir(norm)):
        sub = os.path.join(norm, entry)
        if is_zip_dictionary(sub):
            title = get_simple_title(sub)
            if title not in seen_titles:
                found.append(sub)
                seen_titles.add(title)

    return found


def merge_type_targets(type_mappings: Dict[str, Dict[str, Any]],
                       prev_config: Dict[str, Any]) -> Dict[str, Any]:
    """
    Builds the note-type portion of the config: the multi-type
    'targets' dict plus a legacy mirror (note_type + flat fields) of
    the FIRST target so old configs and hand-edited config.json
    files keep working.

    This is the Qt-free core of the config dialog's collection step
    (gui.py delegates to it after stashing the dropdowns), extracted
    so the crash-safety contract is directly unit-testable:
    - Only COMPLETE mappings (word + definition fields) become
      targets; incomplete ones stay UI-only.
    - An empty UI mapping NEVER clobbers a previously saved targets
      dict (dialog opened but types not yet loaded, Yomitan-toggle
      race — the v1.0.20 "had to redo Note Types" bug).
    - A legacy single-type config is preserved as a target when the
      UI has nothing else.
    """
    targets: Dict[str, Dict[str, str]] = {}
    for type_name, mapping in (type_mappings or {}).items():
        if not isinstance(mapping, dict):
            continue
        word = mapping.get("word_field", "")
        def_f = mapping.get("definition_field", "")
        # Only complete mappings can generate; incomplete ones are
        # kept in the UI but not saved as targets.
        if word and def_f:
            targets[type_name] = {
                "word_field": word,
                "reading_field": mapping.get("reading_field", ""),
                "definition_field": def_f,
            }
    # Never clobber existing targets with empty during early saves
    # (dialog opened but types not yet loaded, or Yomitan toggle race).
    # This was the "had to redo Note Types" bug after v1.0.20.
    prev_targets = (prev_config or {}).get("targets")
    if not targets and isinstance(prev_targets, dict) and prev_targets:
        # Sanity: only preserve if it looks like a valid targets dict
        if any(isinstance(v, dict) and v.get("word_field")
               and v.get("definition_field")
               for v in prev_targets.values()):
            targets = {str(k): dict(v) for k, v in prev_targets.items()
                       if isinstance(v, dict)}
    # Also handle legacy single-type configs that were migrated to targets
    prev_cfg = prev_config or {}
    if (not targets and prev_cfg.get("note_type")
            and prev_cfg.get("word_field")
            and prev_cfg.get("definition_field")):
        # Preserve legacy single-type if we have nothing else
        _legacy_type = str(prev_cfg["note_type"])
        targets = {
            _legacy_type: {
                "word_field": str(prev_cfg.get("word_field") or ""),
                "reading_field": str(prev_cfg.get("reading_field") or ""),
                "definition_field": str(
                    prev_cfg.get("definition_field") or ""),
            }
        }
    first_name = next(iter(targets), "")
    first = targets.get(first_name, {})
    return {
        "targets": targets,
        # Legacy mirror: first configured target in flat form.
        "note_type": first_name,
        "word_field": first.get("word_field", ""),
        "reading_field": first.get("reading_field", ""),
        "definition_field": first.get("definition_field", ""),
    }
