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

    pending = db.pending_notifications(conn, chat_id=42, is_admin=True)
    assert [(listing.listing_id, evaluation.recommended) for listing, evaluation, _ in pending] == [
        ("good", True)
    ]


def test_recorded_notification_is_never_pending_again(conn):
    db.insert_discovered(conn, make_listing("good"))
    evaluation_id = db.save_evaluation(conn, make_evaluation("good", search_spec_id=None))
    db.record_notification(conn, "good", chat_id=42, evaluation_id=evaluation_id)

    assert db.pending_notifications(conn, chat_id=42, is_admin=True) == []
    assert len(db.pending_notifications(conn, chat_id=7, is_admin=True)) == 1  # different chat


def test_listing_recommended_under_two_specs_is_sent_once(conn):
    spec_a = db.insert_search_spec(conn, 42, make_spec(query="a"))
    spec_b = db.insert_search_spec(conn, 42, make_spec(query="b"))
    db.insert_discovered(conn, make_listing("1"))
    first = db.save_evaluation(conn, make_evaluation("1", search_spec_id=spec_a))
    db.save_evaluation(conn, make_evaluation("1", search_spec_id=spec_b))

    assert len(db.pending_notifications(conn, 42, is_admin=True)) == 1
    db.record_notification(conn, "1", 42, first)
    assert db.pending_notifications(conn, 42, is_admin=True) == []


def test_pending_notifications_can_be_scoped_to_a_spec(conn):
    spec_a = db.insert_search_spec(conn, 42, make_spec(query="a"))
    spec_b = db.insert_search_spec(conn, 42, make_spec(query="b"))
    db.insert_discovered(conn, make_listing("1"))
    db.insert_discovered(conn, make_listing("2"))
    db.save_evaluation(conn, make_evaluation("1", search_spec_id=spec_a))
    db.save_evaluation(conn, make_evaluation("2", search_spec_id=spec_b))

    pending = db.pending_notifications(conn, 42, search_spec_id=spec_b, is_admin=True)
    assert [listing.listing_id for listing, _, _ in pending] == ["2"]


def test_picks_for_spec_are_recommended_best_first_and_marked_if_seen(conn):
    spec_id = db.insert_search_spec(conn, 42, make_spec())
    for listing_id in ("a", "b", "c"):
        db.insert_discovered(conn, make_listing(listing_id))
    db.save_evaluation(conn, make_evaluation("a", search_spec_id=spec_id, score=75))
    seen_id = db.save_evaluation(conn, make_evaluation("b", search_spec_id=spec_id, score=90))
    db.save_evaluation(conn, make_evaluation("c", search_spec_id=spec_id, recommended=False, score=40))
    db.record_notification(conn, "b", 42, seen_id)

    picks = db.picks_for_spec(conn, 42, spec_id)
    assert [(listing.listing_id, seen) for listing, _, _, seen in picks] == [("b", True), ("a", False)]
    assert picks[0][2] == seen_id
    assert db.picks_for_spec(conn, 7, spec_id) == []  # another chat never sees this search


def test_get_evaluation_by_id(conn):
    db.insert_discovered(conn, make_listing("a"))
    evaluation_id = db.save_evaluation(conn, make_evaluation("a", search_spec_id=None))
    found = db.get_evaluation(conn, evaluation_id)
    assert found is not None and found[0].listing_id == "a" and found[1].judgment.score == 80
    assert db.get_evaluation(conn, evaluation_id + 1) is None


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


# --- users -----------------------------------------------------------------------


def test_sync_admins_makes_allowlisted_ids_approved_admins(conn):
    db.sync_admins(conn, [42])
    admin = db.get_user(conn, 42)
    assert admin is not None and admin.is_admin and admin.status == "approved"


def test_sync_admins_demotes_ids_no_longer_allowlisted(conn):
    db.sync_admins(conn, [42, 43])
    db.sync_admins(conn, [42])
    former = db.get_user(conn, 43)
    assert former is not None and not former.is_admin


def test_request_access_creates_one_pending_user(conn):
    user, created = db.request_access(conn, 7, "Sam")
    assert created and user.status == "pending" and not user.is_admin and user.name == "Sam"
    again, created_again = db.request_access(conn, 7, "Sam")
    assert not created_again and again.status == "pending"


def test_request_access_never_reopens_a_blocked_user(conn):
    db.request_access(conn, 7, "Sam")
    db.set_user_status(conn, 7, "blocked")
    user, created = db.request_access(conn, 7, "Sam")
    assert not created and user.status == "blocked"


def test_set_user_status_reports_whether_anything_changed(conn):
    db.request_access(conn, 7, "Sam")
    assert db.set_user_status(conn, 7, "approved") is True
    assert db.set_user_status(conn, 7, "approved") is False
    assert db.set_user_status(conn, 999, "approved") is False  # unknown user


def test_user_settings_round_trip(conn):
    db.request_access(conn, 7, "Sam")
    db.set_user_home(conn, 7, "20095")
    db.set_user_radius(conn, 7, 0)
    user = db.get_user(conn, 7)
    assert user is not None and user.home_plz == "20095" and user.radius_km == 0
    db.set_user_radius(conn, 7, None)
    assert db.get_user(conn, 7).radius_km is None  # type: ignore[union-attr]


def test_list_users_admins_first(conn):
    db.request_access(conn, 7, "Sam")
    db.sync_admins(conn, [42])
    assert [user.telegram_id for user in db.list_users(conn)] == [42, 7]


def test_active_search_specs_can_be_limited_to_owners(conn):
    mine = db.insert_search_spec(conn, 42, make_spec(query="oled"))
    db.insert_search_spec(conn, 7, make_spec(query="oled"))
    assert [spec_id for spec_id, _ in db.active_search_specs(conn, owner_ids=[42])] == [mine]
    assert db.active_search_specs(conn, owner_ids=[]) == []


# --- who may see an evaluation ------------------------------------------------------


def _two_users_with_picks(conn):
    """Admin 42 and friend 7, each with one recommended pick of their own search,
    plus one pick from an alert email that matched no search."""
    db.sync_admins(conn, [42])
    db.request_access(conn, 7, "Sam")
    db.set_user_status(conn, 7, "approved")
    admin_spec = db.insert_search_spec(conn, 42, make_spec(query="oled"))
    friend_spec = db.insert_search_spec(conn, 7, make_spec(query="fahrrad"))
    for listing_id in ("admin", "friend", "email"):
        db.insert_discovered(conn, make_listing(listing_id))
    return {
        "admin": db.save_evaluation(conn, make_evaluation("admin", search_spec_id=admin_spec)),
        "friend": db.save_evaluation(conn, make_evaluation("friend", search_spec_id=friend_spec)),
        "email": db.save_evaluation(conn, make_evaluation("email", search_spec_id=None)),
    }


def test_users_see_only_their_own_evaluations(conn):
    ids = _two_users_with_picks(conn)
    assert db.evaluation_visible_to(conn, ids["friend"], 7, is_admin=False)
    assert not db.evaluation_visible_to(conn, ids["admin"], 7, is_admin=False)
    assert not db.evaluation_visible_to(conn, ids["email"], 7, is_admin=False)


def test_admins_see_their_own_and_email_evaluations_but_not_friends(conn):
    ids = _two_users_with_picks(conn)
    assert db.evaluation_visible_to(conn, ids["admin"], 42, is_admin=True)
    assert db.evaluation_visible_to(conn, ids["email"], 42, is_admin=True)
    assert not db.evaluation_visible_to(conn, ids["friend"], 42, is_admin=True)


def test_unknown_evaluation_is_not_visible(conn):
    ids = _two_users_with_picks(conn)
    assert not db.evaluation_visible_to(conn, max(ids.values()) + 1, 42, is_admin=True)


def test_alert_list_for_the_admin_never_contains_a_friends_picks(conn):
    _two_users_with_picks(conn)
    pending = db.pending_notifications(conn, 42, is_admin=True)
    assert sorted(listing.listing_id for listing, _, _ in pending) == ["admin", "email"]


def test_pending_notifications_for_a_friend_contain_only_their_picks(conn):
    _two_users_with_picks(conn)
    pending = db.pending_notifications(conn, 7, is_admin=False)
    assert [listing.listing_id for listing, _, _ in pending] == ["friend"]


def test_listing_picked_by_friend_and_admin_is_still_listed_for_the_admin(conn):
    """The one-row-per-listing rule must not pick the friend's evaluation and
    then hide the listing from the admin."""
    db.sync_admins(conn, [42])
    friend_spec = db.insert_search_spec(conn, 7, make_spec(query="fahrrad"))
    db.insert_discovered(conn, make_listing("shared"))
    db.save_evaluation(conn, make_evaluation("shared", search_spec_id=friend_spec))  # lower id
    email_id = db.save_evaluation(conn, make_evaluation("shared", search_spec_id=None))
    assert [eid for _, _, eid in db.pending_notifications(conn, 42, is_admin=True)] == [email_id]


def test_picks_for_spec_are_empty_for_a_chat_that_does_not_own_the_spec(conn):
    spec_id = db.insert_search_spec(conn, 7, make_spec())
    db.insert_discovered(conn, make_listing("a"))
    db.save_evaluation(conn, make_evaluation("a", search_spec_id=spec_id))
    assert db.picks_for_spec(conn, 42, spec_id) == []


# --- search runs (the daily limit) ---------------------------------------------------


def test_reserve_search_run_allows_one_active_run_per_user(conn):
    first = db.reserve_search_run(conn, 7, daily_limit=5)
    assert isinstance(first, int)
    assert db.reserve_search_run(conn, 7, daily_limit=5) == "busy"
    assert isinstance(db.reserve_search_run(conn, 8, daily_limit=5), int)  # other users aren't affected

    db.finish_search_run(conn, first, "done", search_spec_id=None)
    assert isinstance(db.reserve_search_run(conn, 7, daily_limit=5), int)


def test_reserve_search_run_enforces_the_daily_limit(conn):
    for _ in range(2):
        run_id = db.reserve_search_run(conn, 7, daily_limit=2)
        assert isinstance(run_id, int)
        db.finish_search_run(conn, run_id, "failed", search_spec_id=None)  # failed runs count too
    assert db.reserve_search_run(conn, 7, daily_limit=2) == "limit"


def test_daily_limit_is_a_rolling_24_hours(conn):
    from datetime import UTC, datetime, timedelta

    run_id = db.reserve_search_run(conn, 7, daily_limit=1)
    assert isinstance(run_id, int)
    db.finish_search_run(conn, run_id, "done", search_spec_id=None)
    later = datetime.now(UTC) + timedelta(hours=24, minutes=1)
    assert isinstance(db.reserve_search_run(conn, 7, daily_limit=1, now=later), int)


def test_no_daily_limit_for_unlimited_users(conn):
    for _ in range(3):
        run_id = db.reserve_search_run(conn, 42, daily_limit=None)
        assert isinstance(run_id, int)
        db.finish_search_run(conn, run_id, "done", search_spec_id=None)


def test_two_simultaneous_reservations_give_exactly_one_run(tmp_path):
    import threading

    path = str(tmp_path / "runs.db")
    setup = db.connect(path)
    db.init_db(setup)
    setup.close()
    barrier = threading.Barrier(2)
    results: list[object] = []

    def reserve():
        connection = db.connect(path)
        barrier.wait()
        results.append(db.reserve_search_run(connection, 7, daily_limit=5))
        connection.close()

    threads = [threading.Thread(target=reserve) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sum(isinstance(r, int) for r in results) == 1 and "busy" in results


def test_unfinished_runs_are_marked_interrupted_and_still_count(conn):
    run_id = db.reserve_search_run(conn, 7, daily_limit=1)
    assert isinstance(run_id, int)
    db.start_search_run(conn, run_id)
    assert db.interrupt_unfinished_runs(conn) == 1
    assert db.reserve_search_run(conn, 7, daily_limit=1) == "limit"  # not busy any more, but counted
