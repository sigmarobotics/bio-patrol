"""LINE webhook service for bio-patrol (GCP project: xinyin7f).

Two jobs:
1. POST /webhook — receives LINE platform events, verifies X-Line-Signature,
   and records every group/room/user the bot can push to into Firestore
   collection ``sources``. Inviting the bot to a LINE group (or messaging it
   1:1) is how a push target becomes known; "leave" (bot removed) and
   "unfollow" (user blocked the bot) remove the target.
2. GET /groups — the bio-patrol backend fetches recorded sources so the
   operator can pick notification targets in the Settings UI. Guarded by a
   static bearer key.

Env: XINYIN7F_LINE_CHANNEL_SECRET, XINYIN7F_LINE_CHANNEL_ACCESS_TOKEN,
XINYIN7F_LINE_GROUPS_API_KEY — names match the workstation token registry
(~/.config/sigma/dev.env), so a local run picks up the right channel.
See README.md for deployment.
"""
import base64
import hashlib
import hmac
import json
import logging
import os
import re

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("line-webhook")

CHANNEL_SECRET = os.environ["XINYIN7F_LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["XINYIN7F_LINE_CHANNEL_ACCESS_TOKEN"]
GROUPS_API_KEY = os.environ["XINYIN7F_LINE_GROUPS_API_KEY"]

app = FastAPI()
db = firestore.AsyncClient()
sources = db.collection("sources")
http_client = httpx.AsyncClient(timeout=5.0)

SOURCE_ID_KEYS = {"group": "groupId", "room": "roomId", "user": "userId"}
# LINE ids are [UCR] + 32 hex; reject anything that could break a URL path
# or Firestore document path.
SOURCE_ID_RE = re.compile(r"[A-Za-z0-9_-]+")
# Bot removed from group / user blocked the bot — target is no longer pushable.
GONE_EVENTS = ("leave", "unfollow")


def _valid_signature(body: bytes, signature: str) -> bool:
    digest = hmac.new(CHANNEL_SECRET.encode(), body, hashlib.sha256).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


async def _display_name(source_type: str, source_id: str) -> str:
    """Best-effort friendly name from the LINE API; falls back to the raw id."""
    if source_type == "group":
        url = f"https://api.line.me/v2/bot/group/{source_id}/summary"
        name_key = "groupName"
    elif source_type == "user":
        url = f"https://api.line.me/v2/bot/profile/{source_id}"
        name_key = "displayName"
    else:
        return source_id
    try:
        res = await http_client.get(
            url, headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
        )
        if res.status_code == 200:
            return res.json().get(name_key) or source_id
    except (httpx.HTTPError, ValueError):
        pass
    return source_id


@app.post("/webhook")
async def webhook(request: Request, x_line_signature: str = Header("")):
    body = await request.body()
    if not _valid_signature(body, x_line_signature):
        raise HTTPException(status_code=403, detail="bad signature")

    for event in json.loads(body).get("events", []):
        source = event.get("source", {})
        source_type = source.get("type", "")
        source_id = source.get(SOURCE_ID_KEYS.get(source_type, ""), "")
        if not source_id or not SOURCE_ID_RE.fullmatch(source_id):
            continue
        event_type = event.get("type", "")
        ref = sources.document(source_id)

        if event_type in GONE_EVENTS:
            await ref.delete()
            logger.info("removed %s %s (%s)", source_type, source_id, event_type)
            continue
        # Chatty groups fire one webhook per member message — once the source
        # is recorded, a message event needs no name refetch or rewrite.
        if event_type == "message" and (await ref.get()).exists:
            continue

        doc = {
            "id": source_id,
            "type": source_type,
            "name": await _display_name(source_type, source_id),
            "last_event": event_type,
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
        await ref.set(doc, merge=True)
        logger.info(
            "recorded %s %s name=%r event=%s",
            source_type, source_id, doc["name"], event_type,
        )
    return {"ok": True}


@app.get("/groups")
async def groups(authorization: str = Header("")):
    if not hmac.compare_digest(authorization, f"Bearer {GROUPS_API_KEY}"):
        raise HTTPException(status_code=401, detail="unauthorized")
    docs = [d.to_dict() async for d in sources.stream()]
    docs.sort(key=lambda d: (d.get("type", ""), d.get("name", "")))
    return {"sources": docs}
