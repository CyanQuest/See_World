"""Exercise the running local app using its saved credentials; never print secrets."""
import argparse
import json
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
import httpx

parser=argparse.ArgumentParser()
parser.add_argument('action',choices=['test','start','retry','status','export'])
parser.add_argument('--task')
parser.add_argument('--provider',choices=['moss','llm'])
args=parser.parse_args()
if args.action!='test' and not args.task:
    parser.error('--task is required to avoid operating on an unrelated recent task')
with httpx.Client(base_url='http://127.0.0.1:8765',timeout=960,trust_env=False) as client:
    client.headers['x-travel-csrf']=client.get('/api/session').raise_for_status().json()['csrf']
    if args.action=='test':
        for provider in ([args.provider] if args.provider else ('llm','moss')):
            result=client.post('/api/settings/test',json={'provider':provider,'acknowledge_cost':True})
            print(json.dumps({'provider':provider,'http_status':result.status_code,'result':result.json()},ensure_ascii=False),flush=True)
    else:
        task_id=args.task
        task=client.get('/api/tasks/'+task_id).raise_for_status().json()
        if args.action=='start':
            selected=[s['id'] for s in task['sources'] if s['selected']]
            response=client.post('/api/tasks/'+task_id+'/selection',json={'source_ids':selected})
            print(json.dumps({'task_id':task_id,'selected':len(selected),'http_status':response.status_code,'result':response.json()},ensure_ascii=False))
        elif args.action=='retry':
            response=client.post('/api/tasks/'+task_id+'/retry',json={})
            print(json.dumps({'task_id':task_id,'http_status':response.status_code,'result':response.json()},ensure_ascii=False))
        elif args.action=='status':
            print(json.dumps({'task_id':task_id,'status':task['status'],'stage':task['stage'],'error':task['error'],'sources':[{'id':s['id'],'title':s['meta']['title'],'status':s['status'],'error':s['error'],'has_transcript':s['has_transcript'],'selected':s['selected']} for s in task['sources'] if s['selected']],'chunks':task['chunks'],'usage':task['usage'],'versions':task['versions'],'events':task['events'][:8]},ensure_ascii=False))
        elif args.action=='export':
            response=client.get('/api/tasks/'+task_id+'/export').raise_for_status()
            output=Path(__file__).resolve().parent.parent/'docs'/'成都_真实生成示例.md'
            output.write_text(response.text)
            print(output)
