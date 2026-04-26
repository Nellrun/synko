import json

import xbmc
import xbmcvfs
from xbmc import Player
from xbmcgui import Dialog

from syncplay.socket import send
from syncplay.util import gs


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
        # may send both keys in one Set:user payload, and we want to handle the
        # file in either case.
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
    """If the other user's file exists in our library, offer to open it."""
    # Don't interrupt an active session — only catch up when we're idle.
    if Player().isPlaying():
        return

    name = file_info.get("name", "")
    if not name:
        return

    expected_size = int(file_info.get("size") or 0)
    path = _find_in_library(name, expected_size)
    if not path:
        xbmc.log("Syncplay: no library match for '{}'".format(name), xbmc.LOGINFO)
        return

    if Dialog().yesno(
        "Syncplay",
        "{} is watching:\n{}\n\nOpen the local copy?".format(other_user, name)
    ):
        Player().play(path)


def _find_in_library(name: str, expected_size: int) -> str:
    """Look up a file in Kodi's VideoLibrary by exact filename, optionally size."""
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
            if not path:
                continue
            # Size match guarantees same release. If we don't know the size or
            # can't stat the file, fall through and accept the name match alone.
            if expected_size:
                try:
                    actual_size = int(xbmcvfs.File(path).size())
                    if actual_size and actual_size != expected_size:
                        continue
                except Exception:
                    pass
            return path

    return ""
