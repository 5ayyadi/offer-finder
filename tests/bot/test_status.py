import asyncio

from offer_finder.bot.status import StatusMessage, format_status
from offer_finder.core.pipeline import Progress, RunReport
from tests.helpers.factories import make_spec
from tests.helpers.fakes import FakeBot

# --- format_status ------------------------------------------------------------------


def test_first_it_shows_the_search_and_loads_the_results_page():
    text = format_status(make_spec(), None)
    assert text.startswith('🔎 Searching Kleinanzeigen: "OLED 65 Zoll", up to €700, Mainz within 30 km')
    assert "⏳ Loading the results page…" in text
    assert "▫️ Reading listing pages" in text and "▫️ Evaluating" in text


def test_waiting_for_another_run_is_shown():
    text = format_status(make_spec(), None, waiting=True)
    assert "⏳ Waiting for the running search or Gmail poll to finish…" in text
    assert "Loading the results page" not in text


def test_while_reading_listing_pages_it_counts_up():
    report = RunReport(found=12, new=10, out_of_range=4)
    text = format_status(make_spec(), Progress("fetching", 3, 8, report))
    assert "✅ Results page: 12 listings (4 outside your price/radius)" in text
    assert "⏳ Reading listing pages: 3/8" in text
    assert "▫️ Evaluating" in text


def test_while_evaluating_it_shows_the_good_ones_so_far():
    report = RunReport(found=12, fetched=5, evaluated=2, recommended=1)
    text = format_status(make_spec(), Progress("evaluating", 2, 5, report))
    assert "✅ Read 5 listing pages" in text
    assert "⏳ Evaluating: 2/5 · 1 good so far" in text


def test_finished_search_shows_totals_and_time():
    report = RunReport(found=12, fetched=5, evaluated=5, recommended=2)
    text = format_status(make_spec(), Progress("evaluating", 5, 5, report), finished_s=95)
    assert "✅ Evaluated 5 · 2 recommended" in text
    assert "Done in 1m 35s." in text
    assert "⏳" not in text and "▫️" not in text


def test_counts_of_one_are_singular():
    report = RunReport(found=1, fetched=1, evaluated=1)
    text = format_status(make_spec(), Progress("evaluating", 1, 1, report), finished_s=3)
    assert "✅ Results page: 1 listing\n" in text and "✅ Read 1 listing page\n" in text


def test_finished_search_with_nothing_new_says_so():
    report = RunReport(found=2)
    text = format_status(make_spec(), Progress("evaluating", 0, 0, report), finished_s=3)
    assert "✅ No new listing pages to read" in text
    assert "✅ Nothing new to evaluate" in text


def test_stopped_search_shows_why_instead_of_the_remaining_steps():
    text = format_status(make_spec(), None, error="DRY_RUN is on and this page isn't cached.")
    assert "❌ Stopped: DRY_RUN is on and this page isn't cached." in text
    assert "⏳" not in text and "▫️" not in text


# --- StatusMessage ------------------------------------------------------------------


def test_status_message_is_sent_once_then_edited_only_when_it_changes():
    bot = FakeBot()
    status = StatusMessage(bot, 42, make_spec())

    async def scenario():
        await status.send()
        await status.refresh()  # nothing changed
        status.set_progress(Progress("fetching", 1, 4, RunReport(found=4, new=4)))
        await status.refresh()

    asyncio.run(scenario())
    assert [kind for kind, _ in bot.sent] == ["message", "edit"]
    edit = bot.sent[1][1]
    assert edit["message_id"] == 1 and "Reading listing pages: 1/4" in edit["text"]


def test_failed_status_edit_never_breaks_the_search():
    bot = FakeBot(fail_edits=True)
    status = StatusMessage(bot, 42, make_spec())

    async def scenario():
        await status.send()
        status.set_progress(Progress("fetching", 1, 4, RunReport(found=4)))
        await status.refresh()

    asyncio.run(scenario())  # no exception
    assert [kind for kind, _ in bot.sent] == ["message"]


def test_several_places_count_their_results_pages():
    from offer_finder.core.models import Location

    spec = make_spec(locations=[Location(name="Wiesbaden", plz="65183"), Location(name="Mainz", plz="55116")])
    assert "⏳ Loading results pages: 1/2 (Wiesbaden)" in format_status(spec, None)
    loading_second = Progress("results", 1, 2, RunReport())
    assert "⏳ Loading results pages: 2/2 (Mainz)" in format_status(spec, loading_second)
    fetching = Progress("fetching", 0, 3, RunReport(found=9))
    assert "✅ Results pages (2 places): 9 listings" in format_status(spec, fetching)
