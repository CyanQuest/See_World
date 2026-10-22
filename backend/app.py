import json
import os
import secrets
import shutil
import sys
import wave
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, ValidationError
from . import store, media, worker, quality
from .models import Preferences, Settings
from .providers import request as provider_request
from .revision import select_baseline
from .security import canonical_video, filter_cookies, safe_error

CSRF = secrets.token_urlsafe(32)
TERMINAL = ('completed','completed_with_warnings','failed','cancelled','needs_attention')

def snapshot(config): return {k:v for k,v in config.items() if k not in ('moss_key','llm_key','cookies','moss_base','llm_base','llm_model','llm_provider')}

@asynccontextmanager
async def lifespan(app):
    store.init()
    yield

app = FastAPI(title='见世界 · 本机旅行资料工作台',lifespan=lifespan)

@app.middleware('http')
async def local_only(req: Request, call_next):
    host = req.url.hostname
    if host not in ('127.0.0.1','localhost','testserver'):
        return JSONResponse({'detail':'只允许本机访问'},status_code=403)
    origin=req.headers.get('origin')
    permitted={f'http://{h}:{p}' for h in ('localhost','127.0.0.1') for p in ('8765','5173')}
    if origin and origin not in permitted:
        return JSONResponse({'detail':'请求来源不被允许'},status_code=403)
    if req.url.path.startswith('/api') and req.method not in ('GET','HEAD','OPTIONS'):
        if not secrets.compare_digest(req.headers.get('x-travel-csrf',''),CSRF): return JSONResponse({'detail':'页面会话已更新，请刷新后再试'},status_code=403)
    response=await call_next(req)
    response.headers['X-Content-Type-Options']='nosniff'
    response.headers['Referrer-Policy']='no-referrer'
    response.headers['X-Frame-Options']='DENY'
    response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' data: https://*.hdslb.com; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    if req.url.path.startswith('/api'): response.headers['Cache-Control']='no-store'
    return response

@app.exception_handler(KeyError)
async def missing(req,exc): return JSONResponse({'detail':'记录不存在'},status_code=404)
@app.exception_handler(ValueError)
async def invalid(req,exc): return JSONResponse({'detail':str(exc)},status_code=422)

@app.get('/api/session')
def session(): return {'csrf':CSRF}

@app.get('/api/health')
def health():
    import yt_dlp.version
    try: ff=Path(media.ffmpeg()).is_file()
    except Exception: ff=False
    return {'ok':True,'python':sys.version.split()[0],'yt_dlp':yt_dlp.version.__version__,'ffmpeg':ff,'ffprobe':bool(shutil.which('ffprobe')),'probe_mode':'ffprobe' if shutil.which('ffprobe') else 'FFmpeg 音轨与时长检查','data_bytes':sum(p.stat().st_size for p in store.DATA.rglob('*') if p.is_file())}

@app.get('/api/settings')
def get_settings(): return store.settings(False)
@app.put('/api/settings')
def put_settings(value:dict):
    allowed=set(Settings.model_fields)
    if any(k not in allowed for k in value): raise ValueError('包含未知设置字段')
    if value.get('cookies'): value['cookies']=filter_cookies(value['cookies'])
    for key in ('moss_base','llm_base'):
        if value.get(key):
            p=urlparse(value[key])
            if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment: raise ValueError('API 地址需为不含密钥、查询参数的 HTTPS Base URL')
    try: store.save_settings(value)
    except ValidationError as exc: raise ValueError('设置值超出允许范围：'+', '.join(str(e['loc'][0]) if e['loc'] else e['msg'] for e in exc.errors()))
    return store.settings(False)

@app.delete('/api/settings/credentials/{name}')
def clear_credential(name:str):
    if name not in ('moss_key','llm_key','cookies'): raise ValueError('凭据名称无效')
    with store.db() as c:
        row=c.execute('SELECT value FROM settings WHERE id=1').fetchone()
        if row:
            value=json.loads(row['value']); value[name]=''
            c.execute('UPDATE settings SET value=? WHERE id=1',(store.dumps(value),))
    return {'ok':True}

class TestService(BaseModel):
    provider: str
    acknowledge_cost: bool = False

@app.post('/api/settings/test')
def test_service(value:TestService):
    if not value.acknowledge_cost: raise ValueError('连接测试会实际调用服务，需确认可能产生少量费用')
    config=store.settings()
    try:
        if value.provider=='moss':
            path=store.DATA/'connection-test.wav'
            with wave.open(str(path),'wb') as w:
                w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000); w.writeframes(b'\0' * 32000)
            try: result=provider_request(config,None,'moss','连接测试（1秒静音）',audio=path,seconds=1)
            finally: path.unlink(missing_ok=True)
            if not isinstance(result.get('text'),str): raise ValueError('响应没有 text 字段，请核对服务协议')
            return {'ok':True,'message':'转写测试请求成功；静音样本不代表真实语音识别效果已验证。'}
        if value.provider=='llm':
            result=provider_request(config,None,'llm','连接测试',payload={'model':config['llm_model'],'messages':[{'role':'user','content':'只回复：连接成功'}],'max_tokens':512})
            if not (result.get('choices') or [{}])[0].get('message',{}).get('content'): raise ValueError('响应不符合 Chat Completions 协议')
            return {'ok':True,'message':'模型测试请求成功；仅表示本次请求成功。'}
        raise ValueError('未知服务')
    except Exception as exc: raise HTTPException(400,safe_error(exc))

@app.post('/api/tasks',status_code=201)
def create_task(prefs:Preferences,req:Request):
    for link in prefs.links: canonical_video(link)
    idem=req.headers.get('idempotency-key') or store.uid()
    if len(idem)>128: raise ValueError('请求标识过长')
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        old=c.execute('SELECT id FROM tasks WHERE idem=?',(idem,)).fetchone()
        if old: return {'id':old['id']}
        tid=store.uid(); timestamp=store.now()
        c.execute('INSERT INTO tasks(id,idem,status,stage,prefs,limits,created,updated) VALUES(?,?,?,?,?,?,?,?)',(tid,idem,'queued','等待开始',store.dumps(prefs.model_dump()),store.dumps(snapshot(store.settings())),timestamp,timestamp))
    return {'id':tid}

@app.get('/api/tasks')
def list_tasks():
    with store.db() as c: rows=c.execute('SELECT t.*, (SELECT COUNT(*) FROM sources s WHERE s.task_id=t.id AND s.transcript IS NOT NULL) source_count,(SELECT MAX(version) FROM guides g WHERE g.task_id=t.id) version FROM tasks t ORDER BY created DESC').fetchall()
    return [{**{k:r[k] for k in ('id','status','stage','created','updated','error','source_count','version')},'prefs':json.loads(r['prefs'])} for r in rows]

def task_sources(c,tid):
    # Status view: no transcript or fact bodies and no long video description.
    rows=c.execute("SELECT id,task_id,canonical,json_remove(meta,'$.description') meta,selected,status,reason,error,complete,transcript IS NOT NULL AND transcript NOT IN ('','null','{}') has_transcript FROM sources WHERE task_id=? ORDER BY rowid",(tid,))
    return [dict(r,meta=json.loads(r['meta']) if r['meta'] else None,has_transcript=bool(r['has_transcript'])) for r in rows]

@app.get('/api/tasks/{tid}')
def get_task(tid:str):
    # Polled while a task runs, so it reads everything over one connection and writes nothing.
    with store.db() as c:
        row=c.execute('SELECT * FROM tasks WHERE id=?',(tid,)).fetchone()
        if not row: raise KeyError()
        result=dict(row,prefs=json.loads(row['prefs']),limits=json.loads(row['limits']))
        result['events']=[dict(x) for x in c.execute('SELECT * FROM events WHERE task_id=? ORDER BY id DESC LIMIT 100',(tid,))]
        result['versions']=[dict(x) for x in c.execute('SELECT version,created,model FROM guides WHERE task_id=? ORDER BY version DESC',(tid,))]
        result['usage']=[dict(x) for x in c.execute('SELECT provider,COUNT(*) requests,SUM(seconds) seconds,SUM(tokens) tokens,SUM(cost) cost,SUM(CASE WHEN tokens IS NULL THEN 1 ELSE 0 END) unknown_tokens,SUM(CASE WHEN cost IS NULL THEN 1 ELSE 0 END) unknown_cost FROM usage WHERE task_id=? GROUP BY provider',(tid,))]
        result['chunks']=[dict(x) for x in c.execute('SELECT ch.id,ch.source_id,ch.start,ch.end,ch.status,ch.error FROM chunks ch JOIN sources s ON s.id=ch.source_id WHERE s.task_id=? ORDER BY ch.start',(tid,))]
        used=quality.attempted_ids(c,tid)
        state=c.execute('SELECT * FROM quality_cycles WHERE task_id=?',(tid,)).fetchone()
        usage=c.execute('SELECT COUNT(*) n,COALESCE(SUM(seconds),0) seconds,COALESCE(SUM(COALESCE(tokens,reserved_tokens)),0) tokens FROM usage WHERE task_id=?',(tid,)).fetchone()
        result['version_selection']=select_baseline(tid,c)
        result['evidence_searches']=[json.loads(r[0]) for r in c.execute('SELECT value FROM evidence_searches WHERE task_id=?',(tid,))]
        result['revision_issues']=[json.loads(r[0]) for r in c.execute('SELECT value FROM revision_issues WHERE task_id=? ORDER BY version',(tid,))]
        metrics=c.execute('SELECT value FROM request_metrics WHERE task_id=?',(tid,)).fetchone()
        result['sources']=task_sources(c,tid)
    result['quality_cycle']=dict(state) if state else None
    if state:
        for key in ('triage','history'): result['quality_cycle'][key]=json.loads(state[key])
    cfg=result['limits']
    video_minutes=sum((s['meta'].get('actual_duration') or s['meta'].get('duration') or 0)/60 for s in result['sources'] if s['id'] in used)
    result['resources']=[{'label':label,'used':used_value,'limit':limit,'remaining':max(0,limit-used_value)} for label,used_value,limit in [
        ('累计视频',len(used),15),('视频总时长（分钟）',round(video_minutes,1),cfg['max_minutes']),('提交音频（分钟）',round(usage['seconds']/60,1),cfg['max_audio_minutes']),('请求次数',usage['n'],cfg['max_requests']),('任务 tokens（含未知请求预留）',usage['tokens'],cfg['max_llm_tokens'])]]
    result['request_metrics']=json.loads(metrics[0]) if metrics else None
    result['request_limits']={'output_tokens':cfg['output_tokens'],'context_budget':cfg['context_budget']}
    return result

@app.get('/api/tasks/{tid}/sources')
def get_sources(tid:str):
    with store.db() as c:
        if not c.execute('SELECT 1 FROM tasks WHERE id=?',(tid,)).fetchone(): raise KeyError()
        return task_sources(c,tid)

class Links(BaseModel): links:list[str]=Field(min_length=1,max_length=30)

def editable(tid):
    task=store.task(tid)
    if task['status'] not in (*TERMINAL,'selecting') or task['owner']:
        raise HTTPException(409,'任务正在处理，请等待当前处理停止后再操作')
    return task

@app.post('/api/tasks/{tid}/sources')
def add_links(tid:str,value:Links):
    editable(tid)
    links=[canonical_video(x) for x in value.links]
    if len(store.sources(tid))+len(links)>60: raise ValueError('候选视频最多 60 个')
    config=store.settings(); results=[]; errors=[]
    for link in links:
        try: results.append(worker.add_source(tid,link,config))
        except Exception as exc: errors.append(safe_error(exc))
    if results: store.set_status(tid,'selecting')
    return {'added':results,'errors':errors}

class Selection(BaseModel): source_ids:list[str]=Field(min_length=1,max_length=15)
@app.post('/api/tasks/{tid}/selection')
def select_sources(tid:str,value:Selection):
    task=editable(tid)
    config=store.settings(); sources=store.sources(tid); selected=set(value.source_ids)
    if not selected <= {s['id'] for s in sources}: raise ValueError('包含不属于本任务的视频')
    if len(selected)>min(5,config['max_videos']): raise ValueError('每批最多5个视频，并遵守设置中的更低数量限制')
    durations=[s['meta'].get('duration') or 0 for s in sources if s['id'] in selected]
    if any(d<=0 or d>config['max_video_minutes']*60 for d in durations) or sum(durations)>config['max_minutes']*60: raise ValueError('视频时长未知或超过单条/总时长限制')
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT status,owner FROM tasks WHERE id=?',(tid,)).fetchone()
        if current['owner'] or current['status'] not in (*TERMINAL,'selecting'): raise HTTPException(409,'任务状态已变化')
        c.execute('DELETE FROM quality_cycles WHERE task_id=?',(tid,))
        c.execute('UPDATE sources SET selected=0 WHERE task_id=?',(tid,))
        for sid in selected: c.execute('UPDATE sources SET selected=1 WHERE id=?',(sid,))
        c.execute("UPDATE tasks SET status='queued',action='new',error='',limits=?,updated=? WHERE id=?",(store.dumps(snapshot(config)),store.now(),tid))
    return {'ok':True}

@app.post('/api/tasks/{tid}/cancel')
def cancel(tid:str):
    task=store.task(tid)
    if task['status'] not in worker.ACTIVE and task['status']!='selecting': raise HTTPException(409,'此任务当前无需取消')
    store.set_status(tid,'cancelled','已停止后续调度。已提交的外部请求可能继续处理或计费。')
    return {'ok':True}

class Retry(BaseModel): acknowledge_unknown:bool=False
@app.post('/api/tasks/{tid}/retry')
def retry(tid:str,value:Retry,req:Request):
    key=req.headers.get('idempotency-key') or store.uid()
    with store.db() as c:
        operation=c.execute('SELECT task_id,kind FROM operations WHERE key=?',(key,)).fetchone()
        if operation:
            if operation['task_id']!=tid or operation['kind']!='retry': raise HTTPException(409,'请求标识已用于其他操作')
            return {'ok':True}
    task=editable(tid)
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM operations WHERE key=?',(key,)).fetchone(): return {'ok':True}
        unknown=c.execute("SELECT COUNT(*) FROM usage WHERE task_id=? AND status='unknown'",(tid,)).fetchone()[0]
        if unknown and not value.acknowledge_unknown: raise HTTPException(409,'请求结果未知，继续重试可能重复计费；请明确确认')
        c.execute('DELETE FROM task_retries WHERE task_id=?',(tid,))
        c.execute('INSERT INTO operations VALUES(?,?,?)',(key,tid,'retry'))
        c.execute("UPDATE usage SET status='acknowledged_unknown' WHERE task_id=? AND status='unknown'",(tid,))
        c.execute("UPDATE chunks SET status='pending' WHERE source_id IN (SELECT id FROM sources WHERE task_id=?) AND status IN ('failed','unknown')",(tid,))
        c.execute("UPDATE tasks SET status='queued',error='',limits=?,updated=? WHERE id=?",(store.dumps(snapshot(store.settings())),store.now(),tid))
    return {'ok':True}

@app.post('/api/tasks/{tid}/regenerate')
def regenerate(tid:str,prefs:Preferences,req:Request):
    key=req.headers.get('idempotency-key') or store.uid()
    with store.db() as c:
        operation=c.execute('SELECT task_id,kind FROM operations WHERE key=?',(key,)).fetchone()
        if operation:
            if operation['task_id']!=tid or operation['kind']!='regenerate': raise HTTPException(409,'请求标识已用于其他操作')
            return {'ok':True}
    task=editable(tid)
    if prefs.destination!=task['prefs']['destination']: raise ValueError('修改目的地请新建任务；重新生成仅调整旅行偏好')
    if not any(s['transcript'] and s['selected'] for s in store.sources(tid)): raise ValueError('尚无可复用的来源文本')
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM operations WHERE key=?',(key,)).fetchone(): return {'ok':True}
        c.execute('DELETE FROM quality_cycles WHERE task_id=?',(tid,))
        c.execute('INSERT INTO operations VALUES(?,?,?)',(key,tid,'regenerate'))
        c.execute("UPDATE tasks SET prefs=?,status='queued',action='regenerate',error='',limits=?,updated=? WHERE id=?",(store.dumps(prefs.model_dump()),store.dumps(snapshot(store.settings())),store.now(),tid))
    return {'ok':True}

@app.post('/api/tasks/{tid}/revise')
def revise(tid:str,req:Request):
    key=req.headers.get('idempotency-key') or store.uid()
    with store.db() as c:
        op=c.execute('SELECT * FROM operations WHERE key=?',(key,)).fetchone()
        if op:
            if op['task_id']!=tid or op['kind']!='revise': raise HTTPException(409,'请求标识已用于其他操作')
            return {'ok':True}
    editable(tid)
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT status,owner FROM tasks WHERE id=?',(tid,)).fetchone()
        if current['owner'] or current['status'] not in TERMINAL: raise HTTPException(409,'任务状态已变化')
        if c.execute("SELECT 1 FROM usage WHERE task_id=? AND status IN ('unknown','submitting')",(tid,)).fetchone(): raise HTTPException(409,'存在结果未知请求，请先检查服务商记录并使用继续操作')
        row=c.execute('SELECT version,content FROM guides WHERE task_id=? ORDER BY version DESC LIMIT 1',(tid,)).fetchone()
        if not row: raise ValueError('尚无可修改正文')
        content=json.loads(row['content']); feedback=content.get('quality_review')
        if not feedback: raise ValueError('请先完成已有正文审核')
        if feedback['passed']: raise ValueError('此版本已经通过审核')
        c.execute('INSERT OR IGNORE INTO quality_cycles(task_id,phase) VALUES(?,?)',(tid,'classify'))
        c.execute("UPDATE quality_cycles SET phase='classify',version=?,feedback=? WHERE task_id=?",(row['version'],store.dumps(feedback),tid))
        c.execute('INSERT INTO operations VALUES(?,?,?)',(key,tid,'revise'))
        c.execute("UPDATE tasks SET status='queued',action='revise',error='',limits=?,updated=? WHERE id=?",(store.dumps(snapshot(store.settings())),store.now(),tid))
    return {'ok':True}

# The page needs only these parts of a guide's content; the rest (writing input,
# all facts, route) stays internal and is several hundred KB per version.
GUIDE_FIELDS=('quality_review','warnings','source_ids')

@app.get('/api/tasks/{tid}/guides/{version}')
def get_guide(tid:str,version:int):
    fields=','.join(f"json_extract(content,'$.{k}') {k}" for k in GUIDE_FIELDS)
    with store.db() as c:
        row=c.execute(f'SELECT task_id,version,markdown,prefs,model,prompt_version,created,{fields} FROM guides WHERE task_id=? '+('ORDER BY version DESC LIMIT 1' if version==0 else 'AND version=?'),(tid,) if version==0 else (tid,version)).fetchone()
    if not row: raise KeyError()
    result={k:row[k] for k in row.keys() if k not in GUIDE_FIELDS}; result['prefs']=json.loads(result['prefs'])
    result['content']={k:json.loads(row[k]) for k in GUIDE_FIELDS if row[k] is not None}
    from .guide import public_markdown
    body,separator,appendix=result['markdown'].partition('\n## GLM 质量审核')
    result['markdown']=public_markdown(body.rstrip())
    result['review_markdown']=('## GLM 质量审核'+appendix) if separator else ''
    return result

@app.get('/api/tasks/{tid}/export')
def export(tid:str,format:str='md',version:int=0):
    if format!='md': raise ValueError('支持 Markdown 导出；PDF 请使用打印功能')
    guide=get_guide(tid,version)
    return Response(guide['markdown'],media_type='text/markdown; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="travel-guide-v{guide["version"]}.md"'})

@app.get('/api/sources/{sid}/transcript')
def transcript(sid:str,format:str='json'):
    with store.db() as c: row=c.execute('SELECT * FROM sources WHERE id=?',(sid,)).fetchone()
    if not row: raise KeyError()
    value=json.loads(row['transcript']) if row['transcript'] else None
    if format=='txt': return Response(value['text'] if value else '尚无文本',media_type='text/plain',headers={'Content-Disposition':'attachment; filename="transcript.txt"'})
    with store.db() as c: chunks=[{k:x[k] for k in ('id','start','end','status','text','raw','error','hash')} for x in c.execute('SELECT * FROM chunks WHERE source_id=? ORDER BY start',(sid,))]
    return {'meta':json.loads(row['meta']),'transcript':value,'facts':json.loads(row['facts']) if row['facts'] else None,'chunks':chunks}

@app.delete('/api/tasks/{tid}')
def delete_task(tid:str):
    editable(tid)
    with store.db() as c:
        c.execute('DELETE FROM operations WHERE task_id=?',(tid,)); c.execute('DELETE FROM tasks WHERE id=?',(tid,))
    folder=store.DATA/'media'/tid
    if folder.exists(): shutil.rmtree(folder)
    return {'ok':True}

DIST=store.ROOT/'frontend'/'dist'
if DIST.exists():
    app.mount('/assets',StaticFiles(directory=DIST/'assets'),name='assets')
    @app.get('/{path:path}')
    def spa(path:str):
        if path.startswith('api/'): raise HTTPException(404,'接口不存在')
        return FileResponse(DIST/'index.html')
