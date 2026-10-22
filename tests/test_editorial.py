import json
import hashlib
import pytest
from backend import store, quality, editorial, guide, writing, media, worker
from backend.models import Settings
from backend.security import InvalidOutput


def saved(tid, score=70):
    review=quality.validate_review(dict(scores={'可执行性':25,'需求匹配':15,'信息可信度':15,'路线合理性':9,'阅读与应变':6},critical_issues=[],issues=['重排跨区活动'],missing_topics=[]))
    review['score']=score
    content={'writing':{},'writing_input':{'sources':[],'facts':[]},'warnings':[],'quality_review':review}
    with store.db() as c:
        c.execute('INSERT INTO guides VALUES(?,?,?,?,?,?,?,?)',(tid,1,store.dumps(content),'正文',store.dumps(store.task(tid)['prefs']),'glm','test',store.now()))
        c.execute("INSERT INTO quality_cycles(task_id,phase,version,feedback) VALUES(?,'classify',1,?)",(tid,store.dumps(review)))


def test_repair_before_search_and_no_retranscription(monkeypatch,task_id):
    saved(task_id)
    calls=[]
    monkeypatch.setattr(editorial,'triage',lambda *a:{'rules_version':'phase2-v1','search_complete':True,'repairable':['重排跨区活动'],'evidence_gaps':['待确认交通'],'queries':['成都公园交通']})
    def revise(tid,config,quality_pending=False,revision=None):
        assert revision==1
        calls.append('revise')
        with store.db() as c: c.execute("UPDATE quality_cycles SET phase='review',revision_round=1 WHERE task_id=?",(tid,))
    monkeypatch.setattr(guide,'generate',revise)
    monkeypatch.setattr(quality,'llm_json',lambda *a:dict(scores=quality.WEIGHTS,critical_issues=[],issues=[],missing_topics=[]))
    monkeypatch.setattr(media,'search',lambda *a:pytest.fail('should revise before search'))
    monkeypatch.setattr(worker,'process_source',lambda *a:pytest.fail('revision must not transcribe'))
    quality.run(task_id,Settings().model_dump())
    assert calls==['revise']


def test_no_gap_stops_without_more_video(monkeypatch,task_id):
    saved(task_id)
    monkeypatch.setattr(editorial,'triage',lambda *a:{'rules_version':'phase2-v1','search_complete':True,'repairable':['重复文字'],'evidence_gaps':[],'queries':[]})
    monkeypatch.setattr(media,'search',lambda *a:pytest.fail('no evidence gap'))
    quality.run(task_id,Settings(revision_rounds=0).model_dump())
    assert '未达标' in store.task(task_id)['error']


def test_checkpoint_survives_changed_limits(monkeypatch,task_id):
    calls=[]
    monkeypatch.setattr(editorial,'llm_json',lambda *a:calls.append(1) or {'valid':True,'issues':[],'evidence_gaps':[]})
    editorial.structured(task_id,'路线检查:one',editorial.Gate,'test',{},Settings().model_dump())
    editorial.structured(task_id,'路线检查:one',editorial.Gate,'test',{},Settings(output_tokens=7000).model_dump())
    assert calls==[1]


def test_unknown_reference_is_never_hidden():
    packet={'facts':[{'fact_id':'F1'}]}
    with pytest.raises(InvalidOutput,match='未发送'):
        writing.validate_writing({'writing':{'overview':{'text':'位置','fact_ids':['UNKNOWN']}}},packet,{},defer_numeric_review=True)


def test_strict_types():
    with pytest.raises(InvalidOutput): editorial.checked(editorial.Gate,{'valid':'false','issues':[],'evidence_gaps':[]})
    with pytest.raises(InvalidOutput): quality.validate_review([])


def test_review_hash_matches_export(client,monkeypatch,task_id):
    saved(task_id)
    captured=[]
    def llm(*args):
        captured.append(args[3]['markdown'])
        return dict(scores=quality.WEIGHTS,critical_issues=[],issues=[],missing_topics=[])
    monkeypatch.setattr(quality,'llm_json',llm)
    result=quality.review(task_id,1,Settings().model_dump())
    exported=client.get(f'/api/tasks/{task_id}/export').text.split('\n## GLM 质量审核',1)[0].rstrip()
    assert captured==[exported]
    assert result['body_sha256']==hashlib.sha256(exported.encode()).hexdigest()


def test_revision_api_retains_round_cap_and_history(client,task_id):
    saved(task_id)
    with store.db() as c:
        c.execute("UPDATE tasks SET status='completed_with_warnings' WHERE id=?",(task_id,))
        c.execute("UPDATE quality_cycles SET phase='done',revision_round=2 WHERE task_id=?",(task_id,))
    response=client.post(f'/api/tasks/{task_id}/revise',headers={'idempotency-key':'same'})
    assert response.status_code==200
    assert client.post(f'/api/tasks/{task_id}/revise',headers={'idempotency-key':'same'}).status_code==200
    with store.db() as c: assert c.execute('SELECT revision_round FROM quality_cycles WHERE task_id=?',(task_id,)).fetchone()[0]==2


def test_schema_retry_bounded_and_then_resume_cached(monkeypatch,task_id):
    calls=[]
    def llm(*args):
        calls.append(args[2])
        return {'valid':'yes' if len(calls)==1 else True,'issues':[],'evidence_gaps':[]}
    monkeypatch.setattr(editorial,'llm_json',llm)
    editorial.structured(task_id,'gate',editorial.Gate,'test',{},Settings().model_dump())
    editorial.structured(task_id,'gate',editorial.Gate,'test',{},Settings(output_tokens=7000).model_dump())
    assert len(calls)==2 and '结构检查失败' in calls[1]


def test_route_cannot_promote_unknown_place(monkeypatch,task_id):
    packet={'facts':[{'fact_id':'F1'}],'preferences':{'days':1}}
    def llm(cfg,tid,instruction,data,stage):
        if '建立地点目录' in instruction:
            return {'places':[{'place_id':'P1','name':'同名公园','aliases':[],'region':'','category':'景点','fact_ids':['F1'],'confirmed':False,'conflict':'归属不明'}]}
        return {'days':[{'day':1,'start':'待定','end':'待定','activities':[{'place_id':'P1','duration':'建议1小时','optional':False}],'legs':[],'meals_rest_checkin':'午餐休息入住','buffer':'20%','cut_order':'删可选','contingencies':'雨天留在住处'}],'rationale':'建议','evidence_gaps':[]}
    monkeypatch.setattr(editorial,'llm_json',llm)
    with pytest.raises(InvalidOutput,match='未确认'): editorial.route(task_id,packet,Settings().model_dump())
    with store.db() as c: assert c.execute('SELECT count(*) FROM guides WHERE task_id=?',(task_id,)).fetchone()[0]==0


def test_no_improvement_prevents_more_rewrites(monkeypatch,task_id):
    saved(task_id)
    with store.db() as c: c.execute('UPDATE quality_cycles SET revision_round=1,baseline_score=70 WHERE task_id=?',(task_id,))
    monkeypatch.setattr(editorial,'triage',lambda *a:{'rules_version':'phase2-v1','search_complete':True,'repairable':['仍需重排'],'evidence_gaps':[],'queries':[]})
    monkeypatch.setattr(guide,'generate',lambda *a,**kw:pytest.fail('score did not improve'))
    quality.run(task_id,Settings().model_dump())
    assert store.task(task_id)['status']=='completed_with_warnings'


def test_request_metrics_show_block_before_payment(task_id):
    from backend import providers
    with pytest.raises(InvalidOutput,match='上下文'):
        providers.llm_json(Settings(llm_model='glm').model_dump(),task_id,'x',{'text':'长'*30000},'提取事实')
    with store.db() as c:
        metrics=json.loads(c.execute('SELECT value FROM request_metrics WHERE task_id=?',(task_id,)).fetchone()[0])
        assert metrics['blocked'] and metrics['context_used']>metrics['context_limit']
        assert c.execute('SELECT count(*) FROM usage WHERE task_id=?',(task_id,)).fetchone()[0]==0


def test_review_appendix_separate_from_body(client,monkeypatch,task_id):
    saved(task_id)
    monkeypatch.setattr(quality,'llm_json',lambda *a:dict(scores=quality.WEIGHTS,critical_issues=[],issues=[],missing_topics=[]))
    quality.review(task_id,1,Settings().model_dump())
    row=client.get(f'/api/tasks/{task_id}/guides/1').json()
    assert row['markdown']=='正文'
    assert '100/100' in row['review_markdown']
    assert client.get(f'/api/tasks/{task_id}/export?version=1').text=='正文'


def test_initial_selection_never_exceeds_five():
    candidates=[{'id':str(i),'meta':{'title':'成都旅行','description':'','author':str(i),'duration':60,'published':'20260901'}} for i in range(15)]
    selected,_=media.choose(candidates,'成都',Settings(max_videos=15).model_dump())
    assert len(selected)==5


def test_export_hash_includes_canonical_source_whitespace(client,monkeypatch,task_id):
    saved(task_id)
    markdown='正文\n\n## 来源列表\n\n### S1 · 示例\n作者：作者 · 发布日期：20260901\n[在 Bilibili 查看视频](https://www.bilibili.com/video/BV1xx411c7mD)'
    with store.db() as c: c.execute('UPDATE guides SET markdown=? WHERE task_id=?',(markdown,task_id))
    monkeypatch.setattr(quality,'llm_json',lambda *a:dict(scores=quality.WEIGHTS,critical_issues=[],issues=[],missing_topics=[]))
    result=quality.review(task_id,1,Settings().model_dump())
    text=client.get(f'/api/tasks/{task_id}/export').text
    assert hashlib.sha256(text.encode()).hexdigest()==result['body_sha256']
    assert client.get(f'/api/tasks/{task_id}/guides/1').json()['markdown']==text


def test_editorial_stage_name_drops_attempt_number(monkeypatch,task_id):
    stages=[]
    monkeypatch.setattr(editorial,'llm_json',lambda *a:stages.append(a[4]) or {'valid':True,'issues':[],'evidence_gaps':[]})
    editorial.structured(task_id,'路线检查1:seed',editorial.Gate,'test',{},Settings().model_dump())
    assert stages==['路线检查']


def test_every_model_stage_has_output_limit():
    import re
    from pathlib import Path
    from backend import providers
    source=''.join(p.read_text() for p in Path(providers.__file__).parent.glob('*.py'))
    used=set(re.findall(r"(?:structured|cached_step)\((?:tid|task_id),f?'([^':{]+)",source))
    used|=set(re.findall(r"llm_json\([^\n]*,'([^']+)'\)",source))
    # Every stage named in code has a cap, and the table has no unused names.
    assert used==set(providers.OUTPUT_LIMITS)
