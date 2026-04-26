import os
from datetime import timedelta
from urllib.parse import unquote, urlparse

import xbmcvfs
from xbmc import Player, sleep
from xbmcgui import Dialog

from syncplay.handler import set, state, hello
from syncplay.socket import connect, disconnect
from syncplay.util import gs, gsi, gsb  # Added gs and gsb imports


def _filemeta(path: str) -> tuple:
    if not path:
        return ("", 0)

    # Strip query string for URLs and decode percent-encoding so the
    # filename matches what other Syncplay clients see on disk.
    name = os.path.basename(unquote(urlparse(path).path) if "://" in path else path)

    try:
        size = int(xbmcvfs.File(path).size())
    except Exception:
        size = 0

    return (name, size)


class _Player(Player):
    def onAVStarted(self):
        path = self.getPlayingFile() if self.isPlaying() else ""
        name, size = _filemeta(path)
        # Fall back to the media-tag title if we somehow have no filename.
        if not name:
            name = self.getVideoInfoTag().getTitle()
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