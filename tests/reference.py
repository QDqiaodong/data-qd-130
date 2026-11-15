"""Independent exhaustive reference for tiny instances (test-only helper).

Deliberately naive: enumerate every start assignment over a finite horizon,
checking precedence and capacity per event interval.  Prefix sums make the
capacity check cheap; nothing is shared with ``project_path.rcpsp``.
"""


def reference_optimum(tasks, capacities):
    """Return ``(makespan, (starts for tasks sorted by id))`` or None."""
    ids = sorted(tasks)
    horizon = sum(tasks[t]['duration'] for t in ids)
    assign = {}
    best = [None]

    def feasible():
        points = {0, horizon}
        for t in ids:
            points.add(assign[t])
            points.add(assign[t] + tasks[t]['duration'])
        points = sorted(points)
        for a, b in zip(points, points[1:]):
            if a == b:
                continue
            for r in range(len(capacities)):
                used = 0
                for t in ids:
                    duration = tasks[t]['duration']
                    if duration and assign[t] < b and a < assign[t] + duration:
                        used += tasks[t]['demand'][r]
                if used > capacities[r]:
                    return False
        return True

    def recurse(index):
        if index == len(ids):
            if feasible():
                vector = tuple(assign[t] for t in ids)
                candidate = (max(assign[t] + tasks[t]['duration'] for t in ids), vector)
                if best[0] is None or candidate < best[0]:
                    best[0] = candidate
            return
        t = ids[index]
        low = max((assign[p] + tasks[p]['duration']
                   for p in tasks[t]['predecessors'] if p in assign), default=0)
        for s in range(low, horizon + 1):
            assign[t] = s
            recurse(index + 1)
        if t in assign:
            del assign[t]

    recurse(0)
    return best[0]


def greedy_id_schedule(tasks, capacities):
    """Simple id-priority serial SGS, used only to demonstrate suboptimality."""
    ids = sorted(tasks)
    done, start, finish, profile = set(), {}, {}, []

    def fits(s, duration, demand):
        used = [0] * len(capacities)
        for a, b, vector in profile:
            if a < s + duration and s < b:
                for r in range(len(capacities)):
                    used[r] += vector[r]
        return all(used[r] + demand[r] <= capacities[r] for r in range(len(capacities)))

    while len(done) < len(ids):
        eligible = [t for t in ids if t not in done
                    and all(p in done for p in tasks[t]['predecessors'])]
        t = min(eligible)
        release = max((finish[p] for p in tasks[t]['predecessors']), default=0)
        candidates = {release} | ({b for a, b, _ in profile if b > release}
                                 if tasks[t]['duration'] else set())
        for s in sorted(candidates):
            if fits(s, tasks[t]['duration'], tasks[t]['demand']):
                break
        else:
            raise AssertionError('no fit')
        start[t] = s
        finish[t] = s + tasks[t]['duration']
        done.add(t)
        if tasks[t]['duration']:
            profile.append((s, finish[t], tasks[t]['demand']))
    makespan = max(finish.values(), default=0)
    return makespan, tuple(start[t] for t in ids)
