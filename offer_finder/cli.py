"""Dev CLI: run the pipeline without Telegram, set up Gmail, inspect the DB.
Run from the repo root (or use the make targets, see Makefile):

  uv run python -m offer_finder.cli search "65+ OLED TV around Mainz under 700" [--notify]
  uv run python -m offer_finder.cli gmail-auth
  uv run python -m offer_finder.cli gmail-poll [--notify]
  uv run python -m offer_finder.cli stats
  uv run python -m offer_finder.cli export evaluations.csv
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from pathlib import Path

from offer_finder import runner
from offer_finder.bot.notify import format_notification, format_summary, send_pending
from offer_finder.config import Settings
from offer_finder.core.models import Evaluation, Listing
from offer_finder.core.search_spec import SpecError, describe, next_turn
from offer_finder.services.gmail import GoogleGmailClient, authorize
from offer_finder.storage import db


def _print_evaluation(listing: Listing, evaluation: Evaluation) -> None:
    judgment = evaluation.judgment
    if evaluation.recommended:
        print(f"[{judgment.score}/100 RECOMMENDED]")
        print(format_notification(listing, evaluation, html=False))
    else:
        product, deal = judgment.product_assessment, judgment.deal_assessment
        print(f"[{judgment.score}/100] {listing.title} · {listing.price_raw or 'price unknown'}")
        print(f"  product {product.verdict} ({product.requirements_match}), deal {deal.verdict}")
        for reason in evaluation.checks.hard_fail_reasons:
            print(f"  hard fail: {reason}")
        print(f"  {listing.url}")
    unverified = [c.claim for c in [*judgment.highlights, *judgment.concerns] if not c.verified]
    if unverified:
        print(f"  (dropped {len(unverified)} claim(s) without verifiable evidence)")
    print()


async def _send_to_telegram(settings: Settings, search_spec_id: int | None) -> int:
    from telegram import Bot

    chat_id = settings.notify_chat_id
    if not settings.telegram_bot_token or chat_id is None:
        raise SystemExit("--notify needs TELEGRAM_BOT_TOKEN and TELEGRAM_ALLOWLIST (or TELEGRAM_NOTIFY_CHAT_ID)")
    bot = Bot(settings.telegram_bot_token)
    async with bot:
        conn = runner.open_db(settings)
        try:
            return await send_pending(bot, conn, chat_id, search_spec_id)
        finally:
            conn.close()


def cmd_search(settings: Settings, args: argparse.Namespace) -> int:
    provider = runner.make_provider(settings)
    try:
        turn = next_turn(
            [("user", args.text)],
            provider,
            settings.home_plz,
            max_questions=0,
            default_radius_km=settings.default_radius_km,
        )
    except SpecError as exc:
        print(f"Could not build a search from that: {exc}")
        return 1
    assert turn.spec is not None
    print(describe(turn.spec), end="\n\n")

    outcome = runner.run_search_once(settings, provider, settings.notify_chat_id or 0, turn.spec)
    if outcome.error:
        print(outcome.error)
        return 1
    conn = runner.open_db(settings)
    try:
        for listing, evaluation in db.evaluations_for_spec(conn, outcome.search_spec_id):
            _print_evaluation(listing, evaluation)
    finally:
        conn.close()
    print(format_summary(outcome.report, sent=outcome.report.recommended, threshold=settings.recommendation_threshold))
    if args.notify:
        print(f"Sent {asyncio.run(_send_to_telegram(settings, outcome.search_spec_id))} Telegram notification(s).")
    return 0


def cmd_gmail_auth(settings: Settings, _args: argparse.Namespace) -> int:
    if not Path(settings.gmail_credentials_path).exists():
        print(
            f"{settings.gmail_credentials_path} not found. In Google Cloud Console enable the Gmail API, "
            "create an OAuth client of type 'Desktop app' and save its JSON there."
        )
        return 1
    authorize(settings.gmail_credentials_path, settings.gmail_token_path)
    print(f"Saved Gmail token to {settings.gmail_token_path}")
    return 0


def cmd_gmail_poll(settings: Settings, args: argparse.Namespace) -> int:
    try:
        client = GoogleGmailClient(settings.gmail_token_path)
    except FileNotFoundError as exc:
        print(exc)
        return 1
    outcome = runner.poll_gmail_once(settings, runner.make_provider(settings), client)
    print(f"{outcome.messages} new email(s), {outcome.emails_without_listings} without listing links")
    report = outcome.report
    print(f"{report.found} listing links, {report.fetched} fetched, {report.evaluated} evaluated, "
          f"{report.recommended} recommended")
    if report.stopped_reason:
        print(f"Stopped early: {report.stopped_reason}")
    if outcome.error:
        print(outcome.error)

    conn = runner.open_db(settings)
    try:
        for listing, evaluation, _ in db.pending_notifications(conn, settings.notify_chat_id or 0):
            print(f"[{evaluation.judgment.score}/100 RECOMMENDED, not sent yet]")
            print(format_notification(listing, evaluation, html=False), end="\n\n")
    finally:
        conn.close()
    if args.notify:
        print(f"Sent {asyncio.run(_send_to_telegram(settings, None))} Telegram notification(s).")
    return 1 if outcome.error else 0


def cmd_stats(settings: Settings, _args: argparse.Namespace) -> int:
    conn = runner.open_db(settings)
    try:
        print(json.dumps(db.stats(conn), indent=2))
    finally:
        conn.close()
    return 0


def cmd_export(settings: Settings, args: argparse.Namespace) -> int:
    conn = runner.open_db(settings)
    try:
        rows = db.export_rows(conn)
    finally:
        conn.close()
    fieldnames = list(rows[0]) if rows else ["listing_id", "my_label"]
    with open(args.path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {len(rows)} evaluation(s) to {args.path}. Fill in my_label to build a labelled set.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m offer_finder.cli", description="offer-finder dev CLI")
    sub = parser.add_subparsers(dest="command", required=True)
    search = sub.add_parser("search", help="one message -> spec -> search -> evaluate -> print")
    search.add_argument("text")
    search.add_argument("--notify", action="store_true", help="also send recommendations to Telegram")
    sub.add_parser("gmail-auth", help="one-time Gmail OAuth consent in the browser")
    poll = sub.add_parser("gmail-poll", help="process new Kleinanzeigen alert emails once")
    poll.add_argument("--notify", action="store_true", help="also send recommendations to Telegram")
    sub.add_parser("stats", help="row counts per table/status")
    export = sub.add_parser("export", help="evaluations as CSV with an empty my_label column")
    export.add_argument("path")

    args = parser.parse_args(argv)
    settings = Settings()
    runner.setup_logging(settings)
    commands = {
        "search": cmd_search,
        "gmail-auth": cmd_gmail_auth,
        "gmail-poll": cmd_gmail_poll,
        "stats": cmd_stats,
        "export": cmd_export,
    }
    return commands[args.command](settings, args)


if __name__ == "__main__":
    sys.exit(main())
