import json
import pytest
from backend import editorial, revision, writing, store
from backend.models import Settings
from backend.security import InvalidOutput, Attention


def sample_route():
    directory={'places':[{'place_id':p,'name':p,'confirmed':True} for p in ('A','B','C')]}
    day=lambda n:{'day':n,'start':'A','end':'A','activities':[{'place_id':'A','optional':False,'duration':'建议休息'}],'legs':[],'branches':[]}
    return {'days':[day(1),day(2)]},directory,{'preferences':{'days':2},'facts':[{'fact_id':'F1','evidence_excerpt':'A到B需要30分钟'}]}


@pytest.mark.parametrize('mutation,match',[
    (lambda r,d:r['days'][1].update(start='B'),'跨日断裂'),
    (lambda r,d:d['places'][0].update(confirmed=False),'未确认'),
    (lambda r,d:r['days'][0].update(end='UNKNOWN'),'未确认'),
])
def test_route_endpoint_guards(mutation,match):
    route,directory,packet=sample_route(); mutation(route,directory)
    with pytest.raises(InvalidOutput,match=match): editorial.validate_route(route,directory,packet)


def test_branch_must_cover_next_start_and_delete_real_node():
    route,directory,packet=sample_route()
    route['days'][0]['branches']=[{'branch_id':'rain','condition':'下雨','drop_node_ids':['D1-A1'],'end':'B','next_start':'B','next_day_action':'在B休息'}]
    with pytest.raises(InvalidOutput,match='次日未覆盖'): editorial.validate_route(route,directory,packet)
    route['days'][1]['accepted_starts']=['B']
    editorial.validate_route(route,directory,packet)
    route['days'][0]['branches'][0]['drop_node_ids']=[]
    with pytest.raises(InvalidOutput,match='实际删减'): editorial.validate_route(route,directory,packet)


def test_transport_known_time_and_context():
    route,directory,packet=sample_route()
    leg={'start':'A','end':'B','fact_ids':['F1'],'time_kind':'source','minutes':30,'evidence_start':'A','evidence_end':'C','time_context':'A到B需要30分钟'}
    route['days'][0]['legs']=[leg]
    with pytest.raises(InvalidOutput,match='起终点不匹配'): editorial.validate_route(route,directory,packet)
    leg['evidence_end']='B'; route['days'][0]['window_minutes']=20
    with pytest.raises(InvalidOutput,match='时间窗口'): editorial.validate_route(route,directory,packet)
    route['days'][0]['window_minutes']=None; leg.update(time_kind='unknown')
    with pytest.raises(InvalidOutput,match='精确分钟'): editorial.validate_route(route,directory,packet)


def prose():
    return {'overview':{'text':'概览','fact_ids':['F1']},'days':[{'day':d,**{p:{'text':f'第{d}天{p}不变','fact_ids':['F1']} for p in revision.PERIODS}} for d in range(1,5)],'sections':{}}


def test_patch_preserves_unrelated_days_and_rejects_targets():
    original=prose(); target='day:3:上午'
    patch={'patches':[{'target':target,'block':{'text':'建议在住处休息','fact_ids':[],'kind':'planning'},'reason':'删去重复提醒'}],'unresolved':[]}
    result=revision.apply_patch(original,patch,{target},{'F1'})
    for key,value in revision.blocks(original).items():
        if key!=target: assert revision.digest(value)==revision.digest(revision.blocks(result)[key])
    assert original==prose()
    patch['patches'][0]['target']='day:2:上午'
    with pytest.raises(InvalidOutput,match='越界'): revision.apply_patch(original,patch,{target},{'F1'})
    patch['patches'][0]['target']=target; patch['patches'][0]['block']['fact_ids']=['invented']
    with pytest.raises(InvalidOutput,match='未知引用'): revision.apply_patch(original,patch,{target},{'F1'})


def test_daily_link_cannot_reference_other_day():
    route,directory,packet=sample_route(); editorial.validate_route(route,directory,packet)
    p=prose(); p['days']=p['days'][:2]
    for d in p['days']:
        for period in revision.PERIODS: d[period]['route_node_ids']=[f"D{d['day']}-A1"]
    writing.validate_route_links(p,route)
    p['days'][0]['上午']['route_node_ids']=['D2-A1']
    with pytest.raises(InvalidOutput,match='其他日期'): writing.validate_route_links(p,route)


def add_raw(tid,text):
    with store.db() as c: c.execute('INSERT INTO sources(id,task_id,canonical,meta,transcript,status) VALUES(?,?,?,?,?,?)',('raw',tid,'url',store.dumps({'title':'字幕','url':'url'}),store.dumps({'text':text,'hash':'h'}),'partial'))


def issue():
    return {'issue_id':'I1','description':'甲城至乙城交通','terms':['甲城','乙城'],'days':[1],'targets':['day:1:上午'],'kind':'day','action':'核实','basis':'核心交通缺依据','impact':'第1天'}


def test_search_finds_raw_not_in_previous_packet_and_resumes(monkeypatch,task_id):
    add_raw(task_id,'未整理原文：甲城至乙城大巴全程约两小时。')
    calls=[]
    def model(*args):
        candidates=args[3]['candidates']; calls.append(1)
        return {'status':'sufficient','hit_ids':[candidates[0]['hit_id']],'conclusion':'原文支持全程时长','limitations':'视频时点','unresolved':''}
    monkeypatch.setattr(editorial,'llm_json',model)
    result=revision.search_existing(task_id,issue(),Settings().model_dump())
    assert result['complete'] and result['status']=='sufficient'
    assert result['hits'][0]['location']['start']==0
    assert result['scope']['sources']==['raw']
    revision.search_existing(task_id,issue(),Settings(output_tokens=7000).model_dump())
    assert len(calls)==1


def test_empty_search_is_traceable(monkeypatch,task_id):
    monkeypatch.setattr(editorial,'llm_json',lambda *a:pytest.fail('no candidate'))
    result=revision.search_existing(task_id,issue(),Settings().model_dump())
    assert result['complete'] and result['status']=='gap'
    with store.db() as c: assert json.loads(c.execute('SELECT value FROM evidence_searches').fetchone()[0])==result


def test_interrupted_search_is_not_gap(monkeypatch,task_id):
    add_raw(task_id,'甲城至乙城的交通待核实。')
    monkeypatch.setattr(editorial,'llm_json',lambda *a:(_ for _ in ()).throw(Attention('额度不足')))
    with pytest.raises(Attention): revision.search_existing(task_id,issue(),Settings().model_dump())
    with store.db() as c: result=json.loads(c.execute('SELECT value FROM evidence_searches').fetchone()[0])
    assert not result['complete'] and result['status']=='searching' and result['hits']


def test_regression_keeps_latest_and_prior_baseline(task_id):
    for version,score in [(5,72),(6,79),(7,73)]:
        content={'quality_review':{'score':score,'critical_issues':[],'rubric_version':'v2'}}
        with store.db() as c: c.execute('INSERT INTO guides VALUES(?,?,?,?,?,?,?,?)',(task_id,version,store.dumps(content),'正文',store.dumps(store.task(task_id)['prefs']),'glm','test',store.now()))
    selection=revision.select_baseline(task_id)
    assert selection['baseline_version']==6 and selection['latest_version']==7
    assert '73' in selection['baseline_reason'] and '79' in selection['baseline_reason']
    with store.db() as c: assert c.execute('SELECT COUNT(*) FROM guides').fetchone()[0]==3


def test_equal_score_with_verified_resolution_can_advance(task_id):
    for version in (1,2):
        content={'quality_review':{'score':79,'critical_issues':[],'rubric_version':'v2'}}
        if version==2: content['revision_comparison']={'resolved':['I1'],'regressed':[],'uncertain':False}
        with store.db() as c: c.execute('INSERT INTO guides VALUES(?,?,?,?,?,?,?,?)',(task_id,version,store.dumps(content),'正文',store.dumps(store.task(task_id)['prefs']),'glm','test',store.now()))
    assert revision.select_baseline(task_id)['baseline_version']==2


def test_cache_invalidates_changed_evidence_or_prompt(monkeypatch,task_id):
    calls=[]
    monkeypatch.setattr(editorial,'llm_json',lambda *a:calls.append(1) or {'valid':True,'issues':[],'evidence_gaps':[]})
    for prompt,data in [('p',{'a':1}),('p',{'a':1}),('new',{'a':1}),('new',{'a':2})]:
        editorial.structured(task_id,'gate',editorial.Gate,prompt,data,Settings().model_dump())
    assert len(calls)==3


def test_program_owns_node_ids_and_updates_branch_references():
    route,directory,packet=sample_route()
    day1,day2=route['days']
    day1['activities']=[{'place_id':'A','optional':False,'duration':'1h','node_id':'D1N1'},{'place_id':'B','optional':True,'duration':'1h','node_id':'D1-A1'}]
    day1['legs']=[{'start':'A','end':'B','fact_ids':[],'node_id':'leg_x','time_kind':'unknown','minutes':None}]
    day1['branches']=[{'branch_id':'rain','condition':'下雨','drop_node_ids':['D1N1','leg_x'],'end':'A','next_start':'A','next_day_action':'次日照常'}]
    day2['activities'][0]['node_id']='D1-A3'  # another day's ID copied into day 2
    editorial.validate_route(route,directory,packet)
    assert [a['node_id'] for a in day1['activities']]==['D1-A2','D1-A1']  # valid ID kept, invented one gets a free number
    assert day1['legs'][0]['node_id']=='D1-L1' and day2['activities'][0]['node_id']=='D2-A1'
    assert day1['branches'][0]['drop_node_ids']==['D1-A2','D1-L1']


def test_duplicate_valid_node_id_is_rejected_with_its_location():
    route,directory,packet=sample_route()
    route['days'][0]['activities']=[{'place_id':'A','optional':False,'duration':'1h','node_id':'D1-A1'},{'place_id':'B','optional':True,'duration':'1h','node_id':'D1-A1'}]
    with pytest.raises(InvalidOutput,match='第1天.*D1-A1'): editorial.validate_route(route,directory,packet)


def test_route_errors_name_day_node_and_expected_value():
    route,directory,packet=sample_route()
    leg={'start':'A','end':'B','fact_ids':['F1'],'time_kind':'source','minutes':30,'evidence_start':'A','evidence_end':'C','time_context':'A到B需要30分钟'}
    route['days'][0]['legs']=[leg]
    with pytest.raises(InvalidOutput,match='起终点不匹配：第1天交通 D1-L1.*A→B.*A→C'): editorial.validate_route(route,directory,packet)
    leg.update(evidence_end='B',time_kind='unknown')
    with pytest.raises(InvalidOutput,match='精确分钟：第1天交通 D1-L1.*30'): editorial.validate_route(route,directory,packet)
    route,directory,packet=sample_route(); route['days'][1]['start']='B'
    with pytest.raises(InvalidOutput,match='跨日断裂：第1天终点 A.*第2天起点 B'): editorial.validate_route(route,directory,packet)


def test_route_draft_errors_name_day_and_empty_fields(monkeypatch,task_id):
    packet={'facts':[{'fact_id':'F1'}],'preferences':{'days':1}}
    def llm(cfg,tid,instruction,data,stage):
        if stage=='地点整理': return {'places':[{'place_id':'P1','name':'公园','aliases':[],'region':'成都','category':'景点','fact_ids':['F1'],'confirmed':True,'conflict':''}]}
        return {'days':[{'day':1,'start':'P1','end':'P1','activities':[],'legs':[],'meals_rest_checkin':'午餐','buffer':' ','cut_order':'删可选','contingencies':'雨天休息'}],'rationale':'建议','evidence_gaps':[]}
    monkeypatch.setattr(editorial,'llm_json',llm)
    with pytest.raises(InvalidOutput,match='第1天.*buffer'): editorial.route(task_id,packet,Settings().model_dump())


def test_route_reports_every_problem_in_one_error():
    route,directory,packet=sample_route()
    route['days'][0]['activities']=[{'place_id':'B','optional':False,'duration':'1h'}]  # A→B→A, no legs
    route['days'][1]['start']='C'; route['days'][1]['end']='C'                          # cross-day break, C→A→C, no legs
    with pytest.raises(InvalidOutput) as error: editorial.validate_route(route,directory,packet)
    message=str(error.value)
    assert message.startswith('共 5 处问题，请一次全部修正：')
    for part in ('第1天 A→B','第1天 B→A','跨日断裂：第1天终点 A','第2天 C→A','第2天 A→C'): assert part in message


def test_route_problem_list_is_capped_at_twenty():
    route,directory,packet=sample_route()
    directory['places']+=[{'place_id':f'P{i}','name':f'P{i}','confirmed':True} for i in range(25)]
    route['days'][0]['activities']=[{'place_id':f'P{i}','optional':False,'duration':'1h'} for i in range(25)]  # 26 distinct missing legs
    with pytest.raises(InvalidOutput) as error: editorial.validate_route(route,directory,packet)
    message=str(error.value)
    assert message.startswith('共 26 处问题') and '20. ' in message and '21. ' not in message and '另有 6 处未列出' in message


def test_route_draft_combines_draft_and_structure_problems_and_states_adjacency_rule(monkeypatch,task_id):
    packet={'facts':[{'fact_id':'F1'}],'preferences':{'days':1}}
    prompts=[]
    def llm(cfg,tid,instruction,data,stage):
        if stage=='地点整理': return {'places':[{'place_id':p,'name':p,'aliases':[],'region':'成都','category':'景点','fact_ids':['F1'],'confirmed':True,'conflict':''} for p in ('P1','P2')]}
        prompts.append(instruction)
        return {'days':[{'day':1,'start':'P1','end':'P1','activities':[{'place_id':'P2','duration':'1h','optional':False}],'legs':[],'meals_rest_checkin':'午餐','buffer':' ','cut_order':'删可选','contingencies':'雨天休息'}],'rationale':'建议','evidence_gaps':[]}
    monkeypatch.setattr(editorial,'llm_json',llm)
    with pytest.raises(InvalidOutput) as error: editorial.route(task_id,packet,Settings().model_dump())
    message=str(error.value)
    assert '共 3 处问题' in message and 'buffer' in message and '第1天 P1→P2' in message and '第1天 P2→P1' in message
    assert '相邻两点' in prompts[0] and '共 3 处问题' in prompts[1]  # the retry sees every problem
