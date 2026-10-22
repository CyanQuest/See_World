import concurrent.futures
import fcntl
import hashlib
import json
import os
import shutil
import threading
import time
from pathlib import Path
from . import store, media
from .guide import generate
from .providers import request
from .security import Attention, DeferredRetry, Cancelled, safe_error

ACTIVE = ('queued','searching','processing','generating')

def add_source(task_id, url, config, selected=False):
    canonical = media.canonical_video(url)
    existing = next((s for s in store.sources(task_id) if s['canonical']==canonical), None)
    if existing: return existing['id']
    meta, _ = media.metadata(url,task_id,config)
    sid=store.uid()
    with store.db() as c:
        c.execute('INSERT OR IGNORE INTO sources(id,task_id,canonical,meta,selected) VALUES(?,?,?,?,?)',(sid,task_id,meta['url'],store.dumps(meta),int(selected)))
        row=c.execute('SELECT id FROM sources WHERE task_id=? AND canonical=?',(task_id,meta['url'])).fetchone()
    return row['id']

def transcribe_chunk(chunk, task_id, config, stop):
    if chunk['status']=='completed': return
    if stop.is_set(): return
    media.check(task_id)
    if chunk['status'] in ('submitting','unknown'): raise Attention('有分片请求结果未知，重试可能重复计费；请先检查服务记录')
    with store.db() as c: c.execute("UPDATE chunks SET status='submitting',error='' WHERE id=?",(chunk['id'],))
    try:
        result=request(config,task_id,'moss','音频分片转写',audio=chunk['path'],seconds=chunk['end']-chunk['start'])
        text=result.get('text')
        if not isinstance(text,str) or len(text.strip())<4: raise ValueError('转写为空或没有足够有效语音')
        with store.db() as c: c.execute("UPDATE chunks SET status='completed',text=?,raw=?,error='' WHERE id=?",(text.strip(),store.dumps(result),chunk['id']))
        store.event(task_id,'转写音频',f"已完成音频大致位置 {int(chunk['start'])}–{int(chunk['end'])} 秒",chunk['source_id'])
    except Cancelled:
        with store.db() as c: c.execute("UPDATE chunks SET status='unknown',error='任务取消，外部请求可能仍在处理' WHERE id=? AND status='submitting'",(chunk['id'],))
        stop.set(); raise
    except Attention as exc:
        unknown='未知' in str(exc) or '计费' in str(exc)
        with store.db() as c: c.execute('UPDATE chunks SET status=?,error=? WHERE id=?',('unknown' if unknown else 'failed',safe_error(exc),chunk['id']))
        stop.set(); raise
    except Exception as exc:
        with store.db() as c: c.execute("UPDATE chunks SET status='failed',error=? WHERE id=?",(safe_error(exc),chunk['id']))

def process_source(source, task_id, config, prefs):
    if source['transcript'] and source['complete']: return
    store.update_source(source['id'],status='processing',error='')
    store.event(task_id,'获取文本','正在读取视频字幕与元信息',source['id'])
    meta,info=media.metadata(source['meta']['url'],task_id,config)
    store.update_source(source['id'],meta=meta)
    if prefs['transcription']=='subtitle' and not source['transcript']:
        try:
            text=media.subtitle(info,config)
            store.update_source(source['id'],transcript=text,complete=1,status='completed'); return
        except Cancelled: raise
        except Exception:
            store.event(task_id,'获取文本','没有合格中文字幕，转为下载音频并使用 Moss',source['id'],level='warning')
    if not config['moss_key']: raise Attention('此视频需要语音转写，请先配置 Moss API Key')
    store.event(task_id,'下载音频','正在下载并检查完整音轨',source['id'])
    path,duration=media.download(source,info,task_id,config)
    other_duration=sum((s['meta'].get('actual_duration') or s['meta'].get('duration') or 0) for s in store.sources(task_id) if s['selected'] and s['id']!=source['id'])
    if other_duration+duration>config['max_minutes']*60+1: raise Attention('实际媒体总时长超过任务限制，请调整限制或减少视频')
    meta['actual_duration']=duration
    store.update_source(source['id'],meta=meta)
    chunk_list=media.chunks(path,duration,source['id'],task_id,config)
    store.event(task_id,'转写音频',f'音频已分为 {len(chunk_list)} 段；显示的是应用处理进度',source['id'])
    stop=threading.Event(); attention=None
    # Bounded batches ensure an authentication failure stops future submissions.
    with concurrent.futures.ThreadPoolExecutor(max_workers=config['transcribe_concurrency']) as executor:
        for offset in range(0,len(chunk_list),config['transcribe_concurrency']):
            if stop.is_set(): break
            pending=[executor.submit(transcribe_chunk,ch,task_id,config,stop) for ch in chunk_list[offset:offset+config['transcribe_concurrency']]]
            for future in concurrent.futures.as_completed(pending):
                try: future.result()
                except (Attention,Cancelled) as exc: attention=exc; stop.set()
    with store.db() as c: rows=[dict(x) for x in c.execute('SELECT * FROM chunks WHERE source_id=? ORDER BY start',(source['id'],))]
    valid=[x for x in rows if x['status']=='completed']; missing=[[x['start'],x['end']] for x in rows if x['status']!='completed']
    if valid:
        merged=media.merge_text([x['text'] for x in valid])
        transcript={'type':'moss','precision':'chunk','language':'自动识别','text':merged,'raw':'\n'.join(x['text'] for x in valid),'segments':[{'start':x['start'],'end':x['end'],'text':x['text'],'chunk_id':x['id']} for x in valid],'missing':missing,'complete':not missing,'hash':hashlib.sha256(merged.encode()).hexdigest()}
        store.update_source(source['id'],transcript=transcript,complete=int(not missing),status='completed' if not missing else 'partial')
    if attention: raise attention
    if not valid: raise ValueError('此视频的全部音频分片均未取得有效转写')

def execute(task_id, config):
    task=store.task(task_id); prefs=task['prefs']
    from . import quality
    with store.db() as c: cycle=c.execute('SELECT 1 FROM quality_cycles WHERE task_id=?',(task_id,)).fetchone()
    if cycle or task['action']=='regenerate': quality.run(task_id,config); return
    current=store.sources(task_id)
    if not current:
        store.set_status(task_id,'searching')
        links=prefs.get('links') or media.search(prefs['destination'],prefs['interests'],task_id,config)
        for i,url in enumerate(links):
            media.check(task_id)
            store.event(task_id,'筛选视频',f'正在检查第 {i+1}/{len(links)} 个候选视频')
            try: add_source(task_id,url,config)
            except Cancelled: raise
            except Exception as exc: store.event(task_id,'筛选视频',safe_error(exc),level='warning')
        current=store.sources(task_id)
        if not current:
            with store.db() as c: warning=c.execute("SELECT message FROM events WHERE task_id=? AND level='warning' ORDER BY id DESC LIMIT 1",(task_id,)).fetchone()
            store.set_status(task_id,'selecting',((warning['message']+'。') if warning else '')+'未找到可用视频，请补充标准 Bilibili 视频链接'); return
        selected,reasons=media.choose(current,prefs['destination'],config)
        if prefs.get('links'):
            selected=[]; total=0
            for s in current:
                d=s['meta'].get('duration') or 0
                if 0<d<=config['max_video_minutes']*60 and total+d<=config['max_minutes']*60 and len(selected)<min(5,config['max_videos']):
                    selected.append(s['id']); total+=d; reasons[s['id']]='用户提供的视频链接'
        for s in current: store.update_source(s['id'],selected=int(s['id'] in selected),reason=reasons[s['id']])
        missing = media.missing_topics([s for s in current if s['id'] in selected])
        if missing and not prefs.get('links'):
            message = '候选资料缺少「'+'、'.join(missing)+'」类视频，未开始付费处理。请补充相关视频或调整选择；标题覆盖还需原文验证。'
            store.event(task_id,'筛选视频',message,level='warning')
            store.set_status(task_id,'selecting',message); return
        if prefs['selection']=='manual' or not selected:
            store.set_status(task_id,'selecting','' if selected else '没有视频符合自动选择规则，请手动确认'); return
    if not config['llm_key'] or not config['llm_base'] or not config['llm_model']:
        raise Attention('视频已找到，请在设置中配置攻略模型后继续；需要音频转写时也请配置 Moss')
    store.set_status(task_id,'processing')
    for source in store.sources(task_id):
        if not source['selected']: continue
        media.check(task_id)
        quality.reserve(task_id,source['id'])
        try: process_source(source,task_id,config,prefs)
        except (Cancelled,Attention): raise
        except Exception as exc:
            store.update_source(source['id'],status='failed',error=safe_error(exc))
            store.event(task_id,'获取文本',safe_error(exc),source['id'],level='warning')
    if not any(s['selected'] and s['transcript'] for s in store.sources(task_id)): raise ValueError('没有获得有效字幕或转写，请检查失败摘要并补充视频链接')
    quality.run(task_id,config)

def cleanup():
    base=store.DATA/'media'
    if not base.exists(): return
    for folder in base.iterdir():
        try: task=store.task(folder.name)
        except KeyError: continue
        if task['limits'].get('retain_media') or task['status'] in ACTIVE: continue
        age=store.now()-task['updated']
        ttl=86400 if task['status'].startswith('completed') else 7*86400
        if age>ttl: shutil.rmtree(folder)

def loop(stop=None):
    store.init(); stop=stop or threading.Event()
    # One full task per local app. OS releases this lock on a crash.
    lock=(store.DATA/'worker.lock').open('a+')
    try: fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError: lock.close(); return
    owner=store.uid()
    with store.db() as c:
        c.execute("UPDATE usage SET status='unknown' WHERE status='submitting'")
        c.execute("UPDATE chunks SET status='unknown',error='应用重启前已提交，结果未知' WHERE status='submitting'")
        c.execute("UPDATE tasks SET lease=0,owner='' WHERE status IN ('queued','searching','processing','generating')")
        c.execute("UPDATE tasks SET status='needs_attention',error='应用重启前有外部请求结果未知，请检查服务商记录；继续可能重复计费' WHERE status IN ('queued','searching','processing','generating') AND id IN (SELECT task_id FROM usage WHERE status='unknown')")
    try:
        while not stop.is_set():
            with store.db() as c:
                c.execute('BEGIN IMMEDIATE')
                row=c.execute("SELECT id FROM tasks WHERE status IN ('queued','searching','processing','generating') AND lease<? AND NOT EXISTS(SELECT 1 FROM task_retries r WHERE r.task_id=tasks.id AND r.next_at>?) ORDER BY created LIMIT 1",(store.now(),store.now())).fetchone()
                if row: c.execute('UPDATE tasks SET lease=?,owner=? WHERE id=?',(store.now()+20,owner,row['id']))
            if not row:
                cleanup()
                stop.wait(2); continue
            task_id=row['id']; done=threading.Event()
            def heartbeat():
                while not done.wait(3):
                    with store.db() as c: c.execute('UPDATE tasks SET lease=?,updated=? WHERE id=? AND owner=?',(store.now()+20,store.now(),task_id,owner))
            beat=threading.Thread(target=heartbeat,daemon=True); beat.start()
            try:
                task=store.task(task_id); config=store.settings(); config.update(task['limits'])
                execute(task_id,config)
                with store.db() as c: c.execute('DELETE FROM task_retries WHERE task_id=?',(task_id,))
            except DeferredRetry:
                with store.db() as c:
                    prior=c.execute('SELECT attempts FROM task_retries WHERE task_id=?',(task_id,)).fetchone()
                    attempts=(prior['attempts'] if prior else 0)+1
                    delay=(60,180,600)[min(attempts-1,2)]
                    c.execute('INSERT OR REPLACE INTO task_retries VALUES(?,?,?)',(task_id,attempts,store.now()+delay))
                if store.task(task_id)['status']=='cancelled': pass
                elif attempts<=3:
                    store.set_status(task_id,'queued')
                    store.event(task_id,'等待模型恢复',f'模型繁忙，{delay} 秒后自动继续（第 {attempts}/3 次）；已完成结果保留。')
                else: store.set_status(task_id,'needs_attention','模型持续繁忙，三轮自动等待后仍不可用；已保留进度，请稍后继续。')
            except Cancelled: store.set_status(task_id,'cancelled')
            except Attention as exc:
                if store.task(task_id)['status']!='cancelled': store.set_status(task_id,'needs_attention',safe_error(exc))
            except Exception as exc:
                if store.task(task_id)['status']!='cancelled': store.set_status(task_id,'failed',store.task(task_id)['stage']+'失败：'+safe_error(exc)+'。已保留完成阶段；修正设置或输入后点击继续 / 重试。')
            finally:
                done.set(); beat.join()
                with store.db() as c: c.execute("UPDATE tasks SET lease=0,owner='' WHERE id=?",(task_id,))
            cleanup()
    finally: lock.close()

if __name__=='__main__': loop()
