from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CATALOG_PATH = ROOT / "data" / "destinations.json"


class CatalogError(ValueError):
    pass


def normalize_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value or "").strip().casefold()


class Catalog:
    """Read-only access to the single canonical destination data file."""

    def __init__(self, path: Path | str = DEFAULT_CATALOG_PATH) -> None:
        self.path = Path(path)
        raw = self.path.read_bytes()
        self.digest = hashlib.sha256(raw).hexdigest()[:16]
        self.document: dict[str, Any] = json.loads(raw)
        self.destinations: dict[str, dict[str, Any]] = self.document["destinations"]
        self.allowed_icons: tuple[str, ...] = tuple(self.document["allowedIcons"])
        expected = self.document.get("destinationCount")
        if expected != 69 or len(self.destinations) != expected:
            raise CatalogError(
                f"canonical catalog must contain 69 destinations, got {len(self.destinations)}"
            )
        self._aliases = self._build_aliases()

    @property
    def source(self) -> dict[str, Any]:
        return self.document["source"]

    def get(self, destination_id: str) -> dict[str, Any]:
        key = normalize_text(destination_id)
        if key in self.destinations:
            return self.destinations[key]
        resolved = self.resolve_alias(destination_id)
        if resolved:
            return self.destinations[resolved]
        raise CatalogError(f"unknown destination: {destination_id}")

    def resolve_alias(self, value: str) -> str | None:
        normalized = normalize_text(value)
        direct = self._aliases.get(normalized)
        if direct:
            return direct
        compact = re.sub(r"[\s_-]+", "", normalized)
        return self._aliases.get(compact)

    def find_mentions(self, query: str) -> list[dict[str, Any]]:
        normalized = normalize_text(query)
        candidates: list[tuple[int, int, str, str]] = []
        for alias, destination_id in self._aliases.items():
            if len(alias) < 2:
                continue
            escaped = re.escape(alias)
            if re.search(r"[a-z]", alias):
                pattern = rf"(?<![a-z0-9]){escaped}(?![a-z0-9])"
            else:
                pattern = escaped
            for match in re.finditer(pattern, normalized):
                candidates.append((match.start(), match.end(), destination_id, alias))

        candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
        mentions: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        occupied_until = -1
        for start, end, destination_id, alias in candidates:
            if start < occupied_until or destination_id in seen_ids:
                continue
            mentions.append(
                {
                    "destination_id": destination_id,
                    "start": start,
                    "end": end,
                    "alias": alias,
                }
            )
            seen_ids.add(destination_id)
            occupied_until = end
        return mentions

    def search(
        self,
        query: str = "",
        region: str = "",
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        q = normalize_text(query)
        region_key = normalize_text(region)
        region_aliases = {
            "아시아": "asia",
            "북미": "north-america",
            "유럽": "europe",
            "아프리카": "africa",
            "오세아니아": "resort",
            "남미": "south-america",
        }
        region_key = region_aliases.get(region_key, region_key)
        rows = []
        for destination in self.destinations.values():
            if region_key and destination.get("region") != region_key:
                continue
            haystack = " ".join(
                str(destination.get(key, ""))
                for key in ("id", "city", "cityKo", "country", "countryKo", "summary")
            ).casefold()
            if q and q not in haystack:
                continue
            rows.append(self.public_destination(destination))
        return rows[: max(1, min(int(limit), 100))]

    def public_destination(self, destination: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": destination["id"],
            "city": destination["city"],
            "city_ko": destination["cityKo"],
            "country": destination["country"],
            "country_ko": destination["countryKo"],
            "region": destination["region"],
            "currency": destination["currency"],
            "time_zone": destination["timeZone"],
            "default_days": len(destination["itineraryTemplate"]),
            "summary": destination["summary"],
            "hero_image": destination["heroImage"],
        }

    def _build_aliases(self) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for destination_id, destination in self.destinations.items():
            values = {
                destination_id,
                destination_id.replace("-", " "),
                destination["city"],
                destination["cityKo"],
            }
            for value in values:
                normalized = normalize_text(value)
                aliases[normalized] = destination_id
                aliases[re.sub(r"[\s_-]+", "", normalized)] = destination_id
        return aliases
