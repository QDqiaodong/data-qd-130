"""CLI: as-built resource settlement against a transactional SQLite ledger.

``plan-billing settle`` takes an invoice id, an execution document and a
pricing document.  It verifies instantaneous capacity, prices the invoice in
progressive tiers shared per ``(resource, period)``, and writes the invoice,
its groups, its detail lines and the cumulative usage ledger in one SQLite
transaction.  Retrying identical bytes replays without double-booking usage;
different bytes under the same id are a conflict; any failure rolls back.

``plan-billing query`` reads committed results and the ledger, never writes.
"""
DESCRIPTION = 'As-built resource settlement with progressive tiers and an atomic SQLite ledger'
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .billing import query, settle
from .common import save

SCHEMA_VERSION = 1


def _distinct_paths(*paths):
    resolved = [Path(p).resolve() for p in paths if p is not None]
    if len(set(resolved)) != len(resolved):
        raise ValueError('ledger, sources and output paths must differ')


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest='command', required=True)

    sett = sub.add_parser('settle', help='verify capacity and settle one invoice')
    sett.add_argument('--db', type=Path, required=True)
    sett.add_argument('--invoice-id', required=True, dest='invoice_id')
    sett.add_argument('--execution', type=Path, required=True)
    sett.add_argument('--pricing', type=Path, required=True)
    sett.add_argument('--output', type=Path)

    qry = sub.add_parser('query', help='read committed invoices and the ledger')
    qry.add_argument('--db', type=Path, required=True)
    qry.add_argument('--invoice-id', dest='invoice_id')
    qry.add_argument('--output', type=Path)

    args = parser.parse_args()
    try:
        if args.command == 'settle':
            _distinct_paths(args.db, args.execution, args.pricing, args.output)
            report = settle(args.db, args.invoice_id, args.execution,
                            args.pricing)
        else:
            _distinct_paths(args.db, args.output)
            report = query(args.db, args.invoice_id)
        if args.output is not None:
            save(args.output, report)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
