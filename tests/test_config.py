from pathlib import Path

from offer_finder.config import Settings


def _settings(monkeypatch, **values):
    monkeypatch.chdir("/tmp")  # ensure no stray .env in cwd affects defaults
    return Settings(_env_file=None, **values)


def test_settings_load_with_defaults(monkeypatch):
    settings = _settings(monkeypatch)
    assert settings.home_plz == "10115"
    assert settings.max_requests_per_run == 40
    assert settings.default_radius_km == 20
    assert settings.dry_run is True
    assert settings.recommendation_threshold == 70
    assert settings.db_path == "data/offer_finder.db"
    assert settings.gmail_enabled is False


def test_notify_chat_defaults_to_first_allowlisted_user(monkeypatch):
    assert _settings(monkeypatch, telegram_allowlist=[5, 6]).notify_chat_id == 5


def test_explicit_notify_chat_wins(monkeypatch):
    settings = _settings(monkeypatch, telegram_allowlist=[5], telegram_notify_chat_id=99)
    assert settings.notify_chat_id == 99


def test_notify_chat_is_none_without_allowlist(monkeypatch):
    assert _settings(monkeypatch).notify_chat_id is None


def test_empty_values_from_env_example_are_accepted(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        (Path(__file__).parent.parent / ".env.example").read_text(encoding="utf-8"), encoding="utf-8"
    )
    settings = Settings()
    assert settings.telegram_notify_chat_id is None
    assert settings.notify_chat_id == 123456789


def test_blank_default_radius_means_any_distance(monkeypatch):
    assert _settings(monkeypatch, default_radius_km="").default_radius_km is None
