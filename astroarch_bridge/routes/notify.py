"""Route /api/notify: notifications from external programs, with no internet.

The main channel is UDP (see notify/listener.py), meant for senders that
would rather not deal with a token. This is the authenticated REST
equivalent: it serves senders that already speak HTTP, and it makes the
end-to-end chain testable without bringing a UDP socket into it.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from ..auth import require_token
from ..deps import Bridge, get_bridge
from ..notify.listener import MAX_MESSAGE, MAX_SOURCE, MAX_TITLE, VALID_LEVELS

router = APIRouter(prefix="/api/notify", tags=["notify"],
                   dependencies=[Depends(require_token)])


@router.post("")
async def push(payload: dict = Body(default={}),
               bridge: Bridge = Depends(get_bridge)) -> dict:
    """Forward a notification to every connected client.

    Body: {"message": "...", "title": "...", "level": "info|warning|error",
           "source": "..."}. Only `message` (or `title`) is required.
    """
    title = str(payload.get("title", "") or "")[:MAX_TITLE]
    message = str(payload.get("message", "") or "")[:MAX_MESSAGE]
    if not message and not title:
        raise HTTPException(status_code=400,
                            detail="either 'message' or 'title' is required")
    level = str(payload.get("level", "info") or "info").lower()
    if level not in VALID_LEVELS:
        level = "info"
    source = str(payload.get("source", "") or "")[:MAX_SOURCE]
    notif = await bridge.state.handle_notification({
        "title": title, "message": message, "level": level, "source": source,
    })
    return {"ok": True, "notification": notif}


@router.delete("")
async def clear(bridge: Bridge = Depends(get_bridge)) -> dict:
    """Empty the alert history.

    Deliberately manual, and deliberately not tied to a new Ekos session: the
    most valuable alert this channel carries is "KStars is gone", and whoever
    restarts KStars right after would wipe exactly that message. The history
    is capped anyway, and it lives in memory, so it also clears itself when
    the bridge restarts.
    """
    removed = await bridge.state.clear_notifications()
    return {"ok": True, "removed": removed}


@router.get("/recent")
async def recent(limit: int = Query(50, ge=1, le=50),
                 bridge: Bridge = Depends(get_bridge)) -> dict:
    """History of the notifications received most recently.

    A client returning from the background already finds them in the
    WebSocket snapshot; this route is for whoever does not keep the WS open.
    """
    items = await bridge.state.recent_notifications(limit)
    return {"count": len(items), "items": items}
