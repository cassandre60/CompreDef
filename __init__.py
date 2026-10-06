"""
CompreDef - Anki Add-on Entry Point

Automatically generates definitions for Japanese vocabulary cards strictly tailored
to the user's known vocabulary and kanji.
"""

from aqt import mw
from aqt.qt import QMenu
from aqt import gui_hooks
from .gui import show_config_dialog, show_knowledge_dialog, show_scope_dialog
from .editor_browser import setup_editor_browser_hooks
from . import anki

def _add_tools_menu_entry() -> None:
    """
    Adds a direct 'CompreDef Configuration...' entry under Anki's Tools menu.

    Previously the only path was Tools -> Add-ons -> (double-click CompreDef),
    which was buried; the user explicitly asked for one-click access.
    Uses a lambda import guard so the dialog opens fresh config every time.
    """
    if not mw or not hasattr(mw, "form"):
        return
    try:
        tools_menu: QMenu = mw.form.menuTools
        # Primary entry opens directly on the Scope tab (most-used)
        scope_action = tools_menu.addAction("CompreDef Scope...")
        scope_action.setShortcut("Ctrl+Shift+S")
        scope_action.triggered.connect(lambda _: show_scope_dialog())
        action = tools_menu.addAction("CompreDef Configuration...")
        action.setShortcut("Ctrl+Shift+C")
        action.triggered.connect(lambda _: show_config_dialog())
        # Separate visually from Anki's own entries
        tools_menu.insertSeparator(action)
        knowledge_action = tools_menu.addAction("CompreDef Learner Knowledge...")
        knowledge_action.triggered.connect(lambda _: show_knowledge_dialog())
    except Exception as e:
        # Menu injection must never break Anki startup
        print(f"CompreDef: Failed to add Tools menu entry: {e}")


# Register the configuration dialog callback with Anki's Addon Manager
# This allows users to configure the add-on directly from Tools → Add-ons → Config
if mw:
    # Trigger asynchronous build of learner knowledge snapshot on startup
    anki.init_caches_async()
    mw.addonManager.setConfigAction(__name__, show_config_dialog)
    setup_editor_browser_hooks()
    _add_tools_menu_entry()
    # One-line load banner: answers "which version is actually running?"
    # from the debug console. v1.2.15 diagnosis aid — users repeatedly
    # mistook a stale (tray-resident) Anki process for a broken install.
    try:
        import json as _json
        import os as _os
        with open(_os.path.join(_os.path.dirname(__file__),
                               "manifest.json"),
                  encoding="utf-8") as _mf:
            print(f"CompreDef {(_json.load(_mf) or {}).get('human_version', '?')} loaded.")
    except Exception:
        pass


def _on_profile_opened() -> None:
    """
    Builds the learner-knowledge snapshot as soon as the profile (and
    thus the collection) opens. Add-ons load BEFORE the profile, so the
    init_caches_async() call above finds mw.col None and deliberately
    does nothing — this hook is what starts the real build.

    Fresh installs also get their Scope auto-seeded here (Japanese
    decks pre-selected, once) BEFORE the snapshot builds, so scoring
    personalizes immediately without opening the config dialog.
    """
    try:
        from .scope import (maybe_auto_init_scope as _guess,
                            SCOPE_CONFIG_KEY as _KEY,
                            SCOPE_AUTOINIT_CONFIG_KEY as _FLAG)
    except Exception:
        try:
            from scope import (maybe_auto_init_scope as _guess,
                               SCOPE_CONFIG_KEY as _KEY,
                               SCOPE_AUTOINIT_CONFIG_KEY as _FLAG)
        except Exception:
            _guess = None
    try:
        if _guess is not None and mw and mw.addonManager and mw.col:
            name = mw.addonManager.addonFromModule(__name__) or __name__.split('.')[0]
            cfg = mw.addonManager.getConfig(name) or {}
            if not cfg.get(_FLAG) and not list(cfg.get(_KEY) or []):
                guess = _guess(cfg, mw.col)
                cfg[_FLAG] = True
                if guess:
                    cfg[_KEY] = list(guess)
                    print(f"CompreDef: fresh-install Scope auto-selected "
                          f"{len(guess)} Japanese deck(s).")
                mw.addonManager.writeConfig(name, cfg)
    except Exception as e:
        # Must never break profile loading
        print(f"CompreDef: scope auto-init on profile open failed: {e}")
    try:
        anki.init_caches_async()
    except Exception as e:
        # Must never break profile loading
        print(f"CompreDef: knowledge build on profile open failed: {e}")


# profile_did_open fires right after the collection loads (aqt/main.py),
# once per profile open. The hasattr guard keeps compatibility should a
# future Anki rename/remove the hook — the snapshot then builds lazily on
# the first generation instead.
if hasattr(gui_hooks, "profile_did_open"):
    gui_hooks.profile_did_open.append(_on_profile_opened)

