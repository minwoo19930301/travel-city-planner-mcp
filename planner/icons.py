from __future__ import annotations


ICON_ALIASES = {
    "beer": "utensils-crossed",
    "church": "landmark",
    "compass": "map",
    "gamepad": "ticket",
    "hotel": "luggage",
    "map-pin": "map",
    "train": "train-front",
    "utensils": "utensils-crossed",
    "waves": "sun",
}


def normalize_icon(value: str | None, allowed: set[str] | frozenset[str]) -> str:
    icon = (value or "map").strip()
    icon = ICON_ALIASES.get(icon, icon)
    return icon if icon in allowed else "map"
