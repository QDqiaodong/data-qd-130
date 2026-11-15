"""Tests for the bitemporal SQLite plan revision store and its CLI."""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from project_path.history import import_batch, parse_batch, query_store

ROOT = Path(__file__).resolve().parents[1]


def batch(batch_id, revisions):
    return {'batch_id': batch_id, 'revisions': revisions}


def upsert(task_id, effective_at, duration, predecessors):
    return {'task_id': task_id, 'effective_at': effective_at, 'op': 'upsert',
            'duration': duration, 'predecessors': predecessors}


def delete(task_id, effective_at):
    return {'task_id': task_id, 'effective_at': effective_at, 'op': 'delete'}


BASE = batch('b1', [
    upsert('A', 0, 3, []),
    upsert('B', 0, 2, ['A']),
    upsert('C', 0, 4, ['A']),
    upsert('D', 0, 1, ['B', 'C']),
])


class StoreContract(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'store.db'

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, doc):
        path = self.td / name
        path.write_text(json.dumps(doc), encoding='utf-8')
        return path

    def import_doc(self, name, doc):
        return import_batch(self.db, self.write(name, doc))

    def test_parse_rejects_in_batch_duplicate(self):
        doc = batch('x', [upsert('A', 0, 1, []), delete('A', 0)])
        with self.assertRaises(ValueError):
            parse_batch(doc)

    def test_parse_rejects_negative_effective_at(self):
        with self.assertRaises(ValueError):
            parse_batch(batch('x', [upsert('A', -1, 1, [])]))

    def test_parse_rejects_unknown_fields_and_empty_batch(self):
        doc = batch('x', [upsert('A', 0, 1, [])])
        doc['extra'] = 1
        with self.assertRaises(ValueError):
            parse_batch(doc)
        with self.assertRaises(ValueError):
            parse_batch(batch('x', []))

    def test_accept_baseline_and_replay_with_schedule_evidence(self):
        report = self.import_doc('b1.json', BASE)
        self.assertEqual((report['status'], report['known_seq']), ('accepted', 1))
        view = query_store(self.db, 0, 1)
        self.assertEqual(view['state'], 'active')
        self.assertEqual(view['known_seq'], 1)
        self.assertEqual(view['schedule']['project_duration'], 8)
        self.assertEqual(view['schedule']['one_critical_path'], ['A', 'C', 'D'])
        source = view['revision_sources']['D']
        self.assertEqual((source['batch_id'], source['op'], source['effective_at']),
                         ('b1', 'upsert', 0))
        b = next(t for t in view['tasks'] if t['task_id'] == 'B')
        self.assertEqual(b['source']['batch_id'], 'b1')
        self.assertEqual(b['duration'], 2)

    def test_backfill_cycle_in_middle_period_is_rejected_and_no_seq_consumed(self):
        self.import_doc('b1.json', BASE)
        # At t=5 add edge B->C jointly with an unrelated valid revision; the new
        # edge creates the cycle B->C->B only inside the middle interval
        # [5, ...), while the t=0 baseline breakpoint stays valid.
        cyclic = batch('b2', [upsert('C', 5, 4, ['A', 'B']),
                              upsert('B', 5, 2, ['C'])])
        with self.assertRaises(ValueError):
            import_batch(self.db, self.write('b2.json', cyclic))
        # Sequence counter untouched: the next accepted batch still gets seq 2.
        follow = self.import_doc('b3.json',
                                 batch('b3', [upsert('A', 2, 2, [])]))
        self.assertEqual(follow['known_seq'], 2)
        view = query_store(self.db, 5, 2)
        self.assertEqual(view['known_seq'], 2)
        self.assertEqual(view['revision_sources']['C']['batch_id'], 'b1')
        # The old t=0 answer remains reproducible after the failed batch.
        old = query_store(self.db, 0, 1)
        self.assertEqual(old['schedule']['project_duration'], 8)

    def test_same_effective_time_joint_edge_edit_applies_together(self):
        self.import_doc('b1.json', BASE)
        # At t=4: shorten A and repoint D to B only; joint application keeps the
        # graph consistent at the new breakpoint (no mixed partial state).
        joint = batch('joint', [upsert('A', 4, 1, []),
                                upsert('D', 4, 1, ['B'])])
        report = self.import_doc('joint.json', joint)
        self.assertEqual(report['known_seq'], 2)
        before = query_store(self.db, 3, 2)
        after = query_store(self.db, 4, 2)
        self.assertEqual(before['revision_sources']['A']['batch_id'], 'b1')
        self.assertEqual(after['revision_sources']['A']['batch_id'], 'joint')
        self.assertEqual(after['revision_sources']['D']['batch_id'], 'joint')
        # At t=4: A(1) -> B(2) -> D(1) = 4; C stays duration 4 but is now
        # disconnected from D, so its branch A->C ends at 5 and the project
        # endpoint is max(4, 5) = 5.
        self.assertEqual(after['schedule']['project_duration'], 5)

    def test_dangling_predecessor_rolls_whole_batch_back(self):
        self.import_doc('b1.json', BASE)
        bad = batch('dangle', [upsert('A', 1, 3, ['GHOST'])])
        with self.assertRaises(ValueError):
            import_batch(self.db, self.write('dangle.json', bad))
        self.assertEqual(query_store(self.db, 9, 9)['revision_sources']['A']['batch_id'], 'b1')

    def test_delete_then_restore(self):
        self.import_doc('b1.json', BASE)
        # Deleting every task at t=3 leaves an empty snapshot -> rejected.
        empty = batch('wipe', [delete(t, 3) for t in ('A', 'B', 'C', 'D')])
        with self.assertRaises(ValueError):
            import_batch(self.db, self.write('wipe.json', empty))
        # Deleting one task whose child would dangle is also rejected.
        with self.assertRaises(ValueError):
            import_batch(self.db, self.write('wipe2.json',
                                             batch('wipe2', [delete('A', 3)])))
        # A consistent deletion: remove leaf D, then restore it later.
        self.import_doc('del.json', batch('del', [delete('D', 3)]))
        gone = query_store(self.db, 3, 2)
        self.assertEqual(gone['state'], 'active')
        self.assertNotIn('D', gone['revision_sources'])
        self.assertEqual(gone['schedule']['project_duration'], 7)
        self.import_doc('restore.json',
                        batch('restore', [upsert('D', 6, 1, ['B', 'C'])]))
        still_gone = query_store(self.db, 5, 3)
        self.assertNotIn('D', still_gone['revision_sources'])
        restored = query_store(self.db, 6, 3)
        self.assertEqual(restored['revision_sources']['D']['batch_id'], 'restore')
        self.assertEqual(restored['schedule']['project_duration'], 8)

    def test_cross_batch_correction_ingestion_tie_break(self):
        self.import_doc('b1.json', BASE)
        # Out-of-order backfill: a later batch revises A from t=2.
        self.import_doc('fix.json', batch('fix', [upsert('A', 2, 5, [])]))
        # known_seq=1 must show the original duration even queried past t=2.
        old = query_store(self.db, 9, 1)
        self.assertEqual(old['revision_sources']['A']['known_seq'], 1)
        self.assertEqual(old['schedule']['project_duration'], 8)
        # known_seq=2 at t<2 keeps b1; at t>=2 takes the correction.
        at1 = query_store(self.db, 1, 2)
        at2 = query_store(self.db, 2, 2)
        self.assertEqual(at1['revision_sources']['A']['batch_id'], 'b1')
        self.assertEqual(at2['revision_sources']['A']['batch_id'], 'fix')
        self.assertEqual(at2['schedule']['project_duration'], 10)
        # At t=2 two revisions share effective_at; max(known_seq, seq) wins.
        self.assertEqual(at2['revision_sources']['A']['known_seq'], 2)

    def test_old_known_seq_results_are_permanently_reproducible(self):
        self.import_doc('b1.json', BASE)
        self.import_doc('fix.json', batch('fix', [upsert('A', 0, 9, [])]))
        first = query_store(self.db, 0, 1)
        again = query_store(self.db, 0, 1)
        self.assertEqual(first, again)
        self.assertEqual(first['schedule']['project_duration'], 8)
        self.assertEqual(query_store(self.db, 0, 2)['schedule']['project_duration'], 14)

    def test_query_before_any_visible_batch_is_absent(self):
        self.import_doc('b1.json', BASE)
        view = query_store(self.db, 0, 0)
        self.assertEqual(view['state'], 'absent')
        self.assertIsNone(view['known_seq'])


class Command(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'store.db'

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args):
        return subprocess.run([sys.executable, '-m', 'project_path.history', *args],
                              capture_output=True, text=True)

    def write(self, name, doc):
        path = self.td / name
        path.write_text(json.dumps(doc), encoding='utf-8')
        return path

    def test_import_query_reopen_retry_and_failure_history(self):
        src = self.write('b1.json', BASE)
        r = self.call('import', '--db', str(self.db), '--source', str(src))
        self.assertEqual(r.returncode, 0, r.stderr)
        first = json.loads(r.stdout)
        self.assertEqual(first['known_seq'], 1)
        # Identical retry (same batch_id and source bytes) returns the same seq.
        r = self.call('import', '--db', str(self.db), '--source', str(src))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['known_seq'], 1)
        # Different content reusing the batch_id is a conflict.
        conflict = self.write('conflict.json',
                              batch('b1', [upsert('A', 0, 4, [])]))
        r = self.call('import', '--db', str(self.db), '--source', str(conflict))
        self.assertEqual(r.returncode, 2)
        self.assertIn('conflict', r.stderr)
        # Failed validation does not consume a sequence: next batch is seq 2.
        cyclic = self.write('cyc.json',
                            batch('b2', [upsert('C', 5, 4, ['A', 'B']),
                                         upsert('B', 5, 2, ['C'])]))
        r = self.call('import', '--db', str(self.db), '--source', str(cyclic))
        self.assertEqual(r.returncode, 2)
        good = self.write('good.json', batch('b3', [upsert('A', 2, 2, [])]))
        r = self.call('import', '--db', str(self.db), '--source', str(good))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['known_seq'], 2)
        # A brand-new process reopens the SQLite file and replays history.
        r = self.call('query', '--db', str(self.db),
                      '--effective-at', '2', '--known-seq', '2')
        self.assertEqual(r.returncode, 0, r.stderr)
        view = json.loads(r.stdout)
        self.assertEqual(view['revision_sources']['A']['batch_id'], 'b3')
        self.assertEqual(view['schedule']['project_duration'], 7)
        # Old known_seq stays reproducible across process restarts.
        r = self.call('query', '--db', str(self.db),
                      '--effective-at', '2', '--known-seq', '1')
        old = json.loads(r.stdout)
        self.assertEqual(old['known_seq'], 1)
        self.assertEqual(old['schedule']['project_duration'], 8)

    def test_query_missing_store_and_output_safety(self):
        other = self.td / 'nope.db'
        r = self.call('query', '--db', str(other),
                      '--effective-at', '0', '--known-seq', '1')
        self.assertEqual(r.returncode, 2)
        self.assertFalse(other.exists())
        src = self.write('b1.json', BASE)
        r = self.call('import', '--db', str(self.db),
                      '--source', str(src), '--output', str(self.db))
        self.assertEqual(r.returncode, 2)

    def test_atomic_output_published_and_reused_on_retry(self):
        src = self.write('b1.json', BASE)
        out = self.td / 'report.json'
        r = self.call('import', '--db', str(self.db),
                      '--source', str(src), '--output', str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(out.read_text())
        self.assertEqual(report['known_seq'], 1)
        r = self.call('import', '--db', str(self.db),
                      '--source', str(src), '--output', str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(out.read_text())['status'], 'replayed')
