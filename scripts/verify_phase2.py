"""Resume the two acceptance tasks within their saved limits; never add allowance."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json
from backend import store,quality,revision
from backend.security import Attention,InvalidOutput,safe_error

IDS=('41219bc18e0b4a3eb6b30df88abf81fb','927d553df80c46d99b104174ca7831e3')

def counts(tid):
    with store.db() as c:
        return dict(c.execute('SELECT COUNT(*) requests,COALESCE(SUM(tokens),0) actual_tokens,COALESCE(SUM(COALESCE(tokens,reserved_tokens)),0) reserved_total FROM usage WHERE task_id=?',(tid,)).fetchone())

def main():
    store.init(); results=[]
    for tid in IDS:
        task=store.task(tid)
        with store.db() as c:
            uncertain=c.execute("SELECT 1 FROM usage WHERE task_id=? AND status IN ('unknown','submitting')",(tid,)).fetchone()
        if task['owner'] or uncertain: raise RuntimeError('任务正在运行或有未知请求，停止验收，不自动重发')
        sources_before={s['id']:revision.digest([s['transcript'],s['facts']]) for s in store.sources(tid)}
        before=counts(tid); config=store.settings(); config.update(task['limits'])
        try:
            quality.run(tid,config)
            stop=store.task(tid)['error']
        except (Attention,InvalidOutput) as exc:
            stop=safe_error(exc); store.set_status(tid,'needs_attention',stop)
        results.append({'task_id':tid,'selection':revision.select_baseline(tid),'before':before,'after':counts(tid),'source_payloads_unchanged':sources_before=={s['id']:revision.digest([s['transcript'],s['facts']]) for s in store.sources(tid)},'processed_videos':len(quality.attempted(tid)),'stop_reason':stop})
        print(json.dumps(results[-1],ensure_ascii=False),flush=True)
    Path('docs/第二阶段运行验证.json').write_text(json.dumps(results,ensure_ascii=False,indent=2))

if __name__=='__main__': main()
