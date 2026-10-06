"""
gui.py - Configuration GUI for the CompreDef Anki add-on.

Provides a PyQt dialog allowing users to:
- Select the Target Note Type and map Target Word & Definition fields
  with intelligent automatic field matching.
- Configure your Dictionaries (add folder/ZIP, drag-and-drop or Move Up/Down
  buttons). Every dictionary contributes its definitions; the most
  readable one wins regardless of order (order-independent scoring).
  Recommended: include the richest dictionaries you can almost read.
"""

import os
from typing import Optional, Dict, Any, List
from aqt import mw
from aqt.utils import tooltip
from aqt.qt import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QFormLayout,
    QComboBox,
    QPushButton,
    QCheckBox,
    QFileDialog,
    QDialogButtonBox,
    QWidget,
    QListWidget,
    QListWidgetItem,
    QAbstractItemView,
    QTableWidget,
    QTableWidgetItem,
    QHeaderView,
    Qt,
    QLabel,
    QGroupBox,
    QLineEdit,
    QTabWidget,
    QGridLayout,
)

from .core import get_provider
from .anki import reset_caches as _reset_knowledge_caches
from .utils import (
    find_dictionary_folders,
    is_zip_dictionary,
    merge_type_targets,
)
from .provider import IndexingError
from .parser import get_single_dictionary
from .scope import (
    SCOPE_CONFIG_KEY,
    SCOPE_AUTOINIT_CONFIG_KEY,
    get_scope_decks,
    get_all_deck_names,
    implied_note_types,
    missing_scope_decks,
    maybe_auto_init_scope,
)


# Cross-version PyQt5/PyQt6 enum helpers
def _user_role() -> int:
    """Returns Qt.UserRole / Qt.ItemDataRole.UserRole across PyQt5 and PyQt6."""
    if hasattr(Qt, "ItemDataRole") and hasattr(Qt.ItemDataRole, "UserRole"):
        return Qt.ItemDataRole.UserRole
    return Qt.UserRole


def _internal_move_mode() -> Any:
    """Returns QAbstractItemView.DragDropMode.InternalMove across PyQt5 and PyQt6."""
    if hasattr(QAbstractItemView, "DragDropMode") and hasattr(QAbstractItemView.DragDropMode, "InternalMove"):
        return QAbstractItemView.DragDropMode.InternalMove
    return QAbstractItemView.InternalMove


def _user_checkable_flag() -> Any:
    """Returns Qt.ItemIsUserCheckable across PyQt5 and PyQt6.

    PyQt6 scoped the enums (Qt.ItemFlag.ItemIsUserCheckable); the raw
    top-level spelling crashed the config dialog on Anki's Qt6 builds
    (production bug, v1.0.14).
    """
    if hasattr(Qt, "ItemFlag") and hasattr(Qt.ItemFlag, "ItemIsUserCheckable"):
        return Qt.ItemFlag.ItemIsUserCheckable
    return Qt.ItemIsUserCheckable


def _size_button(btn: QPushButton) -> QPushButton:
    """Applies uniform comfortable sizing so buttons never render cramped.

    Without explicit minimums, Qt compresses buttons below their natural
    height whenever the dialog runs out of vertical space (the 'cramped
    buttons' problem). Minimums make the layout grow the dialog instead
    of squashing the widgets.
    """
    btn.setMinimumWidth(140)
    btn.setMinimumHeight(30)
    return btn


# Knowledge-dialog table helpers (PyQt5/PyQt6 cross-version)
def _table_select_rows_flag() -> Any:
    """QAbstractItemView.SelectionBehavior.SelectRows across Qt versions."""
    if hasattr(QAbstractItemView, "SelectionBehavior") and hasattr(
            QAbstractItemView.SelectionBehavior, "SelectRows"):
        return QAbstractItemView.SelectionBehavior.SelectRows
    return QAbstractItemView.SelectRows


def _table_single_selection_flag() -> Any:
    """QAbstractItemView.SelectionMode.SingleSelection across versions."""
    if hasattr(QAbstractItemView, "SelectionMode") and hasattr(
            QAbstractItemView.SelectionMode, "SingleSelection"):
        return QAbstractItemView.SelectionMode.SingleSelection
    return QAbstractItemView.SingleSelection


def _header_resize_stretch_flag() -> Any:
    """QHeaderView.ResizeMode.Stretch across PyQt5/PyQt6."""
    if hasattr(QHeaderView, "ResizeMode") and hasattr(
            QHeaderView.ResizeMode, "Stretch"):
        return QHeaderView.ResizeMode.Stretch
    return QHeaderView.Stretch


def _header_resize_resize_to_contents_flag() -> Any:
    """QHeaderView.ResizeMode.ResizeToContents across versions."""
    if hasattr(QHeaderView, "ResizeMode") and hasattr(
            QHeaderView.ResizeMode, "ResizeToContents"):
        return QHeaderView.ResizeMode.ResizeToContents
    return QHeaderView.ResizeToContents


# Keyword lists for auto-matching target word and definition fields
_TARGET_WORD_KEYWORDS = [
    "word", "expression", "kanji", "reading", "furigana",
    "hiragana", "romaji", "katakana", "jp", "japanese", "ja"
]

# Fields that may carry the word's kana reading (furigana markup or plain
# kana). The engine parses these to disambiguate homographs like
# 先ず(まず) vs 先ず(せんず).
_READING_FIELD_KEYWORDS = [
    "furigana", "reading", "kana", "hiragana", "katakana",
    "readingfield", "pronunciation", "yomi", "読み",
]

# Fields that are the word itself with readings embedded (e.g. 先[ま]ず
# inside the Expression field) also count as a reading source.
_READING_KEYWORDS_SECONDARY = ["expression", "word", "front"]

_DEFINITION_KEYWORDS = [
    "definition", "meaning", "glossary", "translation",
    "translation_", "explanation", "sense", "desc"
]


def _get_addon_name() -> str:
    """
    Safely retrieves the root Anki add-on name for config persistence.
    """
    if hasattr(mw, 'addonManager'):
        root_name = mw.addonManager.addonFromModule(__name__)
        if root_name:
            return root_name
    return __name__.split('.')[0]


def _find_best_field_match(fields: List[str], keywords: List[str], fallback: Optional[str] = None) -> Optional[str]:
    """
    Finds the best matching field name based on keyword similarity.
    """
    if not fields or not keywords:
        return fallback

    fields_lower = [f.lower().replace("_", " ").replace("-", " ") for f in fields]

    for keyword in keywords:
        for i, f_lower in enumerate(fields_lower):
            if f_lower == keyword.lower():
                return fields[i]
        for i, f_lower in enumerate(fields_lower):
            if keyword.lower() in f_lower:
                return fields[i]

    return fallback


def _indent_deck_name(name: str, all_names: List[str]) -> tuple:
    """Returns (display_name, has_children) for hierarchical display.

    Subdecks get indented one level per '::' so the tree structure is
    visible in a flat list; decks that contain children get a marker so
    users see that checking them covers everything below (the v1.1.2
    confusion: a leaf was checked while the note lived in a SIBLING
    deck — invisible in a flat list).
    """
    depth = name.count("::")
    indent = "    " * depth
    base = name.rsplit("::", 1)[-1]
    disp = f"{indent}{base}"
    has_children = any(
        n != name and n.startswith(name + "::") for n in all_names
    )
    return disp, has_children


class ScopeDialog(QDialog):
    """
    Stand-alone Scope picker — also the inline engine behind the
    Scope tab in ConfigDialog. Polished: header banner, filter,
    per-deck card counts, live summary, missing-deck warning.
    Returns the checked deck names via selected_decks().
    """

    def __init__(
        self,
        parent: Optional[QWidget],
        deck_counts: List[tuple],
        selected: List[str],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("CompreDef — Scope")
        self.resize(560, 520)
        self._build_ui(deck_counts, selected)

    def _build_ui(self, deck_counts: List[tuple], selected: List[str]) -> None:
        layout = QVBoxLayout()
        layout.setSpacing(10)
        self.setLayout(layout)

        # Banner
        title = QLabel("<b>Scope — which decks count for knowledge</b>")
        layout.addWidget(title)
        hint = QLabel(
            "Only cards in checked decks count for word / kanji knowledge "
            "(the scoring weights behind definition ranking).<br>"
            "Generation itself needs no Scope: the editor's CD toggle + Tab "
            "fills any card's empty Definition.<br>"
            "Checking a deck covers it <b>and all of its subdecks</b>, but "
            "<b>not sibling decks</b> — check the shared parent to cover a whole branch. "
            "Empty scope means empty knowledge (scoring falls back to maximal readability)."
        )
        hint.setTextFormat(Qt.TextFormat.RichText if hasattr(Qt, "TextFormat") else 1)  # type: ignore
        hint.setStyleSheet("color: gray; font-size: 11px;")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # Filter + bulk actions
        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self.filter_edit = QLineEdit()
        self.filter_edit.setPlaceholderText("type to filter decks…")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        filter_row.addWidget(self.filter_edit, stretch=1)
        select_all_btn = _size_button(QPushButton("Select All"))
        select_all_btn.setMinimumWidth(110)
        select_all_btn.clicked.connect(lambda: self._set_all(True))
        filter_row.addWidget(select_all_btn)
        clear_btn = _size_button(QPushButton("Clear"))
        clear_btn.setMinimumWidth(90)
        clear_btn.clicked.connect(lambda: self._set_all(False))
        filter_row.addWidget(clear_btn)
        layout.addLayout(filter_row)

        self.deck_list = QListWidget()
        self.deck_list.setMinimumHeight(260)
        self.deck_list.setToolTip("Check decks to include them in the Scope.")
        role = _user_role()
        all_names = [n for n, _ in deck_counts]
        for name, count in deck_counts:
            disp, has_children = _indent_deck_name(name, all_names)
            # Tristate display: label parents so the subdeck rule is visible.
            suffix = "  ▸ covers all subdecks" if has_children else ""
            label = f"{disp}  —  {count:,} cards{suffix}"
            item = QListWidgetItem(label)
            item.setFlags(item.flags() | _user_checkable_flag())
            item.setCheckState(
                Qt.CheckState.Checked if name in selected
                else Qt.CheckState.Unchecked
            )
            # Full name in tooltip + role data (display is truncated).
            item.setData(role, name)
            item.setToolTip(f"{name}\n({count:,} cards)"
                            + ("\nChecking this covers ALL of its subdecks too." if has_children else ""))
            # Dim empty decks slightly
            if count == 0:
                item.setForeground(Qt.GlobalColor.gray if hasattr(Qt, "GlobalColor") else item.foreground())  # type: ignore
            self.deck_list.addItem(item)
        layout.addWidget(self.deck_list, stretch=1)

        self.summary_label = QLabel("")
        self.summary_label.setStyleSheet("color: #2a7d4f; font-size: 11px;")
        self.summary_label.setWordWrap(True)
        layout.addWidget(self.summary_label)
        self.warning_label = QLabel("")
        self.warning_label.setStyleSheet("color: #c0392b; font-size: 11px;")
        self.warning_label.setWordWrap(True)
        layout.addWidget(self.warning_label)
        self._refresh_status()

        # Live updates: any check change refreshes summary
        self.deck_list.itemChanged.connect(lambda _: self._refresh_status())

        if hasattr(QDialogButtonBox, "StandardButton"):
            ok_flag = QDialogButtonBox.StandardButton.Ok
            cancel_flag = QDialogButtonBox.StandardButton.Cancel
        else:
            ok_flag = QDialogButtonBox.Ok
            cancel_flag = QDialogButtonBox.Cancel
        button_box = QDialogButtonBox(ok_flag | cancel_flag)
        button_box.accepted.connect(self.accept)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _apply_filter(self, text: str) -> None:
        """Hides decks not matching the filter (selection preserved)."""
        needle = text.strip().lower()
        role = _user_role()
        for i in range(self.deck_list.count()):
            item = self.deck_list.item(i)
            # Match against the FULL name (display is truncated/indented).
            name = str(item.data(role) or "")
            item.setHidden(bool(needle) and needle not in name.lower())

    def _set_all(self, checked: bool) -> None:
        """Checks/unchecks every deck, including filter-hidden ones."""
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.deck_list.blockSignals(True)
        for i in range(self.deck_list.count()):
            self.deck_list.item(i).setCheckState(state)
        self.deck_list.blockSignals(False)
        self._refresh_status()

    def _refresh_status(self) -> None:
        """Live summary + missing-deck detection."""
        sel = self.selected_decks()
        if not sel:
            self.summary_label.setText("Scope: <b>none</b> — knowledge empty (generation still works via the CD toggle + Tab).")
            self.summary_label.setStyleSheet("color: #c0392b; font-size: 11px;")
            self.warning_label.setText("Knowledge is empty: scoring cannot personalize until you pick at least one Japanese deck above.")
            self.warning_label.setVisible(True)
            return
        self.summary_label.setStyleSheet("color: #2a7d4f; font-size: 11px;")
        self.summary_label.setText(f"Scope: <b>{len(sel)} deck(s)</b> — {', '.join(sel)}")
        # Missing detection (renamed/deleted)
        try:
            all_names = get_all_deck_names(mw.col if mw else None)
            missing = missing_scope_decks(all_names, sel)
        except Exception:
            missing = []
        if missing:
            self.warning_label.setText("Missing (renamed/deleted?): " + ", ".join(missing))
            self.warning_label.setVisible(True)
        else:
            self.warning_label.setVisible(False)

    def selected_decks(self) -> List[str]:
        """Returns the checked deck names in list order."""
        role = _user_role()
        out: List[str] = []
        for i in range(self.deck_list.count()):
            item = self.deck_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(str(item.data(role) or ""))
        return [n for n in out if n]


class ConfigDialog(QDialog):
    """
    Dialog for configuring CompreDef add-on options.

    Allows picking the Scope decks, mapping note-type fields, and
    managing the dictionary set.
    """

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        # Show version in title so user can verify update instantly
        try:
            import json as _j
            _v = "?"
            _mp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json")
            if os.path.isfile(_mp):
                with open(_mp, "r", encoding="utf-8") as _mf:
                    _v = _j.load(_mf).get("human_version") or _v
            if _v == "?":
                _vp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
                if os.path.isfile(_vp):
                    with open(_vp, "r", encoding="utf-8") as _vf:
                        _v = _vf.read().strip().lstrip("v")
            self.setWindowTitle(f"CompreDef Configuration — v{_v}" if _v != "?" else "CompreDef Configuration")
        except Exception:
            self.setWindowTitle("CompreDef Configuration")
        self.resize(660, 680)

        self.addon_name = _get_addon_name()
        self.config: Dict[str, Any] = mw.addonManager.getConfig(self.addon_name) or {}

        # Paths the user unchecked: kept in config (order preserved) but
        # skipped during generation. Stored as a set for O(1) toggling.
        self.disabled_dicts: set = set()

        # Scope deck names + per-type field mappings. Both are populated
        # by _load_config(); pre-initialised here because dictionary
        # installs during load persist dialog state immediately
        # (crash-safety) and those early saves read both attributes.
        self.scope_decks: List[str] = []
        self.type_mappings: Dict[str, Dict[str, Any]] = {}
        self._active_type: Optional[str] = None

        self._init_ui()
        self._load_config()

    def _init_ui(self) -> None:
        """Sets up the form controls — tabbed so no single pane is cramped."""
        main_layout = QVBoxLayout()
        self.setLayout(main_layout)
        main_layout.setSpacing(8)
        main_layout.setContentsMargins(8, 8, 8, 8)

        self.tabs = QTabWidget()
        main_layout.addWidget(self.tabs, stretch=1)

        # -------------------------------------------------------------
        # Tab 0 — Scope (polished inline picker, its own visual window)
        # -------------------------------------------------------------
        scope_tab = QWidget()
        scope_tab_layout = QVBoxLayout()
        scope_tab_layout.setSpacing(8)
        scope_tab.setLayout(scope_tab_layout)

        scope_title = QLabel("<b>Scope — which decks count for knowledge</b>")
        scope_tab_layout.addWidget(scope_title)
        scope_hint = QLabel(
            "Only cards in checked decks count for word / kanji knowledge "
            "(the scoring weights behind definition ranking).<br>"
            "Generation itself needs no Scope: the editor's CD toggle + Tab "
            "fills any card's empty Definition.<br>"
            "Checking a deck covers it <b>and all of its subdecks</b>, but "
            "<b>not sibling decks</b> — check the shared parent to cover a whole branch. "
            "Empty scope means empty knowledge (scoring falls back to maximal readability)."
        )
        # RichText where available
        try:
            scope_hint.setTextFormat(Qt.TextFormat.RichText)  # type: ignore
        except Exception:
            pass
        scope_hint.setStyleSheet("color: gray; font-size: 11px;")
        scope_hint.setWordWrap(True)
        scope_tab_layout.addWidget(scope_hint)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Filter:"))
        self.scope_filter_edit = QLineEdit()
        self.scope_filter_edit.setPlaceholderText("type to filter decks…")
        self.scope_filter_edit.setClearButtonEnabled(True)
        self.scope_filter_edit.textChanged.connect(self._on_scope_filter)
        filter_row.addWidget(self.scope_filter_edit, stretch=1)
        # Bulk actions live next to the filter (not in a cramped side column)
        self.scope_select_all_btn = QPushButton("Select All")
        self.scope_select_all_btn.setMinimumHeight(28)
        self.scope_select_all_btn.clicked.connect(lambda: self._set_scope_all(True))
        filter_row.addWidget(self.scope_select_all_btn)
        self.scope_clear_btn = QPushButton("Clear")
        self.scope_clear_btn.setMinimumHeight(28)
        self.scope_clear_btn.setToolTip("Empty the scope (disables everything).")
        self.scope_clear_btn.clicked.connect(self._on_clear_scope)
        filter_row.addWidget(self.scope_clear_btn)
        scope_tab_layout.addLayout(filter_row)

        self.scope_deck_list = QListWidget()
        self.scope_deck_list.setMinimumHeight(220)
        self.scope_deck_list.setToolTip("Check decks to include them in the Scope.")
        self.scope_deck_list.itemChanged.connect(self._on_scope_deck_changed)
        scope_tab_layout.addWidget(self.scope_deck_list, stretch=1)

        self.scope_summary_label = QLabel("Scope: none")
        self.scope_summary_label.setStyleSheet("color: #2a7d4f; font-size: 11px;")
        self.scope_summary_label.setWordWrap(True)
        scope_tab_layout.addWidget(self.scope_summary_label)
        self.scope_warning_label = QLabel("")
        self.scope_warning_label.setStyleSheet("color: #c0392b; font-size: 11px;")
        self.scope_warning_label.setWordWrap(True)
        scope_tab_layout.addWidget(self.scope_warning_label)
        self.scope_implied_label = QLabel("")
        self.scope_implied_label.setStyleSheet("color: gray; font-size: 11px;")
        self.scope_implied_label.setWordWrap(True)
        scope_tab_layout.addWidget(self.scope_implied_label)

        # Also keep the pop-out button for users who prefer a separate window
        popout_row = QHBoxLayout()
        self.scope_popout_btn = QPushButton("Open as separate window…")
        self.scope_popout_btn.setToolTip("Open the same picker in a dedicated window.")
        self.scope_popout_btn.clicked.connect(self._on_select_decks)
        popout_row.addWidget(self.scope_popout_btn)
        popout_row.addStretch()
        scope_tab_layout.addLayout(popout_row)

        self.tabs.addTab(scope_tab, "Scope")

        # -------------------------------------------------------------
        # Tab 1 — Field Mappings (types implied by the Scope)
        # -------------------------------------------------------------
        mapping_tab = QWidget()
        mapping_tab_layout = QVBoxLayout()
        mapping_tab.setLayout(mapping_tab_layout)

        mapping_title = QLabel("<b>Field Mapping</b> — how each note type is read")
        mapping_tab_layout.addWidget(mapping_title)
        intro = QLabel(
            "Note types are <b>implied by your Scope</b> for knowledge — but "
            "generation works for <b>any</b> note type: the CD toggle + Tab "
            "uses the mapping below, auto-inferring when a type has none. "
            "Pick a type to check / fix its field mapping. Auto-detected "
            "mappings work immediately; you only need to edit if the guess "
            "is wrong."
        )
        try:
            intro.setTextFormat(Qt.TextFormat.RichText)  # type: ignore
        except Exception:
            pass
        intro.setStyleSheet("color: gray; font-size: 11px;")
        intro.setWordWrap(True)
        mapping_tab_layout.addWidget(intro)

        # Single-select list of in-scope note types (no checkboxes: deck
        # membership decides, this list only maps fields per type).
        self.note_types_list = QListWidget()
        self.note_types_list.setMinimumHeight(96)
        self.note_types_list.setToolTip(
            "Note types found in the Scope decks.\n"
            "Select a row to view/edit its field mapping below."
        )
        self.note_types_list.currentRowChanged.connect(
            lambda _row: self._on_type_selected()
        )
        mapping_tab_layout.addWidget(self.note_types_list)

        mapping_form = QFormLayout()
        mapping_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.ExpandingFieldsGrow)  # type: ignore
        self.word_field_combo = QComboBox()
        mapping_form.addRow("Target Word Field:", self.word_field_combo)

        self.reading_field_combo = QComboBox()
        self.reading_field_combo.setToolTip(
            "Optional. Field containing the word's reading (plain kana or furigana\n"
            "markup like 先[ま]ず). Used to pick the correct definition for words\n"
            "with multiple readings (e.g. 先ず read まず vs せんず)."
        )
        mapping_form.addRow("Reading Field (optional):", self.reading_field_combo)

        self.definition_field_combo = QComboBox()
        mapping_form.addRow("Definition Field:", self.definition_field_combo)
        mapping_tab_layout.addLayout(mapping_form)

        # Subtle hint for auto-inferred case
        self.mapping_hint = QLabel("")
        self.mapping_hint.setStyleSheet("color: gray; font-size: 10px;")
        self.mapping_hint.setWordWrap(True)
        mapping_tab_layout.addWidget(self.mapping_hint)

        self.tabs.addTab(mapping_tab, "Fields")

        # -------------------------------------------------------------
        # Tab 2 — Dictionaries
        # -------------------------------------------------------------
        dicts_tab = QWidget()
        dicts_layout = QVBoxLayout()
        dicts_tab.setLayout(dicts_layout)
        dicts_header = QLabel("<b>Dictionaries</b> — every entry is scored, the most readable wins")
        dicts_layout.addWidget(dicts_header)

        # --- Dictionary Source selector (Local vs Yomitan) ---
        source_row = QHBoxLayout()
        source_row.addWidget(QLabel("Dictionary Source:"))
        self.source_combo = QComboBox()
        self.source_combo.addItem("Local dictionaries (indexed)", "local")
        self.source_combo.addItem("Yomitan API (live, all Yomitan dictionaries)", "yomitan")
        self.source_combo.setToolTip(
            "Local: use indexed dictionaries below (fast, offline).\n"
            "Yomitan API: borrow Yomitan's dictionaries live via http://127.0.0.1:19633\n"
            "(browser must be open + Yomitan API enabled + one-time 'python install_yomitan_api.py').\n"
            "Without the bridge the Test below will stay red — Local is simpler for most users."
        )
        # Restore at creation for crash-safety (same as tab_generate)
        _src = str(self.config.get("dictionary_source") or "local").strip().lower()
        if _src not in ("local", "yomitan"):
            _src = "local"
        for i in range(self.source_combo.count()):
            if self.source_combo.itemData(i) == _src:
                self.source_combo.setCurrentIndex(i)
                break
        self.source_combo.currentIndexChanged.connect(self._on_source_changed)
        source_row.addWidget(self.source_combo)
        source_row.addStretch()
        dicts_layout.addLayout(source_row)

        # --- Yomitan settings row (visible only when Yomitan selected) ---
        self.yomitan_settings_widget = QWidget()
        yomitan_layout = QHBoxLayout()
        yomitan_layout.setContentsMargins(0, 0, 0, 0)
        self.yomitan_settings_widget.setLayout(yomitan_layout)
        yomitan_layout.addWidget(QLabel("Yomitan URL:"))
        self.yomitan_url_edit = QLineEdit()
        self.yomitan_url_edit.setPlaceholderText("http://127.0.0.1:19633")
        self.yomitan_url_edit.setText(str(self.config.get("yomitan_url") or "http://127.0.0.1:19633"))
        self.yomitan_url_edit.setToolTip("Yomitan API bridge URL (default 127.0.0.1:19633). Change only if you edited yomitan_api.py ADDR/PORT.")
        yomitan_layout.addWidget(self.yomitan_url_edit, stretch=1)
        # Second row: extension ID (for custom/source builds) + debug
        yomitan_row2 = QHBoxLayout()
        yomitan_row2.setContentsMargins(0, 0, 0, 0)
        yomitan_row2.addWidget(QLabel("Extension ID (only if custom build):"))
        self.yomitan_extid_edit = QLineEdit()
        self.yomitan_extid_edit.setPlaceholderText("chrome-extension://.../  (leave empty for store install)")
        self.yomitan_extid_edit.setText(str(self.config.get("yomitan_extension_id") or ""))
        self.yomitan_extid_edit.setToolTip(
            "Only needed if you built Yomitan from source (its extension ID differs).\n"
            "Find it on the Yomitan settings page URL (chrome-extension://<id>/settings.html)\n"
            "or about:debugging in Firefox. Store installs work with empty field."
        )
        yomitan_row2.addWidget(self.yomitan_extid_edit, stretch=1)
        self.yomitan_debug_btn = QPushButton("Copy Debug Info")
        self.yomitan_debug_btn.setMinimumHeight(30)
        self.yomitan_debug_btn.setToolTip("Collects bridge manifests, serverVersion, ankiFields for 口, error.log tail into clipboard + console. Paste it back when reporting.")
        self.yomitan_debug_btn.clicked.connect(self._on_copy_yomitan_debug)
        yomitan_row2.addWidget(self.yomitan_debug_btn)
        dicts_layout.addLayout(yomitan_row2)
        self.yomitan_install_btn = _size_button(QPushButton("Install / Repair Bridge"))
        self.yomitan_install_btn.setToolTip(
            "One-click install of the Yomitan bridge (native messaging host).\n"
            "Writes the manifest for Chrome/Firefox/Brave/Edge so Yomitan's\n"
            "'Enable Yomitan API' actually exposes http://127.0.0.1:19633.\n"
            "No terminal needed — just click, then restart browser."
        )
        self.yomitan_install_btn.clicked.connect(self._on_install_yomitan_bridge)
        yomitan_layout.addWidget(self.yomitan_install_btn)
        self.yomitan_test_btn = _size_button(QPushButton("Test"))
        self.yomitan_test_btn.setToolTip("Ping Yomitan API (/serverVersion). Browser must be open + Yomitan API enabled + bridge installed.")
        self.yomitan_test_btn.clicked.connect(self._on_test_yomitan)
        yomitan_layout.addWidget(self.yomitan_test_btn)
        dicts_layout.addWidget(self.yomitan_settings_widget)

        self.yomitan_status_label = QLabel("")
        self.yomitan_status_label.setStyleSheet("color: gray; font-size: 11px;")
        self.yomitan_status_label.setWordWrap(True)
        dicts_layout.addWidget(self.yomitan_status_label)

        desc_label = QLabel(
            "Dictionaries are tried top to bottom; the first definition you can\n"
            "fully read (100% known kanji) wins. Recommended: put the richest\n"
            "dictionary you can comfortably read at the top. The gate counts\n"
            "kanji only — kana is never checked."
        )
        desc_label.setStyleSheet("color: gray; font-size: 11px;")
        desc_label.setWordWrap(True)
        dicts_layout.addWidget(desc_label)

        # List Widget with drag-and-drop reordering
        list_and_buttons_layout = QHBoxLayout()

        # --- Left/center: the dictionary list itself ---
        list_column = QVBoxLayout()

        # Add-actions sit in ONE compact row ABOVE the list (they were a
        # 3-button stack in the side column, which wasted vertical budget
        # and made the dialog cram everything below it).
        add_row = QHBoxLayout()
        self.add_zip_btn = _size_button(QPushButton("Add Zip..."))
        self.add_zip_btn.setToolTip("Select a Yomitan dictionary .zip file")
        self.add_zip_btn.clicked.connect(self._on_add_zip)
        add_row.addWidget(self.add_zip_btn)

        self.add_dict_btn = _size_button(QPushButton("Add Folder..."))
        self.add_dict_btn.setToolTip("Select a single unzipped dictionary folder")
        self.add_dict_btn.clicked.connect(self._on_add_dictionary)
        add_row.addWidget(self.add_dict_btn)

        self.add_folder_btn = _size_button(QPushButton("Scan Folder..."))
        self.add_folder_btn.setToolTip(
            "Scan a parent folder to automatically find and add all "
            "dictionary archives (.zip) and subfolders"
        )
        self.add_folder_btn.clicked.connect(self._on_scan_folder)
        add_row.addWidget(self.add_folder_btn)

        add_row.addStretch()
        list_column.addLayout(add_row)

        self.dict_list = QListWidget()
        self.dict_list.setDragDropMode(_internal_move_mode())
        self.dict_list.setMinimumHeight(120)
        # Track checkbox changes to update disabled_dicts
        self.dict_list.itemChanged.connect(self._on_item_changed)
        self.dict_list.model().rowsMoved.connect(lambda *_: self._refresh_item_labels())
        list_column.addWidget(self.dict_list)
        list_and_buttons_layout.addLayout(list_column, stretch=1)

        # --- Right: list-management actions only ---
        buttons_vbox = QVBoxLayout()

        self.move_up_btn = _size_button(QPushButton("Move Up ↑"))
        self.move_up_btn.setToolTip("Move selected dictionary up in the list")
        self.move_up_btn.clicked.connect(self._on_move_up)
        buttons_vbox.addWidget(self.move_up_btn)

        self.move_down_btn = _size_button(QPushButton("Move Down ↓"))
        self.move_down_btn.setToolTip("Move selected dictionary down in the list")
        self.move_down_btn.clicked.connect(self._on_move_down)
        buttons_vbox.addWidget(self.move_down_btn)

        buttons_vbox.addSpacing(10)

        self.remove_btn = _size_button(QPushButton("Remove"))
        self.remove_btn.setToolTip("Remove selected dictionary (and its index)")
        self.remove_btn.clicked.connect(self._on_remove_dictionary)
        buttons_vbox.addWidget(self.remove_btn)

        self.reindex_btn = _size_button(QPushButton("Reinstall / Update"))
        self.reindex_btn.setToolTip(
            "Re-parse the selected dictionary and rebuild its index.\n"
            "Use this after replacing a dictionary's files on disk.\n"
            "Runs in the background with progress."
        )
        self.reindex_btn.clicked.connect(self._on_reindex_dictionary)
        buttons_vbox.addWidget(self.reindex_btn)

        buttons_vbox.addStretch()
        list_and_buttons_layout.addLayout(buttons_vbox)

        dicts_layout.addLayout(list_and_buttons_layout)
        self.tabs.addTab(dicts_tab, "Dictionaries")

        # -------------------------------------------------------------
        # Tab 3 — Generation / Options
        # -------------------------------------------------------------
        gen_tab = QWidget()
        generation_layout = QVBoxLayout()
        gen_tab.setLayout(generation_layout)
        gen_header = QLabel("<b>Generation</b> — how definitions are created")
        generation_layout.addWidget(gen_header)

        # Tab-to-Generate: auto-fill the definition when the word field is
        # unfocused with an empty definition (restored feature — see
        # editor_browser.py for the stability contract).
        self.tab_generate_check = QCheckBox(
            "Auto-generate (CD toolbar toggle): fill empty definition when leaving the word field "
            "(Tab / clicking away)"
        )
        self.tab_generate_check.setToolTip(
            "The global CD toggle (same switch as the editor toolbar's CD "
            "button). When ON, unfocusing the word field automatically generates a "
            "definition\nif (and only if) the definition field is empty. Existing "
            "definitions are never\noverwritten — clear the field first to regenerate, "
            "or use Browser bulk."
        )
        # CRITICAL: restore the saved state AT CREATION TIME, before any
        # _save_config_now() can fire. _load_config() restores the dictionary
        # set AFTER _init_ui(), and each added dictionary persists the
        # dialog state immediately (crash-safety design). With the default
        # unchecked Qt state, merely OPENING the dialog used to write
        # tab_generate=False to disk before the real value was ever shown.
        self.tab_generate_check.setChecked(bool(self.config.get("tab_generate", True)))
        generation_layout.addWidget(self.tab_generate_check)

        self.plain_text_check = QCheckBox(
            "Plain-text definitions: store plain text instead of Yomitan HTML"
        )
        self.plain_text_check.setToolTip(
            "When enabled, NEW dictionary installs render definitions as plain\n"
            "text (no <ruby>, <span>, data-sc-* or inline CSS). Existing\n"
            "indexed entries are converted on-the-fly via cheap HTML stripping,\n"
            "so toggling does not require a full re-index — but new installs\n"
            "skip HTML generation entirely (saves the costly HTML build)."
        )
        # Same crash-safety timing as tab_generate: restore at creation.
        self.plain_text_check.setChecked(bool(self.config.get("plain_text_definitions", False)))
        generation_layout.addWidget(self.plain_text_check)
        generation_layout.addStretch()
        self.tabs.addTab(gen_tab, "Options")

        # -------------------------------------------------------------
        # OK / Cancel Dialog Buttons
        # -------------------------------------------------------------
        if hasattr(QDialogButtonBox, "StandardButton"):
            ok_flag = QDialogButtonBox.StandardButton.Ok
            cancel_flag = QDialogButtonBox.StandardButton.Cancel
        else:
            ok_flag = QDialogButtonBox.Ok
            cancel_flag = QDialogButtonBox.Cancel

        button_box = QDialogButtonBox(ok_flag | cancel_flag)
        button_box.accepted.connect(self._save_and_accept)
        button_box.rejected.connect(self.reject)
        main_layout.addWidget(button_box)

        # Footer: version + AnkiWeb link (user asked how to check version easily)
        try:
            import json as _json
            _ver = "?"
            _manifest_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "manifest.json")
            if os.path.isfile(_manifest_path):
                with open(_manifest_path, "r", encoding="utf-8") as _mf:
                    _ver = _json.load(_mf).get("human_version") or _ver
            if _ver == "?":
                _ver_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "VERSION")
                if os.path.isfile(_ver_path):
                    with open(_ver_path, "r", encoding="utf-8") as _vf:
                        _ver = _vf.read().strip().lstrip("v")
            footer = QLabel(f'CompreDef v{_ver} — <a href="https://ankiweb.net/shared/info/1619602654">AnkiWeb</a> • <a href="https://github.com/mansourvery-hub/CompreDef">GitHub</a>')
            footer.setOpenExternalLinks(True)
            footer.setStyleSheet("color: gray; font-size: 10px;")
            footer.setAlignment(Qt.AlignmentFlag.AlignCenter if hasattr(Qt, "AlignmentFlag") else Qt.AlignCenter)
            main_layout.addWidget(footer)
        except Exception:
            pass

    def _get_field_names(self, note_type_name: str) -> List[str]:
        """Retrieves field names for the given note type using mw.col.models."""
        if not mw or not mw.col or not note_type_name:
            return []
        model = mw.col.models.by_name(note_type_name)
        if not model or "flds" not in model:
            return []
        return [field["name"] for field in model["flds"]]

    def _on_note_type_changed(self, note_type_name: str) -> None:
        """Populates the field dropdowns for a note type and auto-matches.

        Called when a type's row is selected or checked for the first
        time. Auto-match runs only for types without saved mappings —
        saved mappings survive every re-selection untouched.
        """
        fields = self._get_field_names(note_type_name)

        # Store previous selections
        prev_word = self.word_field_combo.currentText()
        prev_reading = self.reading_field_combo.currentText()
        prev_def = self.definition_field_combo.currentText()

        # Update dropdown items
        self.word_field_combo.clear()
        self.word_field_combo.addItems(fields)

        self.reading_field_combo.blockSignals(True)
        self.reading_field_combo.clear()
        self.reading_field_combo.addItem("")
        self.reading_field_combo.addItems(fields)
        self.reading_field_combo.blockSignals(False)

        self.definition_field_combo.clear()
        self.definition_field_combo.addItems(fields)

        # Compute best matches for new fields
        auto_word = _find_best_field_match(fields, _TARGET_WORD_KEYWORDS)

        # Prefer dedicated reading/furigana field, else word field itself
        auto_reading = _find_best_field_match(fields, _READING_FIELD_KEYWORDS) or \
                       (auto_word if auto_word else "")

        # Definition usually not the word field
        remaining_fields = [f for f in fields if f != auto_word]
        auto_def = _find_best_field_match(remaining_fields, _DEFINITION_KEYWORDS)

        # Restore previous or set auto-match
        self.word_field_combo.setCurrentText(prev_word if prev_word in fields else (auto_word or ""))
        self.reading_field_combo.setCurrentText(prev_reading if prev_reading in fields else (auto_reading or ""))
        self.definition_field_combo.setCurrentText(prev_def if prev_def in fields else (auto_def or ""))

    # ------------------------------------------------------------------
    # Multi-note-type state: {type_name: {'word_field', 'reading_field',
    # 'definition_field', '_auto': True until user edits anything}}
    # ------------------------------------------------------------------

    def _deck_card_counts(self) -> List[tuple]:
        """Returns [(deck_name, card_count)] for the Scope picker.

        Counts come from a single GROUP BY query; deck names via the
        public decks API. Never raises (picker shows zero counts rather
        than crashing the dialog).
        """
        counts: Dict[int, int] = {}
        try:
            if mw and mw.col:
                rows = mw.col.db.all(
                    "SELECT did, COUNT(*) FROM cards GROUP BY did"
                ) or []
                for row in rows:
                    try:
                        counts[int(row[0])] = int(row[1])
                    except (TypeError, ValueError, IndexError):
                        continue
        except Exception:
            counts = {}
        name_to_did: Dict[str, int] = {}
        try:
            if mw and mw.col and getattr(mw.col, "decks", None):
                decks = mw.col.decks
                if hasattr(decks, "all_names_and_ids"):
                    for entry in decks.all_names_and_ids() or []:
                        if isinstance(entry, dict):
                            n, i = entry.get("name"), entry.get("id")
                        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                            n = next((p for p in entry if isinstance(p, str)), None)
                            i = next((p for p in entry if isinstance(p, int)), None)
                        else:
                            n, i = getattr(entry, "name", None), getattr(entry, "id", None)
                        if n is not None and i is not None:
                            try:
                                name_to_did[str(n)] = int(i)
                            except (TypeError, ValueError):
                                continue
                elif hasattr(decks, "all"):
                    for d in decks.all() or []:
                        if isinstance(d, dict) and d.get("name") is not None:
                            try:
                                name_to_did[str(d["name"])] = int(d["id"])
                            except (TypeError, ValueError, KeyError):
                                continue
        except Exception:
            pass
        if not name_to_did:
            # Fall back to raw scope/config names so the picker never
            # opens empty when the decks API is momentarily unavailable.
            try:
                names = get_all_deck_names(mw.col if mw else None)
            except Exception:
                names = []
            return [(n, 0) for n in sorted(set(names) | set(self.scope_decks))]
        return [
            (name, counts.get(did, 0))
            for name, did in sorted(name_to_did.items())
        ]

    def _populate_scope_deck_list(self) -> None:
        """Fills the inline Scope checklist from `self.scope_decks`."""
        deck_counts = self._deck_card_counts()
        role = _user_role()
        all_names = [n for n, _ in deck_counts]
        self.scope_deck_list.blockSignals(True)
        self.scope_deck_list.clear()
        for name, count in deck_counts:
            disp, has_children = _indent_deck_name(name, all_names)
            suffix = "  ▸ covers all subdecks" if has_children else ""
            item = QListWidgetItem(f"{disp}  —  {count:,} cards{suffix}")
            item.setFlags(item.flags() | _user_checkable_flag())
            item.setCheckState(
                Qt.CheckState.Checked if name in self.scope_decks
                else Qt.CheckState.Unchecked
            )
            item.setData(role, name)
            item.setToolTip(f"{name}\n({count:,} cards)"
                            + ("\nChecking this covers ALL of its subdecks too." if has_children else ""))
            if count == 0:
                try:
                    item.setForeground(Qt.GlobalColor.gray)  # type: ignore
                except Exception:
                    pass
            self.scope_deck_list.addItem(item)
        self.scope_deck_list.blockSignals(False)

    def _refresh_scope_label(self) -> None:
        """Updates the Scope summary + warning + implied-types preview."""
        if not self.scope_decks:
            self.scope_summary_label.setText("Scope: <b>none</b> — knowledge empty (generation still works via the CD toggle + Tab).")
            try:
                self.scope_summary_label.setTextFormat(Qt.TextFormat.RichText)  # type: ignore
            except Exception:
                pass
            self.scope_summary_label.setStyleSheet("color: #c0392b; font-size: 11px;")
            self.scope_warning_label.setText("Knowledge is empty: scoring cannot personalize until you pick at least one Japanese deck above.")
            self.scope_warning_label.setVisible(True)
            self.scope_implied_label.setText("")
            return
        self.scope_summary_label.setStyleSheet("color: #2a7d4f; font-size: 11px;")
        try:
            self.scope_summary_label.setTextFormat(Qt.TextFormat.RichText)  # type: ignore
        except Exception:
            pass
        self.scope_summary_label.setText(
            f"Scope: <b>{len(self.scope_decks)} deck(s)</b> — " + ", ".join(self.scope_decks)
        )
        try:
            all_names = get_all_deck_names(mw.col if mw else None)
            missing = missing_scope_decks(all_names, self.scope_decks)
        except Exception:
            missing = []
        if missing:
            self.scope_warning_label.setText(
                "Missing decks (renamed or deleted?): " + ", ".join(missing)
            )
            self.scope_warning_label.setVisible(True)
        else:
            self.scope_warning_label.setVisible(False)
        # Quick preview of implied types
        try:
            implied = implied_note_types(mw.col if mw else None, self.scope_decks)
            if implied:
                self.scope_implied_label.setText(
                    f"Implied note types ({len(implied)}): " + ", ".join(implied[:6])
                    + (" …" if len(implied) > 6 else "")
                    + " — see Fields tab to adjust field mapping."
                )
            else:
                self.scope_implied_label.setText("No note types found in selected decks (empty decks?).")
        except Exception:
            self.scope_implied_label.setText("")

    def _refresh_implied_types(self, select_first: bool = False) -> None:
        """Rebuilds the type list from the decks in scope.

        Saved mappings (including out-of-scope ones) are preserved in
        self.type_mappings; newly implied types without a mapping are
        auto-seeded with heuristic field matches so that picking a deck
        alone is enough — the "no need to add each note type" promise.
        """
        try:
            implied = implied_note_types(mw.col if mw else None, self.scope_decks)
        except Exception:
            implied = []
        for name in implied:
            if name not in self.type_mappings:
                # Auto-infer fields so generation works immediately
                fields = self._get_field_names(name)
                auto_word = _find_best_field_match(fields, _TARGET_WORD_KEYWORDS)
                auto_reading = _find_best_field_match(fields, _READING_FIELD_KEYWORDS) or \
                               (auto_word if auto_word else "")
                remaining = [f for f in fields if f != auto_word]
                auto_def = _find_best_field_match(remaining, _DEFINITION_KEYWORDS)
                # Fallback: _on_note_type_changed's auto does similar; keep empty if insufficient
                if auto_word and auto_def and auto_word != auto_def:
                    self.type_mappings[name] = {
                        "word_field": auto_word,
                        "reading_field": auto_reading or "",
                        "definition_field": auto_def,
                        "_auto": True,
                    }
                else:
                    self.type_mappings[name] = {
                        "word_field": "", "reading_field": "",
                        "definition_field": "", "_auto": True,
                    }
        role = _user_role()
        self.note_types_list.blockSignals(True)
        self.note_types_list.clear()
        for name in implied:
            item = QListWidgetItem(name)
            # Show checkmark + mapping status
            mp = self.type_mappings.get(name, {})
            has_map = bool(mp.get("word_field") and mp.get("definition_field"))
            item.setToolTip(name + (" — mapping ready" if has_map else " — auto-inferred on use"))
            item.setData(role, name)
            self.note_types_list.addItem(item)
        self.note_types_list.blockSignals(False)
        if select_first and self.note_types_list.count():
            self.note_types_list.setCurrentRow(0)
        elif self._active_type:
            for row in range(self.note_types_list.count()):
                if str(self.note_types_list.item(row).data(role)) == self._active_type:
                    self.note_types_list.setCurrentRow(row)
                    break
        # Update the mapping hint in Fields tab
        try:
            if not implied:
                self.mapping_hint.setText("No note types in current Scope — pick decks in the Scope tab.")
            else:
                ready = sum(1 for n in implied if self.type_mappings.get(n, {}).get("word_field")
                            and self.type_mappings.get(n, {}).get("definition_field"))
                self.mapping_hint.setText(
                    f"{ready}/{len(implied)} types have a field mapping (auto-detected; edit only if wrong)."
                )
        except Exception:
            pass

    def _on_scope_filter(self, text: str) -> None:
        """Hides decks not matching the filter (selection preserved)."""
        needle = text.strip().lower()
        role = _user_role()
        for i in range(self.scope_deck_list.count()):
            item = self.scope_deck_list.item(i)
            name = str(item.data(role) or "")
            item.setHidden(bool(needle) and needle not in name.lower())

    def _set_scope_all(self, checked: bool) -> None:
        """Checks/unchecks every deck, including filter-hidden ones."""
        state = Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked
        self.scope_deck_list.blockSignals(True)
        for i in range(self.scope_deck_list.count()):
            self.scope_deck_list.item(i).setCheckState(state)
        self.scope_deck_list.blockSignals(False)
        self._on_scope_deck_changed()

    def _on_scope_deck_changed(self, _item: QListWidgetItem = None) -> None:
        """Inline checklist toggled: sync `self.scope_decks` + dependents."""
        role = _user_role()
        new_scope: List[str] = []
        for i in range(self.scope_deck_list.count()):
            it = self.scope_deck_list.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                new_scope.append(str(it.data(role) or ""))
        # Preserve original order (deck_counts is sorted); user expectation
        # is deck-picker order, not config order drift, so we use list as built.
        if new_scope == self.scope_decks:
            return
        self.scope_decks = [n for n in new_scope if n]
        self._refresh_scope_label()
        self._refresh_implied_types()
        try:
            self._save_config_now()
        except Exception:
            pass
        # Scope changed: knowledge must rebuild for scoring to follow.
        try:
            _reset_knowledge_caches()
        except Exception:
            pass

    def _on_select_decks(self) -> None:
        """Opens the polished Scope picker as a dedicated window."""
        dialog = ScopeDialog(
            parent=self,
            deck_counts=self._deck_card_counts(),
            selected=list(self.scope_decks),
        )
        if dialog.exec():
            self.scope_decks = dialog.selected_decks()
            # Sync inline list to match pop-out result
            self._populate_scope_deck_list()
            self._refresh_scope_label()
            self._refresh_implied_types()
            try:
                self._save_config_now()
            except Exception:
                pass
            # Scope changed: knowledge must rebuild for scoring to follow.
            try:
                _reset_knowledge_caches()
            except Exception:
                pass

    def _on_clear_scope(self) -> None:
        """Empties the scope (fail-closed) and persists immediately."""
        self.scope_decks = []
        # Uncheck all in inline list
        self.scope_deck_list.blockSignals(True)
        for i in range(self.scope_deck_list.count()):
            self.scope_deck_list.item(i).setCheckState(Qt.CheckState.Unchecked)
        self.scope_deck_list.blockSignals(False)
        self._refresh_scope_label()
        self._refresh_implied_types()
        try:
            self._save_config_now()
        except Exception:
            pass
        # Scope changed: knowledge must rebuild for scoring to follow.
        try:
            _reset_knowledge_caches()
        except Exception:
            pass

    def _load_type_mappings(self) -> None:
        """
        Restores the Scope deck selection and per-type field mappings
        from config (multi-type 'targets' with legacy fallback), then
        shows the note types implied by the scoped decks.

        Out-of-scope saved mappings are kept (not deleted) so narrowing
        the scope never destroys field configuration.
        """
        # Fresh installs start with Japanese decks pre-selected (the
        # auto-init guess runs ONCE — the flag keeps a deliberate user
        # Clear from being re-seeded). Scope bounds only knowledge;
        # generation works everywhere via the CD toggle + Tab.
        self.scope_decks = get_scope_decks(self.config)
        if not self.scope_decks and not self.config.get(SCOPE_AUTOINIT_CONFIG_KEY):
            try:
                guess = maybe_auto_init_scope(
                    self.config, mw.col if mw else None)
            except Exception:
                guess = []
            if guess:
                self.scope_decks = list(guess)
                print(f"CompreDef: fresh-install Scope auto-selected "
                      f"{len(guess)} Japanese deck(s): {', '.join(guess)}")
            self.config[SCOPE_AUTOINIT_CONFIG_KEY] = True
            try:
                if mw and hasattr(mw, "addonManager"):
                    _cfg = dict(mw.addonManager.getConfig(self.addon_name) or {})
                    _cfg[SCOPE_AUTOINIT_CONFIG_KEY] = True
                    if self.scope_decks:
                        _cfg[SCOPE_CONFIG_KEY] = list(self.scope_decks)
                    mw.addonManager.writeConfig(self.addon_name, _cfg)
                    self.config = mw.addonManager.getConfig(self.addon_name) or self.config
            except Exception:
                pass
        self.type_mappings = {}
        saved_targets = self.config.get("targets")
        if isinstance(saved_targets, dict) and saved_targets:
            for type_name, mapping in saved_targets.items():
                if isinstance(mapping, dict):
                    self.type_mappings[str(type_name)] = {
                        "word_field": str(mapping.get("word_field", "") or ""),
                        "reading_field": str(mapping.get("reading_field", "") or ""),
                        "definition_field": str(mapping.get("definition_field", "") or ""),
                        "_auto": False,
                    }
        elif self.config.get("note_type"):
            # Legacy single-type config: one mapping, fields as configured.
            self.type_mappings[str(self.config["note_type"])] = {
                "word_field": str(self.config.get("word_field", "") or ""),
                "reading_field": str(self.config.get("reading_field", "") or ""),
                "definition_field": str(self.config.get("definition_field", "") or ""),
                "_auto": False,
            }

        self._populate_scope_deck_list()
        self._refresh_scope_label()
        self._refresh_implied_types()

    def _on_type_selected(self) -> None:
        """A row was selected: load that type's mapping into the form."""
        item = self.note_types_list.currentItem()
        if item is None:
            return
        role = _user_role()
        type_name = item.data(role)
        if not type_name:
            return
        # Remember the previously edited type's dropdown values.
        self._stash_current_mapping()
        self._active_type = type_name
        self._on_note_type_changed(type_name)
        mapping = self.type_mappings.get(type_name, {})
        if mapping and not mapping.get("_auto"):
            # Saved mapping wins over auto-match.
            self.word_field_combo.setCurrentText(mapping.get("word_field", ""))
            self.reading_field_combo.setCurrentText(mapping.get("reading_field", ""))
            self.definition_field_combo.setCurrentText(mapping.get("definition_field", ""))

    def _stash_current_mapping(self) -> None:
        """Saves the visible dropdowns into the active type's mapping."""
        active = getattr(self, "_active_type", None)
        if not active or active not in self.type_mappings:
            return
        self.type_mappings[active].update({
            "word_field": self.word_field_combo.currentText().strip(),
            "reading_field": self.reading_field_combo.currentText().strip(),
            "definition_field": self.definition_field_combo.currentText().strip(),
            "_auto": False,
        })

    def _refresh_item_labels(self) -> None:
        """Updates labels: '[n] Title ✓' and syncs checkbox state."""
        # When Yomitan is active the local list is disabled — keep it as-is
        try:
            if hasattr(self, "source_combo") and self.source_combo.currentData() == "yomitan":
                return
        except Exception:
            pass
        role = _user_role()
        # Prepare a local provider once for title/count (Yomitan provider would return "Yomitan" for any path)
        _local_prov = None
        try:
            from .provider import LocalSQLiteProvider as _Local
            import os as _os
            _addon_dir = _os.path.dirname(_os.path.abspath(__file__))
            _cache_dir = _os.path.join(_addon_dir, "user_files", "cache")
            _os.makedirs(_cache_dir, exist_ok=True)
            _local_prov = _Local(_cache_dir)
        except Exception:
            pass
        # Block signals to prevent itemChanged from triggering a loop during refresh
        self.dict_list.blockSignals(True)
        for i in range(self.dict_list.count()):
            item = self.dict_list.item(i)
            path = item.data(role)
            try:
                if _local_prov is not None:
                    title = _local_prov.get_title(path)
                    if _local_prov.is_installed(path):
                        count = _local_prov.get_entry_count(path)
                        status = f"✓ ({count:,} entries)"
                    else:
                        status = "⚠ not indexed — re-add to install"
                else:
                    title = get_provider().get_title(path)
                    provider = get_provider()
                    if provider.is_installed(path):
                        count = provider.get_entry_count(path)
                        status = f"✓ ({count:,} entries)"
                    else:
                        status = "⚠ not indexed — re-add to install"
            except Exception:
                title = path
                status = "⚠ error"
            item.setText(f"[{i + 1}] {title} {status}")
            item.setCheckState(Qt.CheckState.Checked if path not in self.disabled_dicts else Qt.CheckState.Unchecked)
            item.setToolTip(
                path + ("\n(disabled — skipped during generation)" if path in self.disabled_dicts else "")
            )
        self.dict_list.blockSignals(False)

    def _on_source_changed(self, _idx: int = 0, save: bool = True) -> None:
        """Toggles Yomitan vs Local UI and persists immediately.

        save=False is used during initial load to avoid overwriting the
        set with an empty list before _load_config populates it
        (the v1.0.20 'disappearing dictionaries' bug).
        """
        is_yomitan = self.source_combo.currentData() == "yomitan"
        # Show/hide Yomitan URL row + status
        self.yomitan_settings_widget.setVisible(is_yomitan)
        self.yomitan_status_label.setVisible(is_yomitan)
        if is_yomitan:
            self.yomitan_status_label.setText(
                "Yomitan mode: live dictionaries from browser (no indexing needed).\n"
                "Click 'Install / Repair Bridge' once (no terminal), restart browser,\n"
                "then enable Yomitan → Settings → Advanced → General → Enable Yomitan API.\n"
                "After that 'Test' should turn green."
            )
        # Grey out local dictionaries when Yomitan is active (still visible for reference)
        for w in (self.dict_list, self.add_zip_btn, self.add_dict_btn, self.add_folder_btn,
                  self.move_up_btn, self.move_down_btn, self.remove_btn, self.reindex_btn):
            w.setEnabled(not is_yomitan)
        if save:
            # Persist instantly so closing dialog keeps choice (same crash-safety as others)
            try:
                self._save_config_now()
            except Exception:
                pass
            # Reset provider singleton so next get_provider() reads new source
            try:
                from .core import reset_provider_cache  # type: ignore
                reset_provider_cache()
            except Exception:
                try:
                    from core import reset_provider_cache  # type: ignore
                    reset_provider_cache()
                except Exception:
                    pass

    def _on_test_yomitan(self) -> None:
        """Pings Yomitan API and shows result in status label + tooltip."""
        url = self.yomitan_url_edit.text().strip() or "http://127.0.0.1:19633"
        # Clear stale negative cache so a Test right after browser restart retries for real.
        try:
            if __package__:
                from .yomitan import clear_yomitan_cache
            else:
                from yomitan import clear_yomitan_cache
            clear_yomitan_cache()
        except Exception:
            pass
        self.yomitan_status_label.setText(f"Testing {url} ...")
        self.yomitan_status_label.setStyleSheet("color: gray; font-size: 11px;")
        # Save URL first so test uses what user typed
        try:
            self._save_config_now()
        except Exception:
            pass
        def task():
            import json, urllib.request, urllib.error
            # Step 1: Check bridge HTTP server (serverVersion) — does not need Yomitan
            bridge_data = None
            bridge_err = None
            for path in ("/serverVersion",):
                try:
                    req = urllib.request.Request(url.rstrip("/") + path, data=b"{}", headers={"Content-Type":"application/json"}, method="POST")
                    with urllib.request.urlopen(req, timeout=2.0) as resp:
                        body = resp.read().decode("utf-8")
                        bridge_data = json.loads(body) if body else {}
                        bridge_err = None
                        break
                except Exception as e:
                    bridge_err = e
                    continue
            if bridge_data is None:
                return (None, None, bridge_err, None)
            # Step 2: Check Yomitan dictionaries via ankiFields (needs browser + Yomitan + dictionaries)
            # Use a simple kanji "口" that should exist in any Japanese dict
            try:
                payload = json.dumps({"text": "口", "type": "term", "markers": ["glossary"], "maxEntries": 1, "includeMedia": False}).encode()
                req = urllib.request.Request(url.rstrip("/") + "/ankiFields", data=payload, headers={"Content-Type":"application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    body = resp.read().decode("utf-8")
                    data = json.loads(body) if body else {}
                    fields = data.get("fields") if isinstance(data, dict) else None
                    if isinstance(fields, list) and fields:
                        return ("ankiFields", {"yomitan_fields": len(fields)}, None, bridge_data)
                    else:
                        # Bridge is up but Yomitan returned no fields — could be no dict or Yomitan not enabled
                        return ("ankiFields", None, "Yomitan returned no fields for '口' (is Yomitan enabled with dictionaries?)", bridge_data)
            except urllib.error.HTTPError as he:
                try:
                    body = he.read().decode("utf-8")
                    err_data = json.loads(body) if body else {}
                    err_msg = err_data.get("error") if isinstance(err_data, dict) else str(err_data)
                except Exception:
                    err_msg = str(he)
                return (None, None, f"Yomitan not connected (HTTP {he.code}): {err_msg}. Browser must be open + Yomitan API enabled.", bridge_data)
            except Exception as e:
                return (None, None, e, bridge_data)
            return ("/serverVersion", bridge_data, None, None)
        def on_done(future):
            try:
                path, data, err, bridge_data = future.result()
                if data is not None and bridge_data is not None and path == "ankiFields":
                    # Full success: bridge + Yomitan dictionaries
                    self.yomitan_status_label.setText(f"✓ Yomitan OK (bridge {bridge_data} + dictionaries: {data}) — ready for '口'.")
                    self.yomitan_status_label.setStyleSheet("color: green; font-size: 11px;")
                    tooltip(f"Yomitan OK: {data}")
                elif data is not None and path == "/serverVersion":
                    # Bridge is up but Yomitan dictionaries not yet available — show yellow warning
                    # This happens when bridge was manually started but Yomitan not yet launched it
                    self.yomitan_status_label.setText(f"⚠ Bridge running at {url} ({data}) but Yomitan not yet connected — restart browser and enable Yomitan → Settings → Advanced → General → Enable Yomitan API, then Test again.")
                    self.yomitan_status_label.setStyleSheet("color: orange; font-size: 11px;")
                    tooltip(f"Bridge OK but Yomitan not connected: {data}")
                elif data is not None:
                    self.yomitan_status_label.setText(f"✓ Yomitan OK ({path}: {data}) — all Yomitan dictionaries available.")
                    self.yomitan_status_label.setStyleSheet("color: green; font-size: 11px;")
                    tooltip(f"Yomitan OK: {data}")
                else:
                    # Check if bridge_data is available (bridge is running) but Yomitan failed
                    if bridge_data is not None:
                        msg = (
                            f"⚠ Bridge running at {url} ({bridge_data}) but Yomitan not reachable ({err}).\n"
                            f"Browser must be open, Yomitan → Settings → Advanced → General → Enable Yomitan API must be ON (restart browser after enabling).\n"
                            f"Without Yomitan, CompreDef cannot fetch '口' — switch to Local if you don't want the bridge."
                        )
                        self.yomitan_status_label.setText(msg)
                        self.yomitan_status_label.setStyleSheet("color: orange; font-size: 11px;")
                    else:
                        msg = (
                            f"✗ Yomitan not reachable at {url} ({err}).\n"
                            f"Browser must be open, Yomitan → Settings → Advanced → General → Enable Yomitan API must be ON,\n"
                            f"and click 'Install / Repair Bridge' above once (no terminal) then restart browser.\n"
                            f"Without that bridge CompreDef cannot see Yomitan's dictionaries — switch to Local if you don't want the extra step."
                        )
                        self.yomitan_status_label.setText(msg)
                        self.yomitan_status_label.setStyleSheet("color: red; font-size: 11px;")
                    print(f"CompreDef: Yomitan test failed: {err}")
                    tooltip(msg, parent=self)
            except Exception as e:
                import traceback
                self.yomitan_status_label.setText(f"✗ Test error: {e}")
                self.yomitan_status_label.setStyleSheet("color: red; font-size: 11px;")
                print(traceback.format_exc())
        try:
            mw.taskman.run_in_background(task, on_done)
        except Exception:
            # Fallback synchronous for tests
            task()

    def _on_install_yomitan_bridge(self) -> None:
        """One-click install of the Yomitan native host — no terminal needed."""
        self.yomitan_status_label.setText("Installing Yomitan bridge ...")
        self.yomitan_status_label.setStyleSheet("color: gray; font-size: 11px;")
        try:
            self._save_config_now()
        except Exception:
            pass
        def task():
            try:
                if __package__:
                    from .yomitan_installer import install_bridge
                else:
                    from yomitan_installer import install_bridge
                extid = ""
                try:
                    extid = self.yomitan_extid_edit.text().strip()
                except Exception:
                    pass
                extra = [extid] if extid else []
                results = install_bridge(extra)
                return (results, None)
            except Exception:
                import traceback
                return (None, traceback.format_exc())

        def on_done(future):
            try:
                results, err = future.result()
                if err:
                    self.yomitan_status_label.setText(f"✗ Install failed: {err}")
                    self.yomitan_status_label.setStyleSheet("color: red; font-size: 11px;")
                    print(f"CompreDef: Yomitan bridge install failed:\n{err}")
                    tooltip("Yomitan bridge install failed — see console", parent=self)
                    return
                # Clear Yomitan negative cache so Test retries immediately
                try:
                    if __package__:
                        from .yomitan import clear_yomitan_cache
                    else:
                        from yomitan import clear_yomitan_cache
                    clear_yomitan_cache()
                except Exception:
                    pass
                # Summarize per-browser (plus _cleanup note, no more _bridge autostart)
                if isinstance(results, dict) and results:
                    cleanup_msg = ""
                    if "_cleanup" in results:
                        _ok, _msg = results["_cleanup"]
                        cleanup_msg = f" Cleanup: {_msg}."
                        print(f"CompreDef: Yomitan bridge cleanup: {_msg}")
                    browser_results = {k: v for k, v in results.items() if not k.startswith("_")}
                    ok = [b for b, (s, _) in browser_results.items() if s]
                    fail = [(b, m) for b, (s, m) in browser_results.items() if not s]
                    if ok:
                        msg = (
                            f"✓ Manifests installed for: {', '.join(ok)}.{cleanup_msg} "
                            f"FULLY QUIT the browser (all windows), reopen it, enable "
                            f"Yomitan → Settings → Advanced → General → Enable Yomitan API, then click Test. "
                            f"NOTE: Test only turns fully green when the BROWSER owns port 19633 — "
                            f"a standalone bridge gives serverVersion but always 502 on ankiFields."
                        )
                        self.yomitan_status_label.setText(msg)
                        self.yomitan_status_label.setStyleSheet("color: green; font-size: 11px;")
                        print(f"CompreDef: Yomitan bridge install results: {results}")
                        tooltip(f"Yomitan manifests installed for {', '.join(ok)} — restart browser completely, then Test", parent=self)
                    if fail:
                        print(f"CompreDef: Yomitan bridge partial failures: {fail}")
                        if not ok:
                            self.yomitan_status_label.setText(f"✗ Bridge install failed for all browsers: {fail}.{cleanup_msg}")
                            self.yomitan_status_label.setStyleSheet("color: red; font-size: 11px;")
                else:
                    self.yomitan_status_label.setText("✗ Bridge install returned no results — see console")
                    self.yomitan_status_label.setStyleSheet("color: red; font-size: 11px;")
                # Do NOT auto-Test immediately: browser restart is required for
                # the browser-launched bridge to bind. Auto-test would hit the
                # (now killed) standalone port and confuse with red.
            except Exception as e:
                import traceback
                self.yomitan_status_label.setText(f"✗ Install error: {e}")
                self.yomitan_status_label.setStyleSheet("color: red; font-size: 11px;")
                print(traceback.format_exc())

        try:
            mw.taskman.run_in_background(task, on_done)
        except Exception:
            # Fallback synchronous
            res = task()
            class FakeFuture:
                def result(self): return res
            on_done(FakeFuture())

    def _on_copy_yomitan_debug(self) -> None:
        """Collects bridge diagnostics into clipboard + console for bug reports."""
        url = ""
        try:
            url = self.yomitan_url_edit.text().strip() or "http://127.0.0.1:19633"
        except Exception:
            url = "http://127.0.0.1:19633"
        self.yomitan_status_label.setText("Collecting Yomitan debug info ...")
        def task():
            import json, os, urllib.request, urllib.error, subprocess
            lines = [f"CompreDef Yomitan debug @ {url}"]
            try:
                if __package__:
                    from .yomitan_installer import _get_bridge_dir, get_install_status
                else:
                    from yomitan_installer import _get_bridge_dir, get_install_status
                bdir = _get_bridge_dir()
                bscript = os.path.join(bdir, "yomitan_api.py")
                lines.append(f"bridge script: {bscript} exists={os.path.isfile(bscript)}")
                elog = os.path.join(bdir, "error.log")
                if os.path.isfile(elog):
                    try:
                        with open(elog, encoding="utf-8", errors="replace") as f:
                            tail = f.read()[-2000:]
                        lines.append("error.log tail:\n" + tail)
                    except Exception as e:
                        lines.append(f"error.log unreadable: {e}")
                else:
                    lines.append("error.log: (none — bridge never errored)")
                try:
                    st = get_install_status()
                    lines.append(f"manifests: {st}")
                    candidates = {
                        "chrome": os.path.expanduser("~/.config/google-chrome/NativeMessagingHosts/yomitan_api.json"),
                        "chromium": os.path.expanduser("~/.config/chromium/NativeMessagingHosts/yomitan_api.json"),
                        "brave": os.path.expanduser("~/.config/BraveSoftware/Brave-Browser/NativeMessagingHosts/yomitan_api.json"),
                        "firefox": os.path.expanduser("~/.mozilla/native-messaging-hosts/yomitan_api.json"),
                    }
                    for browser, p in candidates.items():
                        if p and os.path.isfile(p):
                            with open(p, encoding="utf-8") as f:
                                content = f.read()
                            lines.append(f"{browser} manifest ({p}): {content[:400]}")
                        else:
                            lines.append(f"{browser} manifest: MISSING at {p}")
                except Exception as e:
                    lines.append(f"install status failed: {e}")
            except Exception as e:
                lines.append(f"installer import failed: {e}")
            try:
                import socket
                try:
                    with socket.create_connection(("127.0.0.1", 19633), timeout=1.0):
                        lines.append("port 19633: OPEN (something listening)")
                except Exception as e:
                    lines.append(f"port 19633: CLOSED ({e})")
            except Exception as e:
                lines.append(f"socket check failed: {e}")
            try:
                out = subprocess.run(["pgrep", "-af", "yomitan_api.py"], capture_output=True, text=True, timeout=3)
                lines.append("yomitan processes:\n" + (out.stdout.strip() or "(none)"))
            except Exception as e:
                lines.append(f"pgrep failed: {e}")
            try:
                req = urllib.request.Request(url.rstrip("/") + "/serverVersion", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=2.0) as resp:
                    lines.append(f"serverVersion 200: {resp.read().decode()[:300]}")
            except Exception as e:
                lines.append(f"serverVersion FAILED: {e}")
            try:
                req = urllib.request.Request(url.rstrip("/") + "/yomitanVersion", data=b"{}", headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=3.0) as resp:
                    lines.append(f"yomitanVersion 200: {resp.read().decode()[:500]}")
            except urllib.error.HTTPError as he:
                try:
                    body = he.read().decode()[:500]
                except Exception:
                    body = str(he)
                lines.append(f"yomitanVersion HTTP {he.code}: {body}")
            except Exception as e:
                lines.append(f"yomitanVersion FAILED: {e}")
            try:
                payload = json.dumps({"text": "口", "type": "term", "markers": ["glossary"], "maxEntries": 1, "includeMedia": False}).encode()
                req = urllib.request.Request(url.rstrip("/") + "/ankiFields", data=payload, headers={"Content-Type": "application/json"}, method="POST")
                with urllib.request.urlopen(req, timeout=4.0) as resp:
                    body = resp.read().decode()
                    lines.append(f"ankiFields(口) 200 len={len(body)}: {body[:600]}")
            except urllib.error.HTTPError as he:
                try:
                    body = he.read().decode()[:600]
                except Exception:
                    body = str(he)
                lines.append(f"ankiFields(口) HTTP {he.code}: {body}")
            except Exception as e:
                lines.append(f"ankiFields(口) FAILED: {e}")
            return "\n".join(lines)

        def on_done(future):
            try:
                text = future.result()
                print(text)
                try:
                    from aqt.qt import QApplication
                    QApplication.clipboard().setText(text)
                    self.yomitan_status_label.setText("Debug info copied to clipboard + printed to console — paste it back in chat.")
                except Exception:
                    self.yomitan_status_label.setText("Debug info printed to console (clipboard unavailable).")
                self.yomitan_status_label.setStyleSheet("color: gray; font-size: 11px;")
                tooltip("Yomitan debug copied — paste it back", parent=self)
            except Exception as e:
                import traceback
                print(traceback.format_exc())
                self.yomitan_status_label.setText(f"Debug failed: {e}")
        try:
            mw.taskman.run_in_background(task, on_done)
        except Exception:
            res = task()
            class FakeFuture2:
                def result(self): return res
            on_done(FakeFuture2())

    def _on_item_changed(self, item: QListWidgetItem) -> None:
        """Handles checkbox toggles to update disabled_dicts set."""
        role = _user_role()
        path = item.data(role)
        if not path:
            return
        if item.checkState() == Qt.CheckState.Checked:
            self.disabled_dicts.discard(path)
        else:
            self.disabled_dicts.add(path)

    def _on_toggle_dictionary(self) -> None:
        """Deprecated: handled by checkboxes now."""
        pass

    def _add_dict_path(self, path: str) -> bool:
        """Adds a dictionary path (zip or folder) to the set if not already present."""
        if not path:
            return False

        norm_path = os.path.realpath(os.path.expanduser(path))
        is_zip = is_zip_dictionary(norm_path)

        if not is_zip and not os.path.isdir(norm_path):
            return False

        role = _user_role()

        # Check for duplicates
        for i in range(self.dict_list.count()):
            existing_path = self.dict_list.item(i).data(role)
            if existing_path == norm_path:
                return False

        item = QListWidgetItem()
        item.setData(role, norm_path)
        self.dict_list.addItem(item)
        self._refresh_item_labels()

        # Persist the addition NOW, BEFORE the install starts: if Anki
        # crashes mid-install or the user closes the dialog, the new
        # dictionary list must survive (a crash once silently reverted a
        # completed 'remove all + re-import' to the old list).
        self._save_config_now()

        # INSTALL-TIME INDEXING: a newly added dictionary is parsed and
        # indexed exactly once, here, as a background operation with a
        # progress dialog. Normal lookups never parse it again.
        self._install_dictionary_in_background(norm_path)
        return True

    def _install_dictionary_in_background(self, path: str) -> None:
        """
        Installs (indexes) one dictionary in the background with progress.

        Anki stays fully responsive — indexing runs on a background thread
        and the progress dialog shows 'Indexing <title>... N%'. When it
        completes, the dictionary's index persists in SQLite and every
        future lookup is a pure database query.
        """
        dict_obj = get_single_dictionary(path)

        # Already indexed from exactly these files? Nothing to do.
        try:
            if dict_obj.is_indexed() and dict_obj.index_is_current():
                return
        except Exception:
            pass  # fall through and (re-)install

        title = dict_obj.title or os.path.basename(path)

        # Simple non-blocking progress dialog the user can cancel.
        from aqt.qt import QTimer, QDialog, QLabel, QVBoxLayout, QPushButton
        state = {"done": 0, "total": 0, "finished": False, "error": None}
        cancelled = {"flag": False}

        progress = QDialog(self)
        progress.setWindowTitle("CompreDef")
        progress.setModal(True)
        label = QLabel(f"Indexing {title}...\nPreparing...")
        layout = QVBoxLayout()
        layout.addWidget(label)
        bar = QLabel("")  # textual percent; avoids QProgressBar API drift
        layout.addWidget(bar)
        cancel_btn = QPushButton("Cancel")
        layout.addWidget(cancel_btn)
        progress.setLayout(layout)
        cancel_btn.clicked.connect(lambda: cancelled.__setitem__("flag", True))

        def poll() -> None:
            # Update from the latest shared state written by the worker.
            done, total = state.get("done", 0), state.get("total", 0)
            if total:
                pct = min(100, done * 100 // total)
                bar.setText(f"{pct}%  ({done:,} / {total:,} entries)")
            if state.get("finished"):
                progress.accept()

        timer = QTimer(progress)
        timer.timeout.connect(poll)

        def progress_cb(done: int, total: int) -> None:
            # Called on the worker thread: just record numbers; the Qt
            # timer on the main thread does the actual UI update.
            state["done"], state["total"] = done, total

        def cancel_check() -> bool:
            return cancelled["flag"]

        def task() -> int:
            return get_provider().install(path, progress_cb, cancel_check)

        def on_done(future) -> None:
            timer.stop()
            state["finished"] = True
            try:
                count = future.result()
                msg = f"CompreDef: '{title}' indexed ({count:,} entries)."
                print(msg)
                tooltip(msg)
            except IndexingError as e:
                state["error"] = str(e)
                print(f"CompreDef: Indexing '{title}' FAILED: {e}")
                tooltip(f"CompreDef: Indexing '{title}' failed: {e}")
            except Exception:
                import traceback
                err = traceback.format_exc()
                print(f"CompreDef: Indexing '{title}' crashed:\n{err}")
                tooltip(f"CompreDef: Indexing '{title}' crashed — see console.")
            finally:
                try:
                    progress.accept()
                    self._refresh_item_labels()
                except Exception:
                    import traceback
                    print(f"CompreDef: install-dialog cleanup error:\n{traceback.format_exc()}")

        # Show dialog and start worker; dialog closes itself on completion.
        timer.start(250)
        mw.taskman.run_in_background(task, on_done)
        progress.exec()

    def _on_add_dictionary(self) -> None:
        """Opens folder dialog to add an individual unzipped dictionary folder."""
        folder = QFileDialog.getExistingDirectory(self, "Select Dictionary Directory")
        if folder:
            self._add_dict_path(folder)

    def _on_add_zip(self) -> None:
        """Opens file dialog to add a Yomitan dictionary .zip archive."""
        zip_path, _ = QFileDialog.getOpenFileName(
            self,
            "Select Dictionary Zip File",
            "",
            "Yomitan Dictionaries (*.zip);;All Files (*)"
        )
        if zip_path:
            self._add_dict_path(zip_path)

    def _on_scan_folder(self) -> None:
        """Opens folder dialog and scans for all dictionary subfolders."""
        parent_folder = QFileDialog.getExistingDirectory(self, "Select Parent Folder with Dictionaries")
        if parent_folder:
            subfolders = find_dictionary_folders(parent_folder)
            for sub in subfolders:
                self._add_dict_path(sub)

    def _on_move_up(self) -> None:
        """Moves the currently selected dictionary up in the list order."""
        curr_row = self.dict_list.currentRow()
        if curr_row > 0:
            item = self.dict_list.takeItem(curr_row)
            self.dict_list.insertItem(curr_row - 1, item)
            self.dict_list.setCurrentRow(curr_row - 1)
            self._refresh_item_labels()

    def _on_move_down(self) -> None:
        """Moves the currently selected dictionary down in the list order."""
        curr_row = self.dict_list.currentRow()
        if curr_row >= 0 and curr_row < self.dict_list.count() - 1:
            item = self.dict_list.takeItem(curr_row)
            self.dict_list.insertItem(curr_row + 1, item)
            self.dict_list.setCurrentRow(curr_row + 1)
            self._refresh_item_labels()

    def _on_remove_dictionary(self) -> None:
        """
        Removes the selected dictionary from the set AND deletes its
        SQLite index, persisting the config immediately so a crash cannot
        silently revert the removal.
        """
        curr_row = self.dict_list.currentRow()
        if curr_row < 0:
            return
        role = _user_role()
        item = self.dict_list.item(curr_row)
        path = item.data(role)

        removed = self.dict_list.takeItem(curr_row)
        if removed is not None:
            self._refresh_item_labels()

        # Persist the removal NOW (crash-proofing the dialog state).
        self._save_config_now()

        # Drop the index too (background: deleting hundreds of thousands of
        # rows must never block the UI thread).
        if path:
            def task() -> None:
                get_provider().uninstall(path)

            def on_done(_future) -> None:
                self._refresh_item_labels()

            try:
                mw.taskman.run_in_background(task, on_done)
            except Exception:
                import traceback
                print(f"CompreDef: index removal failed:\n{traceback.format_exc()}")

    def _on_reindex_dictionary(self) -> None:
        """
        Explicitly reinstalls (re-indexes) the selected dictionary.

        This is the ONLY path (besides adding a new dictionary) that parses
        dictionary files — replacing a dictionary's files on disk is exactly
        the 'user explicitly installs/replaces' case from the architecture.
        """
        curr_row = self.dict_list.currentRow()
        if curr_row < 0:
            tooltip("Select a dictionary first.")
            return
        role = _user_role()
        path = self.dict_list.item(curr_row).data(role)
        if not path:
            return
        self._install_dictionary_in_background(path)

    def _load_config(self) -> None:
        """Populates form controls with current configuration values."""
        # Scope decks + per-type mappings (field mappings imply nothing;
        # deck membership decides what generates).
        self._load_type_mappings()
        # Select the first implied type so the field form starts populated
        if self.note_types_list.count():
            self.note_types_list.setCurrentRow(0)

        # Load dictionary set
        saved_dicts = self.config.get("dictionaries", [])
        if not saved_dicts and self.config.get("dictionary_folder"):
            # Auto-detect from legacy single folder path
            saved_dicts = find_dictionary_folders(self.config["dictionary_folder"])
        # Recovery: if config was clobbered to [] by the v1.0.20 race but DB still
        # has indexed dictionaries, restore them so user doesn't need to re-import.
        if not saved_dicts:
            try:
                import os as _os
                _addon_dir = _os.path.dirname(_os.path.abspath(__file__))
                _cache_dir = _os.path.join(_addon_dir, "user_files", "cache")
                _db_path = _os.path.join(_cache_dir, "dictionaries.db")
                if _os.path.isfile(_db_path):
                    import sqlite3
                    _conn = sqlite3.connect(_db_path)
                    try:
                        _rows = _conn.execute("SELECT path FROM dictionaries").fetchall()
                        _db_paths = [r[0] for r in _rows if r[0] and _os.path.exists(r[0])]
                        if _db_paths:
                            saved_dicts = _db_paths
                            print(f"CompreDef: recovered {len(_db_paths)} dictionaries from DB after config was empty (v1.0.20 bug)")
                    finally:
                        _conn.close()
            except Exception:
                pass

        # Restore disabled set (paths normalized the same way as _add_dict_path)
        self.disabled_dicts = {
            os.path.realpath(os.path.expanduser(str(p)))
            for p in self.config.get("disabled_dictionaries", [])
            if p
        }

        for d_path in saved_dicts:
            self._add_dict_path(d_path)
        # Sync Yomitan/Local visibility after the set is populated (must be
        # after _add_dict_path so ordered_dicts is not empty when we save).
        try:
            self._on_source_changed(save=False)
        except Exception:
            pass
        # NOTE: tab_generate_check's state is restored in _init_ui (at widget
        # creation) — NOT here. The dictionary loop above persists the dialog
        # state on every add (crash safety), so the checkbox must already
        # carry its saved value by the time _add_dict_path saves.

    def _collect_type_config(self) -> Dict[str, Any]:
        """
        Builds the note-type portion of the config: the multi-type
        'targets' dict plus a legacy mirror (note_type + flat fields) of
        the FIRST target so old configs and hand-edited config.json
        files keep working.

        Delegates to utils.merge_type_targets (Qt-free, unit-tested)
        after stashing the live dropdowns into the active type.
        """
        self._stash_current_mapping()  # dropdowns -> active type
        return merge_type_targets(self.type_mappings, self.config)

    def _save_and_accept(self) -> None:
        """Saves settings to Anki config and closes dialog."""
        role = _user_role()
        ordered_dicts = [
            self.dict_list.item(i).data(role)
            for i in range(self.dict_list.count())
        ]
            # Never clobber local dictionaries when Yomitan is selected — the list is
        # disabled but still holds the Local dictionaries. Preserve them.
        _is_yomitan = self.source_combo.currentData() == "yomitan"
        if _is_yomitan and not ordered_dicts and isinstance(self.config.get("dictionaries"), list) and self.config["dictionaries"]:
            ordered_dicts = list(self.config["dictionaries"])

        updated_config = {
            **self._collect_type_config(),
            # Scope: the deck selection driving KNOWLEDGE (scoring
            # weights). Generation bypasses Scope (CD toggle + Tab).
            SCOPE_CONFIG_KEY: list(self.scope_decks),
            SCOPE_AUTOINIT_CONFIG_KEY: True,
            "dictionaries": ordered_dicts,
            # Disabled paths: kept in `dictionaries` for order preservation,
            # listed here so generation skips them.
            "disabled_dictionaries": sorted(self.disabled_dicts),
            # Auto-generate = the global CD toggle (toolbar button + Tab)
            "tab_generate": self.tab_generate_check.isChecked(),
            "plain_text_definitions": self.plain_text_check.isChecked(),
            "dictionary_source": self.source_combo.currentData() or "local",
            "yomitan_url": self.yomitan_url_edit.text().strip() or "http://127.0.0.1:19633",
            "yomitan_extension_id": self.yomitan_extid_edit.text().strip() if hasattr(self, "yomitan_extid_edit") else "",
            # Backwards compatibility
            "dictionary_folder": ordered_dicts[0] if ordered_dicts else "",
            "mode": "Dictionaries",
        }

        mw.addonManager.writeConfig(self.addon_name, updated_config)
        # Reset provider singleton so next get_provider() respects new source
        try:
            from .core import reset_provider_cache  # type: ignore
            reset_provider_cache()
        except Exception:
            try:
                from core import reset_provider_cache  # type: ignore
                reset_provider_cache()
            except Exception:
                pass
        # The Scope may have changed: rebuild the knowledge snapshot so
        # scoring uses the new decks immediately (async, never blocks).
        try:
            _reset_knowledge_caches()
        except Exception:
            pass
        self.accept()

    def _save_config_now(self) -> None:
        """
        Persists the current dialog state to the config WITHOUT closing.

        Called before any background install starts: if Anki crashes or the
        user closes the dialog mid-install, their dictionary list changes
        are already durable (a crash once silently reverted a completed
        'remove all + re-import' to the previous dictionary list).
        """
        try:
            role = _user_role()
            ordered_dicts = [
                self.dict_list.item(i).data(role)
                for i in range(self.dict_list.count())
            ]
            # Never clobber local dictionaries when Yomitan is selected or during
            # the initial toggle race (v1.0.20 bug). Preserve previous config.
            _is_yomitan_now = False
            try:
                _is_yomitan_now = self.source_combo.currentData() == "yomitan"
            except Exception:
                pass
            if (not ordered_dicts and isinstance(self.config.get("dictionaries"), list) and self.config["dictionaries"]) or (_is_yomitan_now and not ordered_dicts):
                # If the UI list is empty but config still has dictionaries, keep them
                # (Yomitan mode disables the list but should not delete Local dicts).
                if isinstance(self.config.get("dictionaries"), list) and self.config["dictionaries"]:
                    ordered_dicts = list(self.config["dictionaries"])
                else:
                    # Last resort: try DB recovery even for save
                    try:
                        import os as _os2
                        _addon_dir2 = _os2.path.dirname(_os2.path.abspath(__file__))
                        _cache_dir2 = _os2.path.join(_addon_dir2, "user_files", "cache")
                        _db_path2 = _os2.path.join(_cache_dir2, "dictionaries.db")
                        if _os2.path.isfile(_db_path2):
                            import sqlite3
                            _conn2 = sqlite3.connect(_db_path2)
                            try:
                                _rows2 = _conn2.execute("SELECT path FROM dictionaries").fetchall()
                                _db_paths2 = [r[0] for r in _rows2 if r[0] and _os2.path.exists(r[0])]
                                if _db_paths2:
                                    ordered_dicts = _db_paths2
                            finally:
                                _conn2.close()
                    except Exception:
                        pass
            _extid = ""
            try:
                _extid = self.yomitan_extid_edit.text().strip() if hasattr(self, "yomitan_extid_edit") else ""
            except Exception:
                pass
            mw.addonManager.writeConfig(self.addon_name, {
                **self._collect_type_config(),
                SCOPE_CONFIG_KEY: list(self.scope_decks),
                SCOPE_AUTOINIT_CONFIG_KEY: True,
                "dictionaries": ordered_dicts,
                "disabled_dictionaries": sorted(self.disabled_dicts),
                "tab_generate": self.tab_generate_check.isChecked(),
                "plain_text_definitions": self.plain_text_check.isChecked(),
                "dictionary_source": self.source_combo.currentData() or "local",
                "yomitan_url": self.yomitan_url_edit.text().strip() or "http://127.0.0.1:19633",
                "yomitan_extension_id": _extid,
                "dictionary_folder": ordered_dicts[0] if ordered_dicts else "",
                "mode": "Dictionaries",
            })
            self.config = mw.addonManager.getConfig(self.addon_name) or self.config
        except Exception:
            import traceback
            print(f"CompreDef: config pre-save failed:\n{traceback.format_exc()}")


def show_scope_dialog() -> None:
    """Dedicated Scope window — polished picker as its own dialog.

    Persists immediately (same crash-safety as the main dialog) and
    rebuilds the knowledge snapshot so the change is live.
    """
    if not mw or not mw.col:
        return

    def _deck_counts() -> List[tuple]:
        # Reuse ConfigDialog helper to avoid duplication
        tmp = ConfigDialog.__new__(ConfigDialog)  # type: ignore
        tmp.scope_decks = []  # type: ignore
        # Bind minimal mw so _deck_card_counts can run without a full dialog
        try:
            # Build counts directly (duplicate of _deck_card_counts logic)
            counts: Dict[int, int] = {}
            try:
                rows = mw.col.db.all("SELECT did, COUNT(*) FROM cards GROUP BY did") or []
                for row in rows:
                    try:
                        counts[int(row[0])] = int(row[1])
                    except Exception:
                        continue
            except Exception:
                pass
            name_to_did: Dict[str, int] = {}
            if getattr(mw.col, "decks", None):
                decks = mw.col.decks
                if hasattr(decks, "all_names_and_ids"):
                    for entry in decks.all_names_and_ids() or []:
                        if isinstance(entry, dict):
                            n, i = entry.get("name"), entry.get("id")
                        elif isinstance(entry, (list, tuple)) and len(entry) >= 2:
                            n = next((p for p in entry if isinstance(p, str)), None)
                            i = next((p for p in entry if isinstance(p, int)), None)
                        else:
                            n, i = getattr(entry, "name", None), getattr(entry, "id", None)
                        if n is not None and i is not None:
                            try:
                                name_to_did[str(n)] = int(i)
                            except Exception:
                                continue
                elif hasattr(decks, "all"):
                    for d in decks.all() or []:
                        if isinstance(d, dict) and d.get("name") is not None:
                            try:
                                name_to_did[str(d["name"])] = int(d["id"])
                            except Exception:
                                continue
            if not name_to_did:
                try:
                    names = get_all_deck_names(mw.col)
                    return [(n, 0) for n in sorted(set(names))]
                except Exception:
                    return []
            return [(name, counts.get(did, 0)) for name, did in sorted(name_to_did.items())]
        except Exception:
            return []

    addon_name = _get_addon_name()
    cfg = mw.addonManager.getConfig(addon_name) or {}
    scope = get_scope_decks(cfg)
    dialog = ScopeDialog(
        parent=mw.app.activeWindow() if mw and mw.app else None,
        deck_counts=_deck_counts(),
        selected=list(scope),
    )
    if dialog.exec():
        new_scope = dialog.selected_decks()
         # Persist like ConfigDialog does
        try:
            updated = dict(cfg)
            updated[SCOPE_CONFIG_KEY] = list(new_scope)
            mw.addonManager.writeConfig(addon_name, updated)
            # Scope changed: drop the generator's stale knowledge and
            # rebuild the snapshot (same reset as the config dialog).
            try:
                from .core import reset_generator  # type: ignore
                reset_generator()
            except Exception:
                try:
                    from core import reset_generator  # type: ignore
                    reset_generator()
                except Exception:
                    pass
            _reset_knowledge_caches()
            tooltip(f"CompreDef Scope: {len(new_scope)} deck(s) — {'none' if not new_scope else ', '.join(new_scope)}")
        except Exception:
            import traceback
            print(f"CompreDef: scope save failed:\n{traceback.format_exc()}")


def show_config_dialog(initial_tab: int = 0) -> None:
    """Displays the configuration dialog.

    When `initial_tab` is set, that tab is shown first (e.g. Scope).
    """
    dialog = ConfigDialog(parent=mw.app.activeWindow() if mw and mw.app else None)
    try:
        if hasattr(dialog, "tabs"):
            dialog.tabs.setCurrentIndex(int(initial_tab))
    except Exception:
        pass
    dialog.exec()


class _NumericTableItem(QTableWidgetItem):
    """Table item that sorts NUMERICALLY (UserRole float), not by text.

    QTableWidget's default __lt__ compares DisplayRole text, so "999
    days" sorted above "9969 days" and mastery "0.10" above "1.00" —
    the v1.2.4 header-sort bug. Subclassing __lt__ to read the UserRole
    float is Qt's documented way to get value-based sorting.
    """

    def __lt__(self, other: Any) -> bool:
        try:
            mine = float(self.data(_user_role()))
            theirs = float(other.data(_user_role()))
            return mine < theirs
        except (TypeError, ValueError):
            # No numeric data on either side: fall back to text compare.
            return super().__lt__(other)


class KnowledgeDialog(QDialog):
    """Debug-oriented view of the learner-knowledge snapshot.

    Four tabs:
    - Overview: clickable stat cards with X/total readouts; clicking a
      card jumps to the matching detail tab.
    - Kanji: the FULL mastered-kanji set (one per row + mastery
      weight), searchable and sortable by mastery.
    - Vocab: the FULL mastered vocab set (kanji-only compounds),
      searchable and sortable by mastery.
    - Mature Notes: every mature note (first field + interval) the
      snapshot was built from — the provenance of the knowledge —
      sortable by interval.

    Terminology (v1.2.3, the user's definitions):
    - MASTERED kanji/vocab: mastery weight 1.0 — the kanji/vocab sits
      on a note whose interval reached a full YEAR. The knowledge
      snapshot admits only mature (>= 1 year) notes, so its sets ARE
      the mastered sets.
    - SEEN kanji/vocab: appears on any in-scope note with a strictly
      positive interval (> 0) — shown as secondary readouts.

    All heavy data (counts, lists) is fetched via the pure stdlib
    helpers in anki.py on a background thread, then rendered on the
    main thread (AGENTS.md non-blocking mandate).
    """

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("CompreDef Learner Knowledge")
        self.resize(760, 560)
        self._build_ui()
        # Load snapshot data in the background so the dialog opens
        # instantly even on a huge collection.
        self._refresh_async()

    # -- UI construction ------------------------------------------------

    def _build_ui(self) -> None:
        """One-time widget layout (tabs + status bar + close button)."""
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, stretch=1)

        # Overview tab --------------------------------------------------
        self.overview_tab = QWidget()
        ov = QVBoxLayout(self.overview_tab)
        # Full-width flowing paragraph: no manual line breaks, no indent,
        # zero layout margins so the text starts at the left edge (plus
        # only the dialog's own padding) — the v1.2.4 clunkiness fix.
        hint = QLabel(
            "MASTERED = mastery weight 1.0: the kanji/vocab sits on a "
            "note whose card interval reached a FULL YEAR. SEEN = on any "
            "in-scope note with interval > 0 (any age). All counts come "
            "from the FIRST FIELD of notes in your Scope decks — "
            "definitions, examples and other fields are never counted. "
            "'Vocab' = kanji-only compound words. Click a stat card below "
            "to see the full list."
        )
        hint.setWordWrap(True)
        hint.setIndent(0)
        hint.setContentsMargins(0, 0, 0, 0)
        ov.setContentsMargins(0, 0, 0, 0)
        ov.setSpacing(8)
        ov.addWidget(hint)
        # Stat cards are added dynamically once data arrives (2-column
        # grid — a single row of five cards was too cramped).
        self.stat_grid = QGridLayout()
        self.stat_grid.setColumnStretch(0, 1)
        self.stat_grid.setColumnStretch(1, 1)
        ov.addLayout(self.stat_grid)
        self.overview_details = QLabel("")
        self.overview_details.setWordWrap(True)
        self.overview_details.setIndent(0)
        self.overview_details.setContentsMargins(0, 0, 0, 0)
        ov.addWidget(self.overview_details, stretch=1)
        ov.addStretch(1)
        self.tabs.addTab(self.overview_tab, "Overview")

        # Kanji tab -----------------------------------------------------
        self.kanji_tab = self._make_list_tab(
            "Every SEEN kanji (on a note with interval > 0) with its "
            "mastery weight — 1.00 = MASTERED (note interval >= 1 year), "
            "lower = still maturing. Double-click a row to open Anki's "
            "Browser with every note in your Scope whose first field "
            "contains that kanji. Click the column headers to sort.",
            columns=("Kanji", "Mastery"), kind="kanji")
        self.tabs.addTab(self.kanji_tab, "Kanji")

        # Vocab tab (kanji-only compounds) -------------------------------
        self.words_tab = self._make_list_tab(
            "Every SEEN vocab item with its mastery weight — 1.00 = "
            "MASTERED (interval >= 1 year). 'Vocab' means kanji-only "
            "compound words (2+ kanji, no kana); kana-only words and "
            "single kanji are excluded by design (kana words are "
            "inflection-hostile, single kanji are covered by the kanji "
            "tab). Double-click a row to open Anki's Browser with every "
            "note in your Scope whose first field is exactly that word. "
            "Click the column headers to sort.",
            columns=("Vocab", "Mastery"), kind="vocab")
        self.tabs.addTab(self.words_tab, "Vocab")

        # Mature notes tab ----------------------------------------------
        self.notes_tab = self._make_list_tab(
            "The MATURE notes the snapshot was built from: first field "
            "and its interval (the note's longest card interval). "
            "Mature = interval >= 1 year. Click the column headers to "
            "sort.",
            columns=("Note", "Interval"), kind=None)
        self.tabs.addTab(self.notes_tab, "Mature Notes")

        # Status line + refresh + close ----------------------------------
        bar = QHBoxLayout()
        self.status_label = QLabel("Loading knowledge snapshot...")
        self.status_label.setWordWrap(True)
        bar.addWidget(self.status_label, stretch=1)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.setToolTip(
            "Rebuild the snapshot now (picks up new mature cards without "
            "restarting Anki).")
        # force_rebuild=True: opening the dialog reuses the cached
        # snapshot; ONLY this button rebuilds it.
        refresh_btn.clicked.connect(
            lambda _checked=False: self._refresh_async(force_rebuild=True))
        bar.addWidget(refresh_btn)
        layout.addLayout(bar)

        if hasattr(QDialogButtonBox, "StandardButton"):
            close_flag = QDialogButtonBox.StandardButton.Close
        else:
            close_flag = QDialogButtonBox.Close
        button_box = QDialogButtonBox(close_flag)
        button_box.rejected.connect(self.reject)
        layout.addWidget(button_box)

    def _make_list_tab(self, header: str, columns: tuple,
                       kind: Optional[str] = None) -> Any:
        """Builds a (filter + 2-column sortable table + count) tab.

        columns: the two header labels, e.g. ("Kanji", "Mastery"). The
        first column holds the item (kanji/vocab/note text), the second
        its numeric value (mastery weight or interval days) — stored as
        the item's Qt.UserRole data so header-click sorting is NUMERIC,
        not alphabetical ("900" > "1000" lexically).

        kind ('kanji'/'vocab'/None): when set, single-clicking a row
        opens Anki's Browser with the provenance search for that item
        (see _open_browser_for_term). The Mature Notes tab passes None
        (its rows are whole notes, nothing to browse).
        """
        tab = QWidget()
        v = QVBoxLayout(tab)
        head = QLabel(header)
        head.setWordWrap(True)
        head.setIndent(0)
        head.setContentsMargins(0, 0, 0, 0)
        v.addWidget(head)
        filter_row = QHBoxLayout()
        filter_edit = QLineEdit()
        filter_edit.setPlaceholderText("Filter...")
        filter_row.addWidget(filter_edit)
        count_label = QLabel("0")
        filter_row.addWidget(count_label)
        v.addLayout(filter_row)
        table = QTableWidget(0, 2)
        table.setHorizontalHeaderLabels(list(columns))
        table.verticalHeader().setVisible(False)
        table.setAlternatingRowColors(True)
        table.setSelectionBehavior(
            _table_select_rows_flag())
        table.setSelectionMode(
            _table_single_selection_flag())
        # Column 0 (the item) stretches; column 1 (value) sizes to fit.
        table.horizontalHeader().setSectionResizeMode(
            0, _header_resize_stretch_flag())
        table.horizontalHeader().setSectionResizeMode(
            1, _header_resize_resize_to_contents_flag())
        # Numeric sort on header click: UserRole carries the float.
        table.setSortingEnabled(True)
        v.addWidget(table, stretch=1)
        tab._filter_edit = filter_edit      # type: ignore[attr-defined]
        tab._count_label = count_label      # type: ignore[attr-defined]
        tab._table = table                  # type: ignore[attr-defined]
        tab._kind = kind                    # type: ignore[attr-defined]
        filter_edit.textChanged.connect(
            lambda text, t=tab: _knowledge_apply_filter(t))
        if kind:
            # DOUBLE-click opens the Browser with the provenance search
            # (single click was too trigger-happy while scanning rows;
            # v1.2.5 user request).
            table.cellDoubleClicked.connect(
                lambda row, _col, t=tab: _open_browser_for_term(t, row))
        return tab

    # -- Data loading ----------------------------------------------------

    def _refresh_async(self, force_rebuild: bool = False) -> None:
        """Gathers the dialog's data off-thread, then populates the tabs.

        force_rebuild=False (dialog OPEN — the default): the knowledge
        snapshot is REUSED from cache; only the read-only per-view data
        (seen points, totals, mature rows) is queried. The snapshot is
        expensive (a full scope scan) and changes only when cards
        mature, so opening the dialog must never rebuild it (v1.2.5
        'cache aggressively, rebuild rarely' mandate).

        force_rebuild=True (the Refresh button): sync_reset_caches()
        first so brand-new mature cards are picked up.
        """
        self.status_label.setText(
            "Rebuilding knowledge snapshot..." if force_rebuild
            else "Loading knowledge snapshot...")
        if not mw or not hasattr(mw, "taskman"):
            # Headless/test: no taskman — populate synchronously.
            self._populate({})
            return

        def task() -> dict:
            # Runs on the background thread: ONE payload call, itself
            # cached per snapshot generation (v1.2.6) — a warm dialog
            # open performs ZERO database queries. All DB access goes
            # through mw.col.db (never an external sqlite connection).
            try:
                if __package__:
                    from .anki import get_knowledge_dialog_payload
                else:
                    from anki import get_knowledge_dialog_payload  # type: ignore
                return get_knowledge_dialog_payload(
                    force_rebuild=force_rebuild)
            except Exception:
                import traceback
                return {"error": traceback.format_exc()}

        def on_done(future) -> None:
            try:
                data = future.result()
            except Exception:
                import traceback
                data = {"error": traceback.format_exc()}
            self._populate(data)

        mw.taskman.run_in_background(task, on_done)

    # -- Rendering --------------------------------------------------------

    def _populate(self, data: dict) -> None:
        """Fills all four tabs from the background task's result dict."""
        if "error" in data and data.get("error"):
            self.status_label.setText(
                "CompreDef: could not build knowledge summary "
                "(see Anki debug console).")
            print("CompreDef: knowledge dialog error:\n" + str(data["error"]))
            return

        status = data.get("status", {}) or {}
        totals = data.get("totals", {}) or {}
        kanji_points = data.get("kanji_points", {}) or {}
        vocab_points = data.get("vocab_points", {}) or {}
        seen_kanji_points = data.get("seen_kanji_points", {}) or {}
        seen_vocab_points = data.get("seen_vocab_points", {}) or {}
        mature_rows = data.get("mature_rows", []) or []

        mastered_kanji = len(kanji_points)
        mastered_vocab = len(vocab_points)
        seen_kanji = len(seen_kanji_points)
        seen_vocab = len(seen_vocab_points)
        mature_scanned = status.get("mature_notes_scanned",
                                    len(mature_rows))
        scope = status.get("scope", "")

        # Overview: stat cards in a 2-column grid --------------------------
        # (one cramped HBox row was the v1.2.4 clunkiness complaint).
        while self.stat_grid.count():
            item = self.stat_grid.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        cards = [
            ("Mastered kanji",
             f"{mastered_kanji} / {totals.get('kanji', 0)}",
             "Kanji", "Kanji at mastery 1.0 (note interval >= 1 year) / "
             "every kanji in Scope first fields"),
            ("Seen kanji",
             f"{seen_kanji} / {totals.get('kanji', 0)}",
             "Kanji", "Kanji on any in-scope note with interval > 0 / "
             "every kanji in Scope first fields"),
            ("Mastered vocab",
             f"{mastered_vocab} / {totals.get('vocab', 0)}",
             "Vocab", "Kanji-only compound words at mastery 1.0 / every "
             "kanji-only compound in Scope first fields"),
            ("Seen vocab",
             f"{seen_vocab} / {totals.get('vocab', 0)}",
             "Vocab", "Kanji-only compounds on any in-scope note with "
             "interval > 0 / every compound in Scope first fields"),
            ("Mature notes scanned",
             f"{mature_scanned} / {totals.get('scope_notes', 0)}",
             "Mature Notes", "Mature in-scope notes (interval >= 1 year) "
             "the snapshot was built from / every note in your Scope "
             "decks"),
        ]
        for idx, (title, value, target_tab, desc) in enumerate(cards):
            self.stat_grid.addWidget(
                self._make_stat_card(title, value, desc, target_tab),
                idx // 2, idx % 2)

        details_bits = [f"Source: {scope}"]
        if totals.get("total_notes"):
            details_bits.append(
                f"Whole collection: {totals['total_notes']} notes "
                "(Scope is what counts, the rest is never scanned).")
        if status.get("last_error"):
            details_bits.append(f"Last error: {status['last_error']}")
        self.overview_details.setText("\n".join(details_bits))
        self.status_label.setText(
            f"{mastered_kanji} mastered kanji ({seen_kanji} seen) · "
            f"{mastered_vocab} mastered vocab ({seen_vocab} seen) · "
            f"{mature_scanned} mature note(s) in scope"
        )

        # Kanji tab: every SEEN kanji with its mastery weight ----------
        # The seen set is the mastered set's superset (mastered = weight
        # exactly 1.0); listing it makes the mastery sort meaningful —
        # weak kanji sink to the bottom instead of an all-1.00 list.
        self._fill_table(self.kanji_tab, sorted(
            (k, float(pts)) for k, pts in seen_kanji_points.items()))
        self.tabs.setTabText(
            1, f"Kanji ({seen_kanji} seen, {mastered_kanji} mastered)")

        # Vocab tab (kanji-only compounds): every SEEN item -------------
        self._fill_table(self.words_tab, sorted(
            (w, float(pts)) for w, pts in seen_vocab_points.items()))
        self.tabs.setTabText(
            2, f"Vocab ({seen_vocab} seen, {mastered_vocab} mastered)")

        # Mature notes tab --------------------------------------------------
        # Interval = the note's longest card interval (plain "interval";
        # the old "max interval" wording confused the user).
        note_rows = []
        for row in mature_rows:
            word_text = row[0] if isinstance(row, (list, tuple)) else row
            ivl = row[1] if isinstance(row, (list, tuple)) and len(row) > 1 else 0
            try:
                ivl_f = float(ivl)
            except (TypeError, ValueError):
                ivl_f = 0.0
            note_rows.append((str(word_text), ivl_f))
        self._fill_table(self.notes_tab, sorted(note_rows))
        # Same count as the stat card (status), not the row list length
        # (rows can be deduplicated differently on some Anki builds).
        self.tabs.setTabText(3, f"Mature Notes ({mature_scanned})")

        # Provenance searches target first fields of the Scope's note
        # types; resolved once per refresh (schema-proof public APIs).
        try:
            if __package__:
                from .anki import first_field_names_for_scope
            else:
                from anki import first_field_names_for_scope  # type: ignore
            first_fields = first_field_names_for_scope()
        except Exception:
            first_fields = []
        self.kanji_tab._first_fields = first_fields  # type: ignore[attr-defined]
        self.words_tab._first_fields = first_fields  # type: ignore[attr-defined]

    def _make_stat_card(self, title: str, value: str, desc: str,
                        target_tab: str) -> QWidget:
        """Builds one clickable overview stat card."""
        card = QGroupBox()
        card.setTitle(title)
        v = QVBoxLayout(card)
        val_label = QPushButton(value)
        val_label.setToolTip(f"{desc}\nClick to open the {target_tab} tab.")
        val_label.setStyleSheet("font-size: 16px; font-weight: bold;")
        val_label.setFlat(True)
        # v1.2.5: the big number buttons were vertically crushed inside
        # the grid — give them real breathing room (the same fix as
        # _size_button, tuned taller for the 16px font; v1.2.7 bumped to
        # 48 after the user reported they were still crushed).
        val_label.setMinimumHeight(48)
        val_label.setContentsMargins(4, 6, 4, 6)
        # Clicking the big number jumps to the matching detail tab.
        val_label.clicked.connect(
            lambda _, name=target_tab: self._goto_tab(name))
        v.addWidget(val_label)
        d = QLabel(desc)
        d.setWordWrap(True)
        v.addWidget(d)
        return card

    def _fill_table(self, tab: Any, rows: List[tuple]) -> None:
        """Populates a 2-column table tab; updates the visible count.

        rows: sorted (item_text, numeric_value) tuples — Kanji/Vocab
        carry mastery weights (0..1), Mature Notes carry interval days.
        The numeric value is stored as each row's Qt.UserRole data so
        header-click sorting is numeric; the text column shows it
        formatted ("1.00" mastery / "123 days" interval).
        """
        tab._all_rows = [(str(d), float(s)) for d, s in rows]  # type: ignore[attr-defined]
        _knowledge_apply_filter(tab)

    def _goto_tab(self, name: str) -> None:
        """Switches to a detail tab by its tab label prefix."""
        for i in range(self.tabs.count()):
            if self.tabs.tabText(i).startswith(name):
                self.tabs.setCurrentIndex(i)
                return


def _knowledge_table_rows(tab: Any) -> List[tuple]:
    """Returns the tab's rows filtered by the filter box, order intact."""
    edit = getattr(tab, "_filter_edit", None)
    needle = edit.text().strip() if edit is not None else ""
    rows = getattr(tab, "_all_rows", []) or []
    if needle:
        rows = [r for r in rows if needle in r[0]]
    return rows


def _knowledge_apply_filter(tab: Any) -> None:
    """Rebuilds a table tab's visible rows from the filter box.

    Header-click sorting is left to Qt (UserRole numeric — see
    _fill_table); this only (re)populates the table for the current
    filter text and refreshes the 'N shown' count.
    """
    table = getattr(tab, "_table", None)
    if table is None:
        return
    rows = _knowledge_table_rows(tab)
    needle = (getattr(tab, "_filter_edit", None) is not None
              and tab._filter_edit.text().strip())
    is_notes = getattr(tab, "_kind", None) is None

    # Repopulate without triggering per-cell sort churn: sorting stays
    # enabled so header clicks keep working after a filter change.
    table.setSortingEnabled(False)
    table.setRowCount(len(rows))
    user_role = _user_role()
    for i, (text, num) in enumerate(rows):
        first = _NumericTableItem(text)
        if is_notes:
            # Mature notes: interval in days, unknown when unparseable.
            second_txt = (f"{num:.0f} days" if num else "unknown")
        else:
            second_txt = f"{num:.2f}"
        second = _NumericTableItem(second_txt)
        # Numeric sort key on the value column's items.
        second.setData(user_role, float(num))
        first.setData(user_role, float(num))  # same key keeps rows paired
        table.setItem(i, 0, first)
        table.setItem(i, 1, second)
    table.setSortingEnabled(True)
    count_label = getattr(tab, "_count_label", None)
    if count_label is not None:
        total = len(getattr(tab, "_all_rows", []) or [])
        count_label.setText(f"{len(rows)} shown" +
                            (f" / {total}" if needle else ""))


def _open_browser_for_term(tab: Any, row: int) -> None:
    """Opens Anki's Browser with the clicked row's provenance search.

    Kanji rows search first-field CONTAINS (how kanji points are
    admitted); Vocab rows search first-field EXACT (how vocab points
    are admitted) — the search syntax comes from the official Anki
    manual, and every search is LIMITED to the Scope decks (matches
    outside the Scope never contributed mastery points).
    Never raises: a browse failure only prints.
    """
    try:
        table = getattr(tab, "_table", None)
        kind = getattr(tab, "_kind", None)
        if table is None or kind not in ("kanji", "vocab"):
            return
        item = table.item(row, 0)
        if item is None:
            return
        term = item.text()
        if not term:
            return
        # Same context-aware import as every other knowledge helper.
        if __package__:
            from .anki import build_provenance_search, _get_scope_deck_names
        else:
            from anki import (build_provenance_search,  # type: ignore
                              _get_scope_deck_names)
        search = build_provenance_search(
            kind, term, getattr(tab, "_first_fields", None),
            scope_decks=_get_scope_deck_names())
        if not search:
            return
        # dialogs.open returns the existing Browser or creates one —
        # the officially supported way to open the Browse screen.
        from aqt import dialogs
        browser = dialogs.open("Browser", mw)
        # search_for is the modern API; older builds expose searchFor.
        if hasattr(browser, "search_for"):
            browser.search_for(search)
        elif hasattr(browser, "searchFor"):
            browser.searchFor(search)
        print(f"CompreDef: provenance search for '{term}': {search}")
    except Exception:
        import traceback
        print(f"CompreDef: provenance browse failed:\n"
              f"{traceback.format_exc()}")


# Singleton handle for the non-modal knowledge dialog (v1.2.6): the
# old modal exec() LOCKED Anki's Browser while the dialog was open —
# after double-clicking a kanji, the user could not use the Browser
# until closing the knowledge window. A non-modal singleton fixes both
# that and duplicate windows.
_knowledge_dialog_instance: Optional[KnowledgeDialog] = None


def show_knowledge_dialog() -> None:
    """Displays the learner-knowledge debug view — non-modal, singleton.

    Non-modal (show(), not exec()): the dialog never blocks Anki's
    Browser, so the double-click provenance flow works while both
    windows are open. Singleton: re-opening focuses the existing dialog
    (and refreshes its data) instead of stacking copies.
    """
    global _knowledge_dialog_instance
    try:
        if _knowledge_dialog_instance is not None:
            try:
                # Qt object still alive? (sip.isdeleted-style check via
                # a safe attribute touch)
                _knowledge_dialog_instance.isVisible()
            except RuntimeError:
                # C++ object deleted — rebuild below.
                _knowledge_dialog_instance = None
        if _knowledge_dialog_instance is None:
            _knowledge_dialog_instance = KnowledgeDialog(
                parent=mw.app.activeWindow() if mw and mw.app else None)
        # Singleton reuse: just re-show the existing window — NO refresh.
        # Refresh is explicit via the dialog's Refresh button (which calls
        # _refresh_async(force_rebuild=True)). Rebuilding on every menu
        # open is exactly the "loading takes as long as generating" bug:
        # warm opens must be instant (payload cache hit, 0.005 ms).
        _knowledge_dialog_instance.show()
        _knowledge_dialog_instance.raise_()
        _knowledge_dialog_instance.activateWindow()
    except Exception:
        import traceback
        print(f"CompreDef: knowledge dialog failed to open:\n"
              f"{traceback.format_exc()}")
