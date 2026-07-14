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
    hero_images = [destination["heroImage"] for destination in catalog.destinations.values()]
    assert len(hero_images) == len(set(hero_images)) == 69
    for destination in catalog.destinations.values():
        assert destination["itineraryTemplate"]
        assert destination["phrases"]
        assert (ROOT / "viewer" / destination["heroImage"]).is_file()


def test_official_gmarket_sans_assets_and_license_are_checked_in() -> None:
    font_dir = ROOT / "viewer" / "assets" / "fonts"
    for weight in ("Light", "Medium", "Bold"):
        assert (font_dir / f"GmarketSans{weight}.woff2").is_file()
    assert (ROOT / "LICENSES" / "GmarketSans-OFL-1.1.txt").is_file()


def test_catalog_search_uses_korean_and_region_aliases() -> None:
    catalog = Catalog()
    assert catalog.search("도쿄")[0]["id"] == "tokyo"
    europe = catalog.search(region="유럽", limit=100)
    assert europe
    assert all(item["region"] == "europe" for item in europe)


def test_all_69_korean_city_names_resolve_from_natural_language() -> None:
    catalog = Catalog()
    resolved = {
        catalog.find_mentions(f"{destination['cityKo']} 3박 여행")[0]["destination_id"]
        for destination in catalog.destinations.values()
    }
    assert resolved == set(catalog.destinations)
    assert catalog.find_mentions("괌으로 4박 여행")[0]["destination_id"] == "guam"
    assert catalog.find_mentions("빈에서 3박 여행")[0]["destination_id"] == "austria"


def test_single_character_city_aliases_do_not_match_arbitrary_substrings() -> None:
    catalog = Catalog()
    assert catalog.find_mentions("빈티지 숍과 가빈이라는 카페") == []
    assert catalog.find_mentions("괌 여행")[0]["destination_id"] == "guam"
    assert catalog.find_mentions("빈 여행")[0]["destination_id"] == "austria"
