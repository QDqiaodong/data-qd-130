"""Resource-constrained schedule from a bitemporal plan plus a resource file.

The plan snapshot (tasks, durations, predecessors) is read from the SQLite
revision store at a ``(effective_at, known_seq)`` coordinate.  The resource
document supplies one integer capacity per renewable class and one integer demand
per task and class.  The solver returns a proven-optimal makespan, the
lexicographically smallest start vector at that makespan, piecewise resource
usage and the static critical-path lower bound.
"""

from .common import ident, number, require, rows
from .model import analyze
from .rcpsp import MAX_RESOURCES, MAX_TASKS, resource_segments, solve, verify

MAX_CLASSES = MAX_RESOURCES
MAX_CAPACITY = 10 ** 9


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
