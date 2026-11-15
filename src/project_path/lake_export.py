"""Export accepted plan revisions to a transferable data-lake event set.

``plan-history lake-export`` turns the accepted revisions of the bitemporal
SQLite store into a directory of newline-delimited JSON event files plus one
manifest.  It is a pure event dump: no snapshot materialization, no
critical-path analysis and no scheduling ever happen here.

Layout::

    <lake>/manifest.json                        # the single commit point
    <lake>/partitions/known_seq=0000000001.jsonl
    <lake>/partitions/known_seq=0000000002.jsonl

One partition per accepted batch (``known_seq``), rows ordered by
``seq_in_batch``.  Every JSONL row carries the batch id, both time axes
(``effective_at`` and ``known_seq``/``seq_in_batch``), the operation, the
full task value (or the deletion marker ``null``) and the batch
source-bytes SHA256.  The manifest records the upstream identity, the
contiguous watermark, row counts and per-partition SHA256 hashes.

Resume semantics: the source store is opened read-only; gaps in
``known_seq`` are never skipped; committed partitions are never
overwritten; re-running the same range is idempotent; new batches are
appended only.  New partitions are staged to a temporary file and
atomically landed; manifest and partitions are validated before
publication; the atomic manifest replace is the only commit point.  If a
crash happens after a partition landed but before the commit, the next
run verifies the orphan's hash and adopts it instead of rewriting it;
unknown files are never recorded or overwritten; a failed run never
advances the watermark.
"""
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from urllib.parse import quote

from .common import ident, number, read, require, rows, save
from .history import MAX_KNOWN_SEQ, MAX_REVISIONS

SCHEMA_VERSION = 1
KIND = 'plan-history-lake-export'
MANIFEST_NAME = 'manifest.json'
PARTITIONS_DIR = 'partitions'


def partition_name(known_seq):
    """Canonical file name of one partition inside ``partitions/``."""
    return 'known_seq=%010d.jsonl' % known_seq


def _sha256_hex(value):
    if (not isinstance(value, str) or len(value) != 64
            or any(c not in '0123456789abcdef' for c in value)):
        raise ValueError('expected lowercase sha256 hex digest')
    return value


def _hash_stream(stream):
    digest = hashlib.sha256()
    lines = 0
    while True:
        chunk = stream.read(1024 * 1024)
        if not chunk:
            return digest.hexdigest(), lines
        digest.update(chunk)
        lines += chunk.count(b'\n')


def _hash_file(path):
    with open(path, 'rb') as stream:
        return _hash_stream(stream)


def _history_sha256(entries):
    """Upstream identity: a hash over the chain of accepted batch identities."""
    digest = hashlib.sha256()
    for entry in entries:
        line = '%d:%s:%s\n' % (entry['known_seq'], entry['batch_id'],
                               entry['source_sha256'])
        digest.update(line.encode('utf-8'))
    return digest.hexdigest()


def _event_line(row, batch_id, source_sha256, expected_seq):
    """Serialize one revision row as a JSONL event, checking the store contract."""
    known_seq, seq_in_batch, task_id, effective_at, op, duration, value = row
    if seq_in_batch != expected_seq:
        raise ValueError('revision rows out of order at known_seq=%d' % known_seq)
    if not isinstance(task_id, str) or not task_id:
        raise ValueError('revision store row violates the task_id contract')
    if type(effective_at) is not int or effective_at < 0:
        raise ValueError('revision store row violates the effective_at contract')
    if op == 'upsert':
        if type(duration) is not int or not 0 <= duration <= 10 ** 6:
            raise ValueError('revision store row violates the duration contract')
        predecessors = json.loads(value)
        if (not isinstance(predecessors, list)
                or not all(isinstance(p, str) for p in predecessors)):
            raise ValueError('revision store row violates the predecessors contract')
        task = {'duration': duration, 'predecessors': predecessors}
    elif op == 'delete':
        task = None  # deletion marker: the task does not exist from here on
    else:
        raise ValueError('revision store row violates the op contract')
    event = {'batch_id': batch_id, 'known_seq': known_seq,
             'seq_in_batch': seq_in_batch, 'effective_at': effective_at,
             'op': op, 'task_id': task_id, 'task': task,
             'source_sha256': source_sha256}
    return json.dumps(event, sort_keys=True, ensure_ascii=False,
                      allow_nan=False, separators=(',', ':'))


def _stage_partition(conn, partitions_dir, known_seq, batch_id, source_sha256):
    """Stream one batch's events into a staged temp file next to its final path.

    Returns ``(temp_path, sha256, rows)``; the caller decides whether the
    staged content atomically lands, is replaced by an identical orphan
    adoption, or is removed.  Rows stream from the cursor to the file one
    at a time, so memory stays bounded no matter how large the store is.
    """
    temp = tempfile.NamedTemporaryFile(mode='wb', dir=partitions_dir,
                                       prefix='.stage-', delete=False)
    digest = hashlib.sha256()
    count = 0
    try:
        with temp:
            cursor = conn.execute(
                'SELECT known_seq, seq_in_batch, task_id, effective_at, op,'
                ' duration, predecessors FROM revisions'
                ' WHERE known_seq = ? ORDER BY seq_in_batch', (known_seq,))
            for row in cursor:
                count += 1
                line = _event_line(row, batch_id, source_sha256, count)
                data = (line + '\n').encode('utf-8')
                digest.update(data)
                temp.write(data)
    except BaseException:
        os.unlink(temp.name)
        raise
    if count == 0:
        os.unlink(temp.name)
        raise ValueError('accepted batch has no revisions at known_seq=%d'
                         % known_seq)
    return temp.name, digest.hexdigest(), count


def _validate_manifest(manifest):
    """Strict structural validation of a loaded or candidate manifest."""
    require(manifest, 'schema_version kind upstream watermark rows partitions')
    if manifest['schema_version'] != SCHEMA_VERSION:
        raise ValueError('unsupported manifest schema_version')
    if manifest['kind'] != KIND:
        raise ValueError('not a plan-history lake-export manifest')
    watermark = number(manifest['watermark'], 1, MAX_KNOWN_SEQ)
    require(manifest['upstream'], 'batches tip history_sha256')
    upstream = manifest['upstream']
    if number(upstream['batches'], 1, MAX_KNOWN_SEQ) != watermark:
        raise ValueError('manifest watermark and batch count disagree')
    _sha256_hex(upstream['history_sha256'])
    partitions = rows(manifest['partitions'], MAX_KNOWN_SEQ)
    if len(partitions) != watermark:
        raise ValueError('manifest partitions are not contiguous to the watermark')
    total = 0
    for position, entry in enumerate(partitions, start=1):
        require(entry, 'known_seq batch_id source_sha256 file rows sha256')
        if entry['known_seq'] != position:
            raise ValueError('manifest partitions are not contiguous to the watermark')
        ident(entry['batch_id'])
        _sha256_hex(entry['source_sha256'])
        _sha256_hex(entry['sha256'])
        if entry['file'] != PARTITIONS_DIR + '/' + partition_name(position):
            raise ValueError('manifest names a non-canonical partition file')
        total += number(entry['rows'], 1, MAX_REVISIONS)
    if number(manifest['rows'], 1, 10 ** 15) != total:
        raise ValueError('manifest row total disagrees with its partitions')
    require(upstream['tip'], 'known_seq batch_id source_sha256')
    tip, last = upstream['tip'], partitions[-1]
    if ([tip['known_seq'], tip['batch_id'], tip['source_sha256']]
            != [last['known_seq'], last['batch_id'], last['source_sha256']]):
        raise ValueError('manifest tip disagrees with its last partition')
    if upstream['history_sha256'] != _history_sha256(partitions):
        raise ValueError('manifest upstream identity hash mismatch')
    return manifest


def _build_manifest(entries):
    return {
        'schema_version': SCHEMA_VERSION,
        'kind': KIND,
        'upstream': {
            'batches': len(entries),
            'tip': {'known_seq': entries[-1]['known_seq'],
                    'batch_id': entries[-1]['batch_id'],
                    'source_sha256': entries[-1]['source_sha256']},
            'history_sha256': _history_sha256(entries),
        },
        'watermark': len(entries),
        'rows': sum(entry['rows'] for entry in entries),
        'partitions': [dict(entry) for entry in entries],
    }


def _verify_partitions(lake, manifest):
    """Pre-publication gate: every manifest partition must hash-match on disk."""
    for entry in manifest['partitions']:
        path = lake / entry['file']
        if path.is_symlink() or not path.is_file():
            raise ValueError('partition is missing: ' + entry['file'])
        digest, lines = _hash_file(path)
        if digest != entry['sha256'] or lines != entry['rows']:
            raise ValueError('partition verification failed: ' + entry['file'])


def _connect_readonly(db_path):
    """Open the revision store read-only; never create, migrate or rewrite it."""
    path = Path(db_path)
    if path.is_symlink():
        raise ValueError('database path cannot be a symlink')
    if not path.exists():
        raise ValueError('revision store does not exist')
    uri = 'file:%s?mode=ro' % quote(str(path.resolve()), safe='/')
    conn = sqlite3.connect(uri, uri=True)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'batches', 'revisions'} <= tables:
            raise ValueError('revision store schema is missing')
    except BaseException:
        conn.close()
        raise
    return conn


def export_lake(db_path, lake_dir, before_commit=None):
    """Export newly accepted batches into ``lake_dir``; returns the report.

    ``before_commit`` is an optional hook invoked with the candidate
    manifest right before the atomic manifest replace (the only commit
    point); tests use it to inject a crash between partition landing and
    the commit.  Any failure leaves the previously committed manifest and
    its watermark untouched.
    """
    lake = Path(lake_dir)
    if lake.is_symlink():
        raise ValueError('lake directory cannot be a symlink')
    if lake.exists() and not lake.is_dir():
        raise ValueError('lake path is not a directory')
    db = Path(db_path)
    conn = _connect_readonly(db)
    db_sha256 = _hash_file(db)[0]
    try:
        batches = conn.execute(
            'SELECT known_seq, batch_id, source_sha256 FROM batches'
            ' ORDER BY known_seq').fetchall()
        if not batches:
            raise ValueError('revision store has no accepted batches')
        for position, row in enumerate(batches, start=1):
            if row[0] != position:
                raise ValueError(
                    'revision store has a gap in known_seq at %d; missing'
                    ' sequences are never skipped' % position)
        stray = conn.execute(
            'SELECT MAX(known_seq) FROM revisions').fetchone()[0]
        if stray is not None and stray > len(batches):
            raise ValueError('revision rows without an accepted batch')
        manifest_path = lake / MANIFEST_NAME
        if manifest_path.is_symlink():
            raise ValueError('manifest cannot be a symlink')
        committed = []
        if manifest_path.exists():
            committed = _validate_manifest(read(manifest_path)[0])['partitions']
        if len(committed) > len(batches):
            raise ValueError('manifest watermark exceeds the upstream store')
        for entry, row in zip(committed, batches):
            if [entry['batch_id'], entry['source_sha256']] != [row[1], row[2]]:
                raise ValueError(
                    'upstream identity mismatch at known_seq=%d; the store'
                    ' was replaced' % entry['known_seq'])
        written, adopted, fresh = [], [], []
        if len(batches) > len(committed):
            partitions_dir = lake / PARTITIONS_DIR
            if partitions_dir.is_symlink():
                raise ValueError('partitions directory cannot be a symlink')
            partitions_dir.mkdir(parents=True, exist_ok=True)
            for known_seq, batch_id, source_sha256 in batches[len(committed):]:
                temp, digest, count = _stage_partition(
                    conn, partitions_dir, known_seq, batch_id, source_sha256)
                final = partitions_dir / partition_name(known_seq)
                try:
                    if final.is_symlink():
                        raise ValueError('partition path is a symlink: '
                                         + final.name)
                    if final.exists():
                        # Orphan from a crashed run: adopt by hash match,
                        # never rewrite; anything else is an unknown file.
                        if _hash_file(final)[0] != digest:
                            raise ValueError(
                                'unknown file occupies partition path %s;'
                                ' refusing to overwrite' % final.name)
                        adopted.append(known_seq)
                    else:
                        os.replace(temp, final)
                        written.append(known_seq)
                finally:
                    if os.path.exists(temp):
                        os.unlink(temp)
                fresh.append({'known_seq': known_seq, 'batch_id': batch_id,
                              'source_sha256': source_sha256,
                              'file': PARTITIONS_DIR + '/'
                                     + partition_name(known_seq),
                              'rows': count, 'sha256': digest})
        if _hash_file(db)[0] != db_sha256:
            raise ValueError('source database changed during export')
        candidate = _validate_manifest(_build_manifest(committed + fresh))
        _verify_partitions(lake, candidate)
        if fresh:
            if before_commit is not None:
                before_commit(candidate)
            save(manifest_path, candidate)  # the only commit point
            status = 'exported'
        else:
            status = 'up_to_date'
    finally:
        conn.close()
    return {
        'schema_version': SCHEMA_VERSION,
        'status': status,
        'previous_watermark': len(committed),
        'watermark': candidate['watermark'],
        'batches_total': len(batches),
        'partitions_written': written,
        'partitions_adopted': adopted,
        'rows_exported': sum(entry['rows'] for entry in fresh),
        'rows_total': candidate['rows'],
        'db_sha256': db_sha256,
        'history_sha256': candidate['upstream']['history_sha256'],
    }
