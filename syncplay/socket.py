from json import dumps, loads
from socket import AF_INET, SOCK_STREAM, socket, timeout as socket_timeout

import xbmc
from xbmc import Monitor
from xbmcgui import Dialog

from syncplay.util import gs, gsi

sock: socket = None  # type: ignore
_connected = False

# Exponential backoff state for reconnect attempts.
_backoff = 0
_BACKOFF_MIN = 1
_BACKOFF_MAX = 60

# Reentrancy guard: hello.dispatch() goes through send(), which would otherwise
# trigger another reconnect on transient failure and recurse forever.
_reconnecting = False

_mon: Monitor = None  # type: ignore


def _monitor() -> Monitor:
    global _mon
    if _mon is None:
        _mon = Monitor()
    return _mon


def connect():
    global sock, _connected, _backoff

    try:
        if sock:
            try:
                sock.close()
            except:
                pass

        sock = socket(AF_INET, SOCK_STREAM)
        sock.settimeout(10)
        sock.connect((gs("address"), gsi("port")))
        _connected = True
        _backoff = 0
        xbmc.log("Syncplay: Successfully connected to server", xbmc.LOGINFO)
        return True
    except Exception as e:
        xbmc.log(f"Syncplay: Connection failed: {str(e)}", xbmc.LOGERROR)
        _connected = False
        sock = None
        return False


def disconnect():
    global sock, _connected
    if sock:
        try:
            sock.close()
        except:
            pass
        finally:
            sock = None
            _connected = False


def is_connected():
    return _connected and sock is not None


def reconnect():
    """Reconnect with exponential backoff and re-register the session."""
    global _reconnecting, _backoff

    if _reconnecting:
        return False

    _reconnecting = True
    try:
        first_try = _backoff == 0
        disconnect()

        # 1s, 2s, 4s, ... capped at 60s. waitForAbort returns True if Kodi
        # is shutting down — bail out instead of finishing the sleep.
        delay = min(_backoff * 2 if _backoff else _BACKOFF_MIN, _BACKOFF_MAX)
        _backoff = delay

        if first_try:
            Dialog().notification("Syncplay", "Connection lost, reconnecting...", sound=False)

        xbmc.log(f"Syncplay: Reconnect attempt in {delay}s", xbmc.LOGINFO)
        if _monitor().waitForAbort(delay):
            return False

        if not connect():
            return False

        # Re-register with the server. Without this the server still has our
        # old (now-dead) session and will ignore everything we send.
        try:
            from syncplay.handler import hello
            hello.dispatch()
        except Exception as e:
            xbmc.log(f"Syncplay: Failed to send Hello after reconnect: {str(e)}", xbmc.LOGERROR)
            disconnect()
            return False

        Dialog().notification("Syncplay", "Reconnected", sound=False)
        return True
    finally:
        _reconnecting = False


def receive():
    global sock, _connected

    if not is_connected():
        if not reconnect():
            return []

    try:
        sock.settimeout(5)
        raw = sock.recv(4096)
        # Empty recv on a connected TCP socket means the peer closed cleanly.
        if not raw:
            xbmc.log("Syncplay: Server closed connection", xbmc.LOGWARNING)
            _connected = False
            return []

        data = raw.decode("utf-8").split("\r\n")[:-1]
        retdat = []
        for line in data:
            if not line.strip():
                continue
            try:
                retdat.append(loads(line))
            except Exception as e:
                xbmc.log(f"Syncplay: Failed to parse JSON: {line} - Error: {str(e)}", xbmc.LOGWARNING)
        return retdat

    except socket_timeout:
        # No data within the read window — connection is fine, just idle.
        return []

    except OSError as e:
        xbmc.log(f"Syncplay: Socket error in receive(): {str(e)}", xbmc.LOGWARNING)
        _connected = False
        return []

    except Exception as e:
        xbmc.log(f"Syncplay: Unexpected error in receive(): {str(e)}", xbmc.LOGERROR)
        _connected = False
        return []


def send(data: dict):
    global sock, _connected

    if not is_connected():
        if not reconnect():
            return False

    try:
        jsondat = dumps(data, separators=(",", ":"))
        sock.sendall((jsondat + "\r\n").encode("utf-8"))
        return True

    except (BrokenPipeError, OSError) as e:
        xbmc.log(f"Syncplay: Send failed ({str(e)}), will reconnect on next attempt", xbmc.LOGWARNING)
        _connected = False
        return False

    except Exception as e:
        xbmc.log(f"Syncplay: Unexpected error in send(): {str(e)}", xbmc.LOGERROR)
        return False


# Initial connect. If it fails, addon.py will trigger reconnect via its first send().
if not connect():
    xbmc.log("Syncplay: Initial connection failed", xbmc.LOGERROR)
