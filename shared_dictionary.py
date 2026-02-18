#!/usr/bin/env python3
"""
Shared Dictionary – layered meanings

Features:
- Canonical definitions via Wiktionary (cached)
- Personal + shared overlays via dict_*.json
- Word-first interactive REPL
- Sensible sense ranking (first etymology + common POS)
- Overlay search (:search)
- Persistent last-used dictionary for :add
- Mobile-friendly wrapping and spacing
- Optional color output (termcolor)

Design goal:
Meaning should be legible, layered, and non-destructive.
"""

import glob
import html
import json
import os
import re
import shutil
import sys
import textwrap
from datetime import datetime
from typing import Any, Dict, List, Optional

import requests

try:
    from termcolor import colored
except ImportError:
    def colored(text: str, *args, **kwargs) -> str:
        return text  # graceful fallback


# ============================
# Configuration
# ============================

DICT_PATTERN = "dict_*.json"
CANONICAL_CACHE_FILE = "canonical_cache.json"
LAST_USER_FILE = ".last_dict_user"

USER_AGENT = "SharedDictionary/1.4 (CLI)"
COLORS = True


# ============================
# Terminal helpers
# ============================

def term_width() -> int:
    return shutil.get_terminal_size((80, 20)).columns


def max_defs_for_terminal() -> int:
    return 3 if term_width() < 90 else 5


def wrap(text: str, indent: int = 0) -> str:
    width = term_width()
    return "\n".join(
        textwrap.wrap(
            text,
            width=width - indent,
            subsequent_indent=" " * indent,
            break_long_words=False,
            break_on_hyphens=False,
        )
    )


def c(text: str, color: str = None, attrs: list = None) -> str:
    if not COLORS:
        return text
    return colored(text, color, attrs=attrs or [])


# ============================
# Text cleaning
# ============================

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def clean_definition(text: Optional[str]) -> str:
    if not text:
        return ""
    text = html.unescape(text)
    text = _TAG_RE.sub("", text)
    text = _WS_RE.sub(" ", text).strip()
    return text


def is_valid_definition(text: str) -> bool:
    blacklist = [
        "ISO 639",
        "language code for",
        "Misspelling of",
        "Alternative form of",
        "Alternative spelling of",
    ]
    t = text.lower()
    return not any(b.lower() in t for b in blacklist)


# ============================
# JSON helpers
# ============================

def load_json(path: str, default: Any = None) -> Any:
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(c(f"⚠️ Failed to load {path}: {e}", "yellow"))
        return default


def save_json(path: str, data: Any):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(c(f"Error saving {path}: {e}", "red"))


# ============================
# Canonical cache
# ============================

def load_canonical_cache() -> Dict[str, Any]:
    return load_json(CANONICAL_CACHE_FILE, {})


def save_canonical_cache(cache: Dict[str, Any]):
    save_json(CANONICAL_CACHE_FILE, cache)


def load_last_user_file() -> Optional[str]:
    return load_json(LAST_USER_FILE)


def save_last_user_file(filename: str):
    save_json(LAST_USER_FILE, filename)


# ============================
# Wiktionary logic
# ============================

def rank_definition(defn: Dict[str, Any], is_first_etym: bool) -> int:
    pos = (defn.get("part_of_speech") or "").lower()
    text = defn.get("text", "").lower()
    score = 0

    pos_priority = {
        "noun": 10,
        "verb": 8,
        "adjective": 6,
        "adverb": 5,
        "": 0,
    }
    score += pos_priority.get(pos, 2)

    if is_first_etym:
        score += 15

    niche_terms = [
        "poker", "quaker", "assassination", "afterburner", "engine",
        "laboratory", "sexually", "alcohol", "heraldry", "chess",
        "card game", "military", "finance", "computing", "slang"
    ]
    for n in niche_terms:
        if n in text:
            score -= 6

    score += len(text) // 120
    return score


def dedupe_definitions(defs: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = {}
    for d in defs:
        key = d["text"][:50].lower()
        if key not in seen or len(d["text"]) < len(seen[key]["text"]):
            seen[key] = d
    return list(seen.values())


def fetch_from_wiktionary(word: str) -> Dict[str, Any]:
    url = f"https://en.wiktionary.org/api/rest_v1/page/definition/{requests.utils.quote(word)}"

    base = {
        "definitions": [],
        "source": "wiktionary",
        "source_url": f"https://en.wiktionary.org/wiki/{word}",
        "fetched": datetime.utcnow().isoformat() + "Z",
    }

    try:
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=12)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        base["definitions"].append({"text": f"[Lookup error: {e}]", "part_of_speech": None})
        return base

    english = data.get("en", [])
    if not english:
        base["definitions"].append({"text": "[No English definitions found]", "part_of_speech": None})
        return base

    defs = []
    first_etym = True
    for entry in english:
        pos = entry.get("partOfSpeech")
        for d in entry.get("definitions", []):
            cleaned = clean_definition(d.get("definition"))
            if not cleaned or not is_valid_definition(cleaned):
                continue
            defs.append({
                "text": cleaned,
                "part_of_speech": pos,
                "_first": first_etym
            })
        first_etym = False

    defs.sort(
        key=lambda d: rank_definition(d, d.pop("_first", False)),
        reverse=True
    )
    defs = dedupe_definitions(defs)
    base["definitions"] = defs
    return base


def lookup_canonical(word: str, cache: Dict[str, Any]) -> Dict[str, Any]:
    w = word.lower().strip()
    if w in cache:
        return cache[w]
    entry = fetch_from_wiktionary(w)
    cache[w] = entry
    save_canonical_cache(cache)
    return entry


# ============================
# Overlays
# ============================

def load_shared_dictionaries() -> List[Dict[str, Any]]:
    out = []
    for path in sorted(glob.glob(DICT_PATTERN)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                d = json.load(f)
                if isinstance(d, dict) and "entries" in d:
                    out.append({"path": path, **d})
        except Exception as e:
            print(c(f"Failed to load {path}: {e}", "yellow"))
    return out


def aggregate_overlays(word: str, dictionaries: List[Dict[str, Any]]) -> List[Dict]:
    w = word.lower()
    shared = []
    for d in dictionaries:
        user = d.get("user_id") or os.path.basename(d["path"]).replace("dict_", "").replace(".json", "")
        for entry in d.get("entries", {}).get(w, []):
            shared.append({
                "user": user,
                "text": entry.get("text", ""),
                "tags": entry.get("tags", []),
                "created": entry.get("created", "")
            })
    shared.sort(key=lambda x: x.get("created") or "", reverse=True)
    return shared


def search_overlays(pattern: str, dictionaries: List[Dict[str, Any]]) -> List[str]:
    p = pattern.lower()
    hits = set()
    for d in dictionaries:
        for word, entries in d.get("entries", {}).items():
            for e in entries:
                if p in word or p in e.get("text", "").lower():
                    hits.add(word)
    return sorted(hits)


def list_augmented_words(dictionaries: List[Dict[str, Any]]) -> List[str]:
    words = set()
    for d in dictionaries:
        words.update(d.get("entries", {}).keys())
    return sorted(words)


def add_personal_definition(word: str, text: str, user_file: str):
    data = load_json(
        user_file,
        {"user_id": user_file.replace("dict_", "").replace(".json", ""), "entries": {}}
    )

    entry = {
        "text": text.strip(),
        "tags": [],
        "created": datetime.utcnow().isoformat() + "Z"
    }

    data.setdefault("entries", {}).setdefault(word.lower(), []).append(entry)
    save_json(user_file, data)
    save_last_user_file(user_file)
    print(c("✓ Definition added.", "green"))


# ============================
# Output
# ============================

def print_canonical(entry: Dict[str, Any]):
    defs = entry.get("definitions", [])
    max_defs = max_defs_for_terminal()

    print()
    print(c("▸ Canonical meaning", "cyan", attrs=["bold"]))

    for i, d in enumerate(defs[:max_defs], 1):
        pos = d.get("part_of_speech") or ""
        prefix = f"{i}. ({pos}) " if pos else f"{i}. "
        print(wrap(prefix + d.get("text", ""), indent=4))
        print()

    if len(defs) > max_defs:
        print(c(f"  … {len(defs) - max_defs} more (see source)", "grey"))

    print(c(f"  ↳ {entry.get('source_url')}", "blue"))


def print_shared(shared: List[Dict]):
    print()
    if not shared:
        print(c("▸ No personal or shared meanings yet", "yellow"))
        print(c("  Use :add to contribute one.", "grey"))
        return

    print(c("▸ Additional meanings", "magenta", attrs=["bold"]))
    for s in shared:
        print(c(f"• {s['user']}", "magenta"))
        print(wrap(s["text"], indent=4))
        print()


def print_help():
    print(c("\nCommands:", "cyan", attrs=["bold"]))
    print("  word                → look up a word")
    print("  :add                → add your own definition")
    print("  :list               → list words with overlays")
    print("  :search <text>      → search overlay text")
    print("  :help               → show this help")
    print("  :quit / :q / 0      → exit")


# ============================
# REPL
# ============================

def main():
    cache = load_canonical_cache()
    dictionaries = load_shared_dictionaries()
    last_user_file = load_last_user_file()

    print(c("Shared Dictionary", "green", attrs=["bold"]))
    print("Type a word to look it up.")
    print("Type :help for commands.\n")

    while True:
        try:
            raw = input(c("> ", "grey")).strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not raw:
            continue

        cmd = raw.lower()

        if cmd in (":quit", ":q", ":exit", "0"):
            break

        if cmd == ":help":
            print_help()
            continue

        if cmd == ":list":
            words = list_augmented_words(dictionaries)
            if not words:
                print(c("No words with additional meanings yet.", "yellow"))
            else:
                print(c("\nWords with additional meanings:", "cyan"))
                for w in words:
                    print(f"  • {w}")
            continue

        if cmd.startswith(":search "):
            pattern = raw[8:].strip()
            results = search_overlays(pattern, dictionaries)
            if not results:
                print(c(f"No matches for '{pattern}'", "yellow"))
            else:
                print(c("\nFound in overlays:", "cyan"))
                for r in results:
                    print(f"  • {r}")
            continue

        if cmd == ":add":
            word = input("Word: ").strip()
            if not word:
                continue
            text = input("Your definition: ").strip()
            if not text:
                continue

            default = last_user_file or "dict_default.json"
            user_file = input(f"Dictionary file [{default}]: ").strip() or default

            if not user_file.startswith("dict_"):
                user_file = f"dict_{user_file}"
            if not user_file.endswith(".json"):
                user_file += ".json"

            add_personal_definition(word, text, user_file)
            dictionaries = load_shared_dictionaries()
            continue

        # Word lookup
        canonical = lookup_canonical(raw, cache)
        print_canonical(canonical)
        shared = aggregate_overlays(raw, dictionaries)
        print_shared(shared)


if __name__ == "__main__":
    main()