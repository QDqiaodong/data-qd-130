def schedule(tasks,order,children):
    earliest={};latest={}
    for task in order:
        row=tasks[task];start=max((earliest[p][1] for p in row['predecessors']),default=0);earliest[task]=(start,start+row['duration'])
    duration=max((end for start,end in earliest.values()),default=0)
    for task in reversed(order):
        finish=min((latest[c][0] for c in children[task]),default=duration);latest[task]=(finish-tasks[task]['duration'],finish)
    critical=[t for t in sorted(tasks) if earliest[t][0]==latest[t][0]]
    edges=[{'from':p,'to':c} for p in sorted(tasks) for c in sorted(children[p]) if p in critical and c in critical and earliest[p][1]==earliest[c][0]]
    path=[]
    if critical:
        roots=[t for t in critical if not tasks[t]['predecessors'] and earliest[t][0]==0];current=min(roots)
        while True:
            path.append(current);nexts=[e['to'] for e in edges if e['from']==current]
            if not nexts:break
            current=min(nexts)
    return {'project_duration':duration,'activities':[{'id':t,'duration':tasks[t]['duration'],
             'earliest_start':earliest[t][0],'earliest_finish':earliest[t][1],
             'latest_start':latest[t][0],'latest_finish':latest[t][1],
             'total_float':latest[t][0]-earliest[t][0],
             'free_float':min((earliest[c][0] for c in children[t]),default=duration)-earliest[t][1]} for t in sorted(tasks)],
             'critical_activities':critical,'critical_edges':edges,'one_critical_path':path}
