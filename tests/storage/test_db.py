import pytest

from offer_finder.storage import db
from tests.helpers.factories import make_evaluation, make_listing, make_spec


def test_init_db_is_idempotent(conn):
    db.init_db(conn)
    db.init_db(conn)
    assert db.stats(conn)["listings"] == 0


def test_connect_creates_parent_dir_and_persists(tmp_path):
    path = tmp_path / "sub" / "test.db"
    first = db.connect(str(path))
    db.init_db(first)
    db.insert_discovered(first, make_listing("1", status="discovered"))
    first.close()

    second = db.connect(str(path))
    assert db.get_listing(second, "1") is not None


# --- search specs ----------------------------------------------------------------


def test_search_spec_round_trip(conn):
    spec = make_spec()
    spec_id = db.insert_search_spec(conn, chat_id=42, spec=spec)
    assert db.get_search_spec(conn, spec_id) == spec


def test_active_search_specs_newest_first(conn):
    old = db.insert_search_spec(conn, 42, make_spec(query="fahrrad"))
    new = db.insert_search_spec(conn, 42, make_spec(query="oled 65"))
    assert [spec_id for spec_id, _ in db.active_search_specs(conn)] == [new, old]


# --- listings --------------------------------------------------------------------


def test_insert_discovered_stores_new_listing_and_reports_it(conn):
    assert db.insert_discovered(conn, make_listing("1", status="discovered")) is True
    stored = db.get_listing(conn, "1")
    assert stored is not None
    assert stored.status == "discovered"
    assert stored.image_urls == make_listing().image_urls


def test_insert_discovered_never_overwrites_existing_listing(conn):
    db.insert_discovered(conn, make_listing("1", source="search", status="fetched", title="first"))
    assert db.insert_discovered(conn, make_listing("1", source="gmail", status="discovered")) is False
    stored = db.get_listing(conn, "1")
    assert stored is not None
    assert (stored.source, stored.status, stored.title) == ("search", "fetched", "first")


def test_update_listing_replaces_scraped_fields(conn):
    db.insert_discovered(conn, make_listing("1", status="discovered", description=None))
    db.update_listing(conn, make_listing("1", status="fetched", description="full text"))
    stored = db.get_listing(conn, "1")
    assert stored is not None
    assert (stored.status, stored.description) == ("fetched", "full text")


def test_set_listing_status(conn):
    db.insert_discovered(conn, make_listing("1", status="discovered"))
    db.set_listing_status(conn, "1", "fetch_failed")
    stored = db.get_listing(conn, "1")
    assert stored is not None and stored.status == "fetch_failed"


# --- evaluations -----------------------------------------------------------------


def test_listings_pending_evaluation_only_returns_fetched_unevaluated(conn):
    spec_id = db.insert_search_spec(conn, 42, make_spec())
    db.insert_discovered(conn, make_listing("fetched-new", status="fetched"))
    db.insert_discovered(conn, make_listing("fetched-done", status="fetched"))
    db.insert_discovered(conn, make_listing("not-fetched", status="discovered"))
    db.insert_discovered(conn, make_listing("too-expensive", status="out_of_range"))
    db.save_evaluation(conn, make_evaluation("fetched-done", search_spec_id=spec_id))

    pending = db.listings_pending_evaluation(conn, spec_id)
    assert [listing.listing_id for listing in pending] == ["fetched-new"]


def test_listings_pending_evaluation_can_be_limited_to_ids(conn):
    db.insert_discovered(conn, make_listing("a"))
    db.insert_discovered(conn, make_listing("b"))
    pending = db.listings_pending_evaluation(conn, None, listing_ids=["b"])
    assert [listing.listing_id for listing in pending] == ["b"]


def test_evaluation_without_spec_is_tracked_as_its_own_slot(conn):
    spec_id = db.insert_search_spec(conn, 42, make_spec())
    db.insert_discovered(conn, make_listing("1"))
    db.save_evaluation(conn, make_evaluation("1", search_spec_id=None))

    assert db.listings_pending_evaluation(conn, None) == []
    assert [x.listing_id for x in db.listings_pending_evaluation(conn, spec_id)] == ["1"]


def test_save_evaluation_twice_for_same_slot_is_rejected(conn):
    db.insert_discovered(conn, make_listing("1"))
    db.save_evaluation(conn, make_evaluation("1", search_spec_id=None))
    with pytest.raises(db.sqlite3.IntegrityError):
        db.save_evaluation(conn, make_evaluation("1", search_spec_id=None))


def test_saved_evaluation_keeps_all_three_layers_separately(conn):
    db.insert_discovered(conn, make_listing("1"))
    evaluation_id = db.save_evaluation(conn, make_evaluation("1", search_spec_id=None))
    row = conn.execute("SELECT * FROM evaluations WHERE id = ?", (evaluation_id,)).fetchone()
    assert '"facts"' in row["input_json"] and '"checks"' in row["input_json"]
    assert '"product_assessment"' in row["judgment_json"]
    assert (row["product_verdict"], row["deal_verdict"]) == ("good", "ok")


# --- notifications (dedup) -------------------------------------------------------


def test_pending_notifications_returns_recommended_not_yet_sent(conn):
    db.insert_discovered(conn, make_listing("good"))
    db.insert_discovered(conn, make_listing("meh"))
    db.save_evaluation(conn, make_evaluation("good", search_spec_id=None, recommended=True))
    db.save_evaluation(conn, make_evaluation("meh", search_spec_id=None, recommended=False))

    pending = db.pending_notifications(conn, chat_id=42)
    assert [(listing.listing_id, evaluation.recommended) for listing, evaluation, _ in pending] == [
        ("good", True)
    ]


def test_recorded_notification_is_never_pending_again(conn):
    db.insert_discovered(conn, make_listing("good"))
    evaluation_id = db.save_evaluation(conn, make_evaluation("good", search_spec_id=None))
    db.record_notification(conn, "good", chat_id=42, evaluation_id=evaluation_id)

    assert db.pending_notifications(conn, chat_id=42) == []
    assert len(db.pending_notifications(conn, chat_id=7)) == 1  # different chat


def test_listing_recommended_under_two_specs_is_sent_once(conn):
    spec_a = db.insert_search_spec(conn, 42, make_spec(query="a"))
    spec_b = db.insert_search_spec(conn, 42, make_spec(query="b"))
    db.insert_discovered(conn, make_listing("1"))
    first = db.save_evaluation(conn, make_evaluation("1", search_spec_id=spec_a))
    db.save_evaluation(conn, make_evaluation("1", search_spec_id=spec_b))

    assert len(db.pending_notifications(conn, 42)) == 1
    db.record_notification(conn, "1", 42, first)
    assert db.pending_notifications(conn, 42) == []


def test_pending_notifications_can_be_scoped_to_a_spec(conn):
    spec_a = db.insert_search_spec(conn, 42, make_spec(query="a"))
    spec_b = db.insert_search_spec(conn, 42, make_spec(query="b"))
    db.insert_discovered(conn, make_listing("1"))
    db.insert_discovered(conn, make_listing("2"))
    db.save_evaluation(conn, make_evaluation("1", search_spec_id=spec_a))
    db.save_evaluation(conn, make_evaluation("2", search_spec_id=spec_b))

    pending = db.pending_notifications(conn, 42, search_spec_id=spec_b)
    assert [listing.listing_id for listing, _, _ in pending] == ["2"]


# --- gmail messages --------------------------------------------------------------


def test_gmail_message_tracking(conn):
    assert db.gmail_message_seen(conn, "m1") is False
    db.record_gmail_message(conn, "m1", subject="Neue Anzeigen", listing_ids=["1", "2"], search_spec_id=None)
    assert db.gmail_message_seen(conn, "m1") is True


# --- stats / export --------------------------------------------------------------


def test_stats_counts_listings_by_status(conn):
    db.insert_discovered(conn, make_listing("1", status="fetched"))
    db.insert_discovered(conn, make_listing("2", status="out_of_range"))
    stats = db.stats(conn)
    assert stats["listings"] == 2
    assert stats["listings_by_status"] == {"fetched": 1, "out_of_range": 1}


def test_export_rows_include_both_verdicts_and_empty_label(conn):
    db.insert_discovered(conn, make_listing("1"))
    db.save_evaluation(conn, make_evaluation("1", search_spec_id=None))
    rows = db.export_rows(conn)
    assert rows[0]["listing_id"] == "1"
    assert rows[0]["product_verdict"] == "good"
    assert rows[0]["deal_verdict"] == "ok"
    assert rows[0]["my_label"] == ""


def test_evaluations_for_spec_best_first(conn):
    spec_id = db.insert_search_spec(conn, 42, make_spec())
    db.insert_discovered(conn, make_listing("low"))
    db.insert_discovered(conn, make_listing("high"))
    db.save_evaluation(conn, make_evaluation("low", search_spec_id=spec_id, score=40))
    db.save_evaluation(conn, make_evaluation("high", search_spec_id=spec_id, score=90))
    rows = db.evaluations_for_spec(conn, spec_id)
    assert [(listing.listing_id, evaluation.judgment.score) for listing, evaluation in rows] == [
        ("high", 90), ("low", 40)
    ]


def test_listing_attributes_round_trip(conn):
    db.insert_discovered(conn, make_listing("1", attributes={"Zustand": "Gut", "Versand": "Nur Abholung"}))
    stored = db.get_listing(conn, "1")
    assert stored is not None and stored.attributes == {"Zustand": "Gut", "Versand": "Nur Abholung"}
