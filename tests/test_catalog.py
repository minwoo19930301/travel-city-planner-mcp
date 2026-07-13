from pathlib import Path

from planner.catalog import Catalog


ROOT = Path(__file__).resolve().parents[1]


def test_canonical_catalog_has_all_69_destinations_and_assets() -> None:
    catalog = Catalog()
    assert len(catalog.destinations) == 69
    assert len(catalog.allowed_icons) == 27
    assert catalog.get("도쿄")["id"] == "tokyo"
    assert catalog.get("Tokyo")["cityKo"] == "도쿄"
    assert len(list((ROOT / "viewer/assets/heroes").glob("*.jpg"))) == 69
    for destination in catalog.destinations.values():
        assert destination["itineraryTemplate"]
        assert destination["phrases"]
        assert (ROOT / "viewer" / destination["heroImage"]).is_file()


def test_catalog_search_uses_korean_and_region_aliases() -> None:
    catalog = Catalog()
    assert catalog.search("도쿄")[0]["id"] == "tokyo"
    europe = catalog.search(region="유럽", limit=100)
    assert europe
    assert all(item["region"] == "europe" for item in europe)
