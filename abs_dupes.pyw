#!/usr/bin/env python3
"""
ABS Duplicate Finder  v2.0
Finds likely duplicate items in Audiobookshelf libraries and shows them side by side.

Windows : double-click abs_dupes.pyw  (opens http://127.0.0.1:5057 in your browser)
Docker  : see docker-compose.yml       (serves on port 5057, settings in /config)

Scanning is read-only. Editing is OFF until you turn it on in Connection settings.
With editing on:
  * Metadata edits and "embed into audio files" go through the ABS API, so ABS
    stays the source of truth and rewrites its own metadata.json.
  * File/folder operations, direct metadata.json edits and direct tag edits
    touch the disk through a path mapping (ABS path -> local path).
  * Nothing is deleted outright: files and folders go to a trash folder inside
    the library root, marked with a .ignore file so ABS skips it.
  * Every change is logged to abs_dupes_changes.log in the config folder.
"""
import os
import sys

# pythonw.exe has no console; give Flask/werkzeug somewhere harmless to write.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import csv
import functools
import difflib
import io
import json
import re
import secrets
import shutil
import socket
import threading
import time
import unicodedata
import webbrowser
from collections import defaultdict

APP_NAME = "ABS Duplicate Finder"
VERSION = "2.0"
IN_DOCKER = os.path.exists("/.dockerenv") or os.environ.get("IN_DOCKER") == "1"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_DIR = "/config" if IN_DOCKER else BASE_DIR
SETTINGS_FILE = os.path.join(CONFIG_DIR, "abs_dupes_settings.json")
DISMISSED_FILE = os.path.join(CONFIG_DIR, "abs_dupes_dismissed.json")
LAST_SCAN_FILE = os.path.join(CONFIG_DIR, "abs_dupes_last_scan.json")
LOG_FILE = os.path.join(CONFIG_DIR, "abs_dupes_changes.log")
PORT = int(os.environ.get("PORT", "5057"))
HOST = "0.0.0.0" if IN_DOCKER else "127.0.0.1"


def _fatal(msg):
    try:
        import tkinter
        from tkinter import messagebox
        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror(APP_NAME, msg)
    except Exception:
        pass
    print(msg, file=sys.stderr)
    sys.exit(1)


try:
    import requests
    from flask import Flask, Response, jsonify, redirect, render_template_string, request, url_for
except ImportError as e:
    _fatal(f"Missing Python package: {e.name}\n\nOpen a Command Prompt and run:\n\n"
           f"    py -m pip install flask requests rapidfuzz mutagen")

try:
    from rapidfuzz import fuzz

    def similarity(a, b):
        return fuzz.token_sort_ratio(a, b)
    FUZZ_ENGINE = "rapidfuzz"
except ImportError:
    def similarity(a, b):
        a = " ".join(sorted(a.split()))
        b = " ".join(sorted(b.split()))
        return difflib.SequenceMatcher(None, a, b).ratio() * 100
    FUZZ_ENGINE = "difflib (install rapidfuzz for faster scans)"

try:
    import mutagen
    from mutagen.easymp4 import EasyMP4Tags
    EasyMP4Tags.RegisterTextKey("composer", "\xa9wrt")   # narrator lives here in most m4b files
    HAVE_MUTAGEN = True
except ImportError:
    HAVE_MUTAGEN = False

try:
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
except Exception:
    pass


# --------------------------------------------------------------------------- settings / storage

DEFAULT_SETTINGS = {
    "abs_url": os.environ.get("ABS_URL", ""),
    "token": os.environ.get("ABS_TOKEN", ""),
    "verify_ssl": os.environ.get("ABS_VERIFY_SSL", "1") != "0",
    "editing": os.environ.get("ABS_EDITING", "0") == "1",
    "path_map": os.environ.get("PATH_MAP", ""),        # e.g. "/audiobooks=/library"
    "trash_dir": os.environ.get("TRASH_DIR", ".abs-dupes-trash"),
}
DEFAULT_OPTS = {
    "libs": [], "m_ids": True, "m_exact": True, "m_fuzzy": True, "threshold": 90,
    "m_dur": True, "cross_library": True, "cross_format": False,
}


def _read_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


def config_problem():
    """Returns a readable reason the config folder can't be written to, or None."""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        probe = os.path.join(CONFIG_DIR, ".write-test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return None
    except OSError as e:
        who = ""
        if hasattr(os, "geteuid"):
            who = f" The app is running as uid {os.geteuid()}, gid {os.getegid()}."
        return (f"Can't write to the settings folder {CONFIG_DIR}: {e.strerror or e}.{who}"
                + (" Check the folder is mounted and that this user can write to it."
                   if IN_DOCKER else " Check the folder exists and isn't read-only."))


def load_settings():
    s = dict(DEFAULT_SETTINGS)
    saved = _read_json(SETTINGS_FILE, {})
    for k, v in saved.items():
        if v not in ("", None):
            s[k] = v
    s["abs_url"] = (s.get("abs_url") or "").strip().rstrip("/")
    return s


def load_dismissed():
    return set(_read_json(DISMISSED_FILE, []))


def save_dismissed(keys):
    _write_json(DISMISSED_FILE, sorted(keys))


# --------------------------------------------------------------------------- ABS client (GET only)

class ABS:
    def __init__(self, settings):
        self.url = settings["abs_url"]
        self.verify = bool(settings.get("verify_ssl", True))
        self.headers = {"Authorization": f"Bearer {settings.get('token', '')}"}

    def get(self, path, timeout=60, **params):
        r = requests.get(self.url + path, headers=self.headers, params=params,
                         timeout=timeout, verify=self.verify)
        r.raise_for_status()
        return r

    def send(self, method, path, json_body=None, timeout=120, **params):
        r = requests.request(method, self.url + path, headers=self.headers, params=params,
                             json=json_body, timeout=timeout, verify=self.verify)
        r.raise_for_status()
        return r

    def item(self, item_id):
        return self.get(f"/api/items/{item_id}", expanded=1).json()

    def update_media(self, item_id, payload):
        return self.send("PATCH", f"/api/items/{item_id}/media", json_body=payload).json()

    def embed_metadata(self, item_id):
        # ABS writes its metadata, cover and chapters into the audio files with ffmpeg,
        # keeping a backup of the originals in its own metadata/cache folder.
        return self.send("POST", f"/api/tools/item/{item_id}/embed-metadata",
                         forceEmbedChapters=0, backup=1)

    def scan_item(self, item_id):
        return self.send("POST", f"/api/items/{item_id}/scan")

    def scan_library(self, lib_id):
        return self.send("POST", f"/api/libraries/{lib_id}/scan")

    def remove_item(self, item_id):
        # Removes the database entry only; the files were already moved to trash by us.
        return self.send("DELETE", f"/api/items/{item_id}")

    def libraries(self):
        libs = self.get("/api/libraries", timeout=15).json().get("libraries", [])
        return [l for l in libs if l.get("mediaType", "book") == "book"]

    def items(self, lib_id, progress=None):
        out, page = [], 0
        while True:
            d = self.get(f"/api/libraries/{lib_id}/items",
                         limit=500, page=page, minified=1).json()
            batch = d.get("results") or []
            out.extend(batch)
            total = d.get("total") or len(out)
            if progress:
                progress(len(out), total)
            if not batch or len(out) >= total:
                return out
            page += 1


def friendly_error(e, url):
    if isinstance(e, requests.exceptions.SSLError):
        return f"SSL certificate check failed for {url}. Untick “Verify SSL certificate” if you use a self-signed certificate."
    if isinstance(e, requests.exceptions.ConnectionError):
        return f"Could not reach Audiobookshelf at {url}. Check the address and port, and that ABS is running."
    if isinstance(e, requests.exceptions.Timeout):
        return f"Audiobookshelf at {url} took too long to answer."
    if isinstance(e, requests.exceptions.HTTPError):
        code = e.response.status_code if e.response is not None else "?"
        if code == 401:
            return "Audiobookshelf rejected the API token. Paste a fresh one from ABS Settings."
        if code == 403:
            return "Audiobookshelf refused that action. Editing, scanning and embedding need an admin user's token."
        if code == 404:
            return f"Got “not found” from {url}. If ABS runs under a sub-path, include it in the address."
        return f"Audiobookshelf returned HTTP {code}."
    if isinstance(e, requests.exceptions.MissingSchema):
        return "The server address needs to start with http:// or https://"
    return f"Unexpected error: {e!r}"


# --------------------------------------------------------------------------- normalisation

NUMWORDS = {
    "one": "1", "two": "2", "three": "3", "four": "4", "five": "5", "six": "6", "seven": "7",
    "eight": "8", "nine": "9", "ten": "10", "eleven": "11", "twelve": "12",
    "first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5",
    "i": "1", "ii": "2", "iii": "3", "iv": "4", "v": "5", "vi": "6", "vii": "7",
    "viii": "8", "ix": "9", "x": "10",
}
_JUNK_BRACKETS = re.compile(r"[\(\[\{][^\)\]\}]*[\)\]\}]")
_JUNK_WORDS = re.compile(r"\b(unabridged|abridged|audiobook|audio book|dramati[sz]ed|"
                         r"full cast|retail|mp3|m4b|a novel)\b")


def ascii_lower(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()


def norm_title(t):
    t = ascii_lower(t).replace("&", " and ")
    t = _JUNK_BRACKETS.sub(" ", t)
    t = _JUNK_WORDS.sub(" ", t)
    t = re.sub(r"[^a-z0-9 ]+", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    t = re.sub(r"^(the|a|an) ", "", t)
    return t


def main_title(t):
    """Title with any ': subtitle' or ' - subtitle' removed."""
    return norm_title(re.split(r"\s*:\s+|\s+[-–—]\s+", t or "", maxsplit=1)[0])


def split_authors(a):
    parts = re.split(r"\s*(?:,|;|&|\band\b)\s*", a or "")
    return [p.strip() for p in parts if p.strip()]


def norm_author(a):
    a = ascii_lower(a).replace(".", " ")
    a = re.sub(r"[^a-z ]+", " ", a)
    a = re.sub(r"\b(?:jr|sr|phd|md|dr)\b", " ", a)
    words = a.split()
    # join runs of initials: "j r r tolkien" -> "jrr tolkien"
    merged, buf = [], ""
    for w in words:
        if len(w) == 1:
            buf += w
        else:
            if buf:
                merged.append(buf)
                buf = ""
            merged.append(w)
    if buf:
        merged.append(buf)
    surname = merged[-1] if merged else ""
    return " ".join(sorted(merged)), surname


def number_tokens(nt):
    out = set()
    for w in nt.split():
        if w.isdigit():
            out.add(str(int(w)))
        elif w in NUMWORDS:
            out.add(NUMWORDS[w])
    return frozenset(out)


def series_seq(series):
    m = re.search(r"#\s*([\d.]+)", series or "")
    return m.group(1).rstrip(".") if m else ""


def to_record(it, lib):
    m = it.get("media") or {}
    md = m.get("metadata") or {}
    title = md.get("title") or os.path.basename(it.get("relPath") or it.get("path") or "") or "(untitled)"
    author = md.get("authorName") or ""
    series = md.get("seriesName") or ""
    duration = float(m.get("duration") or 0)
    n_audio = int(m.get("numAudioFiles") or m.get("numTracks") or 0)
    ebook = m.get("ebookFormat") or (m.get("ebookFile") or {}).get("ebookFormat") or ""
    if (n_audio or duration) and ebook:
        kind = "Audio + ebook"
    elif n_audio or duration:
        kind = "Audiobook"
    elif ebook:
        kind = "Ebook only"
    else:
        kind = "No media files"
    authors = split_authors(author)
    na, surname = norm_author(authors[0]) if authors else ("", "")
    nt = norm_title(title)
    return {
        "id": it.get("id", ""), "libraryId": lib.get("id", ""), "library": lib.get("name", ""),
        "title": title, "subtitle": md.get("subtitle") or "", "author": author,
        "narrator": md.get("narratorName") or "", "series": series,
        "year": md.get("publishedYear") or "", "asin": (md.get("asin") or "").strip().upper(),
        "isbn": re.sub(r"[^0-9Xx]", "", md.get("isbn") or "").upper(),
        "duration": duration, "size": int(it.get("size") or m.get("size") or 0),
        "numFiles": int(it.get("numFiles") or 0), "numAudio": n_audio,
        "ebook": ebook.upper(), "kind": kind, "path": it.get("path") or "",
        "addedAt": int(it.get("addedAt") or 0),
        "missing": bool(it.get("isMissing")), "invalid": bool(it.get("isInvalid")),
        # match keys (not persisted)
        "_nt": nt, "_mt": main_title(title), "_na": na, "_surname": surname,
        "_nums": number_tokens(nt), "_mnums": number_tokens(main_title(title)),
        "_seq": series_seq(series),
    }


# --------------------------------------------------------------------------- matching

REASONS = {  # label -> strength (lower is stronger)
    "Same ASIN": 0,
    "Same ISBN": 1,
    "Same title and author": 2,
    "Same title, different subtitle": 3,
    "Similar title": 4,
    "Same author and length": 5,
}


def find_duplicates(recs, opts, progress=None):
    n = len(recs)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    edges = []

    def link(i, j, reason, score=100):
        a, b = recs[i], recs[j]
        if not opts["cross_library"] and a["libraryId"] != b["libraryId"]:
            return
        if not opts["cross_format"] and {a["kind"], b["kind"]} == {"Audiobook", "Ebook only"}:
            return
        edges.append((i, j, reason, int(round(score))))
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    def bucket_match(keyfn, reason, max_bucket=50):
        buckets = defaultdict(list)
        for i, r in enumerate(recs):
            k = keyfn(r)
            if k:
                buckets[k].append(i)
        for ids in buckets.values():
            if 1 < len(ids) <= max_bucket:   # huge buckets are junk values, not duplicates
                for x in range(len(ids)):
                    for y in range(x + 1, len(ids)):
                        link(ids[x], ids[y], reason)

    if opts["m_ids"]:
        bucket_match(lambda r: r["asin"] if len(r["asin"]) >= 10 else "", "Same ASIN")
        bucket_match(lambda r: r["isbn"] if len(r["isbn"]) in (10, 13) else "", "Same ISBN")
    if opts["m_exact"]:
        bucket_match(lambda r: (r["_nt"], r["_na"]) if r["_nt"] and r["_na"] else "",
                     "Same title and author")

    blocks = defaultdict(list)
    for i, r in enumerate(recs):
        if r["_surname"]:
            blocks[r["_surname"]].append(i)

    total_blocks = max(len(blocks), 1)
    threshold = opts["threshold"]
    for bn, ids in enumerate(blocks.values()):
        if progress and bn % 200 == 0:
            progress(bn, total_blocks)
        if len(ids) < 2:
            continue
        if (opts["m_fuzzy"] or opts["m_exact"]) and len(ids) <= 4000:
            for x in range(len(ids)):
                a = recs[ids[x]]
                for y in range(x + 1, len(ids)):
                    b = recs[ids[y]]
                    if a["_nt"] == b["_nt"]:
                        continue  # handled by exact match
                    if a["_seq"] and b["_seq"] and a["_seq"] != b["_seq"]:
                        continue  # different entries in a series
                    if a["_na"] != b["_na"] and similarity(a["_na"], b["_na"]) < 85:
                        continue  # same surname, different person
                    if (opts["m_exact"] and len(a["_mt"]) >= 4 and a["_mt"] == b["_mt"]
                            and a["_mnums"] == b["_mnums"]):
                        link(ids[x], ids[y], "Same title, different subtitle")
                        continue
                    if opts["m_fuzzy"] and a["_nums"] == b["_nums"]:
                        s = similarity(a["_nt"], b["_nt"])
                        if s >= threshold:
                            link(ids[x], ids[y], "Similar title", s)
        if opts["m_dur"]:
            by_author = defaultdict(list)
            for i in ids:
                if recs[i]["duration"] >= 1200:
                    by_author[recs[i]["_na"]].append(i)
            for aids in by_author.values():
                aids.sort(key=lambda i: recs[i]["duration"])
                for x in range(len(aids)):
                    da = recs[aids[x]]["duration"]
                    tol = max(3.0, da * 0.0005)   # ~18 s on a 10-hour book
                    for y in range(x + 1, len(aids)):
                        if recs[aids[y]]["duration"] - da > tol:
                            break
                        link(aids[x], aids[y], "Same author and length")

    # assemble groups
    members = defaultdict(set)
    greasons = defaultdict(dict)
    for i, j, reason, score in edges:
        root = find(i)
        members[root].update((i, j))
        greasons[root][reason] = max(score, greasons[root].get(reason, 0))

    groups = []
    for root, idx in members.items():
        items = [public(recs[i]) for i in idx]
        items.sort(key=lambda r: (r["missing"], -r["size"], r["addedAt"]))
        reasons = sorted(({"reason": k, "score": v, "strength": REASONS[k]}
                          for k, v in greasons[root].items()),
                         key=lambda r: (r["strength"], -r["score"]))
        groups.append(describe_group(items, reasons))
    groups.sort(key=lambda g: (g["reasons"][0]["strength"], g["books"][0]["title"].lower()))
    return groups


def public(r):
    return {k: v for k, v in r.items() if not k.startswith("_")}


def describe_group(items, reasons):
    def differs(field, fn=lambda v: (v or "").strip().lower()):
        return len({fn(i[field]) for i in items}) > 1

    diff = [f for f in ("title", "author", "narrator", "series", "kind", "library") if differs(f)]
    durs = [i["duration"] for i in items if i["duration"]]
    if durs and (len(durs) != len(items) or max(durs) - min(durs) > 120):
        diff.append("duration")
    sizes = [i["size"] for i in items]
    added = [i["addedAt"] for i in items]
    text = " ".join(f"{i['title']} {i['subtitle']} {i['author']} {i['narrator']} {i['series']} {i['path']}"
                    for i in items).lower()
    return {
        "key": "|".join(sorted(i["id"] for i in items)),
        "books": items, "reasons": reasons, "diff": diff, "text": text,
        "largest": max(items, key=lambda i: i["size"])["id"] if len(set(sizes)) > 1 else "",
        "newest": max(items, key=lambda i: i["addedAt"])["id"] if len(set(added)) > 1 else "",
    }


# --------------------------------------------------------------------------- disk operations

class DiskError(Exception):
    pass


_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WIN_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
TAG_FIELDS = ["title", "album", "artist", "albumartist", "composer", "genre", "date"]
TAG_LABELS = {"title": "Title", "album": "Album", "artist": "Artist", "albumartist": "Album artist",
              "composer": "Composer (narrator)", "genre": "Genre", "date": "Year"}
AUDIO_EXT = {".mp3", ".m4b", ".m4a", ".mp4", ".flac", ".ogg", ".opus", ".aac", ".wma", ".wav"}


def parse_path_map(text):
    pairs = []
    for line in re.split(r"[;\n]", text or ""):
        if "=" in line:
            a, b = line.split("=", 1)
            a, b = a.strip().rstrip("/"), b.strip().rstrip("/\\")
            if a and b:
                pairs.append((a, b))
    return sorted(pairs, key=lambda p: -len(p[0]))


def to_local(abs_path, settings):
    """Map a path as ABS sees it to a path this app can reach. Returns (local, root) or (None, None)."""
    for a, b in parse_path_map(settings.get("path_map")):
        if abs_path == a or abs_path.startswith(a + "/"):
            rest = [p for p in abs_path[len(a):].split("/") if p]
            return (os.path.join(b, *rest) if rest else b), b
    return None, None


def inside(path, root):
    try:
        rp, rr = os.path.realpath(path), os.path.realpath(root)
        return os.path.commonpath([rp, rr]) == rr
    except ValueError:  # different drives on Windows
        return False


def check_name(name):
    name = (name or "").strip()
    if (not name or name in (".", "..") or _BAD_NAME.search(name) or name.endswith(".")
            or name.split(".")[0].lower() in _WIN_RESERVED or len(name) > 240):
        raise DiskError(f"“{name}” is not a valid file or folder name.")
    return name


def log_change(action, **kw):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "action": action, **kw},
                           ensure_ascii=False) + "\n")


def trash_path(src, root, settings):
    trash = os.path.join(root, check_name(settings.get("trash_dir") or ".abs-dupes-trash"))
    os.makedirs(trash, exist_ok=True)
    marker = os.path.join(trash, ".ignore")          # ABS skips folders containing .ignore
    if not os.path.exists(marker):
        open(marker, "w").close()
    return os.path.join(trash, time.strftime("%Y%m%d-%H%M%S_") + os.path.basename(src.rstrip("/\\")))


def move_to_trash(src, root, settings):
    if not inside(src, root) or os.path.realpath(src) == os.path.realpath(root):
        raise DiskError("Refusing to trash something outside the mapped library folder.")
    if not os.path.exists(src):
        raise DiskError(f"Not found on disk: {src}")
    dest = trash_path(src, root, settings)
    shutil.move(src, dest)
    log_change("trash", src=src, dest=dest)
    return dest


def safe_rename(src, dest, root):
    if not os.path.exists(src):
        raise DiskError(f"Not found on disk: {src}")
    if not inside(src, root) or not inside(dest, root):
        raise DiskError("Both locations must be inside the mapped library folder.")
    if os.path.exists(dest) and os.path.normcase(os.path.realpath(src)) != os.path.normcase(os.path.realpath(dest)):
        raise DiskError(f"Something already exists at {dest}")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    if os.path.normcase(src) == os.path.normcase(dest) and src != dest:   # case-only rename on Windows
        tmp = src + ".renaming"
        os.rename(src, tmp)
        os.rename(tmp, dest)
    else:
        shutil.move(src, dest)
    log_change("rename", src=src, dest=dest)


def read_tags(path):
    if not HAVE_MUTAGEN:
        return {"error": "Install mutagen to read embedded tags."}
    try:
        f = mutagen.File(path, easy=True)
        if f is None:
            return {"error": "Unsupported format"}
        tags = f.tags or {}
        out = {k: "; ".join(str(v) for v in tags.get(k, [])) for k in TAG_FIELDS}
        out["length"] = getattr(f.info, "length", 0) or 0
        return out
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


def write_tags(path, values):
    f = mutagen.File(path, easy=True)
    if f is None:
        raise DiskError(f"Can't write tags to {os.path.basename(path)} (unsupported format).")
    if f.tags is None:
        f.add_tags()
    for k, v in values.items():
        if v == "":
            if k in f.tags:
                del f.tags[k]
        else:
            f.tags[k] = [v]
    f.save()


def drop_from_last_scan(item_id):
    result = _read_json(LAST_SCAN_FILE, None)
    if not result:
        return
    kept = []
    for g in result["groups"]:
        books = [b for b in g["books"] if b["id"] != item_id]
        if len(books) >= 2:
            if len(books) != len(g["books"]):
                g = describe_group(books, g["reasons"])
            kept.append(g)
    result["groups"] = kept
    result["item_count"] = sum(len(g["books"]) for g in kept)
    _write_json(LAST_SCAN_FILE, result)


# --------------------------------------------------------------------------- background scan

STATE = {"running": False, "phase": "", "done": 0, "total": 0, "error": None}
STATE_LOCK = threading.Lock()


def set_state(**kw):
    with STATE_LOCK:
        STATE.update(kw)


def run_scan(lib_ids, opts):
    settings = load_settings()
    try:
        api = ABS(settings)
        libs = {l["id"]: l for l in api.libraries()}
        recs, names = [], []
        for lid in lib_ids:
            lib = libs.get(lid)
            if not lib:
                continue
            names.append(lib.get("name", lid))
            set_state(phase=f"Reading {lib.get('name', lid)}", done=0, total=0)
            items = api.items(lid, progress=lambda d, t: set_state(done=d, total=t))
            recs.extend(to_record(it, lib) for it in items)
        set_state(phase=f"Comparing {len(recs):,} items", done=0, total=0)
        groups = find_duplicates(recs, opts, progress=lambda d, t: set_state(done=d, total=t))
        result = {
            "groups": groups, "scanned": len(recs), "libraries": names, "opts": opts,
            "item_count": sum(len(g["books"]) for g in groups),
            "when": time.strftime("%Y-%m-%d %H:%M"), "engine": FUZZ_ENGINE,
        }
        _write_json(LAST_SCAN_FILE, result)
        set_state(running=False, phase="Done", error=None)
    except Exception as e:  # noqa: BLE001
        set_state(running=False, phase="", error=friendly_error(e, settings.get("abs_url")))


# --------------------------------------------------------------------------- web app

app = Flask(__name__)
CSRF = secrets.token_urlsafe(24)          # guards the editing forms against cross-site posts
ID_RE = re.compile(r"[A-Za-z0-9_\-]{1,64}")


@app.template_filter("hm")
def fmt_duration(sec):
    sec = int(sec or 0)
    if not sec:
        return ""
    h, m = divmod(round(sec / 60), 60)
    return f"{h} h {m} min" if h else f"{m} min"


@app.template_filter("size")
def fmt_size(b):
    b = float(b or 0)
    for unit in ("bytes", "KB", "MB", "GB", "TB"):
        if b < 1024 or unit == "TB":
            return f"{b:.0f} {unit}" if unit in ("bytes", "KB") else f"{b:.1f} {unit}"
        b /= 1024


@app.template_filter("date")
def fmt_date(ms):
    return time.strftime("%Y-%m-%d", time.localtime(ms / 1000)) if ms else ""


@app.errorhandler(Exception)
def show_error(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException) and e.code != 500:
        return e
    app.logger.exception("Unhandled error")
    detail = f"{type(e).__name__}: {e}"
    return render_template_string(ERROR_HTML, css=CSS, detail=detail,
                                  problem=config_problem(), version=VERSION), 500


@app.route("/")
def index():
    s = load_settings()
    if not s["abs_url"] or not s["token"]:
        return redirect(url_for("settings"))
    libs, lib_error = [], None
    try:
        libs = ABS(s).libraries()
    except Exception as e:  # noqa: BLE001
        lib_error = friendly_error(e, s["abs_url"])
    lib_error = lib_error or config_problem()
    result = _read_json(LAST_SCAN_FILE, None)
    opts = dict(DEFAULT_OPTS)
    if result:
        opts.update(result.get("opts") or {})
    dismissed = load_dismissed()
    counts = defaultdict(int)
    visible = 0
    if result:
        for g in result["groups"]:
            if g["key"] in dismissed:
                continue
            visible += 1
            for r in g["reasons"]:
                counts[r["reason"]] += 1
    reason_counts = sorted(counts.items(), key=lambda kv: REASONS.get(kv[0], 9))
    with STATE_LOCK:
        state = dict(STATE)
    dismissed_here = sum(1 for g in (result or {}).get("groups", []) if g["key"] in dismissed)
    return render_template_string(MAIN_HTML, css=CSS, s=s, libs=libs, lib_error=lib_error,
                                  result=result, opts=opts, state=state, dismissed=dismissed,
                                  dismissed_count=dismissed_here, visible_count=visible,
                                  reason_counts=reason_counts, version=VERSION, csrf=CSRF,
                                  msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/settings", methods=["GET", "POST"])
def settings():
    s = load_settings()
    message, ok = None, False
    problem = config_problem()
    if request.method == "POST" and problem:
        message = problem
    elif request.method == "POST":
        new = {
            "abs_url": request.form.get("abs_url", "").strip().rstrip("/"),
            "token": request.form.get("token", "").strip() or s.get("token", ""),
            "verify_ssl": bool(request.form.get("verify_ssl")),
            "editing": bool(request.form.get("editing")),
            "path_map": request.form.get("path_map", "").strip(),
            "trash_dir": request.form.get("trash_dir", "").strip() or ".abs-dupes-trash",
        }
        try:
            check_name(new["trash_dir"])
        except DiskError:
            new["trash_dir"] = ".abs-dupes-trash"
        try:
            _write_json(SETTINGS_FILE, new)
        except OSError as e:
            return render_template_string(
                SETTINGS_HTML, css=CSS, s=load_settings(), ok=False, in_docker=IN_DOCKER,
                config_dir=CONFIG_DIR, version=VERSION, have_mutagen=HAVE_MUTAGEN, map_checks=[],
                message=f"Couldn't save settings to {SETTINGS_FILE}: {e.strerror or e}. "
                        f"Check the folder is mounted and writable by this user"
                        + (f" (uid {os.geteuid()}, gid {os.getegid()})" if hasattr(os, "geteuid") else "") + ".")
        s = load_settings()
        try:
            libs = ABS(s).libraries()
            ok = True
            message = (f"Connected. Found {len(libs)} book "
                       f"{'library' if len(libs) == 1 else 'libraries'}.")
        except Exception as e:  # noqa: BLE001
            message = friendly_error(e, s["abs_url"])
    map_checks = [(a, b, os.path.isdir(b)) for a, b in parse_path_map(s.get("path_map"))]
    if problem and not message:
        message = problem
    return render_template_string(SETTINGS_HTML, css=CSS, s=s, message=message, ok=ok,
                                  in_docker=IN_DOCKER, config_dir=CONFIG_DIR, version=VERSION,
                                  map_checks=map_checks, have_mutagen=HAVE_MUTAGEN)


@app.route("/scan", methods=["POST"])
def scan():
    f = request.form
    try:
        threshold = min(100, max(70, int(f.get("threshold", 90))))
    except ValueError:
        threshold = 90
    opts = {
        "libs": f.getlist("lib"), "m_ids": bool(f.get("m_ids")), "m_exact": bool(f.get("m_exact")),
        "m_fuzzy": bool(f.get("m_fuzzy")), "threshold": threshold, "m_dur": bool(f.get("m_dur")),
        "cross_library": bool(f.get("cross_library")), "cross_format": bool(f.get("cross_format")),
    }
    with STATE_LOCK:
        if STATE["running"]:
            return redirect(url_for("index"))
        if not opts["libs"]:
            STATE["error"] = "Choose at least one library to scan."
            return redirect(url_for("index"))
        STATE.update(running=True, phase="Starting", done=0, total=0, error=None)
    threading.Thread(target=run_scan, args=(opts["libs"], opts), daemon=True).start()
    return redirect(url_for("index"))


@app.route("/status")
def status():
    with STATE_LOCK:
        return jsonify(STATE)


_PLACEHOLDER = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 120 120">'
                '<rect width="120" height="120" fill="#9aa59d"/>'
                '<path d="M40 34h34a6 6 0 0 1 6 6v46H46a6 6 0 0 1-6-6z" fill="none" '
                'stroke="#e8ece5" stroke-width="4"/></svg>')


@app.route("/cover/<item_id>")
def cover(item_id):
    if not re.fullmatch(r"[A-Za-z0-9_\-]{1,64}", item_id):
        return Response(_PLACEHOLDER, mimetype="image/svg+xml")
    s = load_settings()
    try:
        r = ABS(s).get(f"/api/items/{item_id}/cover", timeout=20, width=240, format="jpeg")
        resp = Response(r.content, mimetype=r.headers.get("Content-Type", "image/jpeg"))
    except Exception:  # noqa: BLE001
        resp = Response(_PLACEHOLDER, mimetype="image/svg+xml")
    resp.headers["Cache-Control"] = "max-age=86400"
    return resp


@app.route("/dismiss", methods=["POST"])
def dismiss():
    key = (request.get_json(silent=True) or {}).get("key", "")
    if not key:
        return jsonify(ok=False)
    try:
        d = load_dismissed()
        d.add(key)
        save_dismissed(d)
    except OSError as e:
        return jsonify(ok=False, error=str(e)), 500
    return jsonify(ok=True)


@app.route("/restore", methods=["POST"])
def restore():
    key = (request.get_json(silent=True) or {}).get("key", "")
    try:
        d = load_dismissed()
        d.discard(key)
        save_dismissed(d)
    except OSError as e:
        return jsonify(ok=False, error=str(e)), 500
    return jsonify(ok=True)


@app.route("/export.csv")
def export_csv():
    result = _read_json(LAST_SCAN_FILE, None) or {"groups": []}
    dismissed = load_dismissed()
    s = load_settings()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["group", "match", "dismissed", "title", "subtitle", "author", "narrator", "series",
                "library", "type", "duration", "size_mb", "files", "asin", "isbn", "added",
                "missing", "path", "abs_link"])
    for n, g in enumerate(result["groups"], 1):
        match = "; ".join(r["reason"] + (f" {r['score']}%" if r["score"] < 100 else "")
                          for r in g["reasons"])
        for it in g["books"]:
            w.writerow([n, match, "yes" if g["key"] in dismissed else "", it["title"], it["subtitle"],
                        it["author"], it["narrator"], it["series"], it["library"], it["kind"],
                        fmt_duration(it["duration"]), round(it["size"] / 1048576, 1), it["numFiles"],
                        it["asin"], it["isbn"], fmt_date(it["addedAt"]), "yes" if it["missing"] else "",
                        it["path"], f"{s['abs_url']}/item/{it['id']}"])
    return Response("\ufeff" + buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=abs_duplicates.csv"})



# --------------------------------------------------------------------------- item page + editing

def split_list(text, commas=True):
    parts = re.split(r"[,\n]" if commas else r"\n", text or "")
    return [p.strip() for p in parts if p.strip()]


def item_files(it, s):
    files = []
    for lf in it.get("libraryFiles") or []:
        meta = lf.get("metadata") or {}
        lp, _ = to_local(meta.get("path", ""), s)
        files.append({
            "ino": str(lf.get("ino", "")), "name": meta.get("filename", ""),
            "rel": meta.get("relPath") or meta.get("filename", ""), "size": meta.get("size", 0),
            "type": lf.get("fileType", ""), "local": lp, "exists": bool(lp and os.path.exists(lp)),
            "audio": os.path.splitext(meta.get("filename", ""))[1].lower() in AUDIO_EXT,
        })
    files.sort(key=lambda f: f["rel"].lower())
    return files


def require_local(it, s):
    local, root = to_local(it.get("path", ""), s)
    if not local:
        raise DiskError(f"No path mapping covers {it.get('path')}. Add one in Connection settings.")
    if not os.path.exists(local):
        raise DiskError(f"The mapping points to {local}, but nothing is there. Check the path mapping.")
    if not inside(local, root):
        raise DiskError("The item's path resolves outside the mapped library folder.")
    return local, root


def edit_action(fn):
    @functools.wraps(fn)
    def wrapper(item_id):
        s = load_settings()
        try:
            if not s.get("editing"):
                raise DiskError("Editing is turned off. Turn it on in Connection settings.")
            if request.form.get("csrf") != CSRF:
                raise DiskError("This page is out of date (the app was restarted). Reload it and try again.")
            if not ID_RE.fullmatch(item_id):
                raise DiskError("That item id isn't valid.")
            api = ABS(s)
            out = fn(s, api, api.item(item_id))
            if not isinstance(out, str):
                return out
            return redirect(url_for("item_page", item_id=item_id, msg=out))
        except DiskError as e:
            err = str(e)
        except requests.RequestException as e:
            err = friendly_error(e, s["abs_url"])
        except OSError as e:
            err = f"Disk error: {e.strerror or e}" + (f" ({e.filename})" if getattr(e, "filename", None) else "")
        if request.form.get("next") == "index":
            return redirect(url_for("index", err=err))
        return redirect(url_for("item_page", item_id=item_id, err=err))
    return wrapper


@app.route("/item/<item_id>")
def item_page(item_id):
    s = load_settings()
    if not ID_RE.fullmatch(item_id):
        return redirect(url_for("index", err="That item id isn't valid."))
    try:
        it = ABS(s).item(item_id)
    except requests.RequestException as e:
        err = friendly_error(e, s["abs_url"])
        if getattr(e, "response", None) is not None and e.response.status_code == 404:
            err = "That item is no longer in Audiobookshelf. It may have been moved and rescanned under a new id."
        return redirect(url_for("index", err=err))
    media = it.get("media") or {}
    md = media.get("metadata") or {}
    local, root = to_local(it.get("path", ""), s)
    disk_msg = None
    if not local:
        disk_msg = f"No path mapping covers {it.get('path')}. Add one in Connection settings to work with files."
    elif not os.path.exists(local):
        disk_msg = f"Mapped to {local}, but nothing is there. Check the path mapping."
    disk_ok = disk_msg is None
    files = item_files(it, s)

    tag_rows, tag_diff, common = [], [], {}
    audio = [f for f in files if f["audio"] and f["exists"]]
    if disk_ok and HAVE_MUTAGEN:
        for f in audio[:60]:
            tag_rows.append((f, read_tags(f["local"])))
        for k in TAG_FIELDS:
            vals = {t.get(k, "") for _, t in tag_rows if "error" not in t}
            common[k] = next(iter(vals)) if len(vals) == 1 else ""
            if len(vals) > 1 and k != "title":
                tag_diff.append(k)

    meta_json, json_msg, json_path = None, None, None
    if disk_ok and not it.get("isFile"):
        json_path = os.path.join(local, "metadata.json")
        if os.path.exists(json_path):
            with open(json_path, encoding="utf-8") as fh:
                meta_json = fh.read()
            try:
                meta_json = json.dumps(json.loads(meta_json), indent=2, ensure_ascii=False)
            except ValueError:
                pass  # show it as-is so it can be fixed
        else:
            json_msg = ("There's no metadata.json in this folder. Turn on “Store metadata with item” in ABS "
                        "settings and ABS will write one when metadata changes.")
    elif it.get("isFile"):
        json_msg = "This item is a single file, so it has no folder for metadata.json."
    else:
        json_msg = disk_msg

    form = {
        "title": md.get("title") or "", "subtitle": md.get("subtitle") or "",
        "authors": ", ".join(a.get("name", "") for a in md.get("authors") or []),
        "narrators": ", ".join(md.get("narrators") or []),
        "series": "\n".join(f"{x.get('name', '')}" + (f" #{x['sequence']}" if x.get("sequence") else "")
                            for x in md.get("series") or []),
        "genres": ", ".join(md.get("genres") or []), "tags": ", ".join(media.get("tags") or []),
        "publishedYear": md.get("publishedYear") or "", "publisher": md.get("publisher") or "",
        "language": md.get("language") or "", "isbn": md.get("isbn") or "", "asin": md.get("asin") or "",
        "description": md.get("description") or "", "explicit": bool(md.get("explicit")),
        "abridged": bool(md.get("abridged")),
    }
    rel_folder = os.path.relpath(local, root).replace("\\", "/") if disk_ok else ""
    return render_template_string(
        ITEM_HTML, css=CSS, s=s, it=it, md=md, form=form, files=files, disk_ok=disk_ok,
        disk_msg=disk_msg, local=local, root=root, rel_folder=rel_folder,
        basename=os.path.basename((local or it.get("path", "")).rstrip("/\\")),
        tag_rows=tag_rows, tag_diff=tag_diff, common=common, n_audio=len(audio),
        tag_fields=TAG_FIELDS, tag_labels=TAG_LABELS, have_mutagen=HAVE_MUTAGEN,
        meta_json=meta_json, json_msg=json_msg, json_path=json_path, csrf=CSRF, version=VERSION,
        trash_dir=s.get("trash_dir"), log_file=LOG_FILE,
        msg=request.args.get("msg"), err=request.args.get("err"))


@app.route("/item/<item_id>/metadata", methods=["POST"])
@edit_action
def act_metadata(s, api, it):
    f = request.form
    md = (it.get("media") or {}).get("metadata") or {}
    known_authors = {a.get("name", "").lower(): a.get("id") for a in md.get("authors") or []}
    authors = [{"id": known_authors.get(n.lower()) or f"new-{i}", "name": n}
               for i, n in enumerate(split_list(f.get("authors")))]
    known_series = {x.get("name", "").lower(): x.get("id") for x in md.get("series") or []}
    series = []
    for i, line in enumerate(split_list(f.get("series"), commas=False)):
        m = re.match(r"^(.*?)\s*#\s*([^#]*)$", line)
        name, seq = (m.group(1).strip(), m.group(2).strip()) if m else (line, "")
        if name:
            series.append({"id": known_series.get(name.lower()) or f"new-{i}", "name": name,
                           "sequence": seq or None})
    payload = {
        "metadata": {
            "title": f.get("title", "").strip(), "subtitle": f.get("subtitle", "").strip() or None,
            "authors": authors, "narrators": split_list(f.get("narrators")), "series": series,
            "genres": split_list(f.get("genres")),
            "publishedYear": f.get("publishedYear", "").strip() or None,
            "publisher": f.get("publisher", "").strip() or None,
            "language": f.get("language", "").strip() or None,
            "isbn": f.get("isbn", "").strip() or None, "asin": f.get("asin", "").strip() or None,
            "description": f.get("description", "").strip() or None,
            "explicit": bool(f.get("explicit")), "abridged": bool(f.get("abridged")),
        },
        "tags": split_list(f.get("tags")),
    }
    if not payload["metadata"]["title"]:
        raise DiskError("Title can't be empty.")
    api.update_media(it["id"], payload)
    log_change("metadata", item=it["id"], path=it.get("path"), metadata=payload)
    msg = "Metadata saved in Audiobookshelf."
    if f.get("embed"):
        api.embed_metadata(it["id"])
        msg += " ABS is writing it into the audio files in the background."
    return msg + " Scan for duplicates again to refresh the list."


@app.route("/item/<item_id>/embed", methods=["POST"])
@edit_action
def act_embed(s, api, it):
    api.embed_metadata(it["id"])
    log_change("embed", item=it["id"], path=it.get("path"))
    return "ABS is writing its metadata, cover and chapters into the audio files in the background."


@app.route("/item/<item_id>/tags", methods=["POST"])
@edit_action
def act_tags(s, api, it):
    if not HAVE_MUTAGEN:
        raise DiskError("Install the mutagen package to edit tags directly.")
    local, root = require_local(it, s)
    values = {k: request.form.get(f"tag_{k}", "").strip()
              for k in TAG_FIELDS if k != "title" and request.form.get(f"apply_{k}")}
    if not values:
        raise DiskError("Tick at least one tag to write.")
    targets = [f["local"] for f in item_files(it, s) if f["audio"] and f["exists"]]
    if not targets:
        raise DiskError("No audio files were found on disk for this item.")
    for path in targets:
        if not inside(path, root):
            raise DiskError(f"{path} is outside the mapped library folder.")
    for path in targets:
        write_tags(path, values)
    log_change("tags", item=it["id"], files=targets, values=values)
    api.scan_item(it["id"])
    names = ", ".join(TAG_LABELS[k].lower() for k in values)
    return f"Wrote {names} to {len(targets)} audio {'file' if len(targets) == 1 else 'files'}. ABS is rescanning the item."


@app.route("/item/<item_id>/metajson", methods=["POST"])
@edit_action
def act_metajson(s, api, it):
    local, root = require_local(it, s)
    if it.get("isFile"):
        raise DiskError("Single-file items have no metadata.json.")
    text = request.form.get("metajson", "").replace("\r\n", "\n")
    try:
        json.loads(text)
    except ValueError as e:
        raise DiskError(f"That isn't valid JSON, so nothing was saved: {e}")
    path = os.path.join(local, "metadata.json")
    backup_dir = os.path.join(CONFIG_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)
    backup = None
    if os.path.exists(path):
        backup = os.path.join(backup_dir, f"{it['id']}_{time.strftime('%Y%m%d-%H%M%S')}_metadata.json")
        shutil.copy2(path, backup)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    log_change("metadata.json", item=it["id"], path=path, backup=backup)
    api.scan_item(it["id"])
    return "Saved metadata.json. ABS is rescanning the item." + (" The previous version is in the backups folder." if backup else "")


@app.route("/item/<item_id>/rename-folder", methods=["POST"])
@edit_action
def act_rename_folder(s, api, it):
    local, root = require_local(it, s)
    new = check_name(request.form.get("name"))
    if new == os.path.basename(local.rstrip("/\\")):
        raise DiskError("That's already the name.")
    safe_rename(local, os.path.join(os.path.dirname(local.rstrip("/\\")), new), root)
    api.scan_library(it["libraryId"])
    return f"Renamed to “{new}”. ABS is rescanning the library to pick up the new path."


@app.route("/item/<item_id>/move-folder", methods=["POST"])
@edit_action
def act_move_folder(s, api, it):
    local, root = require_local(it, s)
    parts = [check_name(p) for p in re.split(r"[\\/]", request.form.get("dest", "")) if p.strip()]
    if not parts:
        raise DiskError("Enter a destination inside the library folder, like “Author/Series/Title”.")
    dest = os.path.join(root, *parts)
    if os.path.normcase(os.path.realpath(dest)) == os.path.normcase(os.path.realpath(local)):
        raise DiskError("That's where it already is.")
    safe_rename(local, dest, root)
    api.scan_library(it["libraryId"])
    return f"Moved to {'/'.join(parts)}. ABS is rescanning the library to pick up the new path."


def _find_file(it, s):
    ino = request.form.get("ino", "")
    for f in item_files(it, s):
        if f["ino"] == ino:
            if not f["local"] or not f["exists"]:
                raise DiskError(f"{f['name']} wasn't found on disk.")
            return f
    raise DiskError("That file is no longer part of this item. Reload the page.")


@app.route("/item/<item_id>/file/rename", methods=["POST"])
@edit_action
def act_file_rename(s, api, it):
    _, root = require_local(it, s)
    f = _find_file(it, s)
    new = check_name(request.form.get("name"))
    if new == f["name"]:
        raise DiskError("That's already the name.")
    safe_rename(f["local"], os.path.join(os.path.dirname(f["local"]), new), root)
    api.scan_item(it["id"])
    return f"Renamed {f['name']} to {new}. ABS is rescanning the item."


@app.route("/item/<item_id>/file/trash", methods=["POST"])
@edit_action
def act_file_trash(s, api, it):
    _, root = require_local(it, s)
    f = _find_file(it, s)
    dest = move_to_trash(f["local"], root, s)
    api.scan_item(it["id"])
    return f"Moved {f['name']} to {dest}. ABS is rescanning the item."


@app.route("/item/<item_id>/trash", methods=["POST"])
@edit_action
def act_item_trash(s, api, it):
    local, root = require_local(it, s)
    dest = move_to_trash(local, root, s)
    api.remove_item(it["id"])
    drop_from_last_scan(it["id"])
    title = ((it.get("media") or {}).get("metadata") or {}).get("title") or it["id"]
    return redirect(url_for("index", msg=f"Moved “{title}” to {dest} and removed it from Audiobookshelf."))

# --------------------------------------------------------------------------- templates

CSS = r"""
:root{
  --paper:#E7EBE4; --card:#F7F9F5; --ink:#1B252D; --muted:#56635B; --shelf:#2D5A4B;
  --flag:#D6A02A; --flag-ink:#2A2006; --rule:#C3CCC0; --alert:#A23A2B; --focus:#1D6CB0;
  --diff:#FBE7A8;
}
@media (prefers-color-scheme: dark){
  :root{
    --paper:#131917; --card:#1B2320; --ink:#E1E7DF; --muted:#9AA79E; --shelf:#6DA690;
    --flag:#E2B24A; --flag-ink:#2A2006; --rule:#2C3833; --alert:#E47C68; --focus:#7DB5E6;
    --diff:#4A3F17;
  }
}
*{box-sizing:border-box}
html{font-size:100%}
body{margin:0;background:var(--paper);color:var(--ink);
  font-family:"Atkinson Hyperlegible",system-ui,"Segoe UI",Roboto,sans-serif;line-height:1.5}
a{color:var(--shelf)}
:focus-visible{outline:3px solid var(--focus);outline-offset:2px}
.wrap{max-width:78rem;margin:0 auto;padding:0 1.25rem}
.top{border-bottom:1px solid var(--rule);padding:1.5rem 0 1rem;margin-bottom:1.5rem}
.top h1{margin:0;font-size:2rem;line-height:1.1;letter-spacing:-.01em}
.where{margin:.35rem 0 0;color:var(--muted)}
.where a{margin-right:1rem}
.quiet{background:none;border:0;padding:0;color:var(--muted);text-decoration:underline;cursor:pointer;font:inherit}
.notice{padding:.8rem 1rem;border-radius:6px;margin:0 0 1rem;background:var(--card);border-left:4px solid var(--shelf)}
.notice.bad{border-left-color:var(--alert)}
form.scan{display:grid;grid-template-columns:repeat(auto-fit,minmax(16rem,1fr));gap:1rem 2rem;
  background:var(--card);padding:1.25rem;border-radius:8px;border:1px solid var(--rule)}
fieldset{border:0;margin:0;padding:0;min-width:0}
legend{font-weight:700;margin-bottom:.4rem}
.check{display:flex;gap:.5rem;align-items:baseline;margin:.25rem 0}
.check input{accent-color:var(--shelf)}
.num{width:4.2rem;font:inherit;padding:.1rem .3rem;border:1px solid var(--rule);border-radius:4px;background:var(--paper);color:var(--ink)}
button.primary,.button{font:inherit;font-weight:700;background:var(--shelf);color:#fff;border:0;border-radius:6px;
  padding:.6rem 1.1rem;cursor:pointer;text-decoration:none;display:inline-block;align-self:end;justify-self:start}
button.primary:disabled{opacity:.5;cursor:progress}
@media (prefers-color-scheme: dark){button.primary,.button{color:#0E1512}}
.progress{margin:1.25rem 0}
.progress p{margin:0 0 .4rem}
.bar{height:.5rem;background:var(--rule);border-radius:99px;overflow:hidden}
.bar span{display:block;height:100%;width:0;background:var(--shelf);transition:width .4s}
.results{margin-top:2rem}
.summary h2{margin:0;font-size:1.5rem}
.summary p{margin:.2rem 0 0;color:var(--muted);max-width:70ch}
.tools{display:flex;flex-wrap:wrap;gap:.75rem 1.25rem;align-items:center;margin:1.25rem 0;padding:.75rem 0;
  border-top:1px solid var(--rule);border-bottom:1px solid var(--rule);position:sticky;top:0;background:var(--paper);z-index:2}
.chips{display:flex;flex-wrap:wrap;gap:.4rem}
.chip{font:inherit;font-size:.9rem;border:1px solid var(--rule);background:var(--card);color:var(--ink);
  border-radius:99px;padding:.2rem .75rem;cursor:pointer}
.chip span{color:var(--muted);margin-left:.2rem}
.chip.on{background:var(--ink);color:var(--paper);border-color:var(--ink)}
.chip.on span{color:inherit;opacity:.75}
#q{font:inherit;flex:1 1 16rem;padding:.4rem .6rem;border:1px solid var(--rule);border-radius:6px;background:var(--card);color:var(--ink)}
.group{margin:0 0 2.25rem}
.group.filtered,.group.dismissed{display:none}
body.showdis .group.dismissed:not(.filtered){display:block;opacity:.55}
.ghead{display:flex;justify-content:space-between;align-items:flex-start;gap:1rem}
.ribbons{display:flex;flex-wrap:wrap;gap:.35rem}
.ribbon{background:var(--flag);color:var(--flag-ink);font-weight:700;font-size:.9rem;
  padding:.15rem .65rem .35rem;border-radius:0 0 4px 4px;clip-path:polygon(0 0,100% 0,100% 100%,50% 82%,0 100%)}
.ribbon.s4,.ribbon.s5{background:transparent;color:var(--ink);box-shadow:inset 0 0 0 2px var(--flag);clip-path:none;border-radius:4px;padding:.1rem .6rem}
.shelf{display:flex;gap:1rem;overflow-x:auto;padding:1rem .25rem .9rem;
  border-bottom:7px solid var(--shelf);box-shadow:0 5px 0 -1px color-mix(in srgb,var(--shelf) 55%,#000)}
.book{flex:0 0 17rem;background:var(--card);border:1px solid var(--rule);border-radius:4px;padding:.9rem;
  display:flex;flex-direction:column;gap:.35rem}
.book.missing{border-color:var(--alert);border-style:dashed}
.book img{width:120px;height:120px;object-fit:cover;border-radius:3px;background:var(--rule)}
.book h3{margin:.3rem 0 0;font-size:1.05rem;line-height:1.3}
.sub{margin:0;color:var(--muted);font-size:.9rem}
dl{display:grid;grid-template-columns:auto 1fr;gap:.1rem .6rem;margin:.2rem 0 0;font-size:.9rem}
dt{color:var(--muted)}
dd{margin:0;overflow-wrap:anywhere}
.diff{background:var(--diff);border-radius:3px;padding:0 .2rem;margin:0 -.2rem}
.path{margin:.2rem 0 0;font-size:.8rem;color:var(--muted);overflow-wrap:anywhere}
.tags{margin:0;display:flex;gap:.3rem;flex-wrap:wrap}
.tags:empty{display:none}
.tag{font-size:.8rem;border:1px solid var(--rule);border-radius:3px;padding:0 .4rem}
.tag.bad{border-color:var(--alert);color:var(--alert)}
.open{margin-top:auto;padding-top:.4rem;font-weight:700}
.empty{color:var(--muted);padding:2rem 0}
.foot{color:var(--muted);font-size:.85rem;padding:2rem 0}
.form-narrow{max-width:38rem}
.form-narrow label.field{display:block;font-weight:700;margin:1rem 0 .3rem}
.form-narrow input[type=url],.form-narrow input[type=password]{width:100%;font:inherit;padding:.5rem .6rem;
  border:1px solid var(--rule);border-radius:6px;background:var(--card);color:var(--ink)}
.hint{color:var(--muted);font-size:.9rem;margin:.3rem 0 0}
.notice.good{border-left-color:var(--shelf)}
.actions-row{margin-top:auto;padding-top:.4rem;display:flex;flex-wrap:wrap;gap:.2rem 1rem;align-items:baseline}
.actions-row .open{margin-top:0;padding-top:0}
.actions-row form{display:inline}
.danger-link{background:none;border:0;padding:0;font:inherit;color:var(--alert);text-decoration:underline;cursor:pointer}
.danger-link:disabled{opacity:.4;cursor:not-allowed}
.jump{display:flex;flex-wrap:wrap;gap:.4rem 1.25rem;margin:0 0 1.5rem}
.panel{background:var(--card);border:1px solid var(--rule);border-radius:8px;padding:1.25rem;margin:0 0 1.5rem;scroll-margin-top:1rem}
.panel h2{margin:0 0 .3rem;font-size:1.3rem}
.panel > .hint{margin:0 0 1rem;max-width:70ch}
.panel fieldset:disabled{opacity:.6}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));gap:.9rem 1.25rem}
.grid .wide{grid-column:1/-1}
.field2{display:flex;flex-direction:column;gap:.25rem;font-weight:700}
.field2 small{font-weight:400;color:var(--muted)}
.panel input[type=text],.panel textarea{width:100%;font:inherit;font-weight:400;padding:.45rem .55rem;border:1px solid var(--rule);
  border-radius:6px;background:var(--paper);color:var(--ink)}
textarea.code{font-family:ui-monospace,"Cascadia Mono",Consolas,monospace;font-size:.85rem;line-height:1.45}
.formfoot{display:flex;flex-wrap:wrap;gap:.75rem 1.5rem;align-items:center;margin-top:1rem}
button.secondary{font:inherit;font-weight:700;background:transparent;color:var(--shelf);border:2px solid var(--shelf);
  border-radius:6px;padding:.4rem .9rem;cursor:pointer}
button.secondary:disabled,button.danger:disabled{opacity:.45;cursor:not-allowed}
button.danger{font:inherit;font-weight:700;background:var(--alert);color:#fff;border:0;border-radius:6px;padding:.6rem 1.1rem;cursor:pointer}
.scroll{overflow-x:auto;margin:0 0 1rem}
table{border-collapse:collapse;width:100%;font-size:.9rem}
th,td{text-align:left;padding:.4rem .6rem;border-bottom:1px solid var(--rule);vertical-align:top}
th{color:var(--muted);font-weight:700;white-space:nowrap}
td.fname{overflow-wrap:anywhere;min-width:12rem}
.rowform{display:flex;gap:.4rem;min-width:16rem}
.rowform input{flex:1}
.twocol{display:grid;grid-template-columns:repeat(auto-fit,minmax(18rem,1fr));gap:1rem 1.5rem;margin:1rem 0 1.25rem}
.twocol form{display:flex;flex-direction:column;gap:.4rem}
.twocol .rowform{min-width:0}
.pathinfo{font-size:.9rem;margin:0}
.dangerzone{border-top:1px solid var(--rule);margin-top:1.25rem;padding-top:1rem}
.dangerzone p{margin:0 0 .6rem;max-width:70ch}
.tagform{display:grid;grid-template-columns:repeat(auto-fit,minmax(15rem,1fr));gap:.6rem 1.25rem;margin-top:.5rem}
.tagform label.check{font-weight:700;margin:0}
.tagform .one{display:flex;flex-direction:column;gap:.2rem}
textarea.mapbox{width:100%;font:inherit;padding:.5rem .6rem;border:1px solid var(--rule);border-radius:6px;background:var(--card);color:var(--ink)}
.maplist{margin:.4rem 0 0;padding-left:1.1rem;font-size:.9rem}
.ok{color:var(--shelf)} .no{color:var(--alert)}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}
.pathinfo{display:grid;grid-template-columns:auto 1fr;gap:.2rem .8rem}
.pathinfo dd{margin:0;overflow-wrap:anywhere}
@media (prefers-reduced-motion: reduce){.bar span{transition:none}}
"""

HEAD = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{ title }} · ABS Duplicate Finder</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Atkinson+Hyperlegible:wght@400;700&display=swap" rel="stylesheet">
<style>{{ css|safe }}</style></head>"""

MAIN_HTML = HEAD.replace("{{ title }}", "Duplicates") + """
<body>
<header class="top"><div class="wrap">
  <h1>Duplicate finder</h1>
  <p class="where">Audiobookshelf at <a href="{{ s.abs_url }}" target="_blank" rel="noopener">{{ s.abs_url }}</a>
  <a href="{{ url_for('settings') }}">Change connection</a></p>
</div></header>
<main class="wrap">
{% if lib_error %}<div class="notice bad">{{ lib_error }} <a href="{{ url_for('settings') }}">Check connection</a></div>{% endif %}
{% if state.error %}<div class="notice bad">{{ state.error }}</div>{% endif %}
{% if msg %}<div class="notice good" role="status">{{ msg }}</div>{% endif %}
{% if err %}<div class="notice bad" role="alert">{{ err }}</div>{% endif %}

<form class="scan" method="post" action="{{ url_for('scan') }}">
  <fieldset><legend>Libraries</legend>
    {% for l in libs %}
    <label class="check"><input type="checkbox" name="lib" value="{{ l.id }}"
      {% if not opts.libs or l.id in opts.libs %}checked{% endif %}> {{ l.name }}</label>
    {% else %}<p class="hint">No book libraries found.</p>{% endfor %}
  </fieldset>
  <fieldset><legend>Match on</legend>
    <label class="check"><input type="checkbox" name="m_ids" {% if opts.m_ids %}checked{% endif %}> Same ASIN or ISBN</label>
    <label class="check"><input type="checkbox" name="m_exact" {% if opts.m_exact %}checked{% endif %}> Same title and author</label>
    <label class="check"><input type="checkbox" name="m_fuzzy" {% if opts.m_fuzzy %}checked{% endif %}>
      <span>Similar titles, at least <input class="num" type="number" name="threshold" min="70" max="100"
      value="{{ opts.threshold }}" aria-label="Similarity threshold percent">% alike</span></label>
    <label class="check"><input type="checkbox" name="m_dur" {% if opts.m_dur %}checked{% endif %}> Same author and running time</label>
  </fieldset>
  <fieldset><legend>Scope</legend>
    <label class="check"><input type="checkbox" name="cross_library" {% if opts.cross_library %}checked{% endif %}> Compare across libraries</label>
    <label class="check"><input type="checkbox" name="cross_format" {% if opts.cross_format %}checked{% endif %}> Pair audiobooks with ebook-only items</label>
  </fieldset>
  <button class="primary" {% if state.running or not libs %}disabled{% endif %}>Scan for duplicates</button>
</form>

<div id="progress" class="progress" {% if not state.running %}hidden{% endif %}>
  <p id="phase">{{ state.phase }}</p><div class="bar"><span id="fill"></span></div>
</div>

{% if result %}
<section class="results">
  <div class="summary">
    <h2>{{ visible_count }} possible duplicate {{ 'group' if visible_count == 1 else 'groups' }}</h2>
    <p>{{ result.item_count }} items involved, out of {{ result.scanned }} scanned in
      {{ result.libraries|join(', ') }}. Last scanned {{ result.when }}.
      Highlighted fields differ between copies.</p>
  </div>
  <div class="tools">
    <div class="chips" role="group" aria-label="Filter by match type">
      <button type="button" class="chip on" data-reason="" aria-pressed="true">All</button>
      {% for r, c in reason_counts %}
      <button type="button" class="chip" data-reason="{{ r }}" aria-pressed="false">{{ r }}<span>{{ c }}</span></button>
      {% endfor %}
    </div>
    <input type="search" id="q" placeholder="Filter by title, author, narrator or folder" aria-label="Filter results">
    {% if dismissed_count %}<label class="check"><input type="checkbox" id="showdis"> Show {{ dismissed_count }} dismissed</label>{% endif %}
    <a class="button" href="{{ url_for('export_csv') }}">Export CSV</a>
  </div>

  {% for g in result.groups %}
  <section class="group{% if g.key in dismissed %} dismissed{% endif %}" data-key="{{ g.key }}"
    data-reasons="{{ g.reasons|map(attribute='reason')|join('|') }}" data-text="{{ g.text }}">
    <div class="ghead">
      <div class="ribbons">{% for r in g.reasons %}<span class="ribbon s{{ r.strength }}">{{ r.reason }}{% if r.score < 100 %} {{ r.score }}%{% endif %}</span>{% endfor %}</div>
      <button type="button" class="quiet dismiss">{{ 'Restore' if g.key in dismissed else 'Not duplicates' }}</button>
    </div>
    <div class="shelf">
    {% for it in g.books %}
      <article class="book{% if it.missing %} missing{% endif %}">
        <img src="{{ url_for('cover', item_id=it.id) }}" alt="" loading="lazy" width="120" height="120">
        <h3><span class="{{ 'diff' if 'title' in g.diff else '' }}">{{ it.title }}</span></h3>
        {% if it.subtitle %}<p class="sub">{{ it.subtitle }}</p>{% endif %}
        <dl>
          <dt>Author</dt><dd><span class="{{ 'diff' if 'author' in g.diff else '' }}">{{ it.author or '—' }}</span></dd>
          <dt>Narrator</dt><dd><span class="{{ 'diff' if 'narrator' in g.diff else '' }}">{{ it.narrator or '—' }}</span></dd>
          {% if it.series or 'series' in g.diff %}<dt>Series</dt><dd><span class="{{ 'diff' if 'series' in g.diff else '' }}">{{ it.series or '—' }}</span></dd>{% endif %}
          <dt>Length</dt><dd><span class="{{ 'diff' if 'duration' in g.diff else '' }}">{{ it.duration|hm or '—' }}</span></dd>
          <dt>Type</dt><dd><span class="{{ 'diff' if 'kind' in g.diff else '' }}">{{ it.kind }}{% if it.ebook %} ({{ it.ebook }}){% endif %}</span></dd>
          <dt>Size</dt><dd>{{ it.size|size }}, {{ it.numFiles }} {{ 'file' if it.numFiles == 1 else 'files' }}</dd>
          <dt>Library</dt><dd><span class="{{ 'diff' if 'library' in g.diff else '' }}">{{ it.library }}</span></dd>
          <dt>Added</dt><dd>{{ it.addedAt|date }}</dd>
          {% if it.asin %}<dt>ASIN</dt><dd>{{ it.asin }}</dd>{% endif %}
        </dl>
        <p class="path">{{ it.path }}</p>
        <p class="tags">{% if it.missing %}<span class="tag bad">Missing on disk</span>{% endif %}{% if it.invalid %}<span class="tag bad">Invalid</span>{% endif %}{% if it.id == g.largest %}<span class="tag">Largest</span>{% endif %}{% if it.id == g.newest %}<span class="tag">Newest</span>{% endif %}</p>
        <div class="actions-row">
          <a class="open" href="{{ url_for('item_page', item_id=it.id) }}">{{ 'Edit and manage files' if s.editing else 'Details' }}</a>
          <a href="{{ s.abs_url }}/item/{{ it.id }}" target="_blank" rel="noopener">Open in ABS</a>
          {% if s.editing %}
          <form method="post" action="{{ url_for('act_item_trash', item_id=it.id) }}"
            data-q="Move “{{ it.title }}” to the trash folder and remove it from Audiobookshelf?" onsubmit="return confirm(this.dataset.q)">
            <input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="next" value="index">
            <button class="danger-link">Move to trash</button>
          </form>
          {% endif %}
        </div>
      </article>
    {% endfor %}
    </div>
  </section>
  {% else %}
  <p class="empty">No duplicates found with these settings. Lower the similarity threshold or turn on more match types, then scan again.</p>
  {% endfor %}
  <p id="nomatch" class="empty" hidden>No groups match this filter.</p>
</section>
{% elif not state.running %}
<p class="empty">Pick your libraries and match types, then scan. Results are kept until the next scan.</p>
{% endif %}
<p class="foot">ABS Duplicate Finder {{ version }}. {{ "Editing is on. Changes are logged in the config folder." if s.editing else "Read-only: editing is off." }}</p>
</main>
<script>
(function(){
  const running = {{ 'true' if state.running else 'false' }};
  if (running) {
    const phase = document.getElementById('phase'), fill = document.getElementById('fill');
    const timer = setInterval(async () => {
      try {
        const s = await (await fetch('{{ url_for("status") }}')).json();
        phase.textContent = s.phase + (s.total ? ' (' + s.done.toLocaleString() + ' of ' + s.total.toLocaleString() + ')' : '');
        fill.style.width = (s.total ? Math.round(100 * s.done / s.total) : 5) + '%';
        if (!s.running) { clearInterval(timer); location.reload(); }
      } catch (e) {}
    }, 800);
  }
  const groups = Array.from(document.querySelectorAll('.group'));
  const q = document.getElementById('q'), nomatch = document.getElementById('nomatch');
  let reason = '';
  function apply() {
    const term = q ? q.value.trim().toLowerCase() : '';
    const showDis = document.body.classList.contains('showdis');
    let shown = 0;
    groups.forEach(g => {
      const ok = (!reason || g.dataset.reasons.split('|').includes(reason)) && (!term || g.dataset.text.includes(term));
      g.classList.toggle('filtered', !ok);
      if (ok && (showDis || !g.classList.contains('dismissed'))) shown++;
    });
    if (nomatch) nomatch.hidden = shown > 0 || groups.length === 0;
  }
  document.querySelectorAll('.chip').forEach(c => c.addEventListener('click', () => {
    document.querySelectorAll('.chip').forEach(x => { x.classList.remove('on'); x.setAttribute('aria-pressed', 'false'); });
    c.classList.add('on'); c.setAttribute('aria-pressed', 'true');
    reason = c.dataset.reason; apply();
  }));
  if (q) q.addEventListener('input', apply);
  const sd = document.getElementById('showdis');
  if (sd) sd.addEventListener('change', () => { document.body.classList.toggle('showdis', sd.checked); apply(); });
  document.querySelectorAll('.dismiss').forEach(btn => btn.addEventListener('click', async () => {
    const g = btn.closest('.group'), undo = g.classList.contains('dismissed');
    const r = await fetch(undo ? '{{ url_for("restore") }}' : '{{ url_for("dismiss") }}', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({key: g.dataset.key})});
    if (r.ok) { g.classList.toggle('dismissed'); btn.textContent = undo ? 'Not duplicates' : 'Restore'; apply(); }
  }));
})();
</script>
</body></html>
"""

SETTINGS_HTML = HEAD.replace("{{ title }}", "Connection") + """
<body>
<header class="top"><div class="wrap">
  <h1>Connect to Audiobookshelf</h1>
  {% if s.abs_url and s.token %}<p class="where"><a href="{{ url_for('index') }}">Back to duplicates</a></p>{% endif %}
</div></header>
<main class="wrap form-narrow">
  {% if message %}<div class="notice {{ '' if ok else 'bad' }}">{{ message }}
    {% if ok %}<a href="{{ url_for('index') }}">Go to duplicates</a>{% endif %}</div>{% endif %}
  <form method="post">
    <label class="field" for="abs_url">Server address</label>
    <input type="url" id="abs_url" name="abs_url" required value="{{ s.abs_url }}" placeholder="http://your-abs-server:13378">
    <p class="hint">The address you open ABS at, including the port and any sub-path.
      {% if in_docker %}From inside Docker, use the host's IP address rather than localhost.{% endif %}</p>

    <label class="field" for="token">API token</label>
    <input type="password" id="token" name="token" autocomplete="off"
      placeholder="{{ 'Saved. Leave blank to keep it.' if s.token else 'Paste your token' }}">
    <p class="hint">In Audiobookshelf, open Settings, then API Keys (or Users, pick your user, and copy the API token on older versions).
      Read access is all this tool needs.</p>

    <label class="check" style="margin-top:1rem"><input type="checkbox" name="verify_ssl" {% if s.verify_ssl %}checked{% endif %}>
      Verify SSL certificate</label>

    <h2 style="margin:2rem 0 .3rem;font-size:1.3rem">Editing</h2>
    <p class="hint">Metadata edits go through ABS. Renaming, moving, trashing files and editing tags or metadata.json
      work on the disk directly, so this app needs to reach the same folders ABS uses, with write access.
      Editing and rescans need an admin user's token.</p>
    <label class="check" style="margin-top:.75rem"><input type="checkbox" name="editing" {% if s.editing %}checked{% endif %}>
      Allow editing, renaming and moving to trash</label>

    <label class="field" for="path_map">Path mapping</label>
    <textarea class="mapbox" id="path_map" name="path_map" rows="3" spellcheck="false"
      placeholder="{{ '/audiobooks=/library' if in_docker else '/audiobooks=Z:&#92;Audiobooks'|safe }}">{{ s.path_map }}</textarea>
    <p class="hint">One per line: the folder path as ABS shows it, “=”, then the same folder as this app sees it.
      {% if in_docker %}Mount the library into this container (see docker-compose.yml) and use that mount path.
      {% else %}Use a mapped drive like Z:&#92;Audiobooks or a network path like &#92;&#92;server&#92;share&#92;Audiobooks.{% endif %}</p>
    {% if map_checks %}<ul class="maplist">{% for a, b, okp in map_checks %}
      <li>{{ a }} → {{ b }} <span class="{{ 'ok' if okp else 'no' }}">{{ 'found' if okp else 'not found from here' }}</span></li>{% endfor %}</ul>{% endif %}

    <label class="field" for="trash_dir">Trash folder name</label>
    <input type="text" id="trash_dir" name="trash_dir" value="{{ s.trash_dir }}" style="width:100%;font:inherit;padding:.5rem .6rem;border:1px solid var(--rule);border-radius:6px;background:var(--card);color:var(--ink)">
    <p class="hint">Created inside each mapped library folder, with a .ignore file so ABS skips it. Empty it yourself when you're sure.</p>
    {% if not have_mutagen %}<p class="hint">Direct tag editing needs the mutagen package: py -m pip install mutagen</p>{% endif %}
    <p style="margin-top:1.25rem"><button class="primary">Save and test</button></p>
  </form>
  <p class="foot">Settings are stored in {{ config_dir }}. ABS Duplicate Finder {{ version }}.</p>
</main></body></html>
"""


ERROR_HTML = HEAD.replace("{{ title }}", "Error") + """
<body><main class="wrap" style="padding-top:2rem">
  <h1>Something went wrong</h1>
  {% if problem %}<div class="notice bad">{{ problem }}</div>{% endif %}
  <p>The app hit an error it didn't expect. The details below are also in the container or console log.</p>
  <pre class="code" style="background:var(--card);border:1px solid var(--rule);border-radius:6px;padding:1rem;overflow-x:auto">{{ detail }}</pre>
  <p><a href="{{ url_for('index') }}">Back to duplicates</a> &middot; <a href="{{ url_for('settings') }}">Connection settings</a></p>
  <p class="foot">ABS Duplicate Finder {{ version }}</p>
</main></body></html>
"""


ITEM_HTML = HEAD.replace("{{ title }}", "Item") + """
<body>
<header class="top"><div class="wrap">
  <p class="where" style="margin:0 0 .4rem"><a href="{{ url_for('index') }}">Back to duplicates</a></p>
  <h1>{{ md.title or 'Untitled' }}</h1>
  <p class="where">{{ md.authorName }}
    <a href="{{ s.abs_url }}/item/{{ it.id }}" target="_blank" rel="noopener" style="margin-left:1rem">Open in Audiobookshelf</a></p>
</div></header>
<main class="wrap">
{% if msg %}<div class="notice good" role="status">{{ msg }}</div>{% endif %}
{% if err %}<div class="notice bad" role="alert">{{ err }}</div>{% endif %}
{% if not s.editing %}<div class="notice">Editing is off, so this page is view-only.
  <a href="{{ url_for('settings') }}">Turn it on in Connection settings</a>.</div>{% endif %}
{% set dis = '' if s.editing else 'disabled' %}

<nav class="jump" aria-label="Sections">
  <a href="#meta">Metadata</a><a href="#tags">Embedded tags</a><a href="#json">metadata.json</a><a href="#files">Files and folder</a>
</nav>

<section class="panel" id="meta">
  <h2>Metadata in Audiobookshelf</h2>
  <p class="hint">Saved through the ABS API. With “Store metadata with item” on, ABS rewrites metadata.json itself.</p>
  <form method="post" action="{{ url_for('act_metadata', item_id=it.id) }}">
  <input type="hidden" name="csrf" value="{{ csrf }}">
  <fieldset {{ dis }}>
    <div class="grid">
      <label class="field2">Title<input type="text" name="title" value="{{ form.title }}" required></label>
      <label class="field2">Subtitle<input type="text" name="subtitle" value="{{ form.subtitle }}"></label>
      <label class="field2">Authors <small>Separate with commas</small><input type="text" name="authors" value="{{ form.authors }}"></label>
      <label class="field2">Narrators <small>Separate with commas</small><input type="text" name="narrators" value="{{ form.narrators }}"></label>
      <label class="field2">Series <small>One per line, like “Harry Potter #1”</small><textarea name="series" rows="2">{{ form.series }}</textarea></label>
      <label class="field2">Genres <small>Separate with commas</small><input type="text" name="genres" value="{{ form.genres }}"></label>
      <label class="field2">Tags <small>Separate with commas</small><input type="text" name="tags" value="{{ form.tags }}"></label>
      <label class="field2">Published year<input type="text" name="publishedYear" value="{{ form.publishedYear }}" inputmode="numeric"></label>
      <label class="field2">Publisher<input type="text" name="publisher" value="{{ form.publisher }}"></label>
      <label class="field2">Language<input type="text" name="language" value="{{ form.language }}"></label>
      <label class="field2">ISBN<input type="text" name="isbn" value="{{ form.isbn }}"></label>
      <label class="field2">ASIN<input type="text" name="asin" value="{{ form.asin }}"></label>
      <label class="field2 wide">Description<textarea name="description" rows="6">{{ form.description }}</textarea></label>
    </div>
    <div class="formfoot">
      <label class="check"><input type="checkbox" name="explicit" {% if form.explicit %}checked{% endif %}> Explicit</label>
      <label class="check"><input type="checkbox" name="abridged" {% if form.abridged %}checked{% endif %}> Abridged</label>
      <label class="check"><input type="checkbox" name="embed"> Also write into the audio files</label>
      <button class="primary">Save metadata</button>
    </div>
  </fieldset>
  </form>
  <form method="post" action="{{ url_for('act_embed', item_id=it.id) }}" class="formfoot">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <button class="secondary" {{ dis }}>Write current ABS metadata into audio files</button>
    <span class="hint" style="margin:0">ABS embeds the metadata, cover and chapters with ffmpeg and keeps a backup of the originals.</span>
  </form>
</section>

<section class="panel" id="tags">
  <h2>Embedded tags</h2>
  {% if not have_mutagen %}<p class="hint">Install the mutagen package to read and edit tags here: py -m pip install mutagen</p>
  {% elif not disk_ok %}<p class="hint">{{ disk_msg }}</p>
  {% elif not tag_rows %}<p class="hint">No audio files found on disk for this item.</p>
  {% else %}
  <p class="hint">Read straight from the files. Highlighted columns differ between files.</p>
  <div class="scroll"><table>
    <thead><tr><th>File</th>{% for k in tag_fields %}<th>{{ tag_labels[k] }}</th>{% endfor %}</tr></thead>
    <tbody>{% for f, t in tag_rows %}<tr><td class="fname">{{ f.rel }}</td>
      {% if t.error %}<td colspan="{{ tag_fields|length }}" class="no">{{ t.error }}</td>
      {% else %}{% for k in tag_fields %}<td>{% if t[k] %}<span class="{{ 'diff' if k in tag_diff else '' }}">{{ t[k] }}</span>{% endif %}</td>{% endfor %}{% endif %}
    </tr>{% endfor %}</tbody>
  </table></div>
  {% if n_audio > tag_rows|length %}<p class="hint">Showing the first {{ tag_rows|length }} of {{ n_audio }} audio files.</p>{% endif %}
  <form method="post" action="{{ url_for('act_tags', item_id=it.id) }}"
    data-q="Write the ticked tags into all {{ n_audio }} audio files?" onsubmit="return confirm(this.dataset.q)">
  <input type="hidden" name="csrf" value="{{ csrf }}">
  <fieldset {{ dis }}>
    <legend>Set tags on all {{ n_audio }} audio {{ 'file' if n_audio == 1 else 'files' }}</legend>
    <div class="tagform">
      {% for k in tag_fields if k != 'title' %}
      <div class="one">
        <label class="check"><input type="checkbox" name="apply_{{ k }}"> {{ tag_labels[k] }}</label>
        <input type="text" name="tag_{{ k }}" value="{{ common[k] }}" aria-label="{{ tag_labels[k] }} value">
      </div>
      {% endfor %}
    </div>
    <div class="formfoot">
      <button class="primary">Write tags to files</button>
      <span class="hint" style="margin:0">Only ticked tags are written. A ticked tag left blank is removed. ABS rescans the item afterwards.</span>
    </div>
  </fieldset>
  </form>
  {% endif %}
</section>

<section class="panel" id="json">
  <h2>metadata.json</h2>
  {% if meta_json is none %}<p class="hint">{{ json_msg }}</p>
  {% else %}
  <p class="hint">{{ json_path }}. The current version is backed up to the config folder before saving, then ABS rescans the item.
    ABS only takes these values if metadata.json ranks high in the library's metadata priority.</p>
  <form method="post" action="{{ url_for('act_metajson', item_id=it.id) }}">
  <input type="hidden" name="csrf" value="{{ csrf }}">
  <fieldset {{ dis }}>
    <textarea class="code" name="metajson" rows="20" spellcheck="false" aria-label="metadata.json contents">{{ meta_json }}</textarea>
    <div class="formfoot"><button class="primary">Save metadata.json</button></div>
  </fieldset>
  </form>
  {% endif %}
</section>

<section class="panel" id="files">
  <h2>Files and folder</h2>
  <dl class="pathinfo">
    <dt>In ABS</dt><dd>{{ it.path }}</dd>
    <dt>From here</dt><dd>{{ local or 'No path mapping yet' }}</dd>
  </dl>
  {% if not disk_ok %}<p class="notice bad" style="margin-top:1rem">{{ disk_msg }}</p>{% endif %}
  <div class="twocol">
    <form method="post" action="{{ url_for('act_rename_folder', item_id=it.id) }}">
      <input type="hidden" name="csrf" value="{{ csrf }}">
      <label class="field2" for="newname">Rename {{ 'file' if it.isFile else 'folder' }}</label>
      <div class="rowform"><input type="text" id="newname" name="name" value="{{ basename }}" required {{ dis }} {% if not disk_ok %}disabled{% endif %}>
        <button class="secondary" {{ dis }} {% if not disk_ok %}disabled{% endif %}>Rename</button></div>
    </form>
    <form method="post" action="{{ url_for('act_move_folder', item_id=it.id) }}">
      <input type="hidden" name="csrf" value="{{ csrf }}">
      <label class="field2" for="dest">Move to <small>A path inside {{ root or 'the library folder' }}</small></label>
      <div class="rowform"><input type="text" id="dest" name="dest" value="{{ rel_folder }}" required {{ dis }} {% if not disk_ok %}disabled{% endif %}>
        <button class="secondary" {{ dis }} {% if not disk_ok %}disabled{% endif %}>Move</button></div>
    </form>
  </div>

  <div class="scroll"><table>
    <thead><tr><th>File</th><th>Type</th><th>Size</th><th>Rename</th><th><span class="sr">Trash</span></th></tr></thead>
    <tbody>{% for f in files %}<tr>
      <td class="fname">{{ f.rel }}{% if not f.exists %} <span class="tag bad">{{ 'not found' if f.local else 'unmapped' }}</span>{% endif %}</td>
      <td>{{ f.type }}</td><td style="white-space:nowrap">{{ f.size|size }}</td>
      <td><form class="rowform" method="post" action="{{ url_for('act_file_rename', item_id=it.id) }}">
        <input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="ino" value="{{ f.ino }}">
        <input type="text" name="name" value="{{ f.name }}" aria-label="New name for {{ f.name }}" {{ dis }} {% if not f.exists %}disabled{% endif %}>
        <button class="secondary" {{ dis }} {% if not f.exists %}disabled{% endif %}>Rename</button></form></td>
      <td><form method="post" action="{{ url_for('act_file_trash', item_id=it.id) }}"
        data-q="Move {{ f.name }} to the trash folder?" onsubmit="return confirm(this.dataset.q)">
        <input type="hidden" name="csrf" value="{{ csrf }}"><input type="hidden" name="ino" value="{{ f.ino }}">
        <button class="danger-link" {{ dis }} {% if not f.exists %}disabled{% endif %}>Move to trash</button></form></td>
    </tr>{% endfor %}</tbody>
  </table></div>

  <form class="dangerzone" method="post" action="{{ url_for('act_item_trash', item_id=it.id) }}"
    data-q="Move the whole {{ 'file' if it.isFile else 'folder' }} to the trash folder and remove “{{ md.title }}” from Audiobookshelf?"
    onsubmit="return confirm(this.dataset.q)">
    <input type="hidden" name="csrf" value="{{ csrf }}">
    <p>Move the whole {{ 'file' if it.isFile else 'folder' }} into “{{ trash_dir }}” in the library folder and remove the item
      from Audiobookshelf. To undo, move it back and scan the library.</p>
    <button class="danger" {{ dis }} {% if not disk_ok %}disabled{% endif %}>Move item to trash</button>
  </form>
</section>
<p class="foot">Every change is logged to {{ log_file }}. ABS Duplicate Finder {{ version }}.</p>
</main>
</body></html>
"""

# --------------------------------------------------------------------------- entry point

def _port_in_use(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


if __name__ == "__main__":
    local_url = f"http://127.0.0.1:{PORT}"
    if not IN_DOCKER:
        if _port_in_use(PORT):          # already running: just open the browser
            webbrowser.open(local_url)
            sys.exit(0)
        threading.Timer(1.2, lambda: webbrowser.open(local_url)).start()
    app.run(host=HOST, port=PORT, threaded=True, debug=False)
