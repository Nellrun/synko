import json
import os
import threading
import time
from urllib.parse import unquote, urlparse

import xbmc
import xbmcgui
import xbmcvfs
from xbmc import Player
from xbmcgui import Dialog

from syncplay.socket import send
from syncplay.util import gs

# One search at a time. Names already attempted in this session are skipped so
# we don't re-walk every source each time the server re-announces a user.
_search_lock = threading.Lock()
_search_thread: threading.Thread = None  # type: ignore
_searched_names = set()


def dispatch(args: dict):
    if "ready" in args:
        send({"Set": {"ready": {
            "isReady": args["ready"],
            "manuallyInitiated": True
        }}})
    else:
        send({"Set": {"file": {
            "duration": args["duration"],
            "name": args["name"],
            "size": args["size"] if "size" in args else 0
        }}})


def handle(info: dict):
    if "user" in info:
        info = info["user"]
        name = list(info.keys())[0]
        if name == gs("user"):
            return
        info = info[name]
        if "event" in info:
            event = "joined" if "joined" in info["event"] else "left"
            Dialog().notification(
                "Syncplay",
                "{} {}".format(name, event),
                sound=False
            )
        # Check `file` independently of `event` — when joining a room the server
        # may send both keys in one Set:user payload.
        if "file" in info:
            Dialog().notification(
                "Syncplay",
                "{} is playing {}".format(name, info["file"]["name"]),
                sound=False
            )
            _maybe_open_match(name, info["file"])
    elif "ready" in info:
        info = info["ready"]
        if info["username"] == gs("user"):
            return
        Dialog().notification(
            "Syncplay",
            "{} is {}ready".format(
                info["username"], "" if info["isReady"] else "not "
            ),
            sound=False
        )
    elif "playlistChange" in info:
        info = info["playlistChange"]
        if info["user"] is None or info["user"] == gs("user"):
            return
        Dialog().notification(
            "Syncplay",
            "{} changed the playlist".format(info["user"]),
            sound=False
        )


def _maybe_open_match(other_user: str, file_info: dict):
    """Kick off a background search and offer to open the file once found."""
    global _search_thread

    if Player().isPlaying():
        return

    name = file_info.get("name", "")
    if not name:
        return

    expected_size = int(file_info.get("size") or 0)

    with _search_lock:
        if name in _searched_names:
            return
        if _search_thread is not None and _search_thread.is_alive():
            # Already searching for someone else's file; don't queue another.
            return
        _searched_names.add(name)
        _search_thread = threading.Thread(
            target=_search_and_prompt,
            args=(other_user, name, expected_size),
            daemon=True,
            name="synko-search"
        )
        _search_thread.start()


def _search_and_prompt(other_user: str, name: str, expected_size: int):
    monitor = xbmc.Monitor()
    pbar = xbmcgui.DialogProgressBG()
    pbar.create("Syncplay", "Looking for {}".format(_short(name, 60)))

    try:
        # 1. Fast path: ask the indexed library first.
        path = _find_in_library(name, expected_size)

        # 2. Fallback: walk every Video source recursively. Works even if the
        # library was never scanned, or the file lives on an HTTPS/SMB source
        # that isn't indexed at all.
        if not path:
            pbar.update(10, message="Scanning sources...")
            path = _find_in_sources(name, expected_size, monitor, pbar)
    finally:
        pbar.close()

    if not path:
        xbmc.log("Syncplay: no local match for '{}'".format(name), xbmc.LOGINFO)
        Dialog().notification(
            "Syncplay",
            "No local copy of '{}'".format(_short(name, 40)),
            sound=False
        )
        return

    # User may have started something themselves while we were searching.
    if Player().isPlaying() or monitor.abortRequested():
        return

    if Dialog().yesno(
        "Syncplay",
        "{} is watching:\n{}\n\nFound a local copy. Open it?".format(other_user, name)
    ):
        Player().play(path)


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n - 1] + "…"


def _basename(path: str) -> str:
    """Filename portion of a path or URL, with percent-decoding for URLs."""
    if "://" in path:
        return os.path.basename(unquote(urlparse(path).path))
    return os.path.basename(path)


def _find_in_library(name: str, expected_size: int) -> str:
    queries = [
        ("VideoLibrary.GetEpisodes", "episodes"),
        ("VideoLibrary.GetMovies", "movies"),
    ]
    for method, result_key in queries:
        payload = {
            "jsonrpc": "2.0",
            "method": method,
            "params": {
                "filter": {"field": "filename", "operator": "is", "value": name},
                "properties": ["file"]
            },
            "id": 1
        }
        try:
            response = json.loads(xbmc.executeJSONRPC(json.dumps(payload)))
        except Exception as e:
            xbmc.log("Syncplay: library query failed: {}".format(e), xbmc.LOGWARNING)
            continue

        for item in response.get("result", {}).get(result_key, []) or []:
            path = item.get("file", "")
            if path and _size_ok(path, expected_size):
                return path
    return ""


def _find_in_sources(name: str, expected_size: int, monitor, pbar,
                     max_depth: int = 8, time_budget: float = 120.0) -> str:
    """BFS every video source for an exact basename match."""
    sources = _get_video_sources()
    if not sources:
        xbmc.log("Syncplay: no video sources configured", xbmc.LOGINFO)
        return ""

    deadline = time.time() + time_budget
    queue = [(src, 0) for src in sources]
    needle = name.lower()
    visited = 0

    while queue:
        if monitor.abortRequested():
            return ""
        if Player().isPlaying():
            # User picked something themselves; abandon.
            return ""
        if time.time() > deadline:
            xbmc.log("Syncplay: search hit {}s budget".format(time_budget), xbmc.LOGWARNING)
            return ""

        directory, depth = queue.pop(0)
        visited += 1
        pbar.update(min(99, 10 + visited % 90), message=_short(directory, 80))

        if depth > max_depth:
            continue

        for item in _list_dir(directory):
            file_path = item.get("file", "")
            if not file_path:
                continue

            filetype = item.get("filetype", "")
            if filetype == "directory":
                queue.append((file_path, depth + 1))
            elif filetype == "file":
                if _basename(file_path).lower() == needle and _size_ok(file_path, expected_size):
                    return file_path
    return ""


def _get_video_sources():
    payload = {
        "jsonrpc": "2.0",
        "method": "Files.GetSources",
        "params": {"media": "video"},
        "id": 1
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(payload)))
        return [s["file"] for s in response.get("result", {}).get("sources", []) or [] if s.get("file")]
    except Exception as e:
        xbmc.log("Syncplay: GetSources failed: {}".format(e), xbmc.LOGWARNING)
        return []


def _list_dir(path: str):
    payload = {
        "jsonrpc": "2.0",
        "method": "Files.GetDirectory",
        "params": {
            "directory": path,
            "media": "video",
            "properties": ["file"]
        },
        "id": 1
    }
    try:
        response = json.loads(xbmc.executeJSONRPC(json.dumps(payload)))
        return response.get("result", {}).get("files", []) or []
    except Exception as e:
        xbmc.log("Syncplay: list '{}' failed: {}".format(path, e), xbmc.LOGWARNING)
        return []


def _size_ok(path: str, expected_size: int) -> bool:
    """Compare actual file size to what the room sent. Allow if we can't stat."""
    if not expected_size:
        return True
    try:
        actual = int(xbmcvfs.File(path).size())
        if not actual:
            return True
        return actual == expected_size
    except Exception:
        return True
