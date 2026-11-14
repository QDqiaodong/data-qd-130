from .common import require,ident,number,rows
from .schedule import schedule

def analyze(data):
    require(data,'tasks');tasks={}
    for t in rows(data['tasks'],1000):
        require(t,'id duration predecessors');tid=ident(t['id']);duration=number(t['duration'],0,10**6)
        predecessors=[ident(p) for p in rows(t['predecessors'],1000)]
        if tid in tasks or len(set(predecessors))!=len(predecessors):raise ValueError('duplicate task or predecessor')
        tasks[tid]={'duration':duration,'predecessors':predecessors}
    if not tasks:raise ValueError('tasks cannot be empty')
    children={t:[] for t in tasks};degree={t:len(v['predecessors']) for t,v in tasks.items()}
    for t,row in tasks.items():
        for p in row['predecessors']:
            if p not in tasks:raise ValueError('unknown predecessor')
            children[p].append(t)
    ready=sorted(t for t in tasks if degree[t]==0);order=[]
    while ready:
        t=ready.pop(0);order.append(t)
        for c in sorted(children[t]):
            degree[c]-=1
            if not degree[c]:ready.append(c);ready.sort()
    if len(order)!=len(tasks):raise ValueError('precedence cycle')
    return schedule(tasks,order,children)
