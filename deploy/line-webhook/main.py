"""LINE webhook service for bio-patrol (GCP project: xinyin7f).

Two jobs:
1. POST /webhook — receives LINE platform events, verifies X-Line-Signature,
   and records every group/room/user the bot can push to into Firestore
   collection ``sources``. Inviting the bot to a LINE group (or messaging it
   1:1) is how a push target becomes known.
2. GET /groups — the bio-patrol backend fetches recorded sources so the
   operator can pick notification targets in the Settings UI. Guarded by a
   static bearer key.

Env: LINE_CHANNEL_SECRET, LINE_CHANNEL_ACCESS_TOKEN, GROUPS_API_KEY.
"""
import base64
import hashlib
import hmac
import json
import logging
import os

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from google.cloud import firestore

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("line-webhook")

CHANNEL_SECRET = os.environ["LINE_CHANNEL_SECRET"]
CHANNEL_ACCESS_TOKEN = os.environ["LINE_CHANNEL_ACCESS_TOKEN"]
GROUPS_API_KEY = os.environ["GROUPS_API_KEY"]

app = FastAPI()
db = firestore.Client()
sources = db.collection("sources")

SOURCE_ID_KEYS = {"group": "groupId", "room": "roomId", "user": "userId"}


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
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get(
                url, headers={"Authorization": f"Bearer {CHANNEL_ACCESS_TOKEN}"}
            )
        if res.status_code == 200:
            return res.json().get(name_key) or source_id
    except httpx.HTTPError:
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
        if not source_id:
            continue
        # "leave" = bot removed from the group; keep the doc but mark inactive.
        active = event.get("type") != "leave"
        doc = {
            "id": source_id,
            "type": source_type,
            "active": active,
            "last_event": event.get("type", ""),
            "updated_at": firestore.SERVER_TIMESTAMP,
        }
        if active:
            doc["name"] = await _display_name(source_type, source_id)
        sources.document(source_id).set(doc, merge=True)
        logger.info(
            "recorded %s %s name=%r event=%s active=%s",
            source_type, source_id, doc.get("name"), event.get("type"), active,
        )
    return {"ok": True}


@app.get("/groups")
async def groups(authorization: str = Header("")):
    if authorization != f"Bearer {GROUPS_API_KEY}":
        raise HTTPException(status_code=401, detail="unauthorized")
    docs = [d.to_dict() for d in sources.stream()]
    docs.sort(key=lambda d: (d.get("type", ""), d.get("name", "")))
    return {"sources": docs}


@app.get("/healthz")
async def healthz():
    return {"ok": True}
