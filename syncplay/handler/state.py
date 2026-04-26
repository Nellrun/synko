from time import time

from syncplay.kodi import player, setplaystate
from syncplay.socket import send
from syncplay.util import getrtt, gs, gsi, gsb

# synko runs in follow-only mode: we never volunteer our own playback
# position to the room. Every State we send carries the LAST authoritative
# server position back. This means the server can never decide we're "behind"
# and yank everyone backwards via RewindOnDesync. The leader (someone with a
# real Syncplay client) drives position; we just react to what they do.
#
# We still broadcast pause/unpause as user-initiated actions — those are real
# intent. We do NOT broadcast local seeks; the next server pulse will simply
# pull us back to the room's position.

_cstate = {
    "ping": {
        "latencyCalculation": 0,
        "clientLatencyCalculation": 0.0,
        "clientRtt": 0
    },
    "playstate": {
        "position": 0.0,
        "paused": True
    }
}
seeking = False
# Set by setplaystate before a catch-up seek so onPlayBackSeek can ignore the
# resulting Kodi callback instead of echoing it back to the server.
syncing_to_server = False


def update_local(position: float = 0.0, paused: bool = False):
    """Update local state without sending anything to the server."""
    _cstate["playstate"]["position"] = max(0.0, position)
    _cstate["playstate"]["paused"] = paused


def _setping(sping: dict):
    _cstate["ping"]["latencyCalculation"] = sping["latencyCalculation"]
    _cstate["ping"]["clientLatencyCalculation"] = time()
    if "clientLatencyCalculation" in sping:
        _cstate["ping"]["clientRtt"] = getrtt(
            sping["clientLatencyCalculation"],
            sping["serverRtt"]
        )


def _local_position() -> float:
    """Where Kodi actually is right now. Used only to decide whether to
    catch up locally, NEVER reported back to the server."""
    if not player.isPlaying():
        return 0.0
    t = player.getTime()
    return 0.0 if t < 0 else t


def handle(sstate: dict):
    _setping(sstate["ping"])

    # Always echo server position. This is the heart of follow-only mode —
    # we never tell the room "I'm at X". We tell it "I agree, I'm where you
    # said the room is".
    server_position = sstate["playstate"]["position"]
    _cstate["playstate"]["position"] = server_position

    if "ignoringOnTheFly" in sstate:
        iotf = sstate["ignoringOnTheFly"]
        if "server" in iotf:
            _cstate["ignoringOnTheFly"] = {"server": iotf["server"]}
            if sstate["playstate"]["setBy"] != gs("user"):
                setplaystate(sstate["playstate"], {
                    "position": _local_position(),
                    "paused": _cstate["playstate"]["paused"]
                })
                _cstate["playstate"]["paused"] = sstate["playstate"]["paused"]
        elif "client" in iotf:
            setplaystate(sstate["playstate"], {
                "position": _local_position(),
                "paused": _cstate["playstate"]["paused"]
            })
            _cstate["playstate"]["paused"] = sstate["playstate"]["paused"]
            del _cstate["ignoringOnTheFly"]
    elif "ignoringOnTheFly" in _cstate and "client" not in _cstate["ignoringOnTheFly"]:
        del _cstate["ignoringOnTheFly"]
    elif not seeking and player.isPlaying():
        # Compare Kodi's actual position against the room and catch up locally
        # if needed. This is purely a local decision; nothing about the diff
        # is reported back to the server.
        actual = _local_position()
        diff = server_position - actual
        tolerance_seconds = float(gsi("tolerance")) / 1000

        try:
            rewind_threshold_setting = gs("rewindThreshold")
            rewind_threshold = float(rewind_threshold_setting) if rewind_threshold_setting else 3.0
        except:
            rewind_threshold = 3.0
        rewind_threshold = max(tolerance_seconds * 2, rewind_threshold)

        try:
            rewind_disabled = gsb("disableRewind")
        except:
            rewind_disabled = False

        rtt_compensation = _cstate["ping"]["clientRtt"] / 2 if _cstate["ping"]["clientRtt"] > 0 else 0
        effective_tolerance = tolerance_seconds + rtt_compensation

        if abs(diff) > effective_tolerance:
            cps = {"position": actual, "paused": _cstate["playstate"]["paused"]}
            if diff > effective_tolerance:
                setplaystate(sstate["playstate"], cps)
            elif diff < -rewind_threshold and not rewind_disabled:
                setplaystate(sstate["playstate"], cps)

    send({"State": _cstate})


def dispatch(position: float, paused: bool, seeked: bool):
    """Broadcast a user-initiated state change to the room.

    Position is intentionally NOT taken from the caller — we always send the
    last server position so we don't accidentally drive the room. The only
    thing this actually broadcasts is the pause/unpause toggle.
    """
    if "ignoringOnTheFly" in _cstate:
        return

    if seeked:
        # Local seeks are not broadcast in follow-only mode. The next server
        # pulse will pull Kodi back to the room's position via setplaystate.
        return

    _cstate["playstate"]["paused"] = paused
    _cstate["ignoringOnTheFly"] = {"client": 1}

    send({"State": _cstate})
