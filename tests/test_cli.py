import csv
from pathlib import Path

import pytest

from offer_finder import cli, runner
from tests.helpers.fakes import judgment_response, spec_response
from tests.helpers.offline import cache_search_fixtures, offline_settings

PLZ_CSV = str(Path(__file__).parent.parent / "data" / "plz_de.csv")


class SpecThenJudgeLLM:
    model = "fake"

    def __init__(self):
        self.calls = 0

    def complete_json(self, system_prompt, user_prompt, json_schema):
        self.calls += 1
        if "ready" in json_schema["properties"]:
            return spec_response(query="Fernseher", plz="57074", radius_km=None, max_price_eur=200)
        return judgment_response(85 if "Grundig" in user_prompt else 20)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # no .env here
    settings = offline_settings(tmp_path)
    for key, value in {
        "DB_PATH": settings.db_path, "CACHE_DIR": settings.cache_dir, "DRY_RUN": "true",
        "LLM_MIN_INTERVAL_S": "0", "PLZ_CSV_PATH": PLZ_CSV, "HOME_PLZ": "57074",
    }.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(runner, "make_provider", lambda _settings: SpecThenJudgeLLM())
    return settings


def test_search_prints_spec_and_evaluations(env, capsys):
    cache_search_fixtures(env)
    assert cli.main(["search", "Fernseher bis 200 Euro in Siegen"]) == 0
    out = capsys.readouterr().out
    assert 'Search: "Fernseher"' in out
    assert "85/100 RECOMMENDED" in out and "20/100" in out
    assert "Checked 2 listings" in out


def test_search_problem_exits_nonzero(env, capsys):
    assert cli.main(["search", "Fernseher"]) == 1  # nothing cached in DRY_RUN
    assert "DRY_RUN" in capsys.readouterr().out


def test_stats_and_export(env, capsys, tmp_path):
    cache_search_fixtures(env)
    cli.main(["search", "Fernseher"])
    capsys.readouterr()

    assert cli.main(["stats"]) == 0
    assert '"evaluations": 2' in capsys.readouterr().out

    out_file = tmp_path / "labels.csv"
    assert cli.main(["export", str(out_file)]) == 0
    rows = list(csv.DictReader(out_file.open(encoding="utf-8")))
    assert len(rows) == 2
    assert {"score", "product_verdict", "deal_verdict", "recommended", "url", "my_label"} <= set(rows[0])


def test_gmail_commands_explain_missing_setup(env, capsys):
    assert cli.main(["gmail-poll"]) == 1
    assert "gmail-auth" in capsys.readouterr().out
    assert cli.main(["gmail-auth"]) == 1
    assert "Google Cloud" in capsys.readouterr().out
