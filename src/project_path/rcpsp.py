"""Exact resource-constrained project scheduling (RCPSP), standard library only.

Model
-----
* Tasks are non-preemptive and finish-to-start: a task starts only after every
  predecessor has finished.
* Each task has an integer duration and one integer demand per renewable
  resource class.  Zero-duration milestones consume no capacity.
* Executions occupy half-open intervals ``[start, finish)``; capacity released
  at ``finish`` is immediately reusable at the same instant.
* Objective: global minimum makespan.  Among all schedules attaining it the
  start vector ordered by task id is lexicographically minimal.

Algorithm
---------
The search enumerates *active* schedules through a serial schedule generation
scheme: repeatedly pick one precedence-eligible task and place it at the
earliest instant that satisfies precedence and capacity.  Every active schedule
is represented (choose a different task at each step), and an optimal makespan
is always attained by an active schedule, so the enumeration is exact.

States are memoised on ``(placed positive-duration task set, their start times)``.
That pair fixes the resource profile and every unscheduled task's release
time, so the residual problem is fully determined.  Zero-duration milestones
are forced to their earliest start.  Branch and bound adds a static-tail bound,
a release-time resource-work bound, latest-finish window checks and interval
energetic reasoning.  All bounds are evaluated at event times (task finishes
and releases); time is never enumerated unit by unit.

A greedy schedule is used only to seed the incumbent bound.  It is never
reported as the answer: if an optional time limit is hit ``TimeoutError`` is
raised instead of publishing a heuristic result.
"""

import bisect

MAX_TASKS = 16
MAX_RESOURCES = 3
MAX_DURATION = 10 ** 6
# Identity segments enumerate every (segment, active task) pair they report.
# A pathological all-overlapping input can make that quadratic; fail explicitly
# instead of hanging.
MAX_IDENTITY_PAIRS = 4_000_000


def _topological(tasks):
    """Deterministic topological order plus adjacency."""
    missing = {p for row in tasks.values() for p in row['predecessors']
               if p not in tasks}
    if missing:
        raise ValueError('unknown predecessor')
    duplicate = [t for t, row in tasks.items()
                 if len(set(row['predecessors'])) != len(row['predecessors'])]
    if duplicate:
        raise ValueError('duplicate predecessor')
    children = {t: [] for t in tasks}
    for t, row in tasks.items():
        for p in row['predecessors']:
            children[p].append(t)
    degree = {t: len(row['predecessors']) for t, row in tasks.items()}
    ready = sorted(t for t in tasks if degree[t] == 0)
    order = []
    while ready:
        t = ready.pop(0)
        order.append(t)
        for c in sorted(children[t]):
            degree[c] -= 1
            if degree[c] == 0:
                ready.append(c)
                ready.sort()
    if len(order) != len(tasks):
        raise ValueError('precedence cycle')
    return order, children


def _fits(start, duration, demand, intervals, capacities):
    """True if ``[start, start+duration)`` fits the aggregated profile."""
    used = [0] * len(capacities)
    end = start + duration
    for s, f, use in intervals:
        if s < end and start < f:
            for r in range(len(capacities)):
                used[r] += use[r]
    return all(used[r] + demand[r] <= capacities[r] for r in range(len(capacities)))


def _earliest(release, duration, demand, intervals, capacities):
    """Earliest start >= release; only task finishes are candidate event times."""
    if duration == 0:
        return release
    candidates = {release}
    for s, f, _ in intervals:
        if f > release:
            candidates.add(f)
    for t in sorted(candidates):
        if t >= release and _fits(t, duration, demand, intervals, capacities):
            return t
    raise AssertionError('no feasible start found')


def solve(tasks, capacities, time_limit=None, seed=0):
    """Return ``(makespan, starts, evidence)`` proven optimal.

    ``tasks`` maps id -> {'duration': int, 'predecessors': [id], 'demand': [int]}.
    ``capacities`` holds one non-negative integer per resource class.
    Raises ``TimeoutError`` when ``time_limit`` seconds elapse; a heuristic
    answer is never returned in that case.
    """
    import random
    import time

    if not tasks:
        raise ValueError('tasks cannot be empty')
    if len(tasks) > MAX_TASKS:
        raise ValueError('at most %d tasks are supported' % MAX_TASKS)
    if not 1 <= len(capacities) <= MAX_RESOURCES:
        raise ValueError('between 1 and %d resource classes are supported'
                         % MAX_RESOURCES)
    for t, row in tasks.items():
        if type(row['duration']) is not int or not 0 <= row['duration'] <= MAX_DURATION:
            raise ValueError('duration outside 0..%d' % MAX_DURATION)
        if len(row['demand']) != len(capacities):
            raise ValueError('demand width does not match the capacity vector')
        for value in row['demand']:
            if type(value) is not int or value < 0:
                raise ValueError('resource demand must be a non-negative integer')
    for value in capacities:
        if type(value) is not int or value < 0:
            raise ValueError('capacity must be a non-negative integer')
    started = time.time()
    limit = None if time_limit is None else started + time_limit
    ids = sorted(tasks)
    n = len(ids)
    nres = len(capacities)
    order, children = _topological(tasks)

    duration = {t: tasks[t]['duration'] for t in ids}
    predecessors = {t: list(tasks[t]['predecessors']) for t in ids}
    demand = {t: tuple(tasks[t]['demand']) for t in ids}

    positive = [t for t in ids if duration[t] > 0]
    pos_index = {t: i for i, t in enumerate(positive)}
    full = (1 << len(positive)) - 1

    tail = {}
    for t in reversed(order):
        tail[t] = duration[t] + max((tail[c] for c in children[t]), default=0)

    nodes = [0]

    def tick():
        nodes[0] += 1
        if limit is not None and nodes[0] % 512 == 0 and time.time() > limit:
            raise TimeoutError(
                'exact search exceeded the time limit; no schedule published')

    def zero_closure(done, start, finish):
        """Force every ready zero-duration milestone to its earliest start."""
        added = []
        changed = True
        while changed:
            changed = False
            for t in ids:
                if (duration[t] == 0 and t not in done
                        and all(p in done for p in predecessors[t])):
                    s = max((finish[p] for p in predecessors[t]), default=0)
                    start[t] = finish[t] = s
                    done.add(t)
                    added.append(t)
                    changed = True
        return added

    def release_of(t, finish):
        return max((finish[p] for p in predecessors[t]), default=0)

    def window_bound(mask, done, finish, horizon):
        """Earliest starts, static tails, resource work, latest-finish windows."""
        earliest, pref = {}, {}
        for t in order:
            if (duration[t] > 0 and mask >> pos_index[t] & 1) or t in done:
                continue
            earliest[t] = max((finish[p] if p in done else pref[p]
                               for p in predecessors[t]), default=0)
            pref[t] = earliest[t] + duration[t]
        pending = [t for t in positive if not mask >> pos_index[t] & 1]
        bound = max((earliest[t] + tail[t] for t in pending), default=0)
        for point in {earliest[t] for t in pending}:
            for r in range(nres):
                if capacities[r]:
                    work = sum(duration[t] * demand[t][r]
                               for t in pending if earliest[t] >= point)
                    bound = max(bound, point + -(-work // capacities[r]))
        latest = {}
        for t in reversed(order):
            if (duration[t] > 0 and mask >> pos_index[t] & 1) or t in done:
                continue
            # latest[t] upper-bounds the latest finish; latest start = latest-duration.
            successors = [finish[c] if c in done else latest[c] for c in children[t]]
            latest[t] = min(successors, default=horizon)
            if latest[t] - duration[t] < earliest[t]:
                return horizon + 1, earliest, latest, pending
        return bound, earliest, latest, pending

    def energetic_ok(mask, done, start, finish, horizon):
        """Interval energetic reasoning: False means no schedule meets ``horizon``."""
        earliest, pref, placed = {}, {}, []
        for t in order:
            if (duration[t] > 0 and mask >> pos_index[t] & 1) or t in done:
                continue
            earliest[t] = max((finish[p] if p in done else pref[p]
                               for p in predecessors[t]), default=0)
            pref[t] = earliest[t] + duration[t]
        pending = [t for t in positive if not mask >> pos_index[t] & 1]
        for t in positive:
            if mask >> pos_index[t] & 1:
                placed.append((start[t], finish[t], demand[t]))
        latest = {}
        for t in reversed(order):
            if (duration[t] > 0 and mask >> pos_index[t] & 1) or t in done:
                continue
            successors = [finish[c] if c in done else latest[c] for c in children[t]]
            latest[t] = min(successors, default=horizon)
        points = {0, horizon}
        for t in pending:
            points.add(earliest[t])
            points.add(latest[t])
        points = sorted(points)
        for i, a in enumerate(points):
            if a >= horizon:
                break
            for b in points[i + 1:]:
                width = b - a
                if width <= 0:
                    continue
                for r in range(nres):
                    if not capacities[r]:
                        continue
                    total = 0
                    for s, f, use in placed:
                        lo, hi = max(s, a), min(f, b)
                        if hi > lo:
                            total += (hi - lo) * use[r]
                    for t in pending:
                        overlap = (duration[t] - max(0, a - earliest[t])
                                   - max(0, latest[t] - b))
                        if overlap > 0:
                            total += overlap * demand[t][r]
                    if total > width * capacities[r]:
                        return False
        return True

    def greedy(priority):
        """Serial SGS over all tasks; seeds the incumbent bound only."""
        done, start, finish, intervals = set(), {}, {}, []
        while len(done) < n:
            eligible = [t for t in ids if t not in done
                        and all(p in done for p in predecessors[t])]
            t = min(eligible, key=priority)
            s = _earliest(release_of(t, finish), duration[t], demand[t],
                          intervals, capacities)
            start[t] = s
            finish[t] = s + duration[t]
            done.add(t)
            if duration[t] > 0:
                intervals.append((s, finish[t], demand[t]))
        return max(finish.values(), default=0), tuple(start[t] for t in ids), start

    incumbent = [None]
    for priority in (lambda t: t,
                     lambda t: (-tail[t], t),
                     lambda t: (-duration[t], t),
                     lambda t: (-sum(demand[t]), t),
                     lambda t: (-sum(demand[t][r] * tail[t] for r in range(nres)), t)):
        candidate = greedy(priority)
        if incumbent[0] is None or candidate[:2] < incumbent[0][:2]:
            incumbent[0] = candidate
    rng = random.Random(seed)
    for _ in range(32):
        weights = {t: rng.random() * (tail[t] + 1) for t in ids}
        candidate = greedy(lambda t: -weights[t])
        if candidate[:2] < incumbent[0][:2]:
            incumbent[0] = candidate
    best_known = incumbent[0][0]

    makespan_memo = {}

    def best_makespan(mask, done, start, finish, intervals):
        tick()
        added = zero_closure(done, start, finish)
        key = (mask, tuple((t, start[t]) for t in positive if mask >> pos_index[t] & 1))
        if key in makespan_memo:
            value = makespan_memo[key]
        elif mask == full:
            value = max(finish.values(), default=0)
            makespan_memo[key] = value
        else:
            bound, earliest, latest, pending = window_bound(
                mask, done, finish, best_known - 1)
            if bound >= best_known or not energetic_ok(
                    mask, done, start, finish, best_known - 1):
                # This subtree cannot beat the incumbent.  The incumbent is
                # achievable and is itself inside the enumerated space, so it
                # is a sound stand-in for the subtree's true minimum here.
                value = best_known
            else:
                value = None
                for t in pending:
                    if not all(p in done for p in predecessors[t]):
                        continue
                    s = _earliest(release_of(t, finish), duration[t], demand[t],
                                  intervals, capacities)
                    if s + duration[t] >= best_known:
                        continue
                    start[t], finish[t] = s, s + duration[t]
                    done.add(t)
                    intervals.append((s, finish[t], demand[t]))
                    child = best_makespan(mask | 1 << pos_index[t], done, start,
                                          finish, intervals)
                    intervals.pop()
                    done.discard(t)
                    del start[t], finish[t]
                    if value is None or child < value:
                        value = child
                if value is None:
                    value = best_known
            makespan_memo[key] = value
        for t in added:
            del start[t], finish[t]
            done.discard(t)
        return value

    makespan = best_makespan(0, set(), {}, {}, [])

    vector_memo = {}

    def best_vector(mask, done, start, finish, intervals):
        tick()
        added = zero_closure(done, start, finish)
        key = (mask, tuple((t, start[t]) for t in positive if mask >> pos_index[t] & 1))
        if key in vector_memo:
            value = vector_memo[key]
        elif mask == full:
            value = tuple(start[t] for t in ids)
            vector_memo[key] = value
        else:
            bound, earliest, latest, pending = window_bound(
                mask, done, finish, makespan)
            if bound > makespan or not energetic_ok(
                    mask, done, start, finish, makespan):
                value = None
            else:
                value = None
                for t in pending:
                    if not all(p in done for p in predecessors[t]):
                        continue
                    s = _earliest(release_of(t, finish), duration[t], demand[t],
                                  intervals, capacities)
                    if s + duration[t] > makespan:
                        continue
                    start[t], finish[t] = s, s + duration[t]
                    done.add(t)
                    intervals.append((s, finish[t], demand[t]))
                    child = best_vector(mask | 1 << pos_index[t], done, start,
                                        finish, intervals)
                    intervals.pop()
                    done.discard(t)
                    del start[t], finish[t]
                    if child is not None and (value is None or child < value):
                        value = child
            vector_memo[key] = value
        for t in added:
            del start[t], finish[t]
            done.discard(t)
        return value

    vector = best_vector(0, set(), {}, {}, [])
    if vector is None:
        raise AssertionError('no schedule at the proven makespan')
    starts = dict(zip(ids, vector))
    return makespan, starts, {'search_nodes': nodes[0],
                              'method': 'exact state-space enumeration of active schedules'}


def _identity_segments(tasks, starts, makespan, classes):
    """Sweep-line segments that keep the active task identity.

    Boundaries exist at every start/finish.  Adjacent segments are merged only
    when the *active task set* is identical; equal usage alone is not enough.
    A hand-over at a constant usage rate therefore stays visible as two
    segments, which is what per-task cost attribution needs.  The sweep visits
    only event points, never individual time units.
    """
    events = {}
    for t in tasks:
        duration = tasks[t]['duration']
        if not duration:
            continue
        start = starts[t]
        finish = start + duration
        if not (0 <= start and finish <= makespan):
            continue
        events.setdefault(start, ([], []))[0].append(t)
        events.setdefault(finish, ([], []))[1].append(t)
    points = sorted({0, makespan} | set(events))
    active = []
    current = {cls: 0 for cls in classes}
    index = {cls: i for i, cls in enumerate(classes)}
    segments = []
    pairs = 0
    for position, a in enumerate(points):
        begin, end = events.get(a, ((), ()))
        for t in begin:
            bisect.insort(active, t)
            for cls in classes:
                current[cls] += tasks[t]['demand'][index[cls]]
        for t in end:
            del active[bisect.bisect_left(active, t)]
            for cls in classes:
                current[cls] -= tasks[t]['demand'][index[cls]]
        if position + 1 == len(points):
            break
        b = points[position + 1]
        if a == b:
            continue
        if segments and segments[-1]['end'] == a and segments[-1]['tasks'] == active:
            segments[-1]['end'] = b
        else:
            pairs += len(active)
            if pairs > MAX_IDENTITY_PAIRS:
                raise ValueError('too many concurrent task identities to segment')
            segments.append({'start': a, 'end': b,
                             'usage': {cls: current[cls] for cls in classes},
                             'tasks': list(active)})
    return segments


def resource_segments(tasks, starts, makespan, classes, keep_identity=False):
    """Piecewise resource usage over [0, makespan); zero-duration tasks absent.

    Boundaries only at task starts/finishes.  In the default mode adjacent
    segments whose usage is identical are merged so zero-demand tasks do not
    create noise splits.  With ``keep_identity=True`` every segment also carries
    ``tasks`` (sorted active task ids) and is merged only when the active task
    set is unchanged; this preserves a hand-over where total usage stays
    constant but the responsible tasks change.
    """
    if keep_identity:
        return _identity_segments(tasks, starts, makespan, classes)
    points = {0, makespan}
    for t in tasks:
        if tasks[t]['duration']:
            points.add(starts[t])
            points.add(starts[t] + tasks[t]['duration'])
    points = sorted(p for p in points if 0 <= p <= makespan)
    segments = []
    for a, b in zip(points, points[1:]):
        if a == b:
            continue
        usage = {cls: 0 for cls in classes}
        for t in tasks:
            duration = tasks[t]['duration']
            if duration and starts[t] < b and a < starts[t] + duration:
                for index, cls in enumerate(classes):
                    usage[cls] += tasks[t]['demand'][index]
        if segments and segments[-1]['usage'] == usage:
            segments[-1]['end'] = b
        else:
            segments.append({'start': a, 'end': b, 'usage': usage})
    return segments


def verify(tasks, capacities, starts, makespan):
    """Independently re-check precedence, capacity and makespan claims."""
    for t in tasks:
        for p in tasks[t]['predecessors']:
            if starts[t] < starts[p] + tasks[p]['duration']:
                return False, 'precedence violated for %s' % t
        if starts[t] < 0:
            return False, 'negative start for %s' % t
    actual = max((starts[t] + tasks[t]['duration'] for t in tasks), default=0)
    if actual != makespan:
        return False, 'makespan mismatch'
    points = sorted({starts[t] for t in tasks if tasks[t]['duration']}
                    | {starts[t] + tasks[t]['duration'] for t in tasks
                       if tasks[t]['duration']})
    for a, b in zip(points, points[1:]):
        for r in range(len(capacities)):
            used = sum(tasks[t]['demand'][r] for t in tasks
                       if tasks[t]['duration'] and starts[t] < b
                       and a < starts[t] + tasks[t]['duration'])
            if used > capacities[r]:
                return False, 'capacity %d exceeded in [%d, %d)' % (r, a, b)
    return True, None
