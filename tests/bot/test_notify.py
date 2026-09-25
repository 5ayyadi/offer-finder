import asyncio

from telegram import InputMediaPhoto

from offer_finder.bot.notify import format_notification, format_summary, send_pending
from offer_finder.core.models import Claim, Concern
from offer_finder.core.pipeline import RunReport
from offer_finder.storage import db
from tests.helpers.factories import make_evaluation, make_listing
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


# --- send_pending ----------------------------------------------------------------


def test_sends_album_with_caption_on_first_image_and_records_notification(conn):
    _store_recommended(conn)
    bot = FakeBot()
    assert asyncio.run(send_pending(bot, conn, chat_id=42)) == 1

    kind, payload = bot.sent[0]
    assert kind == "media_group"
    media = payload["media"]
    assert all(isinstance(m, InputMediaPhoto) for m in media)
    assert len(media) == 2
    assert media[0].caption is not None and "80/100" in media[0].caption
    assert media[0].parse_mode == "HTML"
    assert media[1].caption is None
    assert db.pending_notifications(conn, 42) == []


def test_single_image_is_sent_as_photo(conn):
    _store_recommended(conn, image_urls=["https://img.kleinanzeigen.de/only.jpg"])
    bot = FakeBot()
    asyncio.run(send_pending(bot, conn, chat_id=42))
    assert bot.sent[0][0] == "photo"


def test_at_most_three_images(conn):
    _store_recommended(conn, image_urls=[f"https://img.kleinanzeigen.de/{i}.jpg" for i in range(8)])
    bot = FakeBot()
    asyncio.run(send_pending(bot, conn, chat_id=42))
    assert len(bot.sent[0][1]["media"]) == 3


def test_falls_back_to_text_when_telegram_rejects_images(conn):
    _store_recommended(conn)
    bot = FakeBot(fail_photos=True)
    assert asyncio.run(send_pending(bot, conn, chat_id=42)) == 1
    assert bot.sent[0][0] == "message"
    assert "80/100" in bot.sent[0][1]["text"]
    assert bot.sent[0][1]["parse_mode"] == "HTML"


def test_falls_back_to_plain_text_if_telegram_rejects_the_formatting(conn):
    _store_recommended(conn, image_urls=[])
    bot = FakeBot(fail_html=True)
    assert asyncio.run(send_pending(bot, conn, chat_id=42)) == 1
    payload = bot.sent[0][1]
    assert payload["parse_mode"] is None and "<b>" not in payload["text"]


def test_listing_without_images_is_sent_as_text(conn):
    _store_recommended(conn, image_urls=[])
    bot = FakeBot()
    asyncio.run(send_pending(bot, conn, chat_id=42))
    assert bot.sent[0][0] == "message"


def test_failed_send_is_not_recorded_and_retried_next_time(conn):
    _store_recommended(conn)
    assert asyncio.run(send_pending(FakeBot(fail_all=True), conn, chat_id=42)) == 0
    assert len(db.pending_notifications(conn, 42)) == 1

    assert asyncio.run(send_pending(FakeBot(), conn, chat_id=42)) == 1


def test_second_call_sends_nothing_new(conn):
    _store_recommended(conn)
    bot = FakeBot()
    asyncio.run(send_pending(bot, conn, chat_id=42))
    asyncio.run(send_pending(bot, conn, chat_id=42))
    assert len(bot.sent) == 1


# --- format_summary --------------------------------------------------------------


def test_summary_with_recommendations():
    report = RunReport(found=25, fetched=19, evaluated=19, recommended=3, best_score=88)
    text = format_summary(report, sent=3, threshold=70)
    assert "25 listings" in text and "19 evaluated" in text and "3 recommended" in text


def test_summary_without_recommendations_mentions_best_score():
    report = RunReport(found=10, evaluated=10, recommended=0, best_score=58)
    assert "best was 58/100" in format_summary(report, sent=0, threshold=70)


def test_summary_explains_problems():
    report = RunReport(found=10, evaluated=5, evaluation_failed=2, fetch_failed=1,
                       stopped_reason="request budget reached")
    text = format_summary(report, sent=0, threshold=70)
    assert "request budget reached" in text
    assert "2 could not be evaluated" in text
    assert "1 detail page" in text


def test_summary_mentions_already_sent_recommendations():
    report = RunReport(found=5, evaluated=5, recommended=2, best_score=80)
    assert "already sent" in format_summary(report, sent=0, threshold=70)


def test_summary_says_skipped_cards_were_outside_price_or_radius():
    report = RunReport(found=20, evaluated=8, out_of_range=12, best_score=50)
    assert "12 were outside your price range or radius." in format_summary(report, sent=0, threshold=70)
