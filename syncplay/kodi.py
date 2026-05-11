import os
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlparse

import xbmc
import xbmcvfs
from xbmc import Player, sleep
from xbmcgui import Dialog

from syncplay.handler import set, state, hello
from syncplay.socket import connect, disconnect
from syncplay.util import gs, gsi, gsb  # Added gs and gsb imports


def _display_title(player: Player) -> str:
    """Readable name built from Kodi's metadata for the current playing item."""
    try:
        tag = player.getVideoInfoTag()
    except Exception:
        return ""

    show = tag.getTVShowTitle()
    if show:
        parts = [show]
        season, episode = tag.getSeason(), tag.getEpisode()
        if season > 0 and episode > 0:
            parts.append("S{:02d}E{:02d}".format(season, episode))
        ep_title = tag.getTitle()
        if ep_title and ep_title != show:
            parts.append("- " + ep_title)
        return " ".join(parts)

    title = tag.getTitle()
    if not title:
        return ""
    year = tag.getYear()
    return "{} ({})".format(title, year) if year else title


_FILENAME_INFOLABELS = (
    "Player.Filenameandpath",
    "ListItem.FilenameAndPath",
    "ListItem.Property(OriginalPath)",
    "ListItem.FileName",
    "Player.Filename",
)

_VIDEO_EXTS = (".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".m2ts", ".webm",
               ".wmv", ".flv", ".mpg", ".mpeg")


def _filename_from_url(url: str) -> str:
    """Extract a usable filename from a URL.

    Order of preference:
    1. `filename=`/`name=` query param — what debrid services (Torbox, RD)
       use to ship the actual release name when the URL path is a UUID.
    2. URL basename — only if it has a video extension and isn't an obvious
       hash/UUID. Otherwise we'd just propagate the UUID and lose the title.
    """
    if not url or "://" not in url:
        return ""
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    for key in ("filename", "name"):
        values = qs.get(key)
        if values and values[0]:
            return unquote(values[0])
    base = os.path.basename(unquote(parsed.path))
    if base.lower().endswith(_VIDEO_EXTS):
        return base
    return ""


def _resolve_filename(player: Player, primary_path: str) -> str:
    """Walk every place Kodi might have stashed the playing item's URL and
    return the first one that gives us a real filename. Different Kodi code
    paths (and different addons) expose pre- vs. post-resolution URLs, so
    we don't know which one carries the `filename=` query param ahead of time.
    """
    sources = [("getPlayingFile()", primary_path)]
    for label in _FILENAME_INFOLABELS:
        try:
            v = xbmc.getInfoLabel(label)
        except Exception:
            v = ""
        sources.append((label, v))

    chosen = ""
    chosen_source = ""
    for src, val in sources:
        if not val or chosen:
            continue
        found = _filename_from_url(val)
        if found:
            chosen, chosen_source = found, src

    # Fallback: any candidate that's a local-looking path with a video ext.
    if not chosen:
        for src, val in sources:
            if not val:
                continue
            if "://" not in val or val.startswith("file://"):
                base = os.path.basename(val[7:] if val.startswith("file://") else val)
                if base.lower().endswith(_VIDEO_EXTS):
                    chosen, chosen_source = base, src
                    break

    if gsb("debug"):
        _debug_dump_sources(sources, chosen, chosen_source)
    return chosen


def _debug_dump_sources(sources, chosen: str, chosen_source: str):
    """Show every URL candidate Kodi gave us — on screen and in the room
    chat. Lets us debug filename detection without crawling kodi.log."""
    lines = []
    for src, val in sources:
        lines.append("[{}]".format(src))
        lines.append("  {}".format(val if val else "(empty)"))
    lines.append("")
    lines.append("CHOSEN: {}".format(chosen or "(none — falling back)"))
    if chosen_source:
        lines.append("FROM:   {}".format(chosen_source))
    text = "\n".join(lines)

    try:
        Dialog().textviewer("synko: filename debug", text)
    except Exception:
        pass

    # Mirror to chat so it's visible on the desktop Syncplay client.
    try:
        from syncplay.handler import chat
        chat.say("[synko-debug] chosen='{}' from={}".format(
            chosen or "(none)", chosen_source or "n/a"
        ))
        for src, val in sources:
            if val:
                chat.say("[synko-debug] {} = {}".format(src, val))
    except Exception:
        pass


def _filemeta(path: str, player: Player, title: str = "") -> tuple:
    is_url = bool(path) and "://" in path and not path.startswith("file://")

    if is_url:
        # 1. Real filename from any URL Kodi knows. 2. Fallback: metadata
        # title (less ideal — it's cosmetic and won't match disk filenames).
        # 3. Last-ditch: raw URL basename (might be a UUID, but better than "").
        name = _resolve_filename(player, path) or title or os.path.basename(unquote(urlparse(path).path))
    elif path:
        name = os.path.basename(path)
    else:
        name = title

    try:
        size = int(xbmcvfs.File(path).size()) if path else 0
    except Exception:
        size = 0

    return (name, size)


class _Player(Player):
    def onAVStarted(self):
        path = self.getPlayingFile() if self.isPlaying() else ""
        name, size = _filemeta(path, self, _display_title(self))
        xbmc.log("synko: onAVStarted path='{}' name='{}'".format(path, name), xbmc.LOGINFO)
        set.dispatch({
            "duration": self.getTotalTime(),
            "name": name,
            "size": size
        })
        set.dispatch({"ready": True})
        # Update local state silently — the next server pulse will sync us
        # forward to the room. Don't dispatch a client-iotf State here, or
        # the server will broadcast our position (0.0) as a seek and drag
        # everyone back to the start of the file.
        state.update_local(self.getTime() if self.isPlaying() else 0.0, False)

    def onPlayBackPaused(self):
        set.dispatch({"ready": False})
        state.dispatch(self.getTime(), True, False)

    def onPlayBackResumed(self):
        set.dispatch({"ready": True})
        state.dispatch(self.getTime(), False, False)

    def onPlayBackSeek(self, _t, _o):
        # In follow-only mode, local seeks aren't broadcast — state.dispatch
        # with seeked=True is a no-op. The `seeking` flag is still set briefly
        # so state.handle() doesn't fight us with a catch-up seek mid-jump.
        state.seeking = True
        sleep(gsi("seek"))
        state.syncing_to_server = False
        state.seeking = False

    # Rejoin to show that nothing is playing.
    def onPlayBackStopped(self):
        disconnect()
        sleep(500)
        connect()
        hello.dispatch()

    def onPlayBackEnded(self):
        disconnect()
        sleep(500)
        connect()
        hello.dispatch()


player = _Player()

def setplaystate(sps: dict, cps: dict):
    if not player.isPlaying():
        return
        
    # Handle pause/unpause changes
    if sps["paused"] != cps["paused"]:
        player.pause()
        Dialog().notification(
            "Syncplay", 
            "{} {}".format(sps["setBy"], "paused" if sps["paused"] else "resumed"),
            sound=False
        )
    
    # Handle explicit seeks (when someone manually seeks)
    if "doSeek" in sps and sps["doSeek"]:
        state.syncing_to_server = True
        player.seekTime(sps["position"])
        Dialog().notification(
            "Syncplay",
            "{} seeked to {}".format(
                sps["setBy"],
                str(timedelta(seconds=round(sps["position"])))
            ),
            sound=False
        )
    else:
        # Handle automatic sync due to time differences
        # Calculate difference: positive = we're behind, negative = we're ahead
        diff = sps["position"] - cps["position"]
        tolerance_seconds = float(gsi("tolerance")) / 1000
        
        # Get rewind threshold from settings with fallback
        try:
            rewind_threshold_setting = gs("rewindThreshold")
            rewind_threshold = float(rewind_threshold_setting) if rewind_threshold_setting else 3.0
        except:
            rewind_threshold = 3.0
        
        # Ensure rewind threshold is at least 2x tolerance
        rewind_threshold = max(tolerance_seconds * 2, rewind_threshold)
        
        # Check if rewind is disabled
        try:
            rewind_disabled = gsb("disableRewind")
        except:
            rewind_disabled = False
        
        if diff > tolerance_seconds:
            # We're behind - seek forward to server position
            state.syncing_to_server = True
            player.seekTime(sps["position"])
            Dialog().notification(
                "Syncplay",
                "Syncing forward ({:.1f}s behind) with {}".format(diff, sps["setBy"]),
                sound=False
            )
        elif diff < -rewind_threshold and not rewind_disabled:
            # We're way ahead - seek back to server position (only if rewind not disabled)
            state.syncing_to_server = True
            player.seekTime(sps["position"])
            Dialog().notification(
                "Syncplay",
                "Syncing back ({:.1f}s ahead) with {}".format(abs(diff), sps["setBy"]),
                sound=False
            )
        elif diff < -tolerance_seconds and rewind_disabled:
            # Show notification when we're ahead but rewind is disabled
            Dialog().notification(
                "Syncplay",
                "Ahead by {:.1f}s (rewind disabled)".format(abs(diff)),
                sound=False
            )
        # If we're only slightly ahead (between tolerance and rewind threshold), do nothing
        # This prevents the annoying constant rewinding