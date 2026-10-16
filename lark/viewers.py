"""Live viewer count from Twitch Helix. Only called while the user is signed in to Twitch."""


async def fetch_viewers(client):
    """Viewer count while the channel is live, None when it is offline."""
    body = await client._helix("GET", "/streams", params={"user_id": client.state["user_id"], "first": 1})
    rows = body.get("data") or []
    if not rows:
        return None
    n = rows[0].get("viewer_count")
    return n if isinstance(n, int) and not isinstance(n, bool) and n >= 0 else None
