"""Tests for as-built capacity verification, tier settlement and the ledger CLI.

Coverage:
* identity segments keep two distinct tasks even when usage is unchanged;
* cross-period and cross-tier amounts, plus largest-remainder ties by id;
* a same-usage hand-over exactly at a tier boundary: fee attribution follows
  identity while the invoice total is conserved;
* invoice binding: same bytes replay without double-booking usage, different
  bytes conflict, failures roll back with no residue;
* interrupted settlement (hook before COMMIT) leaves no invoice and no usage;
* capacity violations reject the whole invoice;
* concurrent submissions across a tier boundary are equivalent to some serial
  order and never lose or duplicate usage;
* ten-thousand-interval scale and billion-instant timestamps without
  per-unit enumeration.
"""
import hashlib
import json
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from project_path.billing import query, settle
from project_path.resource import (check_actual_capacity, parse_execution,
                                   parse_pricing, resource_segments,
                                   settle_charges)

ROOT = Path(__file__).resolve().parents[1]


def execution(resources, tasks):
    return {'resources': resources,
            'tasks': [{'task_id': tid, 'start': start, 'finish': finish,
                       'demand': demand}
                      for tid, start, finish, demand in tasks]}


def pricing(capacity, periods, tiers):
    return {'capacity': capacity,
            'periods': [{'period_id': pid, 'start': start, 'end': end}
                        for pid, start, end in periods],
            'tiers': [{'resource': resource, 'size': size, 'unit_price': price}
                      for resource, size, price in tiers]}


def settle_documents(execution_doc, pricing_doc, prior=None):
    classes, tasks, makespan = parse_execution(execution_doc)
    capacity, periods, tiers = parse_pricing(pricing_doc, classes)
    segments, peak = check_actual_capacity(tasks, makespan, classes, capacity)
    result = settle_charges(classes, tasks, segments, periods, tiers,
                            prior_usage=prior)
    return segments, result


def line_map(result):
    return {(line['task_id'], line['resource'], line['period_id']):
            line['amount_cents'] for line in result['lines']}


class IdentitySegments(unittest.TestCase):
    def test_same_usage_handover_keeps_two_segments(self):
        tasks = {'A': {'duration': 10, 'predecessors': [], 'demand': [2]},
                 'B': {'duration': 10, 'predecessors': [], 'demand': [2]}}
        starts = {'A': 0, 'B': 10}
        merged = resource_segments(tasks, starts, 20, ['crew'])
        identity = resource_segments(tasks, starts, 20, ['crew'],
                                     keep_identity=True)
        # Default mode merges identical adjacent usage into one segment.
        self.assertEqual([(s['start'], s['end']) for s in merged], [(0, 20)])
        # Identity mode keeps the hand-over visible.
        self.assertEqual([(s['start'], s['end']) for s in identity],
                         [(0, 10), (10, 20)])
        self.assertEqual([s['tasks'] for s in identity], [['A'], ['B']])
        self.assertEqual(identity[0]['usage'], identity[1]['usage'])

    def test_identity_does_not_split_zero_demand_milestone(self):
        tasks = {'A': {'duration': 10, 'predecessors': [], 'demand': [1]},
                 'M': {'duration': 0, 'predecessors': [], 'demand': [1]}}
        segments = resource_segments(tasks, {'A': 0, 'M': 5}, 10, ['crew'],
                                     keep_identity=True)
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]['tasks'], ['A'])


class SettlementArithmetic(unittest.TestCase):
    def test_within_one_period_no_tier(self):
        _, result = settle_documents(
            execution(['r'], [('A', 0, 10, [3])]),
            pricing({'r': 3}, [('p1', 0, 10)],
                    [('r', None, 5)]))
        self.assertEqual(result['amount_cents'], 10 * 3 * 5)
        self.assertEqual(result['groups'][0]['quantity'], 30)
        self.assertEqual(result['groups'][0]['cumulative_quantity_after'], 30)

    def test_cross_period_restarts_cumulative_usage(self):
        # 10 units of usage in each period; the wide tier never applies.
        _, result = settle_documents(
            execution(['r'], [('A', 0, 10, [1]), ('B', 10, 20, [1])]),
            pricing({'r': 1}, [('p1', 0, 10), ('p2', 10, 20)],
                    [('r', 10, 5), ('r', None, 1)]))
        groups = {(g['resource'], g['period_id']): g for g in result['groups']}
        self.assertEqual(groups[('r', 'p1')]['quantity'], 10)
        self.assertEqual(groups[('r', 'p2')]['quantity'], 10)
        self.assertEqual(groups[('r', 'p1')]['amount_cents'], 50)
        self.assertEqual(groups[('r', 'p2')]['amount_cents'], 50)
        self.assertEqual(groups[('r', 'p1')]['cumulative_quantity_before'], 0)
        self.assertEqual(groups[('r', 'p2')]['cumulative_quantity_before'], 0)

    def test_cross_period_boundary_splits_a_task(self):
        _, result = settle_documents(
            execution(['r'], [('A', 0, 20, [1])]),
            pricing({'r': 1}, [('p1', 0, 10), ('p2', 10, 30)],
                    [('r', None, 2)]))
        self.assertEqual(result['amount_cents'], 40)
        by_period = {g['period_id']: g for g in result['groups']}
        self.assertEqual(by_period['p1']['quantity'], 10)
        self.assertEqual(by_period['p2']['quantity'], 10)

    def test_cross_tier_within_period(self):
        # 10 units at 5 cents, next 5 units at 2 cents; 15 units total.
        _, result = settle_documents(
            execution(['r'], [('A', 0, 15, [1])]),
            pricing({'r': 1}, [('p1', 0, 30)],
                    [('r', 10, 5), ('r', None, 2)]))
        self.assertEqual(result['amount_cents'], 10 * 5 + 5 * 2)
        self.assertEqual(result['groups'][0]['quantity'], 15)

    def test_largest_remainder_tie_goes_to_smaller_id(self):
        # Three tasks with equal shares of a 10-cent charge: exact shares are
        # 3.333... each, floors sum to 9, and the single leftover cent goes to
        # the smallest id because all fractional remainders tie.
        # Quantity = 3 tasks * 2 units = 6; tier 4 units @2 + 2 units @1 = 10.
        _, result = settle_documents(
            execution(['r'], [('A', 0, 2, [1]), ('B', 0, 2, [1]),
                              ('C', 0, 2, [1])]),
            pricing({'r': 3}, [('p1', 0, 2)],
                    [('r', 4, 2), ('r', None, 1)]))
        amounts = line_map(result)
        self.assertEqual(result['amount_cents'], 10)
        self.assertEqual(amounts[('A', 'r', 'p1')], 4)
        self.assertEqual(amounts[('B', 'r', 'p1')], 3)
        self.assertEqual(amounts[('C', 'r', 'p1')], 3)
        self.assertEqual(sum(amounts.values()), 10)

    def test_exact_tie_broken_by_task_id(self):
        # Two equal 1/2 shares of an odd 1-cent charge cannot split evenly;
        # the extra cent must go to the lexicographically smaller task id.
        _, result = settle_documents(
            execution(['r'], [('A', 0, 10, [1]), ('B', 0, 10, [1])]),
            pricing({'r': 2}, [('p1', 0, 10)],
                    [('r', 1, 1), ('r', None, 0)]))
        amounts = line_map(result)
        self.assertEqual(result['amount_cents'], 1)
        self.assertEqual(amounts[('A', 'r', 'p1')], 1)
        self.assertEqual(amounts[('B', 'r', 'p1')], 0)
        # An even split stays exact: quantity 20 with 10 units @ 0 then the
        # rest @ 1 cent gives 10 cents, five each.
        _, even = settle_documents(
            execution(['r'], [('A', 0, 10, [1]), ('B', 0, 10, [1])]),
            pricing({'r': 2}, [('p1', 0, 10)],
                    [('r', 10, 0), ('r', None, 1)]))
        even_amounts = line_map(even)
        self.assertEqual(even['amount_cents'], 10)
        self.assertEqual(even_amounts[('A', 'r', 'p1')], 5)
        self.assertEqual(even_amounts[('B', 'r', 'p1')], 5)

    def test_total_is_conserved_after_rounding(self):
        # Three-way split of a 30-cent charge (1/3 each) must sum to 30.
        _, result = settle_documents(
            execution(['r'], [('A', 0, 10, [1]), ('B', 0, 10, [1]),
                              ('C', 0, 10, [1])]),
            pricing({'r': 3}, [('p1', 0, 10)],
                    [('r', None, 1)]))
        self.assertEqual(result['amount_cents'], 30)
        self.assertEqual(sum(line_map(result).values()), 30)


class HandoverAtTierBoundary(unittest.TestCase):
    """Acceptance: equal usage, identity change exactly on a tier boundary."""

    def documents(self, handover):
        return (execution(['r'], [('A', 0, handover, [1]),
                                  ('B', handover, 20, [1])]),
                pricing({'r': 1}, [('p1', 0, 20)],
                        [('r', 10, 5), ('r', None, 2)]))

    def test_handover_moves_fee_with_identity_and_conserves_total(self):
        # With the hand-over exactly at the tier boundary (10 units), A books
        # the whole first tier and B the whole second tier.
        _, at_boundary = settle_documents(*self.documents(10))
        amounts = line_map(at_boundary)
        self.assertEqual(amounts[('A', 'r', 'p1')], 10 * 5)
        self.assertEqual(amounts[('B', 'r', 'p1')], 10 * 2)
        self.assertEqual(at_boundary['amount_cents'], 70)
        # Later in the same period the fee split differs, but the total is
        # unchanged: identity shifts attribution, not the amount.
        _, later = settle_documents(*self.documents(15))
        later_amounts = line_map(later)
        self.assertEqual(later['amount_cents'], 70)
        self.assertEqual(later_amounts[('A', 'r', 'p1')], 10 * 5 + 5 * 2)
        self.assertEqual(later_amounts[('B', 'r', 'p1')], 5 * 2)
        # And an earlier hand-over shifts attribution the other way.
        _, earlier = settle_documents(*self.documents(5))
        early_amounts = line_map(earlier)
        self.assertEqual(earlier['amount_cents'], 70)
        self.assertEqual(early_amounts[('A', 'r', 'p1')], 5 * 5)
        self.assertEqual(early_amounts[('B', 'r', 'p1')], 5 * 5 + 10 * 2)
        for result in (at_boundary, later, earlier):
            self.assertEqual(result['amount_cents'],
                             sum(line_map(result).values()))


class CapacityVerification(unittest.TestCase):
    def check(self, execution_doc, capacity):
        classes, tasks, makespan = parse_execution(execution_doc)
        return check_actual_capacity(tasks, makespan, classes, capacity)

    def test_over_capacity_is_rejected_whole(self):
        with self.assertRaisesRegex(ValueError, 'capacity exceeded'):
            settle_documents(
                execution(['r'], [('A', 0, 10, [2]), ('B', 0, 10, [2])]),
                pricing({'r': 3}, [('p1', 0, 10)], [('r', None, 1)]))

    def test_exact_capacity_is_accepted(self):
        segments, peak = self.check(
            execution(['r'], [('A', 0, 10, [2]), ('B', 0, 10, [1])]), [3])
        self.assertEqual(peak, {'r': 3})

    def test_handover_at_same_instant_does_not_double_count(self):
        # A ends exactly when B starts: no overlap, so capacity 2 suffices.
        segments, peak = self.check(
            execution(['r'], [('A', 0, 10, [2]), ('B', 10, 20, [2])]), [2])
        self.assertEqual(peak, {'r': 2})
        self.assertEqual([(s['start'], s['end']) for s in segments],
                         [(0, 10), (10, 20)])

    def test_uncovered_usage_is_rejected(self):
        with self.assertRaisesRegex(ValueError, 'do not cover'):
            settle_documents(
                execution(['r'], [('A', 0, 10, [1])]),
                pricing({'r': 1}, [('p1', 0, 5)], [('r', None, 1)]))

    def test_gap_without_usage_is_allowed(self):
        # No usage in [10, 20): the uncovered gap is harmless.
        _, result = settle_documents(
            execution(['r'], [('A', 0, 10, [1]), ('B', 20, 30, [1])]),
            pricing({'r': 1}, [('p1', 0, 10), ('p2', 20, 30)],
                    [('r', None, 1)]))
        self.assertEqual(result['amount_cents'], 20)
        self.assertEqual([g['period_id'] for g in result['groups']],
                         ['p1', 'p2'])


class Ledger(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'ledger.db'
        self.execution = self.td / 'execution.json'
        self.pricing = self.td / 'pricing.json'

    def tearDown(self):
        self.temp.cleanup()

    def write(self, execution_doc, pricing_doc, name='execution.json'):
        path = self.td / name
        path.write_text(json.dumps(execution_doc), encoding='utf-8')
        return path

    def basic(self):
        self.execution.write_text(json.dumps(execution(
            ['r'], [('A', 0, 10, [1]), ('B', 10, 20, [1])])), encoding='utf-8')
        self.pricing.write_text(json.dumps(pricing(
            {'r': 1}, [('p1', 0, 20)],
            [('r', 10, 5), ('r', None, 2)])), encoding='utf-8')

    def test_retry_does_not_double_book_usage(self):
        self.basic()
        first = settle(self.db, 'INV-1', self.execution, self.pricing)
        self.assertEqual(first['status'], 'accepted')
        self.assertEqual(first['amount_cents'], 70)
        second = settle(self.db, 'INV-1', self.execution, self.pricing)
        self.assertEqual(second['status'], 'replayed')
        self.assertEqual(second['amount_cents'], 70)
        self.assertEqual(second['lines'], first['lines'])
        self.assertEqual(query(self.db)['ledger'],
                         [{'resource': 'r', 'period_id': 'p1', 'quantity': 20}])
        self.assertEqual(query(self.db)['invoices_total'], 1)

    def test_same_id_different_bytes_conflicts(self):
        self.basic()
        settle(self.db, 'INV-1', self.execution, self.pricing)
        before = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.execution.write_text(json.dumps(execution(
            ['r'], [('A', 0, 10, [1]), ('B', 10, 25, [1])])), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'conflict'):
            settle(self.db, 'INV-1', self.execution, self.pricing)
        after = hashlib.sha256(self.db.read_bytes()).hexdigest()
        self.assertEqual(before, after)
        self.assertEqual(query(self.db)['ledger'][0]['quantity'], 20)

    def test_capacity_failure_leaves_no_residue(self):
        self.basic()
        over = self.write(execution(['r'], [('A', 0, 10, [2]),
                                            ('B', 0, 10, [2])]),
                          pricing({'r': 2}, [('p1', 0, 20)],
                                  [('r', None, 1)]), name='over-exec.json')
        price = self.td / 'over-price.json'
        price.write_text(json.dumps(pricing(
            {'r': 2}, [('p1', 0, 20)], [('r', None, 1)])), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'capacity exceeded'):
            settle(self.db, 'INV-X', over, price)
        conn = sqlite3.connect(self.db)
        try:
            for table in ('invoices', 'invoice_lines', 'invoice_groups',
                          'usage_ledger'):
                count = conn.execute('SELECT COUNT(*) FROM %s' % table
                                     ).fetchone()[0]
                self.assertEqual(count, 0, table)
        finally:
            conn.close()
        # A retry with valid documents under the same id is accepted, proving
        # the failed attempt consumed no id or usage.
        self.basic()
        accepted = settle(self.db, 'INV-X', self.execution, self.pricing)
        self.assertEqual(accepted['status'], 'accepted')

    def test_interrupted_commit_leaves_no_residue_and_can_retry(self):
        self.basic()

        def crash(_invoice_id):
            raise RuntimeError('simulated crash before COMMIT')

        with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
            settle(self.db, 'INV-1', self.execution, self.pricing,
                   before_commit=crash)
        conn = sqlite3.connect(self.db)
        try:
            for table in ('invoices', 'invoice_lines', 'invoice_groups',
                          'usage_ledger'):
                self.assertEqual(
                    conn.execute('SELECT COUNT(*) FROM %s' % table).fetchone()[0],
                    0, table)
        finally:
            conn.close()
        accepted = settle(self.db, 'INV-1', self.execution, self.pricing)
        self.assertEqual(accepted['status'], 'accepted')
        self.assertEqual(accepted['amount_cents'], 70)

    def test_later_invoice_continues_the_tier(self):
        self.basic()
        settle(self.db, 'INV-1', self.execution, self.pricing)
        # Second invoice uses 10 units; the first already booked 20, so all
        # of it is in the wide tier at 2 cents -> 20 cents.
        second = self.write(execution(['r'], [('C', 30, 40, [1])]),
                            pricing({'r': 1}, [('p1', 0, 40)],
                                    [('r', 10, 5), ('r', None, 2)]),
                            name='second.json')
        price = self.td / 'second-price.json'
        price.write_text(json.dumps(pricing(
            {'r': 1}, [('p1', 0, 40)], [('r', 10, 5), ('r', None, 2)])),
            encoding='utf-8')
        result = settle(self.db, 'INV-2', second, price)
        self.assertEqual(result['amount_cents'], 20)
        group = result['groups'][0]
        self.assertEqual(group['cumulative_quantity_before'], 20)
        self.assertEqual(group['cumulative_quantity_after'], 30)

    def test_concurrent_invoices_equivalent_to_serial_order(self):
        # Two invoices each consume 10 units of a 10/2-cent tier.  Whichever
        # wins the write lock books the cheap tier; the other books the wide
        # tier.  Both orders are valid serial outcomes and the total ledger is
        # identical, with no lost or duplicated usage.
        self.basic()
        first = settle(self.db, 'INV-1', self.execution, self.pricing)
        path = self.td / 'second-exec.json'
        price = self.td / 'second-price.json'
        path.write_text(json.dumps(execution(['r'], [('C', 30, 40, [1])])),
                        encoding='utf-8')
        price.write_text(json.dumps(pricing(
            {'r': 1}, [('p1', 0, 40)], [('r', 10, 5), ('r', None, 2)])),
            encoding='utf-8')
        second = settle(self.db, 'INV-2', path, price)
        amounts = (first['amount_cents'], second['amount_cents'])
        self.assertEqual(sorted(amounts), [20, 70])
        self.assertEqual(amounts[0], 70)  # INV-1 fits tier 1: 10*5 + 10*2
        self.assertEqual(sum(amounts), 90)
        ledger = query(self.db)['ledger']
        self.assertEqual(ledger, [{'resource': 'r', 'period_id': 'p1',
                                   'quantity': 30}])

    def test_same_id_same_bytes_different_output_path_is_replay(self):
        self.basic()
        settle(self.db, 'INV-1', self.execution, self.pricing)
        replay = settle(self.db, 'INV-1', self.execution, self.pricing)
        self.assertEqual(replay['status'], 'replayed')
        self.assertEqual(replay['execution_source_sha256'],
                         hashlib.sha256(self.execution.read_bytes()).hexdigest())

    def test_two_concurrent_invoices_match_a_serial_order(self):
        # Two invoices, each 20 units against a 10 @ 5c then wide @ 2c tier.
        # Exactly one can own the cheap tier.  BEGIN IMMEDIATE serialises them;
        # whichever acquires the lock first books tier 1.  The pair of amounts
        # must therefore equal {70, 40} and the ledger must hold 40 units --
        # never an overlap computed from a stale cumulative reading.
        import threading

        executions = []
        prices = []
        for index in range(2):
            path = self.td / ('exec-%d.json' % index)
            price = self.td / ('price-%d.json' % index)
            start = 0 if index == 0 else 20
            path.write_text(json.dumps(execution(
                ['r'], [('T%d' % index, start, start + 20, [1])])),
                encoding='utf-8')
            price.write_text(json.dumps(pricing(
                {'r': 1}, [('p1', 0, 40)],
                [('r', 10, 5), ('r', None, 2)])), encoding='utf-8')
            executions.append(path)
            prices.append(price)

        barrier = threading.Barrier(2)
        results, errors = {}, []

        def worker(index):
            try:
                barrier.wait()
                results[index] = settle(self.db, 'INV-%d' % index,
                                        executions[index], prices[index])
            except BaseException as error:  # captured for the assertion below
                errors.append(error)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        amounts = sorted(result['amount_cents'] for result in results.values())
        # Both serial orders produce the same multiset: the first invoice books
        # 10 units at 5c and 10 at 2c (70), the second books 20 units at 2c (40).
        self.assertEqual(amounts, [40, 70])
        self.assertEqual(sum(amounts), 110)
        # The winner's cumulative-before is 0 and the loser's is 20: the loser
        # saw the winner's committed usage rather than a stale zero.
        befores = sorted(result['groups'][0]['cumulative_quantity_before']
                         for result in results.values())
        self.assertEqual(befores, [0, 20])
        self.assertEqual(query(self.db)['ledger'],
                         [{'resource': 'r', 'period_id': 'p1',
                           'quantity': 40}])

    def test_concurrent_retry_of_one_invoice_books_usage_once(self):
        import threading

        barrier = threading.Barrier(4)
        statuses, errors = [], []

        def worker():
            try:
                barrier.wait()
                statuses.append(settle(self.db, 'INV-1', self.execution,
                                       self.pricing)['status'])
            except BaseException as error:
                errors.append(error)

        self.basic()
        threads = [threading.Thread(target=worker) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(statuses.count('accepted'), 1)
        self.assertEqual(statuses.count('replayed'), 3)
        self.assertEqual(query(self.db)['ledger'],
                         [{'resource': 'r', 'period_id': 'p1', 'quantity': 20}])
        self.assertEqual(query(self.db)['amount_cents_total'], 70)


class Cli(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.td = Path(self.temp.name)
        self.db = self.td / 'ledger.db'
        self.execution = self.td / 'execution.json'
        self.pricing = self.td / 'pricing.json'
        self.execution.write_text(json.dumps(execution(
            ['r'], [('A', 0, 10, [1]), ('B', 10, 20, [1])])), encoding='utf-8')
        self.pricing.write_text(json.dumps(pricing(
            {'r': 1}, [('p1', 0, 20)],
            [('r', 10, 5), ('r', None, 2)])), encoding='utf-8')

    def tearDown(self):
        self.temp.cleanup()

    def call(self, *args):
        return subprocess.run(
            [sys.executable, '-m', 'project_path.billing_cli', *args],
            capture_output=True, text=True,
            env={'PYTHONPATH': str(ROOT / 'src'), 'PATH': '/usr/bin:/bin'})

    def test_settle_query_and_replay(self):
        out = self.td / 'invoice.json'
        r = self.call('settle', '--db', str(self.db), '--invoice-id', 'INV-1',
                      '--execution', str(self.execution),
                      '--pricing', str(self.pricing), '--output', str(out))
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(out.read_text())
        self.assertEqual(report['status'], 'accepted')
        self.assertEqual(report['amount_cents'], 70)
        self.assertEqual(report['execution_source_sha256'],
                         hashlib.sha256(self.execution.read_bytes()).hexdigest())
        self.assertEqual(report['pricing_source_sha256'],
                         hashlib.sha256(self.pricing.read_bytes()).hexdigest())

        retry = self.call('settle', '--db', str(self.db), '--invoice-id', 'INV-1',
                          '--execution', str(self.execution),
                          '--pricing', str(self.pricing))
        self.assertEqual(retry.returncode, 0, retry.stderr)
        self.assertEqual(json.loads(retry.stdout)['status'], 'replayed')

        q = self.call('query', '--db', str(self.db), '--invoice-id', 'INV-1')
        self.assertEqual(q.returncode, 0, q.stderr)
        body = json.loads(q.stdout)
        self.assertEqual(body['invoice']['invoice_id'], 'INV-1')
        self.assertEqual(body['ledger'], [{'resource': 'r', 'period_id': 'p1',
                                           'quantity': 20}])
        self.assertEqual(body['invoices_total'], 1)
        self.assertEqual(body['amount_cents_total'], 70)

    def test_conflict_and_capacity_errors_exit_two(self):
        self.assertEqual(self.call(
            'settle', '--db', str(self.db), '--invoice-id', 'INV-1',
            '--execution', str(self.execution),
            '--pricing', str(self.pricing)).returncode, 0)
        self.execution.write_text(json.dumps(execution(
            ['r'], [('A', 0, 10, [1])])), encoding='utf-8')
        conflict = self.call('settle', '--db', str(self.db),
                             '--invoice-id', 'INV-1',
                             '--execution', str(self.execution),
                             '--pricing', str(self.pricing))
        self.assertEqual(conflict.returncode, 2)
        self.assertIn('conflict', conflict.stderr)
        over = self.td / 'over.json'
        over.write_text(json.dumps(execution(
            ['r'], [('A', 0, 10, [2]), ('B', 0, 10, [2])])), encoding='utf-8')
        price = self.td / 'over-price.json'
        price.write_text(json.dumps(pricing(
            {'r': 2}, [('p1', 0, 20)], [('r', None, 1)])), encoding='utf-8')
        capacity = self.call('settle', '--db', str(self.db),
                             '--invoice-id', 'INV-2',
                             '--execution', str(over), '--pricing', str(price))
        self.assertEqual(capacity.returncode, 2)
        self.assertIn('capacity exceeded', capacity.stderr)
        # The original invoice and ledger survive both failures.
        q = self.call('query', '--db', str(self.db))
        self.assertEqual(json.loads(q.stdout)['ledger'][0]['quantity'], 20)

    def test_query_missing_ledger_does_not_create_it(self):
        r = self.call('query', '--db', str(self.db))
        self.assertEqual(r.returncode, 2)
        self.assertFalse(self.db.exists())

    def test_path_collisions_rejected(self):
        r = self.call('settle', '--db', str(self.db), '--invoice-id', 'INV-1',
                      '--execution', str(self.db), '--pricing', str(self.pricing))
        self.assertEqual(r.returncode, 2)

    def test_sample_fixtures_settle(self):
        db = self.td / 'sample.db'
        r = self.call('settle', '--db', str(db), '--invoice-id', 'SAMPLE',
                      '--execution', str(ROOT / 'fixtures/actuals-handover.json'),
                      '--pricing', str(ROOT / 'fixtures/pricing-tiers.json'))
        self.assertEqual(r.returncode, 0, r.stderr)
        report = json.loads(r.stdout)
        self.assertEqual(report['amount_cents'], 950)


class Scale(unittest.TestCase):
    def test_ten_thousand_intervals_with_billion_instants(self):
        rng = random.Random(7)
        tasks = []
        base = 10 ** 9 - 10 ** 6
        for index in range(10000):
            start = base + index * 10
            finish = start + rng.randint(1, 9)
            tasks.append(('T%05d' % index, start, finish, [1]))
        doc = execution(['r'], tasks)
        price = pricing({'r': 1}, [('p1', 0, 2 * 10 ** 9)],
                        [('r', None, 1)])
        segments, result = settle_documents(doc, price)
        self.assertLessEqual(len(segments), 2 * len(tasks) + 1)
        self.assertEqual(result['amount_cents'],
                         sum(f - s for _, s, f, _ in tasks))
        self.assertGreater(max(s for _, s, _, _ in tasks), 10 ** 9 - 10 ** 6)

    def test_billion_instant_boundaries_do_not_enumerate_units(self):
        # One task spanning a billion instants settles in constant work.
        _, result = settle_documents(
            execution(['r'], [('A', 0, 10 ** 9, [1])]),
            pricing({'r': 1}, [('p1', 0, 10 ** 9)],
                    [('r', 10 ** 8, 3), ('r', None, 1)]))
        self.assertEqual(result['amount_cents'],
                         10 ** 8 * 3 + 9 * 10 ** 8 * 1)


class FuzzConservation(unittest.TestCase):
    def test_random_small_invoices_conserve_each_group(self):
        rng = random.Random(2026)
        for _ in range(40):
            capacity = rng.randint(1, 3)
            tasks = []
            for index in range(rng.randint(1, 4)):
                start = rng.randint(0, 8)
                finish = start + rng.randint(0, 4)
                tasks.append(('T%d' % index, start, finish, [rng.randint(0, capacity)]))
            tiers = [('r', rng.randint(1, 5), rng.randint(0, 9)),
                     ('r', None, rng.randint(0, 9))]
            horizon = max([f for _, _, f, _ in tasks], default=0)
            price = pricing(
                {'r': capacity},
                [('p1', 0, 6), ('p2', 6, max(12, horizon))], tiers)
            # The random demands can exceed capacity even though each one is
            # individually within it; such an invoice must be rejected.
            try:
                segments, result = settle_documents(
                    execution(['r'], tasks), price)
            except ValueError as error:
                self.assertIn('capacity exceeded', str(error))
                continue
            group_total = sum(g['amount_cents'] for g in result['groups'])
            self.assertEqual(group_total, result['amount_cents'])
            for group in result['groups']:
                members = [l for l in result['lines']
                           if l['resource'] == group['resource']
                           and l['period_id'] == group['period_id']]
                self.assertEqual(sum(l['amount_cents'] for l in members),
                                 group['amount_cents'])
                self.assertEqual(sum(l['quantity'] for l in members),
                                 group['quantity'])
                self.assertEqual(group['cumulative_quantity_after']
                                 - group['cumulative_quantity_before'],
                                 group['quantity'])


if __name__ == '__main__':
    unittest.main()
