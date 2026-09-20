import pytest

from core.distance import haversine_km, load_plz_table, plz_distance_km

_TEST_TABLE = {
    "55128": (49.9783406, 8.239083),  # Mainz-Bretzenheim
    "55131": (49.9889241, 8.2716688),  # Mainz-Zollhafen, a few km away
    "57074": (50.8749296, 8.0633292),  # Siegen, far away
}


def test_haversine_km_zero_for_same_point():
    assert haversine_km(49.9783406, 8.239083, 49.9783406, 8.239083) == pytest.approx(0.0, abs=0.01)


def test_haversine_km_known_short_distance():
    # Mainz center to Frankfurt center is roughly 30-35 km as the crow flies
    frankfurt = (50.1109, 8.6821)
    mainz = (49.9929, 8.2473)
    km = haversine_km(mainz[0], mainz[1], frankfurt[0], frankfurt[1])
    assert 25 < km < 40


def test_plz_distance_km_close_pair():
    km = plz_distance_km("55128", "55131", _TEST_TABLE)
    assert km is not None
    assert km < 10


def test_plz_distance_km_far_pair():
    km = plz_distance_km("55128", "57074", _TEST_TABLE)
    assert km is not None
    assert km > 100


def test_plz_distance_km_unknown_plz_returns_none():
    assert plz_distance_km("55128", "99999", _TEST_TABLE) is None
    assert plz_distance_km("00000", "55128", _TEST_TABLE) is None


def test_load_plz_table_reads_real_csv():
    table = load_plz_table("data/plz_de.csv")
    assert "55128" in table
    lat, lon = table["55128"]
    assert 49 < lat < 51
    assert 7 < lon < 9


def test_load_plz_table_missing_file_raises():
    with pytest.raises(FileNotFoundError):
        load_plz_table("data/does_not_exist.csv")
