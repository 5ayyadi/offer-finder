import pytest

from core.catalog import load_catalog, match

_TEST_CATALOG = {
    "LG OLED C2 65": {
        "aliases": ["LG C2 65", "OLED65C2", "C2 65 Zoll"],
        "fair_used_price_eur": 900,
        "quality": "excellent panel",
    },
    "Samsung Q60B 55": {
        "aliases": ["Samsung Q60B", "QE55Q60B"],
        "fair_used_price_eur": 400,
        "quality": None,
    },
}


def test_match_hits_alias_in_title():
    result = match("LG OLED C2 65 Zoll TV, sehr guter Zustand", "", _TEST_CATALOG)
    assert result is not None
    assert result.model_name == "LG OLED C2 65"
    assert result.fair_used_price_eur == 900
    assert result.quality == "excellent panel"


def test_match_hits_alias_in_description_when_not_in_title():
    result = match("TV zu verkaufen", "Es handelt sich um einen QE55Q60B", _TEST_CATALOG)
    assert result is not None
    assert result.model_name == "Samsung Q60B 55"


def test_match_is_case_insensitive():
    result = match("lg c2 65 zoll", "", _TEST_CATALOG)
    assert result is not None
    assert result.model_name == "LG OLED C2 65"


def test_match_returns_none_for_unknown_model():
    result = match("Irgendein Fernseher 32 Zoll", "keine weiteren Details", _TEST_CATALOG)
    assert result is None


def test_match_prefers_title_over_description():
    # title matches C2, description happens to mention the other model's alias
    result = match("LG C2 65 Zoll", "vergleichbar mit Samsung Q60B", _TEST_CATALOG)
    assert result is not None
    assert result.model_name == "LG OLED C2 65"


def test_load_catalog_reads_real_yaml_file():
    catalog = load_catalog("catalog_tv.yaml")
    assert "Grundig 43 GUS 8768" in catalog
    entry = catalog["Grundig 43 GUS 8768"]
    assert entry["fair_used_price_eur"] == 180
    assert "Grundig 43 GUS" in entry["aliases"]


def test_load_catalog_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_catalog("does_not_exist.yaml")


def test_real_catalog_matches_real_grundig_title():
    catalog = load_catalog("catalog_tv.yaml")
    result = match("Grundig 43 GUS Fernseher– 43 Zoll 4K UHD LED Smart TV", "", catalog)
    assert result is not None
    assert result.model_name == "Grundig 43 GUS 8768"
    assert result.fair_used_price_eur == 180
