from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path

from intelligence_bot.config import from_environment
from intelligence_bot.diagnostics import run_dry_run
from intelligence_bot.health import start_health_server
from intelligence_bot.service import check_destination, check_sources, check_telegram, discover_sources, run_service, set_monitoring_state

PROJECT_ROOT = Path(__file__).resolve().parent


def main() -> int:
    parser = argparse.ArgumentParser(description="Automatic Telegram channel news publisher")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--dry-run", action="store_true", help="evaluate recent source entries without publishing or changing the database")
    modes.add_argument("--check-sources", action="store_true", help="fetch and parse enabled feeds; does not contact Telegram")
    modes.add_argument("--telegram-check", action="store_true", help="call Telegram getMe; never sends a message")
    modes.add_argument("--discover-sources", action="store_true", help="check disabled official homepages for declared RSS/Atom links; does not auto-enable")
    modes.add_argument("--verify-destination", action="store_true", help="verify destination channel membership and post permission; sends no message")
    modes.add_argument("--enable-monitoring", action="store_true", help="persist monitoring ON; does not start the publisher")
    modes.add_argument("--disable-monitoring", action="store_true", help="persist monitoring OFF; does not stop the publisher process")
    parser.add_argument("--dry-run-source", help="limit --dry-run to one source id")
    parser.add_argument("--dry-run-sample-size", type=int, help="maximum article pages verified per source in --dry-run (1-50)")
    args = parser.parse_args()
    if not args.dry_run and (args.dry_run_source or args.dry_run_sample_size is not None):
        parser.error("--dry-run-source and --dry-run-sample-size require --dry-run")
    if args.dry_run and args.dry_run_sample_size is not None and not 1 <= args.dry_run_sample_size <= 50:
        parser.error("--dry-run-sample-size must be between 1 and 50")
    config = from_environment(PROJECT_ROOT)
    if args.dry_run:
        return asyncio.run(run_dry_run(config, PROJECT_ROOT, source_id=args.dry_run_source, sample_size=args.dry_run_sample_size or 10))
    if args.check_sources:
        logging.basicConfig(level=getattr(logging, config.log_level, logging.INFO), format="%(levelname)s %(message)s")
        return asyncio.run(check_sources(config, PROJECT_ROOT))
    if args.telegram_check:
        return asyncio.run(check_telegram(config))
    if args.verify_destination:
        return asyncio.run(check_destination(config))
    if args.enable_monitoring:
        return set_monitoring_state(config, PROJECT_ROOT, True)
    if args.disable_monitoring:
        return set_monitoring_state(config, PROJECT_ROOT, False)
    if args.discover_sources:
        return asyncio.run(discover_sources(config, PROJECT_ROOT))
    # Render Web Services require an HTTP port. On Render, the health server
    # binds to the platform-provided PORT while the news publisher runs.
    health_server = start_health_server()
    run_service(config, PROJECT_ROOT)
    if health_server is not None:
        health_server.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
