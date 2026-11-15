"""SQLite bitemporal plan revision store.

Two time axes are kept separately:

* ``effective_at`` -- business time at which a revision applies (non-negative
  integer).  Revisions may be back-filled out of order.
* ``known_seq`` / ingestion order -- the immutable batch sequence assigned when
  a batch is accepted, plus ``seq_in_batch`` for rows inside one batch.

Every revision either fully replaces one task (``upsert``) or deletes it.
Queries replay the state as it was known at a given ``known_seq`` and effective
at a given ``effective_at``; old answers stay reproducible because accepted
rows are never updated or deleted.
"""
DESCRIPTION = 'Bitemporal SQLite plan revision store with batch import and replay'
import argparse
import bisect
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

from .common import ident, number, read, require, rows, save
from .model import analyze

MAX_EFFECTIVE_AT = 10 ** 9
MAX_KNOWN_SEQ = 10 ** 9
MAX_REVISIONS = 20000
# Sorting sentinel for bisect: larger than any real (known_seq, seq_in_batch).
_INGESTION_END = (MAX_KNOWN_SEQ + 1, MAX_REVISIONS + 1)

SCHEMA = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id      TEXT PRIMARY KEY,
    known_seq     INTEGER NOT NULL UNIQUE,
    source_sha256 TEXT NOT NULL,
    source_blob   BLOB NOT NULL
);
CREATE TABLE IF NOT EXISTS revisions (
    known_seq    INTEGER NOT NULL,
    seq_in_batch INTEGER NOT NULL,
    task_id      TEXT NOT NULL,
    effective_at INTEGER NOT NULL,
    op           TEXT NOT NULL CHECK (op IN ('upsert', 'delete')),
    duration     INTEGER,
    predecessors TEXT NOT NULL,
    batch_id     TEXT NOT NULL,
    PRIMARY KEY (known_seq, seq_in_batch),
    FOREIGN KEY (known_seq) REFERENCES batches(known_seq)
);
CREATE INDEX IF NOT EXISTS revisions_lookup
    ON revisions (task_id, effective_at, known_seq, seq_in_batch);
"""


def parse_batch(data):
    """Strictly validate one batch document.

    Returns ``(batch_id, revisions)`` where each revision is a dict with keys
    ``task_id, effective_at, op, duration, predecessors``.  Duplicate
    (task_id, effective_at) rows inside one batch are illegal; the same
    effective time on different tasks is allowed and applies jointly.
    """
    require(data, 'batch_id revisions')
    batch_id = ident(data['batch_id'])
    parsed = []
    seen = set()
    for row in rows(data['revisions'], MAX_REVISIONS):
        if not isinstance(row, dict):
            raise ValueError('each revision must be an object')
        op = row.get('op')
        if op == 'delete':
            require(row, 'task_id effective_at op')
            duration = None
            predecessors = []
        elif op == 'upsert':
            require(row, 'task_id effective_at op duration predecessors')
            duration = number(row['duration'], 0, 10 ** 6)
            predecessors = [ident(p) for p in rows(row['predecessors'], 1000)]
            if len(set(predecessors)) != len(predecessors):
                raise ValueError('duplicate predecessor in revision')
        else:
            raise ValueError("op must be 'upsert' or 'delete'")
        task_id = ident(row['task_id'])
        effective_at = number(row['effective_at'], 0, MAX_EFFECTIVE_AT)
        key = (task_id, effective_at)
        if key in seen:
            raise ValueError('duplicate revision for task at effective_at in one batch')
        seen.add(key)
        parsed.append({'task_id': task_id, 'effective_at': effective_at,
                       'op': op, 'duration': duration, 'predecessors': predecessors})
    if not parsed:
        raise ValueError('batch revisions cannot be empty')
    return batch_id, parsed


def _connect(db_path, expect_store=False):
    path = Path(db_path)
    if path.is_symlink():
        raise ValueError('database path cannot be a symlink')
    if expect_store and not path.exists():
        raise ValueError('revision store does not exist; import a batch first')
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def _load_history(conn, known_seq_limit):
    """Load accepted revisions grouped by task, sorted by replay ordering."""
    by_task = {}
    cur = conn.execute(
        'SELECT known_seq, seq_in_batch, task_id, effective_at, op, duration, '
        'predecessors, batch_id FROM revisions WHERE known_seq <= ? '
        'ORDER BY task_id, effective_at, known_seq, seq_in_batch',
        (known_seq_limit,))
    for r in cur.fetchall():
        rev = (r['effective_at'], r['known_seq'], r['seq_in_batch'], r['op'],
               r['duration'], r['predecessors'], r['batch_id'])
        by_task.setdefault(r['task_id'], []).append(rev)
    return by_task


def _pick(by_task, task_id, effective_at):
    """Latest revision for a task at/before effective_at: max(effective_at, ingestion)."""
    revs = by_task.get(task_id)
    if not revs:
        return None
    idx = bisect.bisect_right(revs, (effective_at,) + _INGESTION_END,
                              key=lambda item: (item[0], item[1], item[2]))
    if idx == 0:
        return None
    return revs[idx - 1]


def _snapshot(by_task, effective_at):
    """Materialize the plan snapshot; deletions mean the task does not exist."""
    tasks = {}
    for task_id in by_task:
        rev = _pick(by_task, task_id, effective_at)
        if rev is None or rev[3] == 'delete':
            continue
        tasks[task_id] = {'id': task_id, 'duration': rev[4],
                          'predecessors': json.loads(rev[5])}
    return {'tasks': [tasks[t] for t in sorted(tasks)]}


def _validate_all_breakpoints(by_task, breakpoints):
    """Validate every effective breakpoint of the candidate history.

    Reuses the strict snapshot model: empty snapshots, dangling predecessors,
    cycles and out-of-bound values all raise ValueError, so the caller can roll
    the whole batch back without consuming a sequence number.
    """
    for effective_at in sorted(breakpoints):
        snapshot = _snapshot(by_task, effective_at)
        if not snapshot['tasks']:
            raise ValueError('empty plan snapshot at effective_at=%d' % effective_at)
        analyze(snapshot)


def import_batch(db_path, source_path):
    """Accept one revision batch atomically; returns the publication report."""
    data, digest = read(source_path)
    source = Path(source_path)
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('source bytes changed while reading')
    batch_id, revisions = parse_batch(data)
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = conn.execute(
            'SELECT known_seq, source_blob FROM batches WHERE batch_id = ?',
            (batch_id,)).fetchone()
        if existing is not None:
            # Idempotent retry: identical bytes return the original sequence;
            # different content with the same batch_id is a conflict.
            if existing['source_blob'] == raw:
                conn.execute('COMMIT')
                return {'schema_version': 1, 'batch_id': batch_id,
                        'known_seq': existing['known_seq'],
                        'source_sha256': digest, 'revisions': len(revisions),
                        'status': 'replayed'}
            raise ValueError('batch_id conflict: same batch_id with different source bytes')
        next_seq = conn.execute(
            'SELECT COALESCE(MAX(known_seq), 0) + 1 AS next_seq FROM batches'
        ).fetchone()['next_seq']
        by_task = _load_history(conn, next_seq - 1)
        breakpoints = set()
        min_effective = MAX_EFFECTIVE_AT
        for seq_in_batch, rev in enumerate(revisions, start=1):
            stored = (rev['effective_at'], next_seq, seq_in_batch, rev['op'],
                      rev['duration'], json.dumps(rev['predecessors']), batch_id)
            by_task.setdefault(rev['task_id'], []).append(stored)
            breakpoints.add(rev['effective_at'])
            min_effective = min(min_effective, rev['effective_at'])
        for revs in by_task.values():
            revs.sort(key=lambda item: (item[0], item[1], item[2]))
        breakpoints.update(r[0] for revs in by_task.values() for r in revs)
        # Earlier breakpoints cannot change: no candidate row is eligible there.
        breakpoints = {b for b in breakpoints if b >= min_effective}
        # Raises on any empty/dangling/cyclic breakpoint; rollback preserves
        # history and leaves the sequence counter untouched.
        _validate_all_breakpoints(by_task, breakpoints)
        conn.execute('INSERT INTO batches (batch_id, known_seq, source_sha256, source_blob)'
                     ' VALUES (?, ?, ?, ?)', (batch_id, next_seq, digest, raw))
        conn.executemany(
            'INSERT INTO revisions (known_seq, seq_in_batch, task_id, effective_at,'
            ' op, duration, predecessors, batch_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [(next_seq, i, r['task_id'], r['effective_at'], r['op'], r['duration'],
              json.dumps(r['predecessors']), batch_id)
             for i, r in enumerate(revisions, start=1)])
        conn.execute('COMMIT')
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()
    return {'schema_version': 1, 'batch_id': batch_id, 'known_seq': next_seq,
            'source_sha256': digest, 'revisions': len(revisions),
            'status': 'accepted'}


def read_snapshot(db_path, effective_at, known_seq):
    """Read-only replay for downstream tools.

    Opens the SQLite file with ``mode=ro`` so the revision store can never be
    created, migrated or rewritten.  Returns the materialized snapshot, each
    surviving task's revision source, and the coordinate actually used.
    """
    from urllib.parse import quote
    effective_at = number(effective_at, 0, MAX_EFFECTIVE_AT)
    known_seq = number(known_seq, 0, MAX_KNOWN_SEQ)
    path = Path(db_path)
    if path.is_symlink():
        raise ValueError('database path cannot be a symlink')
    if not path.exists():
        raise ValueError('revision store does not exist')
    uri = 'file:%s?mode=ro' % quote(str(path.resolve()), safe='/')
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {r['name'] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'batches', 'revisions'} <= tables:
            raise ValueError('revision store schema is missing')
        effective_known = conn.execute(
            'SELECT MAX(known_seq) AS s FROM batches WHERE known_seq <= ?',
            (known_seq,)).fetchone()['s']
        base = {'effective_at': effective_at,
                'requested_known_seq': known_seq,
                'known_seq': effective_known}
        if effective_known is None:
            return {**base, 'state': 'absent', 'snapshot': None,
                    'revision_sources': {}}
        by_task = _load_history(conn, effective_known)
        snapshot = _snapshot(by_task, effective_at)
        if not snapshot['tasks']:
            return {**base, 'state': 'absent', 'snapshot': None,
                    'revision_sources': {}}
        sources = {}
        for row in snapshot['tasks']:
            rev = _pick(by_task, row['id'], effective_at)
            sources[row['id']] = {
                'batch_id': rev[6], 'known_seq': rev[1],
                'seq_in_batch': rev[2], 'effective_at': rev[0], 'op': rev[3]}
        return {**base, 'state': 'active', 'snapshot': snapshot,
                'revision_sources': sources}
    finally:
        conn.close()


def query_store(db_path, effective_at, known_seq):
    """Replay the plan known by ``known_seq`` and effective at ``effective_at``."""
    effective_at = number(effective_at, 0, MAX_EFFECTIVE_AT)
    known_seq = number(known_seq, 0, MAX_KNOWN_SEQ)
    conn = _connect(db_path, expect_store=True)
    try:
        effective_known = conn.execute(
            'SELECT MAX(known_seq) AS s FROM batches WHERE known_seq <= ?',
            (known_seq,)).fetchone()['s']
        if effective_known is None:
            return {'schema_version': 1, 'effective_at': effective_at,
                    'requested_known_seq': known_seq, 'known_seq': None,
                    'state': 'absent', 'tasks': [], 'revision_sources': {},
                    'schedule': None}
        by_task = _load_history(conn, effective_known)
        snapshot = _snapshot(by_task, effective_at)
        if not snapshot['tasks']:
            return {'schema_version': 1, 'effective_at': effective_at,
                    'requested_known_seq': known_seq, 'known_seq': effective_known,
                    'state': 'absent', 'tasks': [], 'revision_sources': {},
                    'schedule': None}
        schedule = analyze(snapshot)
        tasks = []
        sources = {}
        for row in snapshot['tasks']:
            rev = _pick(by_task, row['id'], effective_at)
            source = {'batch_id': rev[6], 'known_seq': rev[1],
                      'seq_in_batch': rev[2], 'effective_at': rev[0], 'op': rev[3]}
            sources[row['id']] = source
            tasks.append({'task_id': row['id'], 'duration': row['duration'],
                          'predecessors': row['predecessors'], 'source': source})
        return {'schema_version': 1, 'effective_at': effective_at,
                'requested_known_seq': known_seq, 'known_seq': effective_known,
                'state': 'active', 'tasks': tasks,
                'revision_sources': sources, 'schedule': schedule}
    finally:
        conn.close()


def _distinct_paths(*paths):
    resolved = [Path(p).resolve() for p in paths if p is not None]
    if len(set(resolved)) != len(resolved):
        raise ValueError('database, source and output paths must differ')


def main():
    parser = argparse.ArgumentParser(description=DESCRIPTION)
    sub = parser.add_subparsers(dest='command', required=True)

    imp = sub.add_parser('import', help='validate and import one revision batch')
    imp.add_argument('--db', type=Path, required=True)
    imp.add_argument('--source', type=Path, required=True)
    imp.add_argument('--output', type=Path)

    qry = sub.add_parser('query', help='replay a bitemporal plan snapshot')
    qry.add_argument('--db', type=Path, required=True)
    qry.add_argument('--effective-at', type=int, required=True, dest='effective_at')
    qry.add_argument('--known-seq', type=int, required=True, dest='known_seq')
    qry.add_argument('--output', type=Path)

    args = parser.parse_args()
    try:
        if args.command == 'import':
            _distinct_paths(args.db, args.source, args.output)
            report = import_batch(args.db, args.source)
        else:
            _distinct_paths(args.db, args.output)
            report = query_store(args.db, args.effective_at, args.known_seq)
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
