"""As-built resource settlement with a transactional SQLite ledger.

``plan-billing`` settles one invoice from two JSON sources: an *execution*
document (actual task intervals and per-resource demands) and a *pricing*
document (integer capacity, billing-period boundaries, progressive usage tiers
and non-negative integer cent unit prices).  Neither document carries a
schedule: this command never plans, it only verifies and bills what happened.

Binding and idempotency
-----------------------
An invoice id is bound to the exact bytes of both sources by SHA256.  The
invoice, its per-``(resource, period)`` groups, its detail lines and the
cumulative usage ledger are written in one SQLite transaction:

* same invoice id + identical bytes -> the original result is replayed and no
  usage is booked twice;
* same invoice id + different bytes -> conflict, nothing is written;
* any failure -> the whole transaction rolls back, leaving no partial invoice,
  no detail lines and no consumed usage.

Progressive tiers are shared per ``(resource, period)`` across invoices.  Each
settlement recomputes from the persisted ledger while holding a
``BEGIN IMMEDIATE`` write lock, so a stale reading of cumulative usage is never
used.  Invoices are accepted in lock-acquisition order; the outcome is
equivalent to some serial submission order of the submitted invoices.

All money is integer cents.  Fractions are used only for the exact
apportionment of a chunk's tier cost to active tasks, then snapped per
``(resource, period)`` with the largest-remainder method (ties: smaller task
id).  The snapped lines always sum to the exact integer group cost.
"""

import hashlib
import sqlite3
from pathlib import Path
from urllib.parse import quote

from .common import ident, read_bytes, read_source
from .resource import (MAX_SOURCE_BYTES, check_actual_capacity, parse_execution,
                       parse_pricing, settle_charges)

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS invoices (
    invoice_id       TEXT PRIMARY KEY,
    execution_sha256 TEXT NOT NULL,
    pricing_sha256   TEXT NOT NULL,
    execution_blob   BLOB NOT NULL,
    pricing_blob     BLOB NOT NULL,
    amount_cents     INTEGER NOT NULL CHECK (amount_cents >= 0),
    accepted_seq     INTEGER NOT NULL UNIQUE
);
CREATE TABLE IF NOT EXISTS invoice_groups (
    invoice_id TEXT NOT NULL,
    group_no   INTEGER NOT NULL,
    resource   TEXT NOT NULL,
    period_id  TEXT NOT NULL,
    quantity   INTEGER NOT NULL CHECK (quantity >= 0),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    cumulative_before INTEGER NOT NULL CHECK (cumulative_before >= 0),
    cumulative_after  INTEGER NOT NULL CHECK (cumulative_after >= 0),
    PRIMARY KEY (invoice_id, group_no),
    FOREIGN KEY (invoice_id) REFERENCES invoices(invoice_id)
);
CREATE TABLE IF NOT EXISTS invoice_lines (
    invoice_id TEXT NOT NULL,
    line_no    INTEGER NOT NULL,
    task_id    TEXT NOT NULL,
    resource   TEXT NOT NULL,
    period_id  TEXT NOT NULL,
    quantity   INTEGER NOT NULL CHECK (quantity >= 0),
    amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
    PRIMARY KEY (invoice_id, line_no),
    FOREIGN KEY (invoice_id) REFERENCES invoices(invoice_id)
);
CREATE TABLE IF NOT EXISTS usage_ledger (
    resource  TEXT NOT NULL,
    period_id TEXT NOT NULL,
    quantity  INTEGER NOT NULL CHECK (quantity >= 0),
    PRIMARY KEY (resource, period_id)
);
CREATE INDEX IF NOT EXISTS invoice_lines_invoice
    ON invoice_lines (invoice_id);
CREATE INDEX IF NOT EXISTS invoice_groups_invoice
    ON invoice_groups (invoice_id);
"""


def _connect(db_path, readonly=False):
    path = Path(db_path)
    if path.is_symlink():
        raise ValueError('ledger path cannot be a symlink')
    if readonly:
        if not path.exists():
            raise ValueError('billing ledger does not exist')
        uri = 'file:%s?mode=ro' % quote(str(path.resolve()), safe='/')
        conn = sqlite3.connect(uri, uri=True, isolation_level=None)
        conn.row_factory = sqlite3.Row
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        required = {'invoices', 'invoice_groups', 'invoice_lines', 'usage_ledger'}
        if not required <= tables:
            conn.close()
            raise ValueError('billing ledger schema is missing')
        return conn
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), isolation_level=None, timeout=60)
    conn.row_factory = sqlite3.Row
    # Structure is committed before any settlement transaction: a failed
    # invoice therefore leaves empty tables, never partial rows.
    conn.executescript(SCHEMA)
    return conn


def _sha256(raw):
    return hashlib.sha256(raw).hexdigest()


def _read_sources(execution_path, pricing_path):
    execution_data, execution_bytes, execution_sha = read_source(
        execution_path, MAX_SOURCE_BYTES)
    pricing_data, pricing_bytes, pricing_sha = read_source(
        pricing_path, MAX_SOURCE_BYTES)
    return ((execution_data, execution_bytes, execution_sha),
            (pricing_data, pricing_bytes, pricing_sha))


def _load_ledger(conn):
    """Cumulative booked usage per ``(resource, period)`` from committed rows."""
    return {(row['resource'], row['period_id']): row['quantity']
            for row in conn.execute(
                'SELECT resource, period_id, quantity FROM usage_ledger')}


def _load_invoice(conn, invoice_id, with_sources=False):
    row = conn.execute(
        'SELECT invoice_id, execution_sha256, pricing_sha256, amount_cents,'
        ' accepted_seq' + (', execution_blob, pricing_blob' if with_sources else '')
        + ' FROM invoices WHERE invoice_id = ?',
        (invoice_id,)).fetchone()
    if row is None:
        return None
    groups = [{'resource': r['resource'], 'period_id': r['period_id'],
               'quantity': r['quantity'], 'amount_cents': r['amount_cents'],
               'cumulative_quantity_before': r['cumulative_before'],
               'cumulative_quantity_after': r['cumulative_after']}
              for r in conn.execute(
                  'SELECT resource, period_id, quantity, amount_cents,'
                  ' cumulative_before, cumulative_after FROM invoice_groups'
                  ' WHERE invoice_id = ? ORDER BY group_no', (invoice_id,))]
    lines = [{'task_id': r['task_id'], 'resource': r['resource'],
              'period_id': r['period_id'], 'quantity': r['quantity'],
              'amount_cents': r['amount_cents']}
             for r in conn.execute(
                 'SELECT task_id, resource, period_id, quantity, amount_cents'
                 ' FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no',
                 (invoice_id,))]
    result = {'invoice_id': row['invoice_id'],
              'execution_sha256': row['execution_sha256'],
              'pricing_sha256': row['pricing_sha256'],
              'amount_cents': row['amount_cents'],
              'accepted_seq': row['accepted_seq'],
              'groups': groups, 'lines': lines}
    if with_sources:
        result['execution_blob'] = row['execution_blob']
        result['pricing_blob'] = row['pricing_blob']
    return result


def _derive(execution_bytes, pricing_bytes):
    """Parse both stored source blobs and re-verify capacity.

    A replay recomputes from the bound bytes: the persisted hashes must still
    describe a valid, capacity-feasible execution, and the reported segment
    count and peaks are derived rather than trusted.
    """
    execution_data = read_bytes(execution_bytes)
    pricing_data = read_bytes(pricing_bytes)
    classes, tasks, makespan = parse_execution(execution_data)
    capacity, periods, tiers = parse_pricing(pricing_data, classes)
    segments, peak = check_actual_capacity(tasks, makespan, classes, capacity)
    return segments, peak


def _report(invoice, status):
    if _sha256(invoice['execution_blob']) != invoice['execution_sha256']:
        raise ValueError('stored execution bytes do not match their hash')
    if _sha256(invoice['pricing_blob']) != invoice['pricing_sha256']:
        raise ValueError('stored pricing bytes do not match their hash')
    if (sum(group['amount_cents'] for group in invoice['groups'])
            != invoice['amount_cents']):
        raise ValueError('stored groups do not sum to the invoice amount')
    if (sum(line['amount_cents'] for line in invoice['lines'])
            != invoice['amount_cents']):
        raise ValueError('stored lines do not sum to the invoice amount')
    segments, peak = _derive(invoice['execution_blob'], invoice['pricing_blob'])
    return {
        'schema_version': SCHEMA_VERSION,
        'status': status,
        'invoice_id': invoice['invoice_id'],
        'execution_source_sha256': invoice['execution_sha256'],
        'pricing_source_sha256': invoice['pricing_sha256'],
        'amount_cents': invoice['amount_cents'],
        'accepted_seq': invoice['accepted_seq'],
        'groups': invoice['groups'],
        'lines': invoice['lines'],
        'segments': len(segments),
        'peak_usage': peak,
        'time_unit_enumeration': False,
    }


def settle(db_path, invoice_id, execution_path, pricing_path,
           before_commit=None):
    """Verify capacity and settle one invoice atomically; return the report.

    ``before_commit`` is an optional test hook invoked after every row is
    staged but before ``COMMIT``; raising from it proves that an interrupted
    settlement leaves no invoice, no detail rows and no booked usage.
    """
    invoice_id = ident(invoice_id)
    execution, pricing = _read_sources(execution_path, pricing_path)
    execution_data, execution_bytes, execution_sha = execution
    pricing_data, pricing_bytes, pricing_sha = pricing
    conn = _connect(db_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        existing = _load_invoice(conn, invoice_id, with_sources=True)
        if existing is not None:
            if (existing['execution_sha256'] != execution_sha
                    or existing['pricing_sha256'] != pricing_sha):
                raise ValueError(
                    'invoice_id conflict: same invoice id with different sources')
            conn.execute('COMMIT')
            return _report(existing, 'replayed')
        classes, tasks, makespan = parse_execution(execution_data)
        capacity, periods, tiers = parse_pricing(pricing_data, classes)
        segments, _ = check_actual_capacity(tasks, makespan, classes, capacity)
        ledger = _load_ledger(conn)
        result = settle_charges(classes, tasks, segments, periods, tiers,
                                prior_usage=ledger)
        accepted_seq = conn.execute(
            'SELECT COALESCE(MAX(accepted_seq), 0) + 1 AS next FROM invoices'
        ).fetchone()['next']
        conn.execute(
            'INSERT INTO invoices (invoice_id, execution_sha256, pricing_sha256,'
            ' execution_blob, pricing_blob, amount_cents, accepted_seq)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?)',
            (invoice_id, execution_sha, pricing_sha, execution_bytes,
             pricing_bytes, result['amount_cents'], accepted_seq))
        conn.executemany(
            'INSERT INTO invoice_groups (invoice_id, group_no, resource,'
            ' period_id, quantity, amount_cents, cumulative_before,'
            ' cumulative_after) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [(invoice_id, index, group['resource'], group['period_id'],
              group['quantity'], group['amount_cents'],
              group['cumulative_quantity_before'],
              group['cumulative_quantity_after'])
             for index, group in enumerate(result['groups'], start=1)])
        conn.executemany(
            'INSERT INTO invoice_lines (invoice_id, line_no, task_id, resource,'
            ' period_id, quantity, amount_cents)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?)',
            [(invoice_id, index, line['task_id'], line['resource'],
              line['period_id'], line['quantity'], line['amount_cents'])
             for index, line in enumerate(result['lines'], start=1)])
        for group in result['groups']:
            conn.execute(
                'INSERT INTO usage_ledger (resource, period_id, quantity)'
                ' VALUES (?, ?, ?) ON CONFLICT(resource, period_id)'
                ' DO UPDATE SET quantity = quantity + excluded.quantity',
                (group['resource'], group['period_id'], group['quantity']))
        if before_commit is not None:
            before_commit(invoice_id)
        conn.execute('COMMIT')
        return {
            'schema_version': SCHEMA_VERSION,
            'status': 'accepted',
            'invoice_id': invoice_id,
            'execution_source_sha256': execution_sha,
            'pricing_source_sha256': pricing_sha,
            'amount_cents': result['amount_cents'],
            'accepted_seq': accepted_seq,
            'groups': result['groups'],
            'lines': result['lines'],
            'segments': result['segments'],
            'peak_usage': result['peak_usage'],
            'time_unit_enumeration': False,
        }
    except BaseException:
        if conn.in_transaction:
            conn.execute('ROLLBACK')
        raise
    finally:
        conn.close()


def replay(db_path, invoice_id):
    """Return a previously accepted invoice without modifying anything."""
    invoice_id = ident(invoice_id)
    conn = _connect(db_path, readonly=True)
    try:
        invoice = _load_invoice(conn, invoice_id, with_sources=True)
        if invoice is None:
            raise ValueError('unknown invoice_id: ' + invoice_id)
        return _report(invoice, 'replayed')
    finally:
        conn.close()


def query(db_path, invoice_id=None):
    """Read the ledger, optionally with one invoice; never writes."""
    conn = _connect(db_path, readonly=True)
    try:
        if invoice_id is not None:
            invoice = _load_invoice(conn, ident(invoice_id))
            if invoice is None:
                raise ValueError('unknown invoice_id: ' + invoice_id)
        else:
            invoice = None
        ledger = [{'resource': key[0], 'period_id': key[1], 'quantity': value}
                  for key, value in sorted(_load_ledger(conn).items())]
        totals = conn.execute(
            'SELECT COUNT(*) AS n, COALESCE(SUM(amount_cents), 0) AS total'
            ' FROM invoices').fetchone()
        return {'schema_version': SCHEMA_VERSION,
                'invoice': invoice,
                'ledger': ledger,
                'invoices_total': totals['n'],
                'amount_cents_total': totals['total']}
    finally:
        conn.close()
