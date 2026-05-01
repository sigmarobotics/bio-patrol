"""Bed configuration endpoints."""
from fastapi import APIRouter

from settings.config import BEDS_FILE
from settings.defaults import DEFAULT_BEDS
from utils.json_io import load_json, save_json

router = APIRouter(prefix="/api", tags=["Beds"])


@router.get("/beds")
async def get_beds():
    """Return beds.json (or defaults if empty/missing)."""
    data = load_json(BEDS_FILE, DEFAULT_BEDS)
    return data or DEFAULT_BEDS


@router.post("/beds")
async def save_beds(body: dict):
    """Save beds.json."""
    save_json(BEDS_FILE, body)
    return {"status": "ok", "data": body}
