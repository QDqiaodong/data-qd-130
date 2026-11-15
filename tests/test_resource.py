"""Tests for exact resource-constrained scheduling and its CLI.

Coverage:
* the exact optimiser and its lexicographic tie-break against an independent
  exhaustive reference on randomly generated tiny instances;
* a hand-built case where id-priority greedy scheduling is strictly worse;
* a million-duration sparse-event case (must not enumerate time units);
* zero-duration milestones, same-instant release reuse, precedence;
* bitemporal coordinates and the guarantee that later back-fill cannot change
  an older coordinate's answer;
* failure semantics: explicit errors, old report preserved, store byte-identical.
"""
import hashlib
import json
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from project_path.history import import_batch
from project_path.rcpsp import resource_segments, solve, verify
from project_path.resource import parse_resources
from project_path.resource_cli import run

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from reference import greedy_id_schedule, reference_optimum  # noqa: E402


def batch(batch_id, revisions):
    return {'batch_id': batch_id, 'revisions': revisions}


def upsert(task_id, effective_at, duration, predecessors):
    return {'task_id': task_id, 'effective_at': effective_at, 'op': 'upsert',
            'duration': duration, 'predecessors': predecessors}


def delete(task_id, effective_at):
    return {'task_id': task_id, 'effective_at': effective_at, 'op': 'delete'}


BASE = batch('base', [
    upsert('A', 0, 3, []),
    upsert('B', 0, 2, ['A']),
    upsert('C', 0, 4, ['A']),
    upsert('D', 0, 1, ['B', 'C']),
    upsert('E', 0, 2, []),
])


class SolverAgainstExhaustive(unittest.TestCase):
    """Random tiny instances must match an independent exhaustive enumerator."""

    def check(self, tasks, capacities):
        makespan, starts, _ = solve({t: dict(row, demand=list(row['demand']))
                                     for t, row in tasks.items()},
                                    capacities)
        expected = reference_optimum(tasks, capacities)
        self.assertIsNotNone(expected)
        got = (makespan, tuple(starts[t] for t in sorted(tasks)))
        self.assertEqual(got, expected)
        ok, reason = verify(tasks, capacities, starts, makespan)
        self.assertTrue(ok, reason)

    def test_random_graphs_match_exhaustive(self):
        rng = random.Random(20240501)
        for _ in range(60):
            n = rng.randint(1, 4)
            ids = [chr(65 + i) for i in range(n)]
            tasks, capacities = {}, (rng.randint(1, 2), rng.randint(1, 2))
            for i, t in enumerate(ids):
                demand = [rng.randint(0, capacities[0]), rng.randint(0, capacities[1])]
                tasks[t] = {'duration': rng.randint(0, 3),
                            'predecessors': [ids[j] for j in range(i)
                                             if rng.random() < 0.3],
                            'demand': demand}
            self.check(tasks, capacities)

    def test_zero_duration_milestones_are_free_and_match_exhaustive(self):
        rng = random.Random(99)
        for _ in range(25):
            tasks = {
                'M0': {'duration': 0, 'predecessors': [], 'demand': [1, 1]},
                'A': {'duration': 2, 'predecessors': ['M0'], 'demand': [1, 0]},
                'M1': {'duration': 0, 'predecessors': ['A'], 'demand': [1, 1]},
                'B': {'duration': rng.randint(1, 3), 'predecessors': [], 'demand': [0, 1]},
            }
            self.check(tasks, (1, 1))

    def test_lexicographic_tie_break_prefers_smaller_id_start(self):
        # Two independent unit tasks share one unit of capacity: A at 0, B at 1
        # beats the mirror image because A's start is compared first.
        tasks = {'A': {'duration': 1, 'predecessors': [], 'demand': [1]},
                 'B': {'duration': 1, 'predecessors': [], 'demand': [1]}}
        makespan, starts, _ = solve(tasks, (1,))
        self.assertEqual(makespan, 2)
        self.assertEqual((starts['A'], starts['B']), (0, 1))
        # Making the smaller id the longer task must still put it as early as
        # possible without lengthening the project.
        tasks = {'A': {'duration': 2, 'predecessors': [], 'demand': [1]},
                 'B': {'duration': 1, 'predecessors': ['A'], 'demand': [1]}}
        makespan, starts, _ = solve(tasks, (1,))
        self.assertEqual((makespan, starts['A'], starts['B']), (3, 0, 2))

    def test_same_instant_release_is_reusable(self):
        # capacity 1, sequential chain of two unit tasks: B starts exactly when
        # A finishes, because [s, f) releases the unit at f.
        tasks = {'A': {'duration': 5, 'predecessors': [], 'demand': [1]},
                 'B': {'duration': 4, 'predecessors': ['A'], 'demand': [1]}}
        makespan, starts, _ = solve(tasks, (1,))
        self.assertEqual((makespan, starts['A'], starts['B']), (9, 0, 5))


class GreedyIsNotOptimal(unittest.TestCase):
    """The CLI must publish the proven optimum, never the greedy answer."""

    def test_id_priority_greedy_is_strictly_worse(self):
        base = {'A': (2, []), 'B': (4, ['A']), 'C': (1, []),
                'D': (2, ['B', 'C']), 'E': (4, ['C']), 'F': (4, ['A', 'B'])}
        demand = {'A': [2, 1], 'B': [0, 0], 'C': [2, 1],
                  'D': [0, 1], 'E': [2, 2], 'F': [0, 1]}
        capacities = (2, 2)
        tasks = {t: {'duration': base[t][0], 'predecessors': base[t][1],
                     'demand': demand[t]} for t in base}
        greedy_makespan, _ = greedy_id_schedule(tasks, capacities)
        makespan, starts, _ = solve({t: dict(row, demand=list(row['demand']))
                                     for t, row in tasks.items()}, capacities)
        self.assertEqual(makespan, 11)
        self.assertEqual(greedy_makespan, 16)
        self.assertEqual(starts, {'A': 0, 'B': 2, 'C': 2, 'D': 7, 'E': 3, 'F': 7})
        self.assertEqual(reference_optimum(tasks, capacities),
                         (11, (0, 2, 2, 7, 3, 7)))


class SparseEvents(unittest.TestCase):
    def test_million_duration_uses_event_times_only(self):
        rng = random.Random(8)
        ids = [chr(65 + i) for i in range(16)]
        tasks = {}
        for i, t in enumerate(ids):
            tasks[t] = {
                'duration': rng.choice([0, 10 ** 6, 999983, rng.randint(1, 10 ** 6)]),
                'predecessors': [ids[j] for j in range(i) if rng.random() < 0.3],
                'demand': [rng.randint(0, 2) for _ in range(3)],
            }
        capacities = (3, 2, 1)
        for row in tasks.values():
            for r in range(3):
                row['demand'][r] = min(row['demand'][r], capacities[r])
        makespan, starts, evidence = solve(
            {t: dict(row, demand=list(row['demand'])) for t, row in tasks.items()},
            capacities, time_limit=30)
        self.assertGreater(makespan, 10 ** 6)
        ok, reason = verify(tasks, capacities, starts, makespan)
        self.assertTrue(ok, reason)
        segments = resource_segments(tasks, starts, makespan, ['r0', 'r1', 'r2'])
        finished = {starts[t] + tasks[t]['duration'] for t in tasks if tasks[t]['duration']}
        starts_set = {starts[t] for t in tasks if tasks[t]['duration']}
        change_points = sorted(finished | starts_set)
        self.assertLessEqual(len(segments), 2 * len(change_points) + 1)
        # Segment boundaries sit on task events, never on every time unit.
        for segment in segments:
            self.assertIn(segment['start'], change_points)
            self.assertIn(segment['end'], change_points)


class ResourceDocument(unittest.TestCase):
    def snapshot(self, tasks):
        return {'tasks': [{'id': t, 'duration': tasks[t]['duration'],
                           'predecessors': tasks[t]['predecessors']}
                          for t in sorted(tasks)]}

    def test_accepts_valid_document(self):
        tasks = {'A': {'duration': 1, 'predecessors': [], 'demand': [1]}}
        doc = {'resources': [{'name': 'r', 'capacity': 2}],
               'demands': [{'task_id': 'A', 'demand': [1]}]}
        classes, capacity, demands = parse_resources(doc, self.snapshot(tasks))
        self.assertEqual((classes, capacity, demands), (['r'], [2], {'A': [1]}))

    def test_missing_task_fails(self):
        tasks = {'A': {'duration': 1, 'predecessors': [], 'demand': [1]},
                 'B': {'duration': 0, 'predecessors': [], 'demand': [0]}}
        doc = {'resources': [{'name': 'r', 'capacity': 2}],
               'demands': [{'task_id': 'A', 'demand': [1]}]}
        with self.assertRaisesRegex(ValueError, 'missing resource demand'):
            parse_resources(doc, self.snapshot(tasks))

    def test_demand_over_capacity_fails(self):
        tasks = {'A': {'duration': 1, 'predecessors': [], 'demand': [3]}}
        doc = {'resources': [{'name': 'r', 'capacity': 2}],
               'demands': [{'task_id': 'A', 'demand': [3]}]}
        with self.assertRaisesRegex(ValueError, 'exceeds capacity'):
            parse_resources(doc, self.snapshot(tasks))

    def test_bad_shapes_fail(self):
        tasks = {'A': {'duration': 1, 'predecessors': [], 'demand': [1]}}
        base = {'resources': [{'name': 'r', 'capacity': 2}],
                'demands': [{'task_id': 'A', 'demand': [1]}]}
        for mutate in (
                lambda d: d.update(extra=1),
                lambda d: d['resources'].append({'name': 'r', 'capacity': 1}),
                lambda d: d['demands'].append({'task_id': 'Q', 'demand': [0]}),
                lambda d: d['demands'][0].update(demand=[1, 0]),
                lambda d: d['demands'][0].update(demand=[-1]),
                lambda d: d['demands'][0].update(demand=[1.5]),
                lambda d: d.update(resources=[]),
        ):
            doc = json.loads(json.dumps(base))
            mutate(doc)
            with self.assertRaises((ValueError, TypeError)):
                parse_resources(doc, self.snapshot(tasks))


class Command(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'history.db'
        self.resources = self.td / 'resources.json'
        self.resources.write_text(json.dumps({
            'resources': [{'name': 'crew', 'capacity': 2},
                          {'name': 'inspector', 'capacity': 1}],
            'demands': [
                {'task_id': 'A', 'demand': [2, 0]},
                {'task_id': 'B', 'demand': [0, 1]},
                {'task_id': 'C', 'demand': [1, 1]},
                {'task_id': 'D', 'demand': [0, 1]},
                {'task_id': 'E', 'demand': [0, 0]},
            ]
        }), encoding='utf-8')
        self.db_before = None

    def tearDown(self):
        self.temp.cleanup()

    def write(self, name, doc):
        path = self.td / name
        path.write_text(json.dumps(doc), encoding='utf-8')
        return path

    def call(self, *args):
        return subprocess.run(
            [sys.executable, '-m', 'project_path.resource_cli', *args],
            capture_output=True, text=True,
            env={'PYTHONPATH': str(ROOT / 'src'), 'PATH': '/usr/bin:/bin'})

    def seed(self, doc=BASE):
        return import_batch(self.db, self.write('seed.json', doc))

    def test_report_contract_and_provenance(self):
        self.seed()
        out = self.td / 'plan.json'
        r = self.call('--db', str(self.db), '--effective-at', '0',
                      '--known-seq', '1', '--resources', str(self.resources),
                      '--output', str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(out.read_text())
        self.assertEqual(report['schema_version'], 1)
        self.assertEqual(report['coordinate'],
                         {'requested_effective_at': 0, 'requested_known_seq': 1,
                          'effective_at': 0, 'effective_known_seq': 1})
        self.assertEqual(report['snapshot_revision_sources']['A']['batch_id'], 'base')
        self.assertEqual(report['resource_source_sha256'],
                         hashlib.sha256(self.resources.read_bytes()).hexdigest())
        result = report['result']
        self.assertEqual(result['makespan'], 10)
        self.assertEqual(result['start_vector'],
                         {'A': 0, 'B': 3, 'C': 5, 'D': 9, 'E': 0})
        self.assertEqual(result['static_critical_path_lower_bound'], 8)
        self.assertEqual(result['optimality']['proven_optimal'], True)
        self.assertEqual(result['optimality']['time_unit_enumeration'], False)
        self.assertEqual([a['task_id'] for a in result['assignments']],
                         ['A', 'B', 'C', 'D', 'E'])
        for a in result['assignments']:
            self.assertEqual(a['finish'] - a['start'], a['duration'])
        self.assertEqual(result['resource_segments'][0]['usage'],
                         {'crew': 2, 'inspector': 0})

    def test_backfill_does_not_change_old_coordinate(self):
        self.seed()
        seeded = hashlib.sha256(self.db.read_bytes()).hexdigest()
        first = run(self.db, 0, 1, self.resources)
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), seeded)
        first_bytes = json.dumps(first, sort_keys=True)
        # A later batch back-fills a longer duration from t=0; only newly known
        # coordinates may see it, the (0, 1) answer stays byte-identical.
        import_batch(self.db, self.write('later.json', batch('later', [
            upsert('A', 0, 6, []),
        ])))
        after_backfill = hashlib.sha256(self.db.read_bytes()).hexdigest()
        again = run(self.db, 0, 1, self.resources)
        self.assertEqual(json.dumps(again, sort_keys=True), first_bytes)
        self.assertEqual(again['snapshot_revision_sources']['A']['known_seq'], 1)
        newer = run(self.db, 0, 2, self.resources)
        self.assertEqual(again['result']['makespan'], 10)
        self.assertGreater(newer['result']['makespan'], 10)
        self.assertEqual(newer['snapshot_revision_sources']['A']['known_seq'], 2)
        # Reading never rewrote the store, even after the back-fill.
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(),
                         after_backfill)

    def test_failure_keeps_prior_report_and_store_untouched(self):
        self.seed()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        out = self.td / 'plan.json'
        out.write_text('prior report', encoding='utf-8')
        broken = self.write('broken.json', {'resources': [], 'demands': []})
        r = self.call('--db', str(self.db), '--effective-at', '0',
                      '--known-seq', '1', '--resources', str(broken),
                      '--output', str(out))
        self.assertEqual(r.returncode, 2)
        self.assertTrue(r.stderr.strip())
        self.assertEqual(out.read_text(), 'prior report')
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_missing_demand_fails_without_side_effects(self):
        self.seed()
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        partial = self.write('partial.json', {
            'resources': [{'name': 'crew', 'capacity': 2}],
            'demands': [{'task_id': 'A', 'demand': [1]}]})
        out = self.td / 'plan.json'
        out.write_text('prior', encoding='utf-8')
        r = self.call('--db', str(self.db), '--effective-at', '0',
                      '--known-seq', '1', '--resources', str(partial),
                      '--output', str(out))
        self.assertEqual(r.returncode, 2)
        self.assertIn('missing resource demand', r.stderr)
        self.assertEqual(out.read_text(), 'prior')
        self.assertEqual(hashlib.sha256(self.db.read_bytes()).hexdigest(), before)

    def test_missing_store_reports_error_without_creating_it(self):
        r = self.call('--db', str(self.db), '--effective-at', '0',
                      '--known-seq', '1', '--resources', str(self.resources))
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.db.exists(), 'query must not create the store')

    def test_absent_coordinate_fails(self):
        self.seed()
        # known_seq=0 means no batch is visible yet: the coordinate is absent.
        r = self.call('--db', str(self.db), '--effective-at', '0',
                      '--known-seq', '0', '--resources', str(self.resources))
        self.assertEqual(r.returncode, 2)
        self.assertIn('no plan snapshot', r.stderr)

    def test_path_collisions_and_symlink_output_rejected(self):
        self.seed()
        for args in (
                (str(self.db), str(self.db)),
                (str(self.resources), str(self.resources)),
                (str(self.db), str(self.resources)),
        ):
            r = self.call('--db', args[0], '--effective-at', '0',
                          '--known-seq', '1', '--resources', args[1],
                          '--output', args[1])
            self.assertEqual(r.returncode, 2)

        link = self.td / 'link.json'
        link.symlink_to(self.resources)
        r = self.call('--db', str(self.db), '--effective-at', '0',
                      '--known-seq', '1', '--resources', str(self.resources),
                      '--output', str(link))
        self.assertEqual(r.returncode, 2)

    def test_deterministic_rerun(self):
        self.seed()
        out = self.td / 'plan.json'
        first = self.call('--db', str(self.db), '--effective-at', '0',
                          '--known-seq', '1', '--resources', str(self.resources),
                          '--output', str(out))
        self.assertEqual(first.returncode, 0, first.stderr)
        snapshot = out.read_bytes()
        second = self.call('--db', str(self.db), '--effective-at', '0',
                           '--known-seq', '1', '--resources', str(self.resources),
                           '--output', str(out))
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(snapshot, out.read_bytes())

    def test_timeout_publishes_nothing_and_keeps_old_report(self):
        # 16 tasks with heavy contention makes the exact search non-trivial;
        # a tiny limit must fail instead of publishing a heuristic answer.
        rng = random.Random(2)
        ids = [chr(65 + i) for i in range(16)]
        revisions = []
        for i, t in enumerate(ids):
            revisions.append(upsert(t, 0, rng.randint(1, 5),
                                    [ids[j] for j in range(i)
                                     if rng.random() < 0.25]))
        self.seed(batch('hard', revisions))
        self.resources.write_text(json.dumps({
            'resources': [{'name': 'crew', 'capacity': 3},
                          {'name': 'rig', 'capacity': 4},
                          {'name': 'lab', 'capacity': 2}],
            'demands': [{'task_id': t,
                         'demand': [rng.randint(1, 2), rng.randint(0, 2),
                                    rng.randint(0, 1)]}
                        for t in ids],
        }), encoding='utf-8')
        out = self.td / 'plan.json'
        out.write_text('prior report', encoding='utf-8')
        r = self.call('--db', str(self.db), '--effective-at', '0',
                      '--known-seq', '1', '--resources', str(self.resources),
                      '--output', str(out), '--time-limit', '0.0001')
        self.assertEqual(r.returncode, 2)
        self.assertIn('time limit', r.stderr)
        self.assertEqual(out.read_text(), 'prior report')


if __name__ == '__main__':
    unittest.main()
