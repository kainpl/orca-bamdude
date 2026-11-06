"""The plugin's sentences in English and Ukrainian (host spec r2.3.2 §10).

OrcaSlicer reports its UI language as a code such as ``uk_UA`` (``app_info()["language"]``, UI thread only); the
plugin speaks Ukrainian for ``uk`` and English otherwise. The sentences live in ``locales/<language>.json`` beside
this module -- the wheel is unpacked on disk, so they are read as files -- and are loaded once per language.
A missing key is answered with the key itself: a sentence never fails the call that shows it.
"""

from __future__ import annotations

import json
import os
import threading

LANGUAGES = ("en", "uk")
_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "locales")
_lock = threading.Lock()
_loaded: dict[str, dict] = {}


def language(code) -> str:
    return "uk" if isinstance(code, str) and code.lower().startswith("uk") else "en"


def _flat(data: dict, prefix: str = "") -> dict:
    out: dict = {}
    for key, value in data.items():
        if isinstance(value, dict):
            out.update(_flat(value, f"{prefix}{key}."))
        elif isinstance(value, str):
            out[f"{prefix}{key}"] = value
    return out


def _sentences(lang: str) -> dict:
    with _lock:
        if lang not in _loaded:
            try:
                with open(os.path.join(_FOLDER, f"{lang}.json"), encoding="utf-8") as handle:
                    data = json.load(handle)
                _loaded[lang] = _flat(data) if isinstance(data, dict) else {}
            except (OSError, ValueError):
                _loaded[lang] = {}
        return _loaded[lang]


def say(lang: str, key: str) -> str:
    text = _sentences(lang if lang in LANGUAGES else "en").get(key)
    if text is None and lang != "en":
        text = _sentences("en").get(key)
    return text if text is not None else key


__all__ = ["LANGUAGES", "language", "say"]
