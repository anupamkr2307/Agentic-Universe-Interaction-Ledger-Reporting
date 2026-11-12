"""CLI interface for running ingestion and report generation."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from solution.config import DEFAULT_DB_PATH
from solution.db import get_connection, init_db
from solution.event_ingestor import ingest_all_events_in_drop
from solution.lead_ingestor import ingest_all_leads_in_drop
from solution.reporter import generate_reports


def cmd_ingest(drop_path: Path, db_path: Path) -> None:
    """Ingests leads and events from a drop directory into the database."""
    if not drop_path.exists() or not drop_path.is_dir():
        print(f"Error: Drop directory '{drop_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    conn = get_connection(db_path)
    init_db(conn)

    print(f"Ingesting leads from {drop_path}...")
    ingest_all_leads_in_drop(conn, drop_path)

    print(f"Ingesting events from {drop_path}...")
    ingest_all_events_in_drop(conn, drop_path)

    conn.close()
    print(f"Successfully processed {drop_path}.")


def cmd_report(out_dir: Path, db_path: Path) -> None:
    """Generates all 5 report files from the database."""
    conn = get_connection(db_path)
    init_db(conn)

    print(f"Generating reports into '{out_dir}'...")
    generate_reports(conn, out_dir)
    conn.close()
    print("Reports generated successfully.")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Agentic Universe - Interaction Ledger & Reporting Pipeline (Assignment A)"
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"Path to SQLite database file (default: {DEFAULT_DB_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcommand: ingest
    ingest_parser = subparsers.add_parser("ingest", help="Ingest a drop directory")
    ingest_parser.add_argument(
        "drop_dir", type=Path, help="Path to drop directory (e.g., data/drop_1)"
    )

    # Subcommand: report
    report_parser = subparsers.add_parser("report", help="Generate reports")
    report_parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="Output directory for generated reports (e.g., reports/)",
    )

    args = parser.parse_args()

    if args.command == "ingest":
        cmd_ingest(args.drop_dir, args.db)
    elif args.command == "report":
        cmd_report(args.out, args.db)


if __name__ == "__main__":
    main()
