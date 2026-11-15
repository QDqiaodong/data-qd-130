"""Tests for plan-history lake-export: data-lake event set publishing.

Coverage:
* JSONL event content: batch id, both time axes, operation, full task
  value or deletion marker, and the batch source-bytes SHA256; partitions
  by known_seq, rows ordered by seq_in_batch; no snapshot, critical-path
  or scheduling computation;
* the manifest: upstream identity, contiguous watermark, row counts and
  partition hashes; the committed sample lake under fixtures/lake;
* resume semantics: read-only source, idempotent re-runs, append-only new
  batches, known_seq gaps are never skipped, close/reopen across
  processes;
* a crash between partition landing and the manifest commit: orphan
  partitions are hash-verified and adopted, never rewritten, never
  recorded twice; unknown files are neither recorded nor overwritten;
* failures never advance the watermark; committed partitions are never
  overwritten (corruption is reported, not repaired);
* a million-row streaming export with bounded memory;
* the source database file stays byte-identical through every scenario.
"""
import hashlib
import json
import os
import resource
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from project_path.history import SCHEMA, import_batch
from project_path.lake_export import (KIND, MANIFEST_NAME, PARTITIONS_DIR,
                                      export_lake, partition_name)

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'fixtures'


def batch(batch_id, revisions):
    return {'batch_id': batch_id, 'revisions': revisions}


def upsert(task_id, effective_at, duration, predecessors):
    return {'task_id': task_id, 'effective_at': effective_at, 'op': 'upsert',
            'duration': duration, 'predecessors': predecessors}


def delete(task_id, effective_at):
    return {'task_id': task_id, 'effective_at': effective_at, 'op': 'delete'}


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path):
    with open(path, 'rb') as stream:
        return [json.loads(line) for line in stream]


BASE = batch('b1', [
    upsert('A', 0, 3, []),
    upsert('B', 0, 2, ['A']),
    upsert('C', 0, 4, ['A']),
    upsert('D', 0, 1, ['B', 'C']),
])


class EventSet(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'store.db'
        self.lake = self.td / 'lake'

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, doc):
        path = self.td / name
        path.write_text(json.dumps(doc), encoding='utf-8')
        return path

    def import_doc(self, name, doc):
        return import_batch(self.db, self.write(name, doc))

    def import_fixtures(self, count=3):
        for source in sorted((FIXTURES / 'history').glob('batch-*.json'))[:count]:
            import_batch(self.db, source)

    def partition(self, known_seq):
        return self.lake / PARTITIONS_DIR / partition_name(known_seq)

    def manifest(self):
        return json.loads((self.lake / MANIFEST_NAME).read_text())

    def test_export_events_and_manifest(self):
        self.import_fixtures()
        before = sha256_file(self.db)
        report = export_lake(self.db, self.lake)
        self.assertEqual(report['status'], 'exported')
        self.assertEqual((report['previous_watermark'], report['watermark']),
                         (0, 3))
        self.assertEqual(report['partitions_written'], [1, 2, 3])
        self.assertEqual(report['partitions_adopted'], [])
        self.assertEqual((report['rows_exported'], report['rows_total']), (9, 9))
        self.assertEqual(report['db_sha256'], before)
        # The export is a pure event dump: no schedule/critical-path keys.
        self.assertNotIn('schedule', json.dumps(report))
        self.assertNotIn('critical', json.dumps(report))
        # The source store is byte-identical after the export.
        self.assertEqual(sha256_file(self.db), before)
        manifest = self.manifest()
        self.assertEqual(manifest['kind'], KIND)
        self.assertEqual(manifest['schema_version'], 1)
        self.assertEqual(manifest['watermark'], 3)
        self.assertEqual(manifest['rows'], 9)
        self.assertEqual(manifest['upstream']['batches'], 3)
        tip = manifest['upstream']['tip']
        self.assertEqual((tip['known_seq'], tip['batch_id']),
                         (3, 'batch-03-restore'))
        self.assertEqual(len(manifest['upstream']['history_sha256']), 64)
        self.assertEqual(report['history_sha256'],
                         manifest['upstream']['history_sha256'])
        entries = manifest['partitions']
        self.assertEqual([e['known_seq'] for e in entries], [1, 2, 3])
        self.assertEqual([e['rows'] for e in entries], [5, 3, 1])
        self.assertEqual([e['batch_id'] for e in entries],
                         ['batch-01-baseline', 'batch-02-backfill',
                          'batch-03-restore'])
        fixtures = sorted((FIXTURES / 'history').glob('batch-*.json'))
        for entry, source in zip(entries, fixtures):
            # Upstream identity: each partition records the batch source
            # document hash; the file hash matches the bytes on disk.
            self.assertEqual(entry['source_sha256'], sha256_file(source))
            self.assertEqual(entry['file'],
                             PARTITIONS_DIR + '/' + partition_name(entry['known_seq']))
            self.assertEqual(sha256_file(self.lake / entry['file']),
                             entry['sha256'])
        events = read_jsonl(self.partition(1))
        self.assertEqual(len(events), 5)
        first = events[0]
        # Every row carries the batch, both time axes, the operation, the
        # full task value (or deletion marker) and the source SHA256.
        self.assertEqual(set(first), {'batch_id', 'known_seq', 'seq_in_batch',
                                      'effective_at', 'op', 'task_id', 'task',
                                      'source_sha256'})
        self.assertEqual(first['batch_id'], 'batch-01-baseline')
        self.assertEqual((first['known_seq'], first['seq_in_batch']), (1, 1))
        self.assertEqual(first['effective_at'], 0)
        self.assertEqual(first['op'], 'upsert')
        self.assertEqual(first['task_id'], 'A')
        self.assertEqual(first['task'], {'duration': 3, 'predecessors': []})
        self.assertEqual(first['source_sha256'], entries[0]['source_sha256'])
        # Rows are ordered by seq_in_batch inside the known_seq partition.
        self.assertEqual([e['seq_in_batch'] for e in events], [1, 2, 3, 4, 5])
        self.assertTrue(all(e['known_seq'] == 1 for e in events))
        # Deletion marker: batch-02 deletes E at effective_at 4.
        backfill = read_jsonl(self.partition(2))
        gone = next(e for e in backfill if e['op'] == 'delete')
        self.assertEqual(gone['task_id'], 'E')
        self.assertIsNone(gone['task'])
        self.assertEqual(gone['effective_at'], 4)
        restored = read_jsonl(self.partition(3))
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]['task'], {'duration': 2, 'predecessors': []})
        for event in events + backfill + restored:
            self.assertEqual(set(event), set(first))

    def test_sample_lake_fixture_matches_byte_for_byte(self):
        self.import_fixtures()
        export_lake(self.db, self.lake)
        sample = FIXTURES / 'lake'
        self.assertEqual((self.lake / MANIFEST_NAME).read_bytes(),
                         (sample / MANIFEST_NAME).read_bytes())
        for known_seq in (1, 2, 3):
            name = partition_name(known_seq)
            self.assertEqual((self.lake / PARTITIONS_DIR / name).read_bytes(),
                             (sample / PARTITIONS_DIR / name).read_bytes())

    def test_same_effective_at_cross_batch_correction_keeps_both_events(self):
        self.import_doc('b1.json', BASE)
        self.import_doc('fix.json', batch('fix', [upsert('A', 2, 5, [])]))
        self.import_doc('fix2.json', batch('fix2', [upsert('A', 2, 9, [])]))
        export_lake(self.db, self.lake)
        # Both corrections of A at effective_at=2 are exported as events in
        # their own partitions; the exporter never resolves them into a
        # snapshot.
        first = [e for e in read_jsonl(self.partition(2)) if e['task_id'] == 'A']
        second = [e for e in read_jsonl(self.partition(3)) if e['task_id'] == 'A']
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertEqual(first[0]['effective_at'], 2)
        self.assertEqual(second[0]['effective_at'], 2)
        self.assertEqual(first[0]['task']['duration'], 5)
        self.assertEqual(second[0]['task']['duration'], 9)
        self.assertEqual((first[0]['known_seq'], second[0]['known_seq']), (2, 3))

    def test_rerun_same_range_is_idempotent(self):
        self.import_fixtures()
        export_lake(self.db, self.lake)
        manifest_bytes = (self.lake / MANIFEST_NAME).read_bytes()
        inodes = {k: self.partition(k).stat().st_ino for k in (1, 2, 3)}
        report = export_lake(self.db, self.lake)
        self.assertEqual(report['status'], 'up_to_date')
        self.assertEqual(report['partitions_written'], [])
        self.assertEqual(report['partitions_adopted'], [])
        self.assertEqual(report['rows_exported'], 0)
        self.assertEqual(report['watermark'], 3)
        # Committed bytes and files are untouched; no stray staging files.
        self.assertEqual((self.lake / MANIFEST_NAME).read_bytes(), manifest_bytes)
        self.assertEqual({k: self.partition(k).stat().st_ino for k in (1, 2, 3)},
                         inodes)
        self.assertEqual(
            sorted(p.name for p in (self.lake / PARTITIONS_DIR).iterdir()),
            [partition_name(k) for k in (1, 2, 3)])

    def test_resume_appends_only_new_batches(self):
        self.import_fixtures(2)
        export_lake(self.db, self.lake)
        inodes = {k: self.partition(k).stat().st_ino for k in (1, 2)}
        content_1 = self.partition(1).read_bytes()
        import_batch(self.db, FIXTURES / 'history' / 'batch-03-restore.json')
        before = sha256_file(self.db)
        report = export_lake(self.db, self.lake)
        self.assertEqual(report['status'], 'exported')
        self.assertEqual(report['previous_watermark'], 2)
        self.assertEqual(report['partitions_written'], [3])
        self.assertEqual(report['watermark'], 3)
        self.assertEqual((report['rows_exported'], report['rows_total']), (1, 9))
        # Committed partitions are never rewritten; new batches append only.
        self.assertEqual({k: self.partition(k).stat().st_ino for k in (1, 2)},
                         inodes)
        self.assertEqual(self.partition(1).read_bytes(), content_1)
        self.assertEqual(sha256_file(self.db), before)
        self.assertEqual(self.manifest()['watermark'], 3)

    def test_known_seq_gap_is_never_skipped(self):
        self.import_fixtures()
        conn = sqlite3.connect(str(self.db))
        conn.execute('DELETE FROM batches WHERE known_seq = 2')
        conn.commit()
        conn.close()
        before = sha256_file(self.db)
        with self.assertRaises(ValueError) as ctx:
            export_lake(self.db, self.lake)
        self.assertIn('gap', str(ctx.exception))
        # Nothing was staged or committed; the source is untouched.
        self.assertFalse(self.lake.exists())
        self.assertEqual(sha256_file(self.db), before)

    def test_orphan_partitions_are_adopted_after_crash_before_commit(self):
        self.import_fixtures()

        def crash(_candidate):
            raise RuntimeError('simulated crash before manifest commit')

        with self.assertRaises(RuntimeError):
            export_lake(self.db, self.lake, before_commit=crash)
        # Partitions landed but the manifest (the only commit point) did not.
        self.assertFalse((self.lake / MANIFEST_NAME).exists())
        orphans = {k: (self.partition(k).stat().st_ino,
                       self.partition(k).read_bytes()) for k in (1, 2, 3)}
        before = sha256_file(self.db)
        report = export_lake(self.db, self.lake)
        self.assertEqual(report['status'], 'exported')
        self.assertEqual(report['partitions_written'], [])
        self.assertEqual(report['partitions_adopted'], [1, 2, 3])
        self.assertEqual(report['rows_exported'], 9)
        # Orphans were hash-verified and adopted in place: never rewritten,
        # never recorded twice.
        for known_seq, (inode, content) in orphans.items():
            self.assertEqual(self.partition(known_seq).stat().st_ino, inode)
            self.assertEqual(self.partition(known_seq).read_bytes(), content)
        manifest = self.manifest()
        self.assertEqual(manifest['watermark'], 3)
        self.assertEqual(manifest['rows'], 9)
        self.assertEqual(sum(e['rows'] for e in manifest['partitions']), 9)
        self.assertEqual(sha256_file(self.db), before)
        # A further run is a no-op.
        self.assertEqual(export_lake(self.db, self.lake)['status'], 'up_to_date')

    def test_partial_orphan_state_adopts_and_appends(self):
        self.import_fixtures()

        def crash(_candidate):
            raise RuntimeError('simulated crash before manifest commit')

        with self.assertRaises(RuntimeError):
            export_lake(self.db, self.lake, before_commit=crash)
        # The crash left orphans 1..3; lose the last one before the retry.
        self.partition(3).unlink()
        inodes = {k: self.partition(k).stat().st_ino for k in (1, 2)}
        report = export_lake(self.db, self.lake)
        self.assertEqual(report['partitions_adopted'], [1, 2])
        self.assertEqual(report['partitions_written'], [3])
        self.assertEqual({k: self.partition(k).stat().st_ino for k in (1, 2)},
                         inodes)
        self.assertEqual(self.manifest()['watermark'], 3)

    def test_unknown_file_at_needed_path_is_never_overwritten(self):
        self.import_fixtures(2)
        junk = self.lake / PARTITIONS_DIR / partition_name(2)
        junk.parent.mkdir(parents=True)
        junk.write_bytes(b'{"foreign": true}\n')
        before = sha256_file(self.db)
        with self.assertRaises(ValueError) as ctx:
            export_lake(self.db, self.lake)
        self.assertIn('unknown file', str(ctx.exception))
        # The unknown file is untouched and the watermark did not advance.
        self.assertEqual(junk.read_bytes(), b'{"foreign": true}\n')
        self.assertFalse((self.lake / MANIFEST_NAME).exists())
        self.assertEqual(sha256_file(self.db), before)
        # Partition 1 was already landed as an orphan; removing the intruder
        # lets the retry adopt it and commit.
        junk.unlink()
        orphan_1 = self.partition(1).stat().st_ino
        report = export_lake(self.db, self.lake)
        self.assertEqual(report['partitions_adopted'], [1])
        self.assertEqual(report['partitions_written'], [2])
        self.assertEqual(self.partition(1).stat().st_ino, orphan_1)
        self.assertEqual(self.manifest()['watermark'], 2)

    def test_unknown_files_elsewhere_are_ignored_and_untouched(self):
        self.import_fixtures(2)
        export_lake(self.db, self.lake)
        stray = self.lake / PARTITIONS_DIR / partition_name(9)
        stray.write_bytes(b'junk\n')
        note = self.lake / PARTITIONS_DIR / 'notes.txt'
        note.write_text('operator notes')
        report = export_lake(self.db, self.lake)
        self.assertEqual(report['status'], 'up_to_date')
        self.assertEqual(stray.read_bytes(), b'junk\n')
        self.assertEqual(note.read_text(), 'operator notes')
        files = str([e['file'] for e in self.manifest()['partitions']])
        self.assertNotIn('notes.txt', files)
        self.assertNotIn(partition_name(9), files)

    def test_committed_partition_corruption_fails_without_repair(self):
        self.import_fixtures(2)
        export_lake(self.db, self.lake)
        manifest_bytes = (self.lake / MANIFEST_NAME).read_bytes()
        committed = self.partition(1)
        original = committed.read_bytes()
        with committed.open('ab') as stream:
            stream.write(b' ')
        corrupted = committed.read_bytes()
        # Even with nothing new to export, verification refuses to pass.
        with self.assertRaises(ValueError):
            export_lake(self.db, self.lake)
        # A new batch cannot be committed on top of a corrupt lake either.
        import_batch(self.db, FIXTURES / 'history' / 'batch-03-restore.json')
        with self.assertRaises(ValueError):
            export_lake(self.db, self.lake)
        # The committed partition was never rewritten; the watermark held.
        self.assertEqual(committed.read_bytes(), corrupted)
        self.assertEqual((self.lake / MANIFEST_NAME).read_bytes(), manifest_bytes)
        # Partition 3 landed as an orphan before verification failed; after
        # the operator restores the corrupted file, the retry adopts it.
        orphan_3 = self.partition(3).stat().st_ino
        committed.write_bytes(original)
        report = export_lake(self.db, self.lake)
        self.assertEqual(report['partitions_adopted'], [3])
        self.assertEqual(self.partition(3).stat().st_ino, orphan_3)
        self.assertEqual(report['watermark'], 3)

    def test_tampered_manifest_is_rejected(self):
        self.import_fixtures(2)
        export_lake(self.db, self.lake)
        path = self.lake / MANIFEST_NAME
        manifest_bytes = path.read_bytes()
        tampered = json.loads(manifest_bytes)
        tampered['rows'] = 999
        path.write_text(json.dumps(tampered))
        with self.assertRaises(ValueError):
            export_lake(self.db, self.lake)
        path.write_bytes(manifest_bytes)
        self.assertEqual(export_lake(self.db, self.lake)['status'], 'up_to_date')

    def test_replaced_upstream_store_is_rejected(self):
        self.import_fixtures(2)
        export_lake(self.db, self.lake)
        manifest_bytes = (self.lake / MANIFEST_NAME).read_bytes()
        # Replace the store with a different history reusing known_seq 1..2.
        self.db.unlink()
        self.import_doc('x.json', batch('x1', [upsert('Z', 0, 1, [])]))
        self.import_doc('y.json', batch('y2', [upsert('Y', 0, 1, ['Z'])]))
        with self.assertRaises(ValueError) as ctx:
            export_lake(self.db, self.lake)
        self.assertIn('upstream identity', str(ctx.exception))
        self.assertEqual((self.lake / MANIFEST_NAME).read_bytes(), manifest_bytes)

    def test_missing_store_is_not_created_and_lake_untouched(self):
        missing = self.td / 'nope.db'
        with self.assertRaises(ValueError):
            export_lake(missing, self.lake)
        self.assertFalse(missing.exists())
        self.assertFalse(self.lake.exists())

    def test_empty_store_is_rejected(self):
        # A failed first import leaves the schema behind with no batches.
        with self.assertRaises(ValueError):
            self.import_doc('cyc.json', batch('cyc', [upsert('A', 0, 1, ['B']),
                                                      upsert('B', 0, 1, ['A'])]))
        self.assertTrue(self.db.exists())
        with self.assertRaises(ValueError):
            export_lake(self.db, self.lake)
        self.assertFalse(self.lake.exists())

    def test_export_succeeds_on_filesystem_readonly_store(self):
        self.import_fixtures()
        before = sha256_file(self.db)
        os.chmod(self.db, 0o444)
        try:
            report = export_lake(self.db, self.lake)
        finally:
            os.chmod(self.db, 0o644)
        self.assertEqual(report['status'], 'exported')
        self.assertEqual(sha256_file(self.db), before)


class MillionRowStreaming(unittest.TestCase):
    """A million revisions export as a stream, not as an in-memory list."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'store.db'
        self.lake = self.td / 'lake'

    def tearDown(self):
        self.temp.cleanup()

    def build_store(self, batches, rows_per_batch, start=1):
        conn = sqlite3.connect(str(self.db))
        conn.executescript(SCHEMA)
        try:
            conn.execute('BEGIN')
            for seq in range(start, start + batches):
                batch_id = 'bulk-%06d' % seq
                digest = hashlib.sha256(batch_id.encode()).hexdigest()
                conn.execute(
                    'INSERT INTO batches (batch_id, known_seq, source_sha256,'
                    ' source_blob) VALUES (?, ?, ?, ?)',
                    (batch_id, seq, digest, batch_id.encode()))
                conn.executemany(
                    'INSERT INTO revisions (known_seq, seq_in_batch, task_id,'
                    ' effective_at, op, duration, predecessors, batch_id)'
                    ' VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                    ((seq, i, 'T%08d' % (seq * 1000000 + i),
                      (seq * 7 + i) % 1000,
                      'delete' if i % 997 == 0 else 'upsert',
                      None if i % 997 == 0 else i % 10,
                      '[]' if i % 997 == 0
                      else '["T%08d"]' % (seq * 1000000 + i - 1),
                      batch_id)
                     for i in range(1, rows_per_batch + 1)))
            conn.execute('COMMIT')
        finally:
            conn.close()

    def partition(self, known_seq):
        return self.lake / PARTITIONS_DIR / partition_name(known_seq)

    def test_million_row_streaming_export_and_resume(self):
        self.build_store(50, 20000)  # exactly one million revisions
        before = sha256_file(self.db)
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        report = export_lake(self.db, self.lake)
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Streaming proof: ru_maxrss is KiB on Linux; materializing a
        # million event dicts would need hundreds of MiB, not <128 MiB.
        self.assertLess(rss_after - rss_before, 128 * 1024)
        self.assertEqual(report['status'], 'exported')
        self.assertEqual(report['rows_total'], 1000000)
        self.assertEqual(report['rows_exported'], 1000000)
        self.assertEqual(report['watermark'], 50)
        self.assertEqual(sha256_file(self.db), before)
        manifest = json.loads((self.lake / MANIFEST_NAME).read_text())
        self.assertEqual(len(manifest['partitions']), 50)
        self.assertEqual(manifest['rows'], 1000000)
        self.assertEqual({e['rows'] for e in manifest['partitions']}, {20000})
        self.assertEqual([e['known_seq'] for e in manifest['partitions']],
                         list(range(1, 51)))
        # Spot-check first and last events plus per-partition line counts.
        with open(self.partition(1), 'rb') as stream:
            first = json.loads(stream.readline())
        self.assertEqual((first['known_seq'], first['seq_in_batch']), (1, 1))
        self.assertEqual(first['batch_id'], 'bulk-000001')
        with open(self.partition(50), 'rb') as stream:
            lines = stream.readlines()
        self.assertEqual(len(lines), 20000)
        last = json.loads(lines[-1])
        self.assertEqual((last['known_seq'], last['seq_in_batch']), (50, 20000))
        self.assertEqual({json.loads(line)['op'] for line in lines[:2000]},
                         {'upsert', 'delete'})
        # Resume: one more batch is appended; committed partitions untouched.
        self.build_store(1, 500, start=51)
        before_resume = sha256_file(self.db)
        inode_1 = self.partition(1).stat().st_ino
        follow = export_lake(self.db, self.lake)
        self.assertEqual(follow['status'], 'exported')
        self.assertEqual(follow['partitions_written'], [51])
        self.assertEqual(follow['watermark'], 51)
        self.assertEqual(follow['rows_total'], 1000500)
        self.assertEqual(self.partition(1).stat().st_ino, inode_1)
        self.assertEqual(sha256_file(self.db), before_resume)
        self.assertEqual(export_lake(self.db, self.lake)['status'], 'up_to_date')


class Command(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'store.db'
        self.lake = self.td / 'lake'

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args):
        return subprocess.run([sys.executable, '-m', 'project_path.history',
                               *args], capture_output=True, text=True)

    def import_fixture(self, name):
        return self.call('import', '--db', str(self.db),
                         '--source', str(FIXTURES / 'history' / name))

    def test_cli_export_resume_across_processes(self):
        self.assertEqual(self.import_fixture('batch-01-baseline.json').returncode, 0)
        self.assertEqual(self.import_fixture('batch-02-backfill.json').returncode, 0)
        out = self.td / 'export.json'
        r = self.call('lake-export', '--db', str(self.db),
                      '--lake', str(self.lake), '--output', str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(report['status'], 'exported')
        self.assertEqual(report['watermark'], 2)
        self.assertEqual(json.loads(out.read_text()), report)
        self.assertNotIn('schedule', r.stdout)
        self.assertNotIn('critical', r.stdout)
        # Close/reopen: a brand-new process resumes and appends batch 3 only.
        self.assertEqual(self.import_fixture('batch-03-restore.json').returncode, 0)
        before = sha256_file(self.db)
        r = self.call('lake-export', '--db', str(self.db), '--lake', str(self.lake))
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(report['status'], 'exported')
        self.assertEqual(report['partitions_written'], [3])
        self.assertEqual(report['watermark'], 3)
        self.assertEqual(sha256_file(self.db), before)
        # A third process finds nothing to do.
        r = self.call('lake-export', '--db', str(self.db), '--lake', str(self.lake))
        self.assertEqual(json.loads(r.stdout)['status'], 'up_to_date')
        # The event set is complete and query still works on the same store.
        events = []
        for known_seq in (1, 2, 3):
            events.extend(read_jsonl(self.lake / PARTITIONS_DIR
                                     / partition_name(known_seq)))
        self.assertEqual(len(events), 9)
        r = self.call('query', '--db', str(self.db),
                      '--effective-at', '6', '--known-seq', '3')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['state'], 'active')

    def test_cli_failures_keep_exit_code_two_and_old_outputs(self):
        r = self.call('lake-export', '--db', str(self.td / 'nope.db'),
                      '--lake', str(self.lake))
        self.assertEqual(r.returncode, 2)
        self.assertFalse((self.td / 'nope.db').exists())
        self.assertFalse(self.lake.exists())
        # Same path for db and lake is rejected.
        self.assertEqual(self.import_fixture('batch-01-baseline.json').returncode, 0)
        r = self.call('lake-export', '--db', str(self.db), '--lake', str(self.db))
        self.assertEqual(r.returncode, 2)
        # The report output must not live inside the managed lake directory.
        r = self.call('lake-export', '--db', str(self.db), '--lake', str(self.lake),
                      '--output', str(self.lake / MANIFEST_NAME))
        self.assertEqual(r.returncode, 2)
        # Unknown file at a needed partition path: exit 2, nothing
        # overwritten, old --output preserved, watermark not advanced.
        junk = self.lake / PARTITIONS_DIR / partition_name(1)
        junk.parent.mkdir(parents=True)
        junk.write_bytes(b'junk\n')
        out = self.td / 'export.json'
        out.write_text('{"old": true}')
        r = self.call('lake-export', '--db', str(self.db), '--lake', str(self.lake),
                      '--output', str(out))
        self.assertEqual(r.returncode, 2)
        self.assertIn('unknown file', r.stderr)
        self.assertEqual(junk.read_bytes(), b'junk\n')
        self.assertEqual(out.read_text(), '{"old": true}')
        self.assertFalse((self.lake / MANIFEST_NAME).exists())

    def test_lake_symlink_is_rejected(self):
        self.assertEqual(self.import_fixture('batch-01-baseline.json').returncode, 0)
        target = self.td / 'real-lake'
        link = self.td / 'link-lake'
        os.symlink(target, link)
        r = self.call('lake-export', '--db', str(self.db), '--lake', str(link))
        self.assertEqual(r.returncode, 2)
        self.assertFalse(target.exists())


if __name__ == '__main__':
    unittest.main()
