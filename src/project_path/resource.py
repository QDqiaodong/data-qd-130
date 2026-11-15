"""Resource-constrained schedule and as-built billing from local JSON.

Two entry layers share this module:

* the planner (``plan-schedule``) reads a bitemporal plan snapshot plus a
  resource document and returns a proven-optimal schedule;
* the as-built settlement (``plan-billing``) reads an *execution* document
  (actual task intervals and demands) plus a *pricing* document (capacity,
  billing-period boundaries, progressive usage tiers and integer cent unit
  rates), verifies instantaneous capacity, and settles one invoice against
  the cumulative usage ledger.

Both measure time as half-open integer intervals ``[start, finish)`` and never
enumerate individual time units.
"""

import bisect
from fractions import Fraction

from .common import ident, number, require, rows
from .model import analyze
from .rcpsp import MAX_RESOURCES, MAX_TASKS, resource_segments, solve, verify

MAX_CLASSES = MAX_RESOURCES
MAX_CAPACITY = 10 ** 9

MAX_ACTUAL_TASKS = 100_000
MAX_PERIODS = 100_000
# Instants are plain integers: a billion-instant horizon costs no extra work
# because only event points and period boundaries are ever visited.
MAX_TIME = 10 ** 12
MAX_RATE = 10 ** 9
MAX_TIERS = 1000
MAX_BILLING_RESOURCES = 32
MAX_SOURCE_BYTES = 64_000_000
MAX_DETAIL_LINES = 1_000_000
# Intermediate (segment, resource, active task) contributions, bounded so a
# pathological all-overlapping input fails explicitly instead of hanging.
MAX_BILLING_WORK = 4_000_000
# SQLite INTEGER is signed 64-bit; every persisted quantity/amount must fit.
MAX_INT64 = 2 ** 63 - 1


def parse_resources(data, snapshot):
    """Validate the resource document against a plan snapshot.

    Layout::

        {"resources": [{"name": "crew", "capacity": 3}, ...],
         "demands": [{"task_id": "A", "demand": [1, 0]}, ...]}

    ``capacity`` and every ``demand`` entry are non-negative integers.  Every
    snapshot task must appear exactly once in ``demands`` (zero-duration
    milestones included); a demand above capacity or a missing task is an
    explicit failure.  Milestone demand values are validated but never consume
    capacity because the task has zero length.
    """
    require(data, 'resources demands')
    classes = []
    capacity = []
    resource_rows = rows(data['resources'], MAX_CLASSES)
    for row in resource_rows:
        require(row, 'name capacity')
        name = ident(row['name'])
        if name in classes:
            raise ValueError('duplicate resource class')
        classes.append(name)
        capacity.append(number(row['capacity'], 0, MAX_CAPACITY))
    if not classes:
        raise ValueError('resources must list at least one class')
    tasks = {row['id']: row for row in snapshot['tasks']}
    if len(tasks) > MAX_TASKS:
        raise ValueError('at most %d tasks are supported' % MAX_TASKS)
    demand_by_task = {}
    for row in rows(data['demands'], MAX_TASKS):
        require(row, 'task_id demand')
        task_id = ident(row['task_id'])
        if task_id not in tasks:
            raise ValueError('resource demand for unknown task: ' + task_id)
        if task_id in demand_by_task:
            raise ValueError('duplicate resource demand for task: ' + task_id)
        vector = [number(v, 0, MAX_CAPACITY) for v in rows(row['demand'], MAX_CLASSES)]
        if len(vector) != len(classes):
            raise ValueError('demand width must equal the resource class count')
        for index, value in enumerate(vector):
            if value > capacity[index]:
                raise ValueError('demand exceeds capacity for task %s on %s'
                                 % (task_id, classes[index]))
        demand_by_task[task_id] = vector
    missing = sorted(set(tasks) - set(demand_by_task))
    if missing:
        raise ValueError('missing resource demand for task: ' + ', '.join(missing))
    return classes, capacity, demand_by_task


def _static_critical_path(tasks):
    """Static (infinite-resource) plan: the classical lower bound and evidence."""
    return analyze({'tasks': [{'id': t, 'duration': tasks[t]['duration'],
                              'predecessors': tasks[t]['predecessors']}
                             for t in sorted(tasks)]})


def build_schedule(snapshot, classes, capacity, demand_by_task, time_limit=None):
    """Solve and assemble the publication report for one snapshot."""
    tasks = {row['id']: {'duration': row['duration'],
                         'predecessors': list(row['predecessors']),
                         'demand': list(demand_by_task[row['id']])}
             for row in snapshot['tasks']}
    baseline = _static_critical_path(tasks)
    makespan, starts, evidence = solve(tasks, capacity, time_limit=time_limit)
    ok, reason = verify(tasks, capacity, starts, makespan)
    if not ok:
        raise AssertionError('internal verification failed: ' + reason)
    assignments = [{
        'task_id': t,
        'duration': tasks[t]['duration'],
        'predecessors': tasks[t]['predecessors'],
        'start': starts[t],
        'finish': starts[t] + tasks[t]['duration'],
        'demand': dict(zip(classes, demand_by_task[t])),
    } for t in sorted(tasks)]
    optimum = makespan
    lower_bound = baseline['project_duration']
    return {
        'resource_classes': classes,
        'capacity': dict(zip(classes, capacity)),
        'assignments': assignments,
        'start_vector': {t: starts[t] for t in sorted(tasks)},
        'makespan': makespan,
        'resource_segments': resource_segments(tasks, starts, makespan, classes),
        'static_critical_path_lower_bound': lower_bound,
        'critical_path_evidence': {
            'one_critical_path': baseline['one_critical_path'],
            'critical_activities': baseline['critical_activities'],
            'unbounded_resource_project_duration': baseline['project_duration'],
        },
        'optimality': {
            'proven_optimal': True,
            'goal': 'minimum makespan, then lexicographically least start vector by task id',
            'makespan': optimum,
            'static_lower_bound_tight': lower_bound == makespan,
            'search_nodes': evidence['search_nodes'],
            'method': evidence['method'],
            'time_unit_enumeration': False,
        },
    }


# ---------------------------------------------------------------------------
# As-built execution: capacity verification and billing-period settlement
# ---------------------------------------------------------------------------

def parse_execution(data):
    """Strictly validate one execution (as-built) document.

    Layout::

        {"resources": ["crew", "inspector"],
         "tasks": [{"task_id": "A", "start": 0, "finish": 10,
                    "demand": [2, 0]}, ...]}

    ``resources`` names the columns of every ``demand`` vector; task ids are
    unique.  ``start``/``finish`` are integers with ``0 <= start <= finish``
    and describe the half-open interval ``[start, finish)``.  A zero-width
    task consumes nothing but is still validated.  Returns
    ``(classes, tasks, makespan)`` where each task carries ``duration`` and
    ``demand`` as a list in class order.
    """
    require(data, 'resources tasks')
    classes = []
    for value in rows(data['resources'], MAX_BILLING_RESOURCES):
        name = ident(value)
        if name in classes:
            raise ValueError('duplicate execution resource')
        classes.append(name)
    if not classes:
        raise ValueError('execution must list at least one resource')
    tasks = {}
    makespan = 0
    for row in rows(data['tasks'], MAX_ACTUAL_TASKS):
        require(row, 'task_id start finish demand')
        task_id = ident(row['task_id'])
        if task_id in tasks:
            raise ValueError('duplicate execution task_id: ' + task_id)
        start = number(row['start'], 0, MAX_TIME)
        finish = number(row['finish'], 0, MAX_TIME)
        if finish < start:
            raise ValueError('task %s finishes before it starts' % task_id)
        vector = [number(v, 0, MAX_CAPACITY)
                  for v in rows(row['demand'], MAX_BILLING_RESOURCES)]
        if len(vector) != len(classes):
            raise ValueError('demand width must equal the execution resource count')
        tasks[task_id] = {'start': start, 'finish': finish,
                          'duration': finish - start, 'demand': vector}
        makespan = max(makespan, finish)
    if not tasks:
        raise ValueError('execution tasks cannot be empty')
    return classes, tasks, makespan


def parse_pricing(data, classes):
    """Validate a pricing document against the execution's resource classes.

    Layout::

        {"capacity": {"crew": 2, "inspector": 1},
         "periods": [{"period_id": "p1", "start": 0, "end": 7200}, ...],
         "tiers": [{"resource": "crew", "size": 10, "unit_price": 5},
                   {"resource": "crew", "size": null, "unit_price": 3}]}

    ``capacity`` must name exactly the execution resources.  Periods are
    non-overlapping, non-empty half-open intervals; their order in the
    document is irrelevant.  Each resource has at least one tier; every tier
    but the last for that resource has a positive integer ``size``, and the
    last has ``"size": null`` so all usage is priced.  Prices are non-negative
    integer cents per unit.
    """
    require(data, 'capacity periods tiers')
    if not isinstance(data['capacity'], dict):
        raise ValueError('capacity must be an object keyed by resource')
    if set(data['capacity']) != set(classes):
        raise ValueError('pricing capacity must name exactly the execution resources')
    capacity = {cls: number(data['capacity'][cls], 0, MAX_CAPACITY)
                for cls in classes}
    periods = []
    seen = set()
    for row in rows(data['periods'], MAX_PERIODS):
        require(row, 'period_id start end')
        period_id = ident(row['period_id'])
        if period_id in seen:
            raise ValueError('duplicate billing period_id')
        seen.add(period_id)
        start = number(row['start'], 0, MAX_TIME)
        end = number(row['end'], 0, MAX_TIME)
        if end <= start:
            raise ValueError('billing period must have positive width')
        periods.append({'period_id': period_id, 'start': start, 'end': end})
    if not periods:
        raise ValueError('pricing must list at least one billing period')
    periods.sort(key=lambda item: (item['start'], item['end'], item['period_id']))
    for previous, current in zip(periods, periods[1:]):
        if current['start'] < previous['end']:
            raise ValueError('billing periods must not overlap')
    tiers = {}
    for row in rows(data['tiers'], MAX_TIERS):
        require(row, 'resource size unit_price')
        resource = ident(row['resource'])
        if resource not in capacity:
            raise ValueError('pricing tier names an unknown resource: ' + resource)
        price = number(row['unit_price'], 0, MAX_RATE)
        size = row['size']
        if size is not None:
            size = number(size, 1, MAX_INT64)
        tiers.setdefault(resource, []).append((size, price))
    for cls in classes:
        entries = tiers.get(cls)
        if not entries:
            raise ValueError('missing pricing tiers for resource: ' + cls)
        for size, _ in entries[:-1]:
            if size is None:
                raise ValueError('only the last tier of a resource may be unbounded')
        if entries[-1][0] is not None:
            raise ValueError('the last tier of every resource must be unbounded')
    return [capacity[cls] for cls in classes], periods, tiers


def check_actual_capacity(tasks, makespan, classes, capacity):
    """Identity-preserving segments plus peaks; raises when any instant exceeds.

    Capacity is checked on exactly the same identity segments the settlement
    uses, so the verdict and the billed timeline can never disagree.
    """
    segmented = {task_id: {'duration': row['duration'],
                           'demand': row['demand']}
                 for task_id, row in tasks.items()}
    starts = {task_id: row['start'] for task_id, row in tasks.items()}
    segments = resource_segments(segmented, starts, makespan, classes,
                                 keep_identity=True)
    peak = {cls: 0 for cls in classes}
    for segment in segments:
        for index, cls in enumerate(classes):
            used = segment['usage'][cls]
            peak[cls] = max(peak[cls], used)
            if used > capacity[index]:
                raise ValueError(
                    'capacity exceeded on %s in [%d, %d): %d > %d'
                    % (cls, segment['start'], segment['end'], used,
                       capacity[index]))
    return segments, peak


def _period_containing(periods, starts, point):
    """Index of the billing period covering ``point``; raises when uncovered."""
    index = bisect.bisect_right(starts, point) - 1
    if index < 0 or point < periods[index]['start'] or point >= periods[index]['end']:
        raise ValueError('no billing period covers time %d' % point)
    return index


def _billing_chunks(tasks, segments, periods, classes):
    """Split identity segments at billing-period boundaries.

    Only event points and period boundaries are visited.  Every instant with
    positive usage must be covered by exactly one period, otherwise the whole
    invoice is rejected.
    """
    period_starts = [period['start'] for period in periods]
    chunks = []
    for segment in segments:
        usage = segment['usage']
        if not any(usage[cls] for cls in classes):
            continue
        index = _period_containing(periods, period_starts, segment['start'])
        position = segment['start']
        identities = [(task_id,
                       dict(zip(classes, tasks[task_id]['demand'])))
                      for task_id in segment['tasks']]
        while position < segment['end']:
            if index >= len(periods):
                raise ValueError('billing periods do not cover %d'
                                 % position)
            period = periods[index]
            if position < period['start'] or position >= period['end']:
                raise ValueError('billing periods do not cover %d' % position)
            stop = min(segment['end'], period['end'])
            chunks.append({
                'period_id': period['period_id'],
                'start': position,
                'end': stop,
                'usage': usage,
                'tasks': identities,
            })
            position = stop
            index += 1
    return chunks


def _tier_cost(bounds, start, quantity):
    """Cost in cents of ``quantity`` units starting at cumulative ``start``.

    ``bounds`` is ``[(upper_bound or None, unit_price), ...]`` with cumulative
    upper bounds.  Integer arithmetic only, proportional to the number of
    tiers, never to the quantity.
    """
    cost = 0
    lower = 0
    end = start + quantity
    for upper, price in bounds:
        top = end if upper is None else min(end, upper)
        if top > lower:
            low = max(start, lower)
            if top > low:
                cost += (top - low) * price
        if upper is None or end <= upper:
            break
        lower = upper
    return cost


def _tier_bounds(tiers):
    bounds = []
    total = 0
    for size, price in tiers:
        if size is None:
            bounds.append((None, price))
        else:
            total += size
            bounds.append((total, price))
    return bounds


def settle_charges(classes, tasks, segments, periods, tiers,
                   prior_usage=None):
    """Price one invoice exactly, then snap each group to integer cents.

    Progressive tiers are shared per ``(resource, period)``.  ``prior_usage``
    maps that key to the cumulative quantity already booked by earlier
    invoices; this invoice's quantities continue from there in **time order**,
    so a task hand-over at a tier boundary moves fee attribution with the
    task identity instead of smearing it over merged usage.

    Within a billing chunk the fee is apportioned to active tasks by their
    demand share of the chunk usage (exact ``Fraction``).  All chunks of one
    ``(resource, period)`` are accumulated exactly, then the largest-remainder
    method converts them to whole cents; ties go to the smaller task id.  The
    group total is the exact integer tier cost, so rounding never creates or
    destroys money.
    """
    prior_usage = prior_usage or {}
    bounds = {cls: _tier_bounds(tiers[cls]) for cls in classes}
    chunks = _billing_chunks(tasks, segments, periods, classes)
    cursor = {}
    exact = {}
    quantities = {}
    group_cost = {}
    work = 0
    for chunk in chunks:
        width = chunk['end'] - chunk['start']
        for cls in classes:
            used = chunk['usage'][cls]
            if not used:
                continue
            key = (cls, chunk['period_id'])
            if key not in cursor:
                cursor[key] = prior_usage.get(key, 0)
            quantity = width * used
            cost = _tier_cost(bounds[cls], cursor[key], quantity)
            cursor[key] += quantity
            group_cost[key] = group_cost.get(key, 0) + cost
            for task_id, demand in chunk['tasks']:
                share = demand[cls]
                if not share:
                    continue
                work += 1
                if work > MAX_BILLING_WORK:
                    raise ValueError('invoice exceeds the settlement work limit')
                detail = (cls, chunk['period_id'], task_id)
                exact[detail] = exact.get(detail, Fraction(0)) + \
                    Fraction(cost * share, used)
                quantities[detail] = quantities.get(detail, 0) + width * share
    if len(exact) > MAX_DETAIL_LINES:
        raise ValueError('invoice exceeds %d detail lines' % MAX_DETAIL_LINES)
    period_order = {period['period_id']: index
                    for index, period in enumerate(periods)}
    class_order = {cls: index for index, cls in enumerate(classes)}
    grouped = {}
    for (cls, period_id, task_id) in exact:
        grouped.setdefault((cls, period_id), []).append(task_id)
    lines = []
    groups = []
    total_cents = 0
    for key in sorted(group_cost, key=lambda item: (class_order[item[0]],
                                                    period_order[item[1]])):
        cls, period_id = key
        members = sorted(grouped[key])
        floors = {}
        remainders = []
        floor_total = 0
        for task_id in members:
            share = exact[(cls, period_id, task_id)]
            floor = share.numerator // share.denominator
            floors[task_id] = floor
            floor_total += floor
            remainders.append((-(share - floor), task_id))
        target = group_cost[key]
        missing = target - floor_total
        if not 0 <= missing <= len(members):
            raise AssertionError('largest-remainder reconciliation failed')
        remainders.sort()
        for index in range(missing):
            floors[remainders[index][1]] += 1
        quantity = 0
        for task_id in members:
            detail = (cls, period_id, task_id)
            amount = floors[task_id]
            line_quantity = quantities[detail]
            if not 0 <= line_quantity <= MAX_INT64 or not 0 <= amount <= MAX_INT64:
                raise ValueError('invoice line exceeds signed 64-bit storage')
            quantity += line_quantity
            lines.append({'task_id': task_id, 'resource': cls,
                          'period_id': period_id, 'quantity': line_quantity,
                          'amount_cents': amount})
        before = cursor[key] - quantity
        after = cursor[key]
        if quantity != after - before:
            raise AssertionError('group quantity reconciliation failed')
        if not 0 <= quantity <= MAX_INT64 or not 0 <= target <= MAX_INT64:
            raise ValueError('invoice group exceeds signed 64-bit storage')
        if not 0 <= after <= MAX_INT64:
            raise ValueError('cumulative usage exceeds signed 64-bit storage')
        groups.append({'resource': cls, 'period_id': period_id,
                       'quantity': quantity, 'amount_cents': target,
                       'cumulative_quantity_before': before,
                       'cumulative_quantity_after': after})
        total_cents += target
        if total_cents > MAX_INT64:
            raise ValueError('invoice total exceeds signed 64-bit storage')
    return {'groups': groups, 'lines': lines, 'amount_cents': total_cents,
            'segments': len(segments),
            'peak_usage': {cls: max((segment['usage'][cls]
                                     for segment in segments), default=0)
                           for cls in classes},
            'time_unit_enumeration': False}
