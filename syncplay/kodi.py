import os
from datetime import timedelta
from urllib.parse import parse_qs, unquote, urlparse

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


def _filename_from_query(parsed) -> str:
    """Debrid services (Torbox, Real-Debrid, etc.) put the real release name
    in a `filename=` query param while the URL path is a UUID."""
    qs = parse_qs(parsed.query)
    for key in ("filename", "name"):
        values = qs.get(key)
        if values and values[0]:
            return unquote(values[0])
    return ""


def _filemeta(path: str, title: str = "") -> tuple:
    is_url = bool(path) and "://" in path and not path.startswith("file://")

    if is_url:
        parsed = urlparse(path)
        # Order of preference: filename= query param → Kodi metadata title →
        # URL basename. The query param wins because it carries the actual
        # release name (e.g. From.S04E03.2160p.AMZN.WEB-DL.H.265.mkv) that
        # other Syncplay clients need for filename-based matching.
        name = _filename_from_query(parsed) or title or os.path.basename(unquote(parsed.path))
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
        name, size = _filemeta(path, _display_title(self))
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