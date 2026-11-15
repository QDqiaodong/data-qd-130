"""CLI: resource-constrained schedule from the bitemporal revision store.

Reads a plan snapshot at ``--effective-at`` / ``--known-seq`` in read-only mode,
reads a resource JSON document, then publishes a proven-optimal schedule.
The SQLite store is never created or modified; failures leave old reports intact.
"""
DESCRIPTION = ('Exact resource-constrained schedule from a bitemporal plan '
               'snapshot and a resource document')
import argparse
import json
import sqlite3
import sys
from pathlib import Path

from .common import read, save
from .history import MAX_EFFECTIVE_AT, MAX_KNOWN_SEQ, read_snapshot
from .resource import build_schedule, parse_resources

SCHEMA_VERSION = 1


def run(db_path, effective_at, known_seq, resource_path, time_limit=None):
    """Assemble the publication report; raises on any contract violation."""
    resources, resource_sha = read(resource_path)
    view = read_snapshot(db_path, effective_at, known_seq)
    if view['state'] != 'active':
        raise ValueError('no plan snapshot at effective_at=%d known_seq=%d'
                         % (effective_at, known_seq))
    classes, capacity, demands = parse_resources(resources, view['snapshot'])
    plan = build_schedule(view['snapshot'], classes, capacity, demands,
                          time_limit=time_limit)
    return {
        'schema_version': SCHEMA_VERSION,
        'coordinate': {
            'requested_effective_at': effective_at,
            'requested_known_seq': known_seq,
            'effective_at': view['effective_at'],
            'effective_known_seq': view['known_seq'],
        },
        'snapshot_revision_sources': view['revision_sources'],
        'resource_source_sha256': resource_sha,
        'result': plan,
    }


def _distinct_paths(*paths):
    resolved = [Path(p).resolve() for p in paths if p is not None]
    if len(set(resolved)) != len(resolved):
        raise ValueError('database, resource and output paths must differ')


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--effective-at', type=int, required=True,
                        dest='effective_at')
    parser.add_argument('--known-seq', type=int, required=True, dest='known_seq')
    parser.add_argument('--resources', type=Path, required=True)
    parser.add_argument('--output', type=Path)
    parser.add_argument('--time-limit', type=float, default=60.0,
                        dest='time_limit',
                        help='seconds before the exact search gives up (default 60); '
                             'no heuristic schedule is published on timeout')
    args = parser.parse_args()
    try:
        if args.time_limit <= 0:
            raise ValueError('time limit must be positive')
        _distinct_paths(args.db, args.resources, args.output)
        report = run(args.db, args.effective_at, args.known_seq,
                     args.resources, time_limit=args.time_limit)
        if args.output is not None:
            save(args.output, report)
    except (ValueError, TypeError, KeyError, OSError, OverflowError,
            RecursionError, TimeoutError, sqlite3.Error) as error:
        print(str(error), file=sys.stderr)
        return 2
    print(json.dumps(report, sort_keys=True, ensure_ascii=False, allow_nan=False))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
