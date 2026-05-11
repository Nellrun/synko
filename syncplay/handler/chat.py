from xbmcgui import Dialog


def handle(info: dict):
    Dialog().notification(
        "Syncplay",
        "{}: {}".format(info["username"], info["message"]),
        sound=False
    )


def say(text: str):
    """Send a chat line to the room. Used as a poor man's remote-log channel
    so debug info from synko shows up in the desktop Syncplay client."""
    # Imported lazily to avoid a circular import with the chat <-> socket pair.
    from syncplay.socket import send
    try:
        send({"Chat": text})
    except Exception:
        pass