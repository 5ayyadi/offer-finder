import asyncio

from telegram import InputMediaPhoto

from offer_finder.bot.notify import (
    Pick,
    format_notification,
    format_pick_list,
    format_summary,
    offer_keyboard,
    send_new_picks,
    send_offer,
    send_search_results,
)
from offer_finder.core.models import Claim, Concern, DealAssessment, ProductAssessment
from offer_finder.core.pipeline import RunReport
from offer_finder.storage import db
from tests.helpers.factories import make_evaluation, make_listing, make_spec
from tests.helpers.fakes import FakeBot


def _store_recommended(conn, listing_id="111", **listing_overrides):
    db.insert_discovered(conn, make_listing(listing_id, **listing_overrides))
    return db.save_evaluation(conn, make_evaluation(listing_id, search_spec_id=None))


# --- format_notification ---------------------------------------------------------

URL = make_listing().url


def test_notification_layout_is_short_and_scannable():
    text = format_notification(make_listing(), make_evaluation())
    assert text.splitlines() == [
        "<b>80/100 — LG OLED65C27LA 65 Zoll — 650 €</b>",
        "Mainz · 2.1 km",
        "",
        "<b>Matches</b>",
        "• Is an OLED",
        "",
        "<i>Unknown: purchase year</i>",
        "",
        "<b>Verdict:</b> OK deal · good product",
        f'<a href="{URL}">View listing</a>',
    ]


def test_notification_shows_only_verified_claims_and_worst_concerns_first():
    evaluation = make_evaluation()
    evaluation.judgment.highlights = [
        Claim(claim="Verified plus", evidence="x", verified=True),
        Claim(claim="Invented plus", evidence="y", verified=False),
    ]
    evaluation.judgment.concerns = [
        Concern(claim="Minor scratch", evidence="x", severity="low", verified=True),
        Concern(claim="Invented concern", evidence="y", severity="high", verified=False),
        Concern(claim="Pixel defect", evidence="x", severity="high", verified=True),
    ]
    text = format_notification(make_listing(), evaluation)
    assert "• Verified plus" in text and "Invented" not in text
    assert "<b>Concerns</b>\n• Pixel defect\n• Minor scratch" in text


def test_place_is_the_town_from_the_detail_page_location():
    evaluation = make_evaluation()
    evaluation.checks.distance_km = 41.8
    listing = make_listing(location_text="67819 Rheinland-Pfalz - Kriegsfeld")
    assert format_notification(listing, evaluation).splitlines()[1] == "Kriegsfeld · 41.8 km"


def test_notification_with_unknown_price_and_distance_says_so():
    evaluation = make_evaluation()
    evaluation.checks.distance_km = None
    lines = format_notification(make_listing(price_raw=None, location_text=None), evaluation).splitlines()
    assert lines[0].endswith("— price unknown</b>")
    assert lines[1] == "location unknown"


def test_partial_requirements_match_is_part_of_the_verdict():
    evaluation = make_evaluation()
    evaluation.judgment.product_assessment.requirements_match = "partial"
    assert "<b>Verdict:</b> OK deal · good product · partly matches" in format_notification(make_listing(), evaluation)


def test_listing_text_is_html_escaped():
    evaluation = make_evaluation()
    evaluation.judgment.highlights = [Claim(claim="Works <100%> & fine", evidence="x", verified=True)]
    text = format_notification(make_listing(title="TV <b>65\" & more"), evaluation)
    assert "TV &lt;b&gt;65&quot; &amp; more" in text
    assert "• Works &lt;100%&gt; &amp; fine" in text


def test_long_title_is_shortened():
    title = "Sehr schöner großer OLED Fernseher mit vielen Extras und Wandhalterung"
    header = format_notification(make_listing(title=title), make_evaluation()).splitlines()[0]
    assert "Sehr schöner großer OLED Fernseher mit vi…" in header


def test_long_notification_fits_the_caption_limit_and_keeps_the_link():
    evaluation = make_evaluation()
    evaluation.judgment.highlights = [Claim(claim="x" * 300, evidence="x", verified=True)] * 3
    evaluation.judgment.concerns = [Concern(claim="y" * 300, evidence="y", severity="low", verified=True)] * 3
    evaluation.judgment.unknowns = ["z" * 300] * 3
    text = format_notification(make_listing(), evaluation, limit=1024)
    assert len(text) <= 1024
    assert text.endswith(f'<a href="{URL}">View listing</a>')


def test_plain_version_for_the_terminal_has_no_markup_and_ends_with_the_url():
    text = format_notification(make_listing(), make_evaluation(), html=False)
    assert "<" not in text and "&amp;" not in text
    assert text.splitlines()[0] == "80/100 — LG OLED65C27LA 65 Zoll — 650 €"
    assert text.splitlines()[-1] == URL


# --- send_offer: the full card behind a list button ------------------------------


def _offer(conn, bot, **listing_overrides):
    evaluation_id = _store_recommended(conn, **listing_overrides)
    return asyncio.run(send_offer(bot, conn, 42, evaluation_id))


def test_offer_is_an_album_with_the_caption_on_the_first_image(conn):
    bot = FakeBot()
    assert _offer(conn, bot) is True
    kind, payload = bot.sent[0]
    assert kind == "media_group"
    media = payload["media"]
    assert all(isinstance(m, InputMediaPhoto) for m in media)
    assert len(media) == 2
    assert media[0].caption is not None and "80/100" in media[0].caption
    assert media[0].parse_mode == "HTML"
    assert media[1].caption is None


def test_offer_with_one_image_is_a_photo(conn):
    bot = FakeBot()
    _offer(conn, bot, image_urls=["https://img.kleinanzeigen.de/only.jpg"])
    assert bot.sent[0][0] == "photo"


def test_offer_shows_at_most_three_images(conn):
    bot = FakeBot()
    _offer(conn, bot, image_urls=[f"https://img.kleinanzeigen.de/{i}.jpg" for i in range(8)])
    assert len(bot.sent[0][1]["media"]) == 3


def test_offer_falls_back_to_text_when_telegram_rejects_images(conn):
    bot = FakeBot(fail_photos=True)
    _offer(conn, bot)
    assert bot.sent[0][0] == "message"
    assert "80/100" in bot.sent[0][1]["text"]
    assert bot.sent[0][1]["parse_mode"] == "HTML"


def test_offer_falls_back_to_plain_text_if_telegram_rejects_the_formatting(conn):
    bot = FakeBot(fail_html=True)
    _offer(conn, bot, image_urls=[])
    payload = bot.sent[0][1]
    assert payload["parse_mode"] is None and "<b>" not in payload["text"]


def test_unknown_offer_is_reported_as_missing(conn):
    assert asyncio.run(send_offer(FakeBot(), conn, 42, 999)) is False


# --- the pick list --------------------------------------------------------------


def _pick(listing_id="111", score=80, seen=False, evaluation_id=1, **judgment):
    evaluation = make_evaluation(listing_id, score=score, **judgment)
    evaluation.checks.nearest_location = "Mainz"
    return Pick(make_listing(listing_id), evaluation, evaluation_id, seen)


def test_list_entry_has_rank_score_price_place_tier_and_both_summaries():
    product = ProductAssessment(verdict="good", requirements_match="match", tier="top", summary="Flagship Wi-Fi 7 router")
    deal = DealAssessment(verdict="good", summary="Complete <with> box, fair price")
    chunks = format_pick_list([_pick(product_assessment=product, deal_assessment=deal, seen=True)], "Header")
    text, buttons = chunks[0]
    assert text.splitlines() == [
        "Header",
        "",
        "<b>1. 80/100 · LG OLED65C27LA 65 Zoll — 650 €</b>",
        "Mainz · 2.1 km from Mainz · top tier · seen before",
        "Flagship Wi-Fi 7 router",
        "Complete &lt;with&gt; box, fair price",
    ]
    assert buttons == [(1, 1)]


def test_long_lists_are_split_and_each_part_has_its_own_buttons():
    picks = [_pick(str(i), evaluation_id=100 + i) for i in range(1, 13)]
    chunks = format_pick_list(picks, "Header", limit=700)
    assert len(chunks) > 1
    assert all(len(text) <= 700 for text, _ in chunks)
    assert chunks[0][0].startswith("Header") and not chunks[1][0].startswith("Header")
    numbers = [number for _, buttons in chunks for number, _ in buttons]
    assert numbers == list(range(1, 13))
    assert chunks[0][1][0] == (1, 101)


def test_keyboard_has_numbered_offer_buttons_five_per_row():
    markup = offer_keyboard([(n, 100 + n) for n in range(1, 8)])
    rows = markup.inline_keyboard
    assert [len(row) for row in rows] == [5, 2]
    assert rows[0][0].text == "1" and rows[0][0].callback_data == "offer:101"


def _search_with_picks(conn):
    spec_id = db.insert_search_spec(conn, 42, make_spec())
    for listing_id, score in (("a", 75), ("b", 90)):
        db.insert_discovered(conn, make_listing(listing_id))
        db.save_evaluation(conn, make_evaluation(listing_id, search_spec_id=spec_id, score=score))
    return spec_id


def test_search_results_are_one_list_message_with_buttons_and_get_recorded(conn):
    spec_id = _search_with_picks(conn)
    bot = FakeBot()
    assert asyncio.run(send_search_results(bot, conn, 42, spec_id)) == 2
    assert [kind for kind, _ in bot.sent] == ["message"]
    payload = bot.sent[0][1]
    assert payload["text"].startswith('🔎 2 picks for "OLED 65 Zoll" · Mainz within 30 km')
    assert payload["text"].index("90/100") < payload["text"].index("75/100")
    assert payload["parse_mode"] == "HTML"
    assert [b.text for row in payload["reply_markup"].inline_keyboard for b in row] == ["1", "2"]
    assert db.pending_notifications(conn, 42) == []


def test_repeated_search_lists_the_picks_again_marked_as_seen(conn):
    spec_id = _search_with_picks(conn)
    asyncio.run(send_search_results(FakeBot(), conn, 42, spec_id))
    bot = FakeBot()
    asyncio.run(send_search_results(bot, conn, 42, spec_id))
    assert bot.sent[0][1]["text"].count("seen before") == 2


def test_no_picks_sends_no_list(conn):
    spec_id = db.insert_search_spec(conn, 42, make_spec())
    bot = FakeBot()
    assert asyncio.run(send_search_results(bot, conn, 42, spec_id)) == 0
    assert bot.sent == []


def test_failed_list_send_is_not_recorded_and_new_picks_are_retried(conn):
    _store_recommended(conn)
    assert asyncio.run(send_new_picks(FakeBot(fail_all=True), conn, 42)) == 0
    assert len(db.pending_notifications(conn, 42)) == 1

    bot = FakeBot()
    assert asyncio.run(send_new_picks(bot, conn, 42)) == 1
    assert bot.sent[0][1]["text"].startswith("📬 1 new pick from your Kleinanzeigen alerts")
    assert asyncio.run(send_new_picks(bot, conn, 42)) == 0  # only new picks, never twice
    assert len(bot.sent) == 1


def test_list_falls_back_to_plain_text_if_telegram_rejects_the_formatting(conn):
    _store_recommended(conn)
    bot = FakeBot(fail_html=True)
    assert asyncio.run(send_new_picks(bot, conn, 42)) == 1
    payload = bot.sent[0][1]
    assert payload["parse_mode"] is None and "<b>" not in payload["text"]
    assert payload["reply_markup"] is not None


# --- format_summary --------------------------------------------------------------


def test_summary_with_recommendations():
    report = RunReport(found=25, fetched=19, evaluated=19, recommended=3, best_score=88)
    text = format_summary(report, threshold=70)
    assert "25 listings" in text and "19 evaluated" in text and "3 recommended" in text


def test_summary_without_recommendations_mentions_best_score():
    report = RunReport(found=10, evaluated=10, recommended=0, best_score=58)
    assert "best was 58/100" in format_summary(report, threshold=70)


def test_summary_explains_problems():
    report = RunReport(found=10, evaluated=5, evaluation_failed=2, fetch_failed=1,
                       stopped_reason="request budget reached")
    text = format_summary(report, threshold=70)
    assert "request budget reached" in text
    assert "2 could not be evaluated" in text
    assert "1 detail page" in text


def test_summary_says_skipped_cards_were_outside_price_or_radius():
    report = RunReport(found=20, evaluated=8, out_of_range=12, best_score=50)
    assert "12 were outside your price range or radius." in format_summary(report, threshold=70)
