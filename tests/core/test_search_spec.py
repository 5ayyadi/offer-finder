import pytest

from offer_finder.core.models import Location, SearchSpec
from offer_finder.core.search_spec import SpecError, describe, log_line, next_turn
from tests.helpers.factories import make_spec
from tests.helpers.fakes import FakeProvider, spec_response


def test_asks_follow_up_when_model_is_not_ready():
    provider = FakeProvider(spec_response(ready=False, question="Welches Budget?", query=None))
    turn = next_turn([("user", "Ich will einen Fernseher")], provider, home_plz="10115")
    assert turn.question == "Welches Budget?"
    assert turn.spec is None


def test_returns_spec_when_model_is_ready():
    provider = FakeProvider(spec_response())
    turn = next_turn([("user", "good 65+ OLED TV around Mainz under 700")], provider, home_plz="10115")
    assert turn.question is None
    assert turn.spec is not None
    assert turn.spec.query == "OLED 65 Zoll"
    assert turn.spec.max_price_eur == 700
    assert turn.spec.locations == [Location(name="Mainz", plz="55116")]
    assert turn.spec.radius_km == 30
    assert "LG C/G-series" in (turn.spec.quality_guide or "")


def test_keeps_every_named_place():
    places = [
        {"name": "Wiesbaden", "plz": "65183"},
        {"name": "Mainz", "plz": "55116"},
        {"name": "Mannheim", "plz": "68159"},
    ]
    provider = FakeProvider(spec_response(locations=places, radius_km=15))
    turn = next_turn([("user", "router in Wiesbaden, Mainz or Mannheim")], provider, home_plz="10115")
    assert turn.spec is not None
    assert [loc.name for loc in turn.spec.locations] == ["Wiesbaden", "Mainz", "Mannheim"]
    assert turn.spec.radius_km == 15


def test_drops_places_without_a_valid_plz():
    places = [{"name": "Mainz", "plz": "55116"}, {"name": "Atlantis", "plz": "551"}]
    provider = FakeProvider(spec_response(locations=places))
    turn = next_turn([("user", "OLED TV")], provider, home_plz="10115")
    assert turn.spec is not None and turn.spec.locations == [Location(name="Mainz", plz="55116")]


@pytest.mark.parametrize("plz", [None, "Mainz", "551"])
def test_falls_back_to_home_plz_when_model_gives_no_valid_plz(plz):
    provider = FakeProvider(spec_response(locations=[{"name": None, "plz": plz}]))
    turn = next_turn([("user", "OLED TV")], provider, home_plz="10115")
    assert turn.spec is not None and turn.spec.locations == [Location(name=None, plz="10115")]


def test_no_radius_given_uses_the_default_radius():
    provider = FakeProvider(spec_response(radius_km=None))
    turn = next_turn([("user", "OLED TV in Mainz")], provider, home_plz="10115", default_radius_km=20)
    assert turn.spec is not None and turn.spec.radius_km == 20


def test_prompt_says_the_buyer_wants_the_best_and_lists_all_places():
    from offer_finder.core.search_spec import SYSTEM_PROMPT

    assert "best" in SYSTEM_PROMPT
    assert "most central" not in SYSTEM_PROMPT
    assert "quality_guide" in SYSTEM_PROMPT


def test_prompt_contains_whole_transcript():
    provider = FakeProvider(spec_response())
    history = [("user", "Fernseher"), ("assistant", "Welche Größe?"), ("user", "65 Zoll")]
    next_turn(history, provider, home_plz="10115")
    prompt = provider.prompts[0]
    assert "Fernseher" in prompt and "Welche Größe?" in prompt and "65 Zoll" in prompt


def test_at_question_limit_prompt_forbids_more_questions_and_spec_is_forced():
    history = [
        ("user", "Fernseher"),
        ("assistant", "q1"), ("user", "a1"),
        ("assistant", "q2"), ("user", "a2"),
        ("assistant", "q3"), ("user", "a3"),
    ]
    provider = FakeProvider(spec_response(ready=False, question="q4?"))
    turn = next_turn(history, provider, home_plz="10115", max_questions=3)
    assert "Do not ask another question" in provider.prompts[0]
    assert turn.question is None
    assert turn.spec is not None and turn.spec.query == "OLED 65 Zoll"


def test_not_ready_without_question_or_query_is_an_error():
    provider = FakeProvider(spec_response(ready=False, question=None, query=None))
    with pytest.raises(SpecError):
        next_turn([("user", "hm")], provider, home_plz="10115")


def test_provider_failure_is_a_spec_error():
    provider = FakeProvider(RuntimeError("503"))
    with pytest.raises(SpecError):
        next_turn([("user", "TV")], provider, home_plz="10115")


def test_twice_invalid_response_is_a_spec_error():
    provider = FakeProvider({"ready": "maybe"}, {"ready": "maybe"})
    with pytest.raises(SpecError):
        next_turn([("user", "TV")], provider, home_plz="10115")


def test_describe_shows_everything_the_user_must_confirm():
    text = describe(make_spec(min_price_eur=200, max_price_eur=700, nice_to_have=["Wandhalterung"]))
    for expected in ["OLED 65 Zoll", "65+ inch OLED TV", "OLED panel", "Wandhalterung", "€200", "€700", "Mainz", "55116", "30 km"]:
        assert expected in text


def test_describe_marks_unset_values():
    text = describe(make_spec(min_price_eur=None, max_price_eur=None, radius_km=None, location_text=None))
    assert "any price" in text
    assert "any distance" in text


def test_describe_and_log_line_list_every_city_and_the_quality_guide():
    spec = make_spec(
        locations=[Location(name="Wiesbaden", plz="65183"), Location(name="Mainz", plz="55116")],
        radius_km=20,
        quality_guide="Top tier: Wi-Fi 6E or 7.",
    )
    text = describe(spec)
    for expected in ["Wiesbaden (PLZ 65183)", "Mainz (PLZ 55116)", "within 20 km", "Best looks like: Top tier: Wi-Fi 6E or 7."]:
        assert expected in text
    assert "Wiesbaden, Mainz within 20 km" in log_line(spec)


def test_old_stored_specs_with_a_single_plz_still_load():
    stored = '{"query": "OLED", "product_summary": "TV", "location_text": "Mainz", "plz": "55116", "radius_km": 30}'
    spec = SearchSpec.model_validate_json(stored)
    assert spec.locations == [Location(name="Mainz", plz="55116")]
    assert spec.quality_guide is None
