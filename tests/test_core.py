import json
import os
import threading
from pathlib import Path
from unittest.mock import Mock
import httpx
import pytest
from backend import store,media,guide,providers,worker
from backend.models import Preferences,Settings
from backend.security import canonical_video,parse_qs,filter_cookies,public_url,Attention,InvalidOutput

URL='https://www.bilibili.com/video/BV1xx411c7mD?p=2'

def authored_result(plan, facts):
    from backend.writing import EMPTY, HEADINGS
    byid={f['fact_id']:f for f in facts}
    block=lambda ids:{'text':byid[ids[0]]['claim'] if ids else '建议在住宿处休息，疲劳时取消可选活动。','fact_ids':ids,'kind':'fact' if ids else 'planning'}
    plan['writing']={'overview':block([facts[0]['fact_id']]),'days':[{'day':d['day'],**{p:dict(block(d[p]),route_node_ids=[f"D{d['day']}-start"]) for p in ('上午','下午','晚上')}} for d in plan['days']], 'sections':{h:[] for h in HEADINGS}}
    for h,c in [('景点推荐','景点'),('美食建议','美食'),('住宿区域建议','住宿'),('交通建议','交通')]:
        plan['writing']['sections'][h]=[block([f['fact_id']]) for f in facts if f['category']==c][:6]
    return plan

def seed(tid,sid='source1',part=2):
    text='成都人民公园可以喝茶，早上去散步，茶馆需要现场确认座位。门票需要出行前核实。'
    meta={'url':URL,'bvid':'BV1xx411c7mD','part':part,'cid':'123','title':'成都旅行攻略','author':'样本作者','published':'20260801','duration':100,'views':100,'description':'成都旅行','fetched':store.now()}
    transcript={'text':text,'raw':text,'segments':[{'text':text,'start':0,'end':100}],'type':'subtitle','precision':'subtitle','complete':True,'hash':'testhash'}
    with store.db() as c: c.execute('INSERT INTO sources(id,task_id,canonical,meta,selected,status,complete,transcript) VALUES(?,?,?,?,1,?,1,?)',(sid,tid,URL,store.dumps(meta),'completed',store.dumps(transcript)))
    return store.sources(tid)[0]

def test_url_canonical_part():
    assert canonical_video(URL+'&spm_id_from=111')==URL
    assert canonical_video('av123')=='https://www.bilibili.com/video/av123?p=1'
    assert canonical_video('BV1xx411c7mD').endswith('?p=1')

@pytest.mark.parametrize('url',['http://127.0.0.1/x','file:///etc/passwd','https://evil.com/video/BV1xx411c7mD','https://www.bilibili.com.evil.com/video/BV1xx411c7mD','https://user:pass@www.bilibili.com/video/BV1xx411c7mD','https://www.bilibili.com/video/BV1xx411c7mD?p=0','https://www.bilibili.com/video/../../etc/passwd','https://www.bilibili.com/video/BV1xx411c7mD?p=no'])
def test_bad_urls(url):
    with pytest.raises(ValueError): canonical_video(url)

def test_private_dns(monkeypatch):
    monkeypatch.setattr('socket.getaddrinfo',lambda *a,**k:[(2,1,6,'',('127.0.0.1',443))])
    with pytest.raises(ValueError): public_url('https://api.example.com')

def test_cookie_filter():
    raw='.bilibili.com\tTRUE\t/\tTRUE\t0\tSESSDATA\tsecret\n.google.com\tTRUE\t/\tTRUE\t0\ttoken\tother\n'
    filtered=filter_cookies(raw)
    assert 'google' not in filtered and 'SESSDATA' in filtered

def test_encrypted_settings(client):
    r=client.put('/api/settings',json={'moss_key':'secret-test-token','llm_key':'other-secret'})
    assert r.status_code==200 and r.json()['moss_key_configured']
    assert 'secret-test-token' not in r.text and 'moss_key' not in client.get('/api/settings').json()
    assert b'secret-test-token' not in (store.DATA/'travel.sqlite3').read_bytes()
    assert os.stat(store.SECRETS/'master.key').st_mode&0o777==0o600
    client.put('/api/settings',json={'moss_key':''})
    assert store.settings()['moss_key']=='secret-test-token'

def test_origin_csrf(client):
    assert client.post('/api/tasks',json={'destination':'成都'},headers={'Origin':'https://evil.test'}).status_code==403
    assert client.post('/api/tasks',json={'destination':'成都'},headers={'x-travel-csrf':''}).status_code==403
    assert client.get('/api/settings',headers={'Host':'evil.test'}).status_code==403

def test_creation_validation_idempotency(client):
    assert client.post('/api/tasks',json={'destination':'  '}).status_code==422
    assert client.post('/api/tasks',json={'destination':'成都','days':31}).status_code==422
    a=client.post('/api/tasks',json={'destination':' 成都 '},headers={'idempotency-key':'one'}).json()
    b=client.post('/api/tasks',json={'destination':'成都'},headers={'idempotency-key':'one'}).json()
    assert a==b
    assert len(client.get('/api/tasks').json())==1
    assert store.task(a['id'])['prefs']['days']==3

def test_subtitle_coverage_union_and_order():
    text='这里是成都旅游攻略推荐人民公园散步喝茶'*10
    raw=json.dumps({'body':[{'from':0,'to':30,'content':text},{'from':20,'to':40,'content':text}]})
    with pytest.raises(ValueError,match='覆盖'): media.parse_subtitle(raw,100)
    good=json.dumps({'body':[{'from':0,'to':60,'content':text}]})
    assert media.parse_subtitle(good,100)['complete']
    unordered=json.dumps({'body':[{'from':30,'to':60,'content':text},{'from':10,'to':20,'content':text}]})
    with pytest.raises(ValueError,match='时间'): media.parse_subtitle(unordered,100)

def test_danmaku_never_subtitle():
    info={'subtitles':{'danmaku':[{'ext':'xml','data':'<d>成都旅行</d>'}],'zh-danmaku':[{'ext':'srt','data':'fake'}]},'duration':10}
    with pytest.raises(ValueError): media.subtitle(info,Settings().model_dump())

def test_vtt_parse():
    body='成都人民公园适合喝茶休息。'*12
    raw=f'WEBVTT\n\n00:00:00.000 --> 00:01:00.000\n{body}\n'
    assert media.parse_subtitle(raw,100)['segments'][0]['end']==60

@pytest.mark.parametrize('duration',[1,30,300,301,600,1800,1801])
def test_segments_cover_tail(duration):
    rows=list(media.boundaries(duration,300,2))
    assert rows[0][0]==0 and rows[-1][1]==duration
    assert all(b>a for a,b in rows)
    assert all(rows[i+1][0]<=rows[i][1] for i in range(len(rows)-1))

def test_overlap(): assert media.merge_text(['成都人民公园很好逛','公园很好逛建议早上去'])=='成都人民公园很好逛建议早上去'

def test_choose_limits_and_authors():
    config=Settings(max_videos=2,max_minutes=10,max_video_minutes=6).model_dump()
    rows=[{'id':str(i),'meta':{'title':'成都旅游攻略','description':'','duration':d,'published':'20260801','author':a,'views':100-i}} for i,(d,a) in enumerate([(300,'A'),(300,'A'),(300,'B'),(999,'C')])]
    chosen,reasons=media.choose(rows,'成都',config)
    assert chosen==['0','2'] and '上限' in reasons['3']

def test_source_selection_part_and_limits(client,task_id):
    source=seed(task_id); store.set_status(task_id,'selecting')
    assert client.post(f'/api/tasks/{task_id}/selection',json={'source_ids':['foreign']}).status_code==422
    assert client.post(f'/api/tasks/{task_id}/selection',json={'source_ids':[source['id']]}).status_code==200
    assert store.sources(task_id)[0]['meta']['part']==2
    assert store.task(task_id)['status']=='queued'

def test_invalid_fact_quote_and_number():
    seg={'text':'门票 20 元','chunk_id':'T1','start':0,'end':10}
    obj={'facts':[{'category':'费用','entity_name':'门票','claim':'门票 20 元','evidence_excerpt':'门票 20 元'}]}
    assert guide.validate_facts(obj,seg,'s','chunk')[0]['timing_precision']=='chunk'
    obj['facts'][0]['claim']='门票 200 元'
    with pytest.raises(InvalidOutput): guide.validate_facts(obj,seg,'s','chunk')
    obj['facts'][0]['evidence_excerpt']='门票 200 元'
    with pytest.raises(InvalidOutput): guide.validate_facts(obj,seg,'s','chunk')

def test_plan_citations():
    facts=[{'fact_id':'F1'}]
    with pytest.raises(InvalidOutput): guide.validate_plan({'days':[{'day':1,'上午':['F404'],'下午':[],'晚上':[]}]},facts,1)
    with pytest.raises(InvalidOutput): guide.validate_plan({'days':[]},facts,3)

def test_subtitle_skips_moss(monkeypatch,task_id):
    source=seed(task_id)
    source['transcript']=None; source['complete']=False
    sub={'text':'真实字幕','segments':[],'type':'subtitle','precision':'subtitle','complete':True}
    monkeypatch.setattr(media,'metadata',lambda *a:(source['meta'],{}))
    monkeypatch.setattr(media,'subtitle',lambda *a:sub)
    download=Mock(side_effect=AssertionError('字幕合格时不应下载'))
    monkeypatch.setattr(media,'download',download)
    worker.process_source(source,task_id,Settings().model_dump(),Preferences(destination='成都').model_dump())
    assert not download.called and store.sources(task_id)[0]['transcript']['type']=='subtitle'

def test_forced_moss(monkeypatch,task_id):
    source=seed(task_id); source['complete']=False; source['transcript']=None
    monkeypatch.setattr(media,'metadata',lambda *a:(source['meta'],{}))
    sub=Mock(side_effect=AssertionError('不应读字幕')); monkeypatch.setattr(media,'subtitle',sub)
    with pytest.raises(Attention,match='Moss'):
        worker.process_source(source,task_id,Settings().model_dump(),Preferences(destination='成都',transcription='moss').model_dump())
    assert not sub.called

def test_resource_atomic_limit(task_id):
    config=Settings(max_requests=5).model_dump()
    for _ in range(5): providers.reserve(task_id,config,'llm','test')
    with pytest.raises(Attention,match='请求'): providers.reserve(task_id,config,'llm','test')

def fake_http(monkeypatch,responses):
    monkeypatch.setattr(providers,'public_url',lambda v:v)
    client=Mock(); client.__enter__=Mock(return_value=client);client.__exit__=Mock(return_value=False)
    client.post.side_effect=responses
    monkeypatch.setattr(providers.httpx,'Client',lambda **kw:client)
    monkeypatch.setattr(providers,'interruptible_wait',lambda *a:None)
    return client

def cfg(): return Settings(llm_key='test',llm_base='https://api.example.test',llm_model='test',moss_key='test').model_dump()

def test_auth_no_retry(monkeypatch,task_id):
    http=fake_http(monkeypatch,[httpx.Response(401)])
    with pytest.raises(Attention,match='认证'): providers.request(cfg(),task_id,'llm','test',payload={'max_tokens':20})
    assert http.post.call_count==1

def test_timeout_unknown_no_replay(monkeypatch,task_id):
    http=fake_http(monkeypatch,[httpx.ReadTimeout('timeout')])
    with pytest.raises(Attention,match='未知'): providers.request(cfg(),task_id,'llm','test',payload={'max_tokens':20})
    assert http.post.call_count==1
    with store.db() as c: assert c.execute('SELECT status FROM usage').fetchone()[0]=='unknown'

def test_rate_limit_bounded(monkeypatch,task_id):
    http=fake_http(monkeypatch,[httpx.Response(429,headers={'Retry-After':'0'})]*3)
    with pytest.raises(Attention,match='限流'): providers.request(cfg(),task_id,'llm','test',payload={'max_tokens':20})
    assert http.post.call_count==3

def test_paid_result_cache(monkeypatch,task_id):
    response={'choices':[{'message':{'content':'ok'}}],'usage':{'total_tokens':8}}
    http=fake_http(monkeypatch,[httpx.Response(200,json=response)])
    for _ in range(2): assert providers.request(cfg(),task_id,'llm','test',payload={'max_tokens':20})==response
    assert http.post.call_count==1

def test_full_mock_guide_versions_exports(monkeypatch,client,task_id):
    from backend import editorial
    monkeypatch.setattr(editorial,'route',lambda tid,packet,*a:{'directory':{'places':[]},'route':{'days':[{'day':d,'activities':[],'legs':[]} for d in range(1,packet['preferences']['days']+1)]},'route_check':{'valid':True}})
    monkeypatch.setattr(editorial,'llm_json',lambda *a:{'valid':True,'issues':[],'evidence_gaps':[]})
    source=seed(task_id); raw=source['transcript']['text']; counts={'extraction':0,'plan':0}
    def fake_llm(config,tid,instruction,data):
        if '提取目的地' in instruction:
            counts['extraction']+=1
            return {'facts':[{'category':'景点','entity_name':'人民公园','claim':'成都人民公园可以喝茶','evidence_excerpt':'成都人民公园可以喝茶','evidence_id':'P1'}]}
        counts['plan']+=1
        fid=data['facts'][0]['fact_id']
        return authored_result({'days':[{'day':i+1,'上午':[fid] if i==0 else [],'下午':[],'晚上':[]} for i in range(data['preferences']['days'])],'conflicts':[]},data['facts'])
    monkeypatch.setattr(guide,'llm_json',fake_llm)
    guide.generate(task_id,cfg())
    assert store.task(task_id)['status']=='completed_with_warnings'
    result=client.get(f'/api/tasks/{task_id}/guides/0').json()
    assert '人民公园' in result['markdown'] and '资料未覆盖' in result['markdown']
    assert 'S1' in result['markdown'] and URL in result['markdown']
    assert len(result['content']['facts'])==1 and result['version']==1
    prefs=Preferences(destination='成都',days=3).model_dump()
    assert client.post(f'/api/tasks/{task_id}/regenerate',json=prefs).status_code==200
    guide.generate(task_id,cfg())
    assert counts['extraction']==1
    assert client.get(f'/api/tasks/{task_id}/guides/0').json()['version']==2
    assert client.get(f'/api/tasks/{task_id}/guides/1').json()['version']==1
    assert client.get(f'/api/tasks/{task_id}/export').status_code==200
    assert client.get('/api/sources/source1/transcript?format=txt').text==raw
    assert client.delete(f'/api/tasks/{task_id}').status_code==200
    with store.db() as c: assert c.execute('SELECT COUNT(*) FROM sources').fetchone()[0]==0

def test_all_empty_no_guide(monkeypatch,task_id):
    seed(task_id)
    monkeypatch.setattr(guide,'llm_json',lambda *a:{'facts':[]})
    with pytest.raises(ValueError,match='旅行事实'): guide.generate(task_id,cfg())
    with store.db() as c: assert c.execute('SELECT COUNT(*) FROM guides').fetchone()[0]==0

def test_cancel_and_unknown_retry(client,task_id):
    assert client.post(f'/api/tasks/{task_id}/cancel').status_code==200
    with pytest.raises(Exception): media.check(task_id)
    with store.db() as c: c.execute("INSERT INTO usage(id,task_id,provider,status,created) VALUES('req',?,'moss','unknown',?)",(task_id,store.now()))
    assert client.post(f'/api/tasks/{task_id}/retry',json={}).status_code==409
    assert client.post(f'/api/tasks/{task_id}/retry',json={'acknowledge_unknown':True}).status_code==200

def test_cleanup_policy(task_id):
    seed(task_id); folder=store.DATA/'media'/task_id; folder.mkdir(parents=True); (folder/'original.mp3').write_bytes(b'test')
    with store.db() as c: c.execute("UPDATE tasks SET status='completed',updated=? WHERE id=?",(store.now()-90000,task_id))
    worker.cleanup()
    assert not folder.exists() and store.sources(task_id)[0]['transcript']

def test_search_normalizes_http_and_deduplicates(monkeypatch,task_id):
    data={'entries':[{'url':URL.replace('https:','http:')},{'url':URL+'&tracking=yes'}]}
    monkeypatch.setattr(media,'ytdlp',lambda *a,**k:json.dumps(data))
    result=media.search('成都',[],task_id,Settings(search_groups=1).model_dump())
    assert result==[URL]

def test_paid_moss_model_and_multipart(monkeypatch,task_id,tmp_path):
    audio=tmp_path/'audio.mp3'; audio.write_bytes(b'test-audio')
    http=fake_http(monkeypatch,[httpx.Response(200,json={'text':'这是成都的旅行介绍'})])
    result=providers.request(cfg(),task_id,'moss','test',audio=audio,seconds=20)
    assert result['text']
    kwargs=http.post.call_args.kwargs
    assert kwargs['data']=={'model':'moss-transcribe-1.0','response_format':'json'}
    assert 'file' in kwargs['files'] and 'url' not in kwargs['data']

def test_restart_pauses_unknown_and_does_not_repeat(monkeypatch,task_id):
    with store.db() as c: c.execute("INSERT INTO usage(id,task_id,provider,status,created) VALUES('uncertain',?,'llm','submitting',?)",(task_id,store.now()))
    stop=threading.Event(); stop.set()
    worker.loop(stop)
    assert store.task(task_id)['status']=='needs_attention'
    with store.db() as c: assert c.execute('SELECT status FROM usage').fetchone()[0]=='unknown'

def test_worker_lock_prevents_duplicate(task_id):
    import fcntl
    with (store.DATA/'worker.lock').open('a+') as f:
        fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
        worker.loop()
    assert store.task(task_id)['status']=='queued'

def test_retry_idempotent_after_dispatch(client,task_id):
    store.set_status(task_id,'failed')
    headers={'idempotency-key':'same-retry'}
    assert client.post(f'/api/tasks/{task_id}/retry',json={},headers=headers).status_code==200
    assert client.post(f'/api/tasks/{task_id}/retry',json={},headers=headers).status_code==200

def test_regenerate_idempotent_after_dispatch(client,task_id):
    seed(task_id);store.set_status(task_id,'completed')
    headers={'idempotency-key':'same-regenerate'}
    for _ in range(2): assert client.post(f'/api/tasks/{task_id}/regenerate',json={'destination':'成都'},headers=headers).status_code==200

def test_partial_segment_retains_success(monkeypatch,task_id,tmp_path):
    source=seed(task_id);source['transcript']=None;source['complete']=False
    store.update_source(source['id'],transcript=None,complete=0)
    path=tmp_path/'original.mp3';path.write_bytes(b'media')
    monkeypatch.setattr(media,'metadata',lambda *a:(source['meta'],{}))
    monkeypatch.setattr(media,'download',lambda *a:(path,100))
    chunks=[]
    with store.db() as c:
        for i,(a,b) in enumerate([(0,51),(49,100)]):
            item={'id':f'ch{i}','source_id':source['id'],'start':a,'end':b,'hash':'hash','status':'completed' if i==0 else 'pending','text':'成都人民公园可以喝茶' if i==0 else None,'raw':'{}','error':'','path':str(path)}
            c.execute('INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?)',tuple(item.values()));chunks.append(item)
    monkeypatch.setattr(media,'chunks',lambda *a:chunks)
    calls=[]
    def fake_request(*args,**kwargs): calls.append(args); raise ValueError('模拟空语音')
    monkeypatch.setattr(worker,'request',fake_request)
    worker.process_source(source,task_id,cfg(),Preferences(destination='成都',transcription='moss').model_dump())
    result=store.sources(task_id)[0]
    assert len(calls)==1 and not result['complete']
    assert result['transcript']['missing']==[[49.0,100.0]]
    assert result['transcript']['segments'][0]['start']==0


def test_ffmpeg_real_chunking_tail_and_decode(task_id,tmp_path):
    source=seed(task_id)
    audio=tmp_path/'sample.wav'
    media.run([media.ffmpeg(),'-v','error','-y','-f','lavfi','-i','sine=frequency=440:duration=61','-ac','1','-ar','16000',str(audio)])
    assert 60.9 < media.probe(audio) < 61.1
    config=Settings(chunk_seconds=30,overlap_seconds=2).model_dump()
    rows=media.chunks(audio,61,source['id'],task_id,config)
    assert rows[-1]['end']==61 and len(rows)==3
    assert all(Path(c['path']).stat().st_size<config['chunk_mb']*1024**2 for c in rows)
    # A partially cleaned retry reconstructs only missing media; text remains intact.
    Path(rows[-1]['path']).unlink()
    restored=media.chunks(audio,61,source['id'],task_id,config)
    assert Path(restored[-1]['path']).exists()

def test_reject_constraints_conflict():
    plan={'days':[{'day':1,'上午':['F1'],'下午':[],'晚上':[]}]}
    with pytest.raises(InvalidOutput,match='不自驾'):
        guide.validate_plan(plan,[{'fact_id':'F1','claim':'建议租车自驾前往'}],1,{'notes':'不自驾'})
    with pytest.raises(InvalidOutput,match='少步行'):
        guide.validate_plan(plan,[{'fact_id':'F1','claim':'安排全天登山'}],1,{'notes':'少走路'})

def test_worker_full_destination_flow_mock(monkeypatch,task_id):
    from backend import editorial
    monkeypatch.setattr(editorial,'route',lambda tid,packet,*a:{'directory':{'places':[]},'route':{'days':[{'day':d,'activities':[],'legs':[]} for d in range(1,packet['preferences']['days']+1)]},'route_check':{'valid':True}})
    monkeypatch.setattr(editorial,'llm_json',lambda *a:{'valid':True,'issues':[],'evidence_gaps':[]})
    from backend import quality
    monkeypatch.setattr(quality,'llm_json',lambda *a:dict(scores=quality.WEIGHTS.copy(),critical_issues=[],issues=[],missing_topics=[]))
    sample=seed(task_id)
    with store.db() as c: c.execute('DELETE FROM sources WHERE task_id=?',(task_id,))
    monkeypatch.setattr(media,'search',lambda *a:[URL,URL])
    monkeypatch.setattr(media,'metadata',lambda *a:(sample['meta'],{}))
    monkeypatch.setattr(media,'subtitle',lambda *a:sample['transcript'])
    def llm(config,tid,instruction,data):
        if '提取目的地' in instruction: return {'facts':[{'category':'景点','entity_name':'人民公园','claim':'成都人民公园可以喝茶','evidence_excerpt':'成都人民公园可以喝茶','evidence_id':'P1'}]}
        return authored_result({'days':[{'day':i+1,'上午':[data['facts'][0]['fact_id']] if i==0 else [],'下午':[],'晚上':[]} for i in range(data['preferences']['days'])],'conflicts':[]},data['facts'])
    monkeypatch.setattr(guide,'llm_json',llm)
    worker.execute(task_id,cfg())
    assert store.task(task_id)['status']=='selecting'
    assert '缺少' in store.task(task_id)['error']
    # Explicit continuation after reviewing the partial candidate set.
    worker.execute(task_id,cfg())
    assert len(store.sources(task_id))==1
    assert store.task(task_id)['status']=='completed_with_warnings'
    with store.db() as c: assert c.execute('SELECT COUNT(*) FROM guides').fetchone()[0]==1

def test_fact_numeric_token_is_not_substring():
    seg={'text':'门票 200 元','chunk_id':'T1','start':0,'end':10}
    obj={'facts':[{'category':'费用','entity_name':'门票','claim':'门票 20 元','evidence_excerpt':'门票 200 元'}]}
    with pytest.raises(InvalidOutput): guide.validate_facts(obj,seg,'s','subtitle')

def test_distinct_facts_sharing_excerpt_have_distinct_ids():
    seg={'text':'人民公园可以喝茶也可以散步','chunk_id':'T1','start':0,'end':10}
    rows=[{'category':'景点','entity_name':'人民公园','claim':claim,'evidence_excerpt':seg['text']} for claim in ['人民公园可以喝茶','人民公园可以散步']]
    facts=guide.validate_facts({'facts':rows},seg,'s','subtitle')
    assert facts[0]['fact_id']!=facts[1]['fact_id']

@pytest.mark.parametrize('base,provider,expected',[
 ('https://api.example.test','llm','https://api.example.test/v1/chat/completions'),
 ('https://api.example.test/v1/','llm','https://api.example.test/v1/chat/completions'),
 ('https://open.bigmodel.cn/api/paas/v4','llm','https://open.bigmodel.cn/api/paas/v4/chat/completions'),
 ('https://api.example.test/compatible-mode/v1','llm','https://api.example.test/compatible-mode/v1/chat/completions'),
 ('https://api.example.test/v1/chat/completions','llm','https://api.example.test/v1/chat/completions'),
 ('https://api.mosi.cn','moss','https://api.mosi.cn/v1/audio/transcriptions'),
 ('https://api.mosi.cn/v1','moss','https://api.mosi.cn/v1/audio/transcriptions'),
])
def test_provider_base_paths(base,provider,expected):
    assert providers.endpoint(base,provider)==expected

def test_glm53_required_reasoning_payload(monkeypatch,task_id):
    config=cfg(); config.update(llm_base='https://open.bigmodel.cn/api/paas/v4',llm_model='glm-5.3-flash')
    http=fake_http(monkeypatch,[httpx.Response(200,json={'choices':[{'message':{'content':'连接成功'}}]})])
    providers.request(config,task_id,'llm','test',payload={'model':config['llm_model'],'messages':[{'role':'user','content':'test'}],'max_tokens':512})
    assert http.post.call_args.args[0]=='https://open.bigmodel.cn/api/paas/v4/chat/completions'
    assert http.post.call_args.kwargs['json']['thinking']=={'type':'enabled'}
    assert http.post.call_args.kwargs['json']['reasoning_effort']=='low'


def test_provider_error_redacts_saved_keys(monkeypatch,task_id):
    config=cfg(); config['llm_key']='unit-test-private-key'
    fake_http(monkeypatch,[httpx.Response(400,json={'error':{'message':'invalid unit-test-private-key'}})])
    with pytest.raises(Attention) as error:
        providers.request(config,task_id,'llm','test',payload={'max_tokens':20})
    assert 'unit-test-private-key' not in str(error.value)

def test_unused_untrusted_format_does_not_block_safe_audio(monkeypatch):
    def check_url(url,media=False):
        if 'unsafe' in url: raise ValueError('untrusted')
        return url
    monkeypatch.setattr(media,'public_url',check_url)
    info={'formats':[
      {'format_id':'audio-high','acodec':'aac','vcodec':'none','abr':128,'url':'https://upos.bilivideo.com/audio'},
      {'format_id':'audio-low','acodec':'aac','vcodec':'none','abr':64,'url':'https://upos.bilivideo.com/low'},
      {'format_id':'video','acodec':'none','vcodec':'avc','url':'https://unsafe.test/video'},
      {'format_id':'unsafe','acodec':'aac','vcodec':'none','abr':192,'url':'https://unsafe.test/audio'},
    ]}
    assert media.download_formats(info)==['audio-high','audio-low']

def test_passage_citations_use_saved_original():
    original='成都人民公园可以喝茶。门票出行前核实。'
    seg={'text':original,'chunk_id':'T1','start':0,'end':10,'passages':guide.passages(original)}
    obj={'facts':[{'category':'景点','entity_name':'人民公园','claim':'成都人民公园可以喝茶','evidence_id':'P1','evidence_excerpt':'这段模型改写不会被当作原文'}]}
    result=guide.validate_facts(obj,seg,'s','chunk')
    assert result[0]['evidence_excerpt']==original
    obj['facts'][0]['evidence_id']='P99'
    with pytest.raises(InvalidOutput): guide.validate_facts(obj,seg,'s','chunk')

def test_passages_keep_entire_original_text():
    original=('成都的茶馆。附近的小吃店！继续走走，看看街道。'*30)+'尾部未完句'
    assert ''.join(p['text'] for p in guide.passages(original))==original


def test_uncertain_fact_not_in_public_guide(task_id):
    source=seed(task_id)
    facts=guide.validate_facts({'facts':[{'category':'美食','entity_name':'未确认店名','claim':'成都人民公园可以喝茶','evidence_excerpt':'成都人民公园可以喝茶'}]}, {'text':source['transcript']['text'],'chunk_id':'T1','start':0,'end':100},source['id'],'subtitle')
    plan={'days':[{'day':1,'上午':[facts[0]['fact_id']],'下午':[],'晚上':[]}],'conflicts':[]}
    result=guide.render(Preferences(destination='成都',days=1).model_dump(),facts,[source],plan,[])
    itinerary=result.split('## 每日行程')[1].split('## 景点推荐')[0]
    food=result.split('## 美食建议')[1].split('## 住宿区域建议')[0]
    assert '未确认店名' not in itinerary
    assert '资料未覆盖' in itinerary
    assert '未确认店名' not in food
    assert '未确认店名' not in result
    assert facts[0]['issue_flags']


def test_selection_reserves_lodging_transport_sights_food():
    titles=['成都美食攻略']*10+['成都住宿攻略','成都交通攻略','成都景点攻略']
    rows=[{'id':str(i),'meta':{'title':title,'description':'','duration':300,'published':'20260901','author':str(i),'views':10000-i}} for i,title in enumerate(titles)]
    chosen,_=media.choose(rows,'成都',Settings(max_videos=5).model_dump())
    assert {'10','11','12'}.issubset(chosen)
    assert media.missing_topics([s for s in rows if s['id'] in chosen])==[]
    assert len(chosen)==5


def test_search_always_includes_four_core_topics(monkeypatch,task_id):
    calls=[]
    def fake(args,*rest):
        calls.append(args[-1]);return '{"entries":[]}'
    monkeypatch.setattr(media,'ytdlp',fake)
    media.search('成都',['美食'],task_id,Settings(search_groups=3).model_dump())
    assert len(calls)==4
    for word in ('住宿','交通','景点','美食'):assert any(word in q for q in calls)


def test_title_hints_do_not_claim_general_guide_covers_all():
    assert media.missing_topics([{'meta':{'title':'成都三日旅游攻略'}}])==['住','行','玩','吃']


def test_search_cache_reuses_results_and_scopes_login(monkeypatch,task_id):
    calls=[]
    def fake(*args,**kwargs):
        calls.append(args);return json.dumps({'entries':[{'url':URL}]})
    monkeypatch.setattr(media,'ytdlp',fake)
    config=Settings().model_dump()
    assert media.search('成都',[],task_id,config)==[URL]
    assert len(calls)==4
    assert media.search('成都',[],task_id,config)==[URL]
    assert len(calls)==4
    config['cookies']='different-session'
    media.search('成都',[],task_id,config)
    assert len(calls)==8


def test_search_rejection_cools_down_without_repeated_requests(monkeypatch,task_id):
    calls=[]
    def rejected(*args,**kwargs):
        calls.append(args);raise Attention('Bilibili 暂时拒绝请求')
    monkeypatch.setattr(media,'ytdlp',rejected)
    assert media.search('成都',[],task_id,Settings().model_dump())==[]
    assert len(calls)==1
    assert media.search('成都',[],task_id,Settings().model_dump())==[]
    assert len(calls)==1


def test_search_records_pacing_interval(monkeypatch,task_id):
    monkeypatch.setattr(media,'SEARCH_INTERVAL',30)
    monkeypatch.setattr(media,'ytdlp',lambda *a,**k:json.dumps({'entries':[{'url':URL}]}))
    before=store.now()
    assert media.search('成都',[],task_id,dict(Settings().model_dump(),evidence_queries=['成都 交通']))==[URL]
    with store.db() as c: next_at=c.execute("SELECT next_at FROM search_pacing WHERE scope='bilibili'").fetchone()[0]
    assert before+30<=next_at<=store.now()+30


def test_bilibili_search_uses_signed_api(monkeypatch):
    from backend import bilibili_search
    monkeypatch.setattr(bilibili_search.BilibiliBaseIE,'_sign_wbi',lambda self,params,q:dict(params,w_rid='signed'))
    fake=Mock();fake._download_json.return_value={'code':0,'data':{'result':[{'bvid':'BV1xx411c7mD','aid':123}]}}
    fake.url_result.side_effect=lambda url,*args:url
    assert list(bilibili_search.search_results(fake,'成都 住宿'))==['https://www.bilibili.com/video/BV1xx411c7mD']
    args,kwargs=fake._download_json.call_args
    assert args[0]=='https://api.bilibili.com/x/web-interface/wbi/search/type'
    assert kwargs['query']['w_rid']=='signed'
    fake._download_json.return_value={'code':-352}
    with pytest.raises(Exception,match='rejected'):list(bilibili_search.search_results(fake,'成都'))


def test_evidence_preview_folds_repeated_asr_without_changing_original():
    raw="对，"*80+"冰粉清爽"
    preview=guide.evidence_preview(raw)
    assert "重复片段已折叠" in preview and "冰粉清爽" in preview
    assert raw.count("对")==80


def test_extraction_preserves_valid_rows_when_model_adds_unknown_category(monkeypatch,task_id):
    source=seed(task_id)
    good={'category':'景点','entity_name':'人民公园','claim':'成都人民公园可以喝茶','evidence_id':'P1'}
    monkeypatch.setattr(guide,'llm_json',lambda *args:{'facts':[good,dict(good,category='购物')]})
    facts=guide.extract(source,task_id,cfg())
    assert len(facts)==1 and facts[0]['entity_name']=='人民公园'
    assert store.sources(task_id)[0]['status']=='completed'
    with store.db() as c:
        assert c.execute("select 1 from events where task_id=? and message like '%排除 1 条%'",(task_id,)).fetchone()


def test_model_overload_defers_instead_of_immediate_repost(monkeypatch,task_id):
    from backend.security import DeferredRetry
    http=fake_http(monkeypatch,[httpx.Response(429,json={'error':{'code':'1305','message':'模型访问量过大'}})])
    with pytest.raises(DeferredRetry):providers.request(cfg(),task_id,'llm','test',payload={'max_tokens':20})
    assert http.post.call_count==1
    with store.db() as c:assert c.execute('select status from usage').fetchone()[0]=='rate_limited'


def test_model_overload_persists_delayed_retry(monkeypatch,task_id):
    from backend.security import DeferredRetry
    stop=threading.Event()
    def overload(*args):
        stop.set();raise DeferredRetry('busy')
    monkeypatch.setattr(worker,'execute',overload)
    worker.loop(stop)
    assert store.task(task_id)['status']=='queued'
    with store.db() as c:
        row=c.execute('select * from task_retries where task_id=?',(task_id,)).fetchone()
        assert row['attempts']==1 and row['next_at']>store.now()+55
        assert not c.execute("select id from tasks where not exists(select 1 from task_retries r where r.task_id=tasks.id and r.next_at>?)",(store.now(),)).fetchone()


def test_plan_excludes_payment_cards_and_duplicate_visits():
    facts=[{'fact_id':'pay','category':'交通','entity_name':'公交卡','source_id':'s'}, {'fact_id':'park','category':'景点','entity_name':'公园','source_id':'s'}]
    plan={'days':[{'day':1,'上午':['pay','park'],'下午':['park'],'晚上':[]}],'conflicts':[]}
    cleaned=guide.validate_plan(plan,facts,1)
    assert cleaned['days'][0]['上午']==['park']
    assert cleaned['days'][0]['下午']==[]


def test_writer_rejects_unsupported_prose_numbers():
    from backend import writing
    fact={'fact_id':'F1','category':'景点','claim':'公园免费开放','evidence_excerpt':'公园免费开放。'}
    packet={'facts':[fact]}
    plan={'days':[{'day':1,'上午':['F1'],'下午':[],'晚上':[]}],'conflicts':[]}
    result=authored_result(plan,[fact])
    result['writing']['days'][0]['上午']['text']='公园门票99元'
    with pytest.raises(InvalidOutput,match='数字'):writing.validate_writing(result,packet,plan)


def test_writer_requires_prose_and_source_coverage():
    from backend import writing
    facts=[{'fact_id':'F1','category':'住宿','claim':'建议住少城','evidence_excerpt':'建议住少城。'}]
    plan={'days':[{'day':1,'上午':[],'下午':[],'晚上':[]}],'conflicts':[]}
    with pytest.raises(InvalidOutput,match='正文'):writing.validate_writing(plan,{'facts':facts},plan)
    result=authored_result(plan,facts);result['writing']['sections']['住宿区域建议']=[]
    with pytest.raises(InvalidOutput,match='住宿'):writing.validate_writing(result,{'facts':facts},plan)


def test_focused_evidence_keeps_original_sentences_and_numbers():
    from backend import writing
    f={'entity_name':'公园','claim':'公园门票20元','evidence_excerpt':'大家记得关注。公园门票20元。旁边有茶馆。今天好开心。'}
    focused=writing.focus_evidence(f)
    assert '公园门票20元。' in focused
    assert all(part in f['evidence_excerpt'] for part in focused.split(' […] '))

@pytest.mark.parametrize('pass_round,expected',[(1,5),(2,10),(99,15)])
def test_quality_expands_five_and_stops(monkeypatch,task_id,pass_round,expected):
    from backend import editorial
    monkeypatch.setattr(editorial,'triage',lambda *a:{'rules_version':'phase2-v1','search_complete':True,'repairable':[],'evidence_gaps':['成都交通衔接'],'queries':['成都交通']})
    from backend import quality
    sample=seed(task_id)
    with store.db() as c:
        c.execute('DELETE FROM sources WHERE task_id=?',(task_id,))
        for i in range(20):
            c.execute('INSERT INTO sources(id,task_id,canonical,meta,selected,status,transcript) VALUES(?,?,?,?,?,?,?)',(str(i),task_id,URL+str(i),store.dumps(sample['meta']),int(i<5),'completed' if i<5 else 'candidate',store.dumps(sample['transcript']) if i<5 else None))
    rounds=[]; processed=[]
    def generate(tid,config,quality_pending=False):
        rounds.append(len(quality.attempted(tid))); v=len(rounds)
        content={'writing_input':{'sources':[],'facts':[]},'writing':{},'warnings':[]}
        with store.db() as c:
            c.execute('INSERT INTO guides VALUES(?,?,?,?,?,?,?,?)',(tid,v,store.dumps(content),'draft',store.dumps(store.task(tid)['prefs']),'same-glm','test',store.now()))
            c.execute("UPDATE quality_cycles SET phase='review',version=? WHERE task_id=?",(v,tid))
    def llm(config,tid,prompt,data):
        scores=quality.WEIGHTS.copy()
        if len(rounds)<pass_round: scores['可执行性']=0
        return dict(scores=scores,critical_issues=[],issues=['需要补充交通资料'],missing_topics=['交通'])
    def process(s,tid,cfg,prefs):
        processed.append(s['id']); store.update_source(s['id'],status='completed',transcript=sample['transcript'],complete=1)
    monkeypatch.setattr(guide,'generate',generate)
    monkeypatch.setattr(quality,'llm_json',llm)
    monkeypatch.setattr(worker,'process_source',process)
    monkeypatch.setattr(media,'search',lambda *a:[])
    quality.run(task_id,cfg())
    assert len(quality.attempted(task_id))==expected
    assert rounds==list(range(5,expected+1,5))
    assert len(processed)==expected-5
    with store.db() as c:
        content=json.loads(c.execute('SELECT content FROM guides ORDER BY version DESC').fetchone()[0])
    assert content['quality_review']['passed']==(pass_round!=99)
    if pass_round==99: assert '15' in store.task(task_id)['error']


def test_quality_critical_failure_overrides_high_score():
    from backend import quality
    result=quality.validate_review(dict(scores=quality.WEIGHTS.copy(),critical_issues=['虚构价格'],issues=[],missing_topics=[]))
    assert result['score']==100 and not result['passed']
    with pytest.raises(InvalidOutput): quality.validate_review(dict(scores={'可执行性':100}))


def test_quality_failed_attempts_count_and_cap(task_id):
    from backend import quality
    sample=seed(task_id)
    with store.db() as c:
        for i in range(15):
            c.execute('INSERT INTO sources(id,task_id,canonical,meta,status) VALUES(?,?,?,?,?)',(str(i),task_id,URL+str(i),store.dumps(sample['meta']),'failed' if i<14 else 'candidate'))
    assert len(quality.attempted(task_id))==15
    quality.reserve(task_id,'source1')
    with pytest.raises(Attention,match='15'): quality.reserve(task_id,'14')


def test_quality_review_resume_preserves_draft(monkeypatch,task_id):
    from backend import quality
    from backend.security import DeferredRetry
    seed(task_id)
    calls=[]
    def fake_generate(tid,config,quality_pending=False):
        calls.append('generate')
        content={'writing_input':{'sources':[],'facts':[]},'writing':{},'warnings':[]}
        with store.db() as c:
            c.execute('INSERT INTO guides VALUES(?,?,?,?,?,?,?,?)',(tid,1,store.dumps(content),'draft','{}','glm','test',store.now()))
            c.execute("UPDATE quality_cycles SET phase='review',version=1 WHERE task_id=?",(tid,))
    def busy(*args): raise DeferredRetry('busy')
    monkeypatch.setattr(guide,'generate',fake_generate)
    monkeypatch.setattr(quality,'llm_json',busy)
    with pytest.raises(DeferredRetry): quality.run(task_id,cfg())
    monkeypatch.setattr(quality,'llm_json',lambda *a:dict(scores=quality.WEIGHTS.copy(),critical_issues=[],issues=[],missing_topics=[]))
    quality.run(task_id,cfg())
    assert calls==['generate']
    assert store.task(task_id)['status']=='completed'


def test_numeric_review_only_deferred_with_quality_gate():
    from backend import writing
    f={'fact_id':'F123abc','category':'景点','claim':'门票五十元','evidence_excerpt':'门票五十元'}
    plan={'days':[{'day':1,'上午':['F123abc'],'下午':[],'晚上':[]}],'conflicts':[]}
    result=authored_result(plan,[f]); result['writing']['days'][0]['上午']['text']='门票50元（F123abc），建议停留2小时'
    with pytest.raises(InvalidOutput):writing.validate_writing(result,{'facts':[f]},plan)
    value=writing.validate_writing(result,{'facts':[f]},plan,defer_numeric_review=True)
    block=value['days'][0]['上午']
    assert block['numeric_checks']==['2','50']
    assert 'F123abc' not in block['text']


def test_settings_output_limit_and_atomic_save(client):
    before=client.get('/api/settings').json()
    rejected=client.put('/api/settings',json={'output_tokens':16385,'max_llm_tokens':524288})
    assert rejected.status_code==422
    assert client.get('/api/settings').json()['max_llm_tokens']==before['max_llm_tokens']
    saved=client.put('/api/settings',json={'output_tokens':16384,'context_budget':65536,'max_llm_tokens':524288})
    assert saved.status_code==200
    actual=client.get('/api/settings').json()
    assert actual['output_tokens']==16384 and actual['max_llm_tokens']==524288


def test_only_duplicate_reference_prefix_is_normalized():
    from backend.writing import normalize_reference_prefixes
    packet={'facts':[{'fact_id':'Fed22c144bad7'}]}
    result={'writing':{'fact_ids':['Ffed22c144bad7','Fed22c144bad7','Fbad22c144bad7']}}
    assert normalize_reference_prefixes(result,packet)['writing']['fact_ids']==['Fed22c144bad7','Fed22c144bad7','Fbad22c144bad7']


def test_structured_issue_flags_preserve_uncertainty():
    flags=guide.normalize_issue_flags([{'type':'名称未确认','note':'转写存疑'},'需核实','需核实'])
    assert len(flags)==2 and all(isinstance(x,str) for x in flags)
    assert '转写存疑' in flags[0]
    assert guide.normalize_issue_flags('需核实')==['需核实']


@pytest.mark.parametrize('base,structured',[('https://open.bigmodel.cn/api/paas/v4',True),('https://example.com/v1',False)])
def test_llm_structured_output_parameter(monkeypatch,base,structured):
    sent=[]
    def fake(*a,**kw):
        sent.append(kw['payload'])
        return {'choices':[{'message':{'content':'{"issues":["正文含\\"引语\\""]}'}}]}
    monkeypatch.setattr(providers,'request',fake)
    result=providers.llm_json(dict(cfg(),llm_base=base),None,'返回 JSON',{})
    assert result['issues']==['正文含"引语"']
    assert (sent[0].get('response_format')=={'type':'json_object'})==structured


def test_review_v2_weights_bands_and_threshold():
    from backend import quality
    assert quality.WEIGHTS=={'可执行性':35,'需求匹配':20,'信息可信度':20,'路线合理性':15,'阅读与应变':10}
    for maximum in (35,20,15,10):
        for score in range(maximum+1):
            labels=[name for lower,name in quality.BANDS[maximum] if score>=lower]
            assert labels
        assert [name for _,name in quality.BANDS[maximum]]==['优秀','良好','基本合格','较弱','不合格']
    scores={'可执行性':30,'需求匹配':17,'信息可信度':17,'路线合理性':13,'阅读与应变':8}
    result=quality.validate_review(dict(scores=scores,critical_issues=[],issues=[],missing_topics=[]))
    assert result['score']==85 and result['passed']
    assert set(result['levels'].values())=={'良好'}
    assert result['rubric_version']=='travel-review-v2'
    scores=dict(scores,预算完整性=0)
    with pytest.raises(InvalidOutput): quality.validate_review(dict(scores=scores,critical_issues=[],issues=[],missing_topics=[]))


@pytest.mark.parametrize('dimension,score,level',[('可执行性',32,'优秀'),('可执行性',31,'良好'),('可执行性',21,'基本合格'),('可执行性',14,'较弱'),('可执行性',13,'不合格'),('路线合理性',14,'优秀'),('阅读与应变',8,'良好')])
def test_review_v2_band_boundaries(dimension,score,level):
    from backend import quality
    scores=dict(quality.WEIGHTS);scores[dimension]=score
    result=quality.validate_review(dict(scores=scores,critical_issues=[],issues=[],missing_topics=[]))
    assert result['levels'][dimension]==level


def test_historical_guide_compact_presentation():
    original='# 攻略\n\n## 每日行程\n活动 [S1](#source-S1)\n\n## 资料冲突与待核实项\n大量待核实材料\n\n## 来源列表\n### S1 · 视频标题\n作者：作者甲 · 发布日期：20260901 · 分 P：1\n[在 Bilibili 查看视频](https://www.bilibili.com/video/BV1xx411c7mD)\n文本来源：Moss 转写\n- 原始语音摘录\n\n## GLM 质量审核\n85/100\n'
    result=guide.public_markdown(original)
    assert '资料冲突与待核实项' not in result and '大量待核实' not in result
    assert '原始语音摘录' not in result and '文本来源' not in result and '分 P' not in result
    for retained in ('视频标题','作者甲','20260901','https://www.bilibili.com/video/BV1xx411c7mD','[S1](#source-S1)','85/100'): assert retained in result
    assert guide.public_markdown(result)==result


def test_completed_writing_checkpoint_survives_before_version_commit(monkeypatch,task_id):
    from backend import editorial
    seed(task_id)
    monkeypatch.setattr(editorial,'route',lambda tid,packet,*a:{'directory':{'places':[]},'route':{'days':[{'day':d,'activities':[],'legs':[]} for d in range(1,packet['preferences']['days']+1)]},'route_check':{'valid':True}})
    monkeypatch.setattr(editorial,'llm_json',lambda *a:{'valid':True,'issues':[],'evidence_gaps':[]})
    calls=[]
    def llm(config,tid,instruction,data):
        calls.append('extract' if '提取目的地' in instruction else 'write')
        if '提取目的地' in instruction:
            return {'facts':[{'category':'景点','entity_name':'人民公园','claim':'成都人民公园可以喝茶','evidence_excerpt':'成都人民公园可以喝茶','evidence_id':'P1'}]}
        fid=data['facts'][0]['fact_id']
        return authored_result({'days':[{'day':i+1,'上午':[fid],'下午':[],'晚上':[]} for i in range(data['preferences']['days'])],'conflicts':[]},data['facts'])
    monkeypatch.setattr(guide,'llm_json',llm)
    monkeypatch.setattr(guide,'check',lambda *a:(_ for _ in ()).throw(Attention('模拟保存版本前中断')))
    with pytest.raises(Attention): guide.generate(task_id,cfg())
    with store.db() as c: assert c.execute('SELECT count(*) FROM guides WHERE task_id=?',(task_id,)).fetchone()[0]==0
    monkeypatch.setattr(guide,'check',lambda *a:None)
    guide.generate(task_id,dict(cfg(),output_tokens=7000))
    assert calls==['extract','write']
