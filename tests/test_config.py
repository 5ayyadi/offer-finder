from config import Settings


def test_settings_load_with_defaults(monkeypatch):
    monkeypatch.chdir("/tmp")  # ensure no stray .env in cwd affects defaults
    settings = Settings(_env_file=None)
    assert settings.home_plz == "10115"
    assert settings.max_requests_per_run == 20
    assert settings.dry_run is True
