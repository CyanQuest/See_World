"""Durable GLM audit and bounded evidence expansion."""
import json
from pathlib import Path
from . import store, media
from .providers import llm_json
from .security import Attention, Cancelled, InvalidOutput, safe_error

WEIGHTS={'可执行性':35,'需求匹配':20,'信息可信度':20,'路线合理性':15,'阅读与应变':10}
RUBRIC_VERSION='travel-review-v2'
BANDS={35:((32,'优秀'),(28,'良好'),(21,'基本合格'),(14,'较弱'),(0,'不合格')),20:((18,'优秀'),(16,'良好'),(12,'基本合格'),(8,'较弱'),(0,'不合格')),15:((14,'优秀'),(12,'良好'),(9,'基本合格'),(6,'较弱'),(0,'不合格')),10:((9,'优秀'),(8,'良好'),(6,'基本合格'),(4,'较弱'),(0,'不合格'))}
LIMIT=15

# Include historical failed/partial work, not just usable transcripts.
COUNTED="task_id=? AND (status IN ('processing','completed','partial','failed') OR transcript IS NOT NULL AND transcript NOT IN ('','null','{}'))"

def attempted(tid):
    with store.db() as c:
        c.execute('INSERT OR IGNORE INTO source_attempts SELECT id,task_id FROM sources WHERE '+COUNTED,(tid,))
        return {r[0] for r in c.execute('SELECT source_id FROM source_attempts WHERE task_id=?',(tid,))}

def attempted_ids(c,tid):
    """Same set as attempted() without recording it, for read-only status pages."""
    return {r[0] for r in c.execute('SELECT source_id FROM source_attempts WHERE task_id=? UNION SELECT id FROM sources WHERE '+COUNTED,(tid,tid))}

def reserve(tid,sid):
    attempted(tid)
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        if c.execute('SELECT 1 FROM source_attempts WHERE source_id=?',(sid,)).fetchone(): return
        if c.execute('SELECT COUNT(*) FROM source_attempts WHERE task_id=?',(tid,)).fetchone()[0]>=LIMIT:
            raise Attention('已达到累计处理 15 个视频的上限，不能再处理新视频')
        c.execute('INSERT INTO source_attempts VALUES(?,?)',(sid,tid))

def validate_review(value):
    if not isinstance(value,dict): raise InvalidOutput('审核结果必须是对象')
    scores=value.get('scores')
    if not isinstance(scores,dict) or set(scores)!=set(WEIGHTS): raise InvalidOutput('审核维度不完整')
    for k,maximum in WEIGHTS.items():
        if type(scores[k]) is not int or not 0<=scores[k]<=maximum: raise InvalidOutput('审核分数超出权重')
    for k in ('critical_issues','issues','missing_topics'):
        if not isinstance(value.get(k),list) or len(value[k])>20 or any(not isinstance(x,str) or len(x)>500 for x in value[k]): raise InvalidOutput('审核意见格式错误')
    if any(x not in ('住宿','交通','景点','美食') for x in value['missing_topics']): raise InvalidOutput('审核缺失类别不在允许范围')
    score=sum(scores.values())
    return {k:value[k] for k in ('scores','critical_issues','issues','missing_topics')}|{'score':score,'passed':score>=85 and not value['critical_issues'],'levels':{k:next(label for lower,label in BANDS[WEIGHTS[k]] if v>=lower) for k,v in scores.items()},'weights':WEIGHTS.copy(),'rubric_version':RUBRIC_VERSION}

def review(tid,version,config,standalone=False):
    with store.db() as c: row=dict(c.execute('SELECT * FROM guides WHERE task_id=? AND version=?',(tid,version)).fetchone())
    content=json.loads(row['content'])
    prompt=(Path(__file__).resolve().parent.parent/'prompts/glm_travel_reviewer.md').read_text()
    packet=content['writing_input']
    # Keep every cited evidence row and all authored prose; uncited input facts
    # are unnecessary for assessing whether written claims are supported.
    prose=content['writing']
    blocks=[prose.get('overview',{})]
    blocks.extend(b for day in prose.get('days',[]) for k,b in day.items() if k!='day')
    blocks.extend(b for section in prose.get('sections',{}).values() for b in section)
    cited={i for b in blocks for i in b.get('fact_ids',[])}
    # Audit original excerpts rather than sending both the excerpt and its
    # redundant extraction summary. Preserve every cited original excerpt.
    evidence=[{k:v for k,v in f.items() if k!='claim'} for f in packet['facts'] if f['fact_id'] in cited]
    data={'preferences':dict(json.loads(row['prefs']),budget_unit='人民币/人',budget_scope='表单预算为人均上限；只有用户其他要求明确另述时才按其要求处理'),'sources':packet['sources'],'facts':evidence,'writing':prose,'checked_route':content.get('editorial'),'revision_issues':[{k:v for k,v in i.items() if k!='evidence'} for i in (content.get('revision_decision') or {}).get('issues',[])],'warnings':content['warnings'],'markdown':__import__('backend.guide',fromlist=['public_markdown']).public_markdown(row['markdown'].split('\n## GLM 质量审核',1)[0].rstrip())}
    # Short transport aliases reduce repeated opaque UUIDs without shortening
    # any authored text or source evidence. They exist only in this request.
    aliases={f['fact_id']:f'F{i+1}' for i,f in enumerate(evidence)}
    aliases.update({s['source_id']:f'S{i+1}' for i,s in enumerate(packet['sources'])})
    def compact(value):
        if isinstance(value,str): return aliases.get(value,value)
        if isinstance(value,list): return [compact(v) for v in value]
        if isinstance(value,dict): return {k:compact(v) for k,v in value.items() if not (k=='kind' and v=='fact')}
        return value
    data=compact(data)
    result=None
    for attempt in range(2):
        try:
            result=validate_review(llm_json(config,tid,prompt,data,'审核攻略')); break
        except InvalidOutput as exc:
            if attempt: raise
            prompt+='\n上次检查错误：'+str(exc)+'。请严格返回五项整数分数及三个字符串数组；文字中的引语请用中文引号「」避免未转义双引号。'
    import hashlib
    result['body_sha256']=hashlib.sha256(data['markdown'].encode()).hexdigest()
    result.update(model=config['llm_model'],processed_videos=len(attempted(tid)),version=version)
    media.check(tid)
    if content.get('quality_review'):
        content.setdefault('quality_review_history',[]).append(content['quality_review'])
    content['quality_review']=result
    if content.get('revision_decision'):
        from .revision import compare
        with store.db() as c: previous=json.loads(c.execute('SELECT content FROM guides WHERE task_id=? AND version=?',(tid,content['revision_of'])).fetchone()[0])
        content['revision_comparison']=compare(tid,previous,content,content['revision_decision'],config)
        comparison=content['revision_comparison']
        result['revision_blocked']=bool(comparison['uncertain'] or comparison['regressed'] or comparison['unresolved'])
        if result['revision_blocked']: result['passed']=False
        with store.db() as c:
            for issue in content['revision_decision']['issues']:
                issue=dict(issue,review_status='resolved' if issue['issue_id'] in comparison['resolved'] else 'unresolved',reviewed_version=version)
                c.execute('UPDATE revision_issues SET value=? WHERE task_id=? AND version=? AND issue_id=?',(store.dumps(issue),tid,content['revision_of'],issue['issue_id']))
    lines=['\n\n## GLM 质量审核','',f"GLM 自评：{result['score']}/100；"+('达到 85 分且无关键错误。' if result['passed'] else '未达标，不能视为合格攻略。'),'',*['- '+k+'：'+str(v)+'/'+str(WEIGHTS[k])+' · '+result['levels'][k] for k,v in result['scores'].items()],'',*['- '+x.replace('\n',' ') for x in result['critical_issues']+result['issues']]]
    from .guide import escape
    # Escape model feedback before inclusion in Markdown exports.
    suffix='\n'.join(lines[:4])+ '\n'+ '\n'.join(escape(x) for x in lines[4:])
    with store.db() as c:
        base=row['markdown'].split('\n## GLM 质量审核',1)[0].rstrip()
        c.execute('UPDATE guides SET content=?,markdown=? WHERE task_id=? AND version=?',(store.dumps(content),base+suffix,tid,version))
        if not standalone:
            c.execute("UPDATE quality_cycles SET phase=?,feedback=? WHERE task_id=?",('done' if result['passed'] else 'classify',store.dumps(result),tid))
        else:
            c.execute("UPDATE quality_cycles SET phase='done',version=?,feedback=? WHERE task_id=?",(version,store.dumps(result),tid))
    if standalone:
        store.set_status(tid,'completed' if result['passed'] and not content['warnings'] else 'completed_with_warnings','' if result['passed'] else '新版标准重新评分未达标；本次仅评分，未补充视频。')
    store.event(tid,'审核攻略',f"第 {version} 版 GLM 自评 {result['score']} 分，"+('通过' if result['passed'] else '未通过'),level='info' if result['passed'] else 'warning')
    return result

def finish(tid,reason=''):
    count=len(attempted(tid))
    with store.db() as c:
        row=c.execute('SELECT * FROM quality_cycles WHERE task_id=?',(tid,)).fetchone()
        g=c.execute('SELECT content,markdown FROM guides WHERE task_id=? AND version=?',(tid,row['version'])).fetchone()
        content=json.loads(g['content']); result=content['quality_review']; reason=reason or result.get('stop_reason',''); result['stop_reason']=reason; result['processed_videos']=count
        from .guide import escape
        suffix='\n\n停止补充资料：'+escape(reason) if reason else ''
        markdown=g['markdown']+(suffix if suffix and not g['markdown'].endswith(suffix) else '')
        c.execute('UPDATE guides SET content=?,markdown=? WHERE task_id=? AND version=?',(store.dumps(content),markdown,tid,row['version']))
        c.execute("UPDATE quality_cycles SET phase='done' WHERE task_id=?",(tid,))
    store.set_status(tid,'completed' if result['passed'] and not content['warnings'] else 'completed_with_warnings',reason)

def run(tid,config):
    from . import worker
    from .guide import generate
    with store.db() as c: c.execute("INSERT OR IGNORE INTO quality_cycles(task_id,phase) VALUES(?,'generate')",(tid,))
    while True:
        media.check(tid)
        with store.db() as c: state=dict(c.execute('SELECT * FROM quality_cycles WHERE task_id=?',(tid,)).fetchone())
        phase=state['phase']
        if phase=='done': finish(tid); return
        if phase=='generate':
            if state['version']:
                with store.db() as c: prior=c.execute('SELECT content FROM guides WHERE task_id=? AND version=?',(tid,state['version'])).fetchone()
                if prior and json.loads(prior[0]).get('writing') and json.loads(prior[0]).get('quality_review'):
                    with store.db() as c: c.execute("UPDATE quality_cycles SET phase='classify' WHERE task_id=?",(tid,))
                    continue
            generate(tid,dict(config,quality_feedback=json.loads(state['feedback'])),quality_pending=True)
        elif phase=='review':
            store.set_status(tid,'generating'); store.event(tid,'审核攻略','GLM 正在按五个维度审核正文、需求与证据')
            review(tid,state['version'],config)
        elif phase=='classify':
            from .editorial import triage
            from .revision import select_baseline
            selection=select_baseline(tid)
            if selection['uncertain']:
                finish(tid,selection['baseline_reason']); return
            baseline=selection['baseline_version'] or state['version']
            decision=triage(tid,baseline,config)
            feedback=json.loads(state['feedback'])
            improved=state['baseline_score']<0 or feedback['score']>state['baseline_score']
            with store.db() as c: current_content=json.loads(c.execute('SELECT content FROM guides WHERE task_id=? AND version=?',(tid,state['version'])).fetchone()[0])
            improved=improved or bool(current_content.get('revision_comparison',{}).get('resolved'))
            if baseline!=state['version']: improved=True
            can_revise=bool(decision['repairable']) and state['revision_round']<config.get('revision_rounds',2) and improved
            next_phase='revise' if can_revise else 'expand'
            decision['baseline_version']=baseline
            decision['baseline_reason']=selection['baseline_reason']
            history=json.loads(state['history'])
            with store.db() as c: version_content=json.loads(c.execute('SELECT content FROM guides WHERE task_id=? AND version=?',(tid,state['version'])).fetchone()[0])
            history.append({'changed_sections':version_content.get('revision_changes',[]),'version':state['version'],'round':state['revision_round'],'score':feedback['score'],'improved':improved,**decision,'next_phase':next_phase})
            with store.db() as c:
                c.execute('UPDATE quality_cycles SET phase=?,triage=?,history=?,baseline_score=? WHERE task_id=?',(next_phase,store.dumps(decision),store.dumps(history),feedback['score'],tid))
            store.event(tid,'问题分类',f"可修改 {len(decision['repairable'])} 项；缺证据 {len(decision['evidence_gaps'])} 项；本批已修改 {state['revision_round']} 轮")
        elif phase=='revise':
            if json.loads(state['triage']).get('rules_version')!='phase2-v1':
                with store.db() as c: c.execute("UPDATE quality_cycles SET phase='classify' WHERE task_id=?",(tid,))
                continue
            store.set_status(tid,'generating')
            store.event(tid,'修改攻略',f"复用现有证据修改第 {state['version']} 版，第 {state['revision_round']+1} 轮；不重新转写")
            generate(tid,dict(config,quality_feedback=json.loads(state['feedback'])),quality_pending=True,revision=json.loads(state['triage']).get('baseline_version',state['version']))
        elif phase=='expand':
            decision=json.loads(state['triage'])
            if not decision:
                with store.db() as c: c.execute("UPDATE quality_cycles SET phase='classify' WHERE task_id=?",(tid,))
                continue
            if not decision.get('search_complete'):
                with store.db() as c: c.execute("UPDATE quality_cycles SET phase='classify' WHERE task_id=?",(tid,))
                continue
            if not decision.get('evidence_gaps') or not decision.get('queries'):
                finish(tid,'攻略仍未达标；本批修改已达上限或无改善，未发现明确的新证据需求，停止重写与补视频。'); return
            used=attempted(tid)
            if len(used)>=LIMIT: finish(tid,'累计已处理 15 个视频，攻略仍未通过审核；已停止补充。'); return
            prefs=store.task(tid)['prefs']; feedback=json.loads(state['feedback'])
            candidates=[s for s in store.sources(tid) if s['id'] not in used]
            if decision['queries']:
                for url in media.search(prefs['destination'],[],tid,dict(config,evidence_queries=decision['queries'])):
                    media.check(tid)
                    if len(store.sources(tid))>=60: break
                    try: worker.add_source(tid,url,config)
                    except (Attention,Cancelled): raise
                    except Exception as exc: store.event(tid,'补充资料',safe_error(exc),level='warning')
                candidates=[s for s in store.sources(tid) if s['id'] not in used]
            target={dict(住宿='住',交通='行',景点='玩',美食='吃').get(t,t) for t in feedback.get('missing_topics',[])}
            import re
            terms=set(re.findall(r'[\u4e00-\u9fff]{2,}', ' '.join(decision['queries'])))
            relevance=lambda s:sum(term[i:i+2] in (s['meta'].get('title','')+s['meta'].get('description','')) for term in terms for i in range(len(term)-1))
            candidates.sort(key=lambda s:(relevance(s),len(media.source_topics(s)&target)),reverse=True)
            duration=sum(s['meta'].get('actual_duration') or s['meta'].get('duration') or 0 for s in store.sources(tid) if s['id'] in used)
            batch=[]
            for s in candidates:
                if not relevance(s): continue
                d=s['meta'].get('duration') or 0
                if 0<d<=config['max_video_minutes']*60 and duration+d<=config['max_minutes']*60:
                    batch.append(s['id']); duration+=d
                if len(batch)>=min(5,LIMIT-len(used)): break
            if not batch: finish(tid,'攻略未通过审核；没有符合时长与数量限制的未处理备选视频，已停止补充。'); return
            with store.db() as c:
                for sid in batch: c.execute('UPDATE sources SET selected=1 WHERE id=?',(sid,))
                c.execute("UPDATE quality_cycles SET phase='process',batch=? WHERE task_id=?",(store.dumps(batch),tid))
            store.event(tid,'补充资料',f'审核未通过，再处理 {len(batch)} 个备选视频；累计最多 15 个')
        elif phase=='process':
            store.set_status(tid,'processing'); batch=json.loads(state['batch'])
            for source in store.sources(tid):
                if source['id'] not in batch: continue
                media.check(tid)
                if source['status']=='failed': continue
                reserve(tid,source['id'])
                try: worker.process_source(source,tid,config,store.task(tid)['prefs'])
                except (Cancelled,Attention): raise
                except Exception as exc: store.update_source(source['id'],status='failed',error=safe_error(exc))
            with store.db() as c: c.execute("UPDATE quality_cycles SET phase='generate',batch='[]',revision_round=0,baseline_score=-1,triage='{}' WHERE task_id=?",(tid,))
        else: raise ValueError('未知质量处理阶段')
