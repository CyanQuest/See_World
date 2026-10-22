"""Issue-scoped evidence search, static patches, and conservative version selection."""
import copy
import hashlib
import json
import re
from pydantic import Field
from . import store, editorial
from .providers import OUTPUT_LIMITS
from .security import InvalidOutput

RULES=editorial.RULES
PERIODS=('上午','下午','晚上')

def digest(value):
    return hashlib.sha256(store.dumps(value).encode()).hexdigest()


def blocks(prose):
    result={'overview':prose.get('overview',{})}
    for day in prose.get('days',[]):
        for period in PERIODS: result[f"day:{day['day']}:{period}"]=day[period]
    for heading,items in prose.get('sections',{}).items():
        for index,item in enumerate(items): result[f'section:{heading}:{index}']=item
    return result


class Issue(editorial.Strict):
    description: str
    kind: str  # text/day/cross_day/global
    targets: list[str]
    days: list[int]
    basis: str
    action: str
    impact: str
    terms: list[str]


class Issues(editorial.Strict):
    issues: list[Issue] = Field(max_length=20)


class Finding(editorial.Strict):
    status: str
    hit_ids: list[str]
    conclusion: str
    limitations: str
    unresolved: str


def corpus(tid):
    """Read ALL saved sources, including unselected and partially extracted text."""
    result=[]
    for source in store.sources(tid):
        for fact in (source.get('facts') or {}).get('facts',[]):
            result.append({'hit_id':fact['fact_id'],'source_id':source['id'],'text':fact['evidence_excerpt'],'fact':fact,'location':{'type':'fact','fact_id':fact['fact_id']}})
        transcript=source.get('transcript') or {}
        text=transcript.get('text','')
        if not text:
            text='\n'.join(x.get('text','') for x in transcript.get('segments',[]))
        # Overlap preserves endpoints around boundaries; offsets refer to saved text.
        for start in range(0,len(text),800):
            excerpt=text[max(0,start-160):start+960]
            if excerpt:
                result.append({'hit_id':'R'+digest([source['id'],transcript.get('hash'),start,excerpt])[:24],'source_id':source['id'],'text':excerpt,'location':{'type':'transcript','start':max(0,start-160),'end':min(len(text),start+960),'transcript_hash':transcript.get('hash')}})
    return result


def search_existing(tid,issue,config,rows=None):
    rows=corpus(tid) if rows is None else rows
    terms=list(dict.fromkeys(t.strip() for t in issue['terms'] if t.strip()))
    grams={word[i:i+2] for word in terms+[issue['description']] for i in range(len(word)-1) if re.fullmatch(r'[\w\u4e00-\u9fff]{2}',word[i:i+2])}
    ranked=[]
    for row in rows:
        text=row['text']+' '+store.dumps(row.get('fact',{}))
        score=sum(5 for term in terms if term in text)+sum(g in text for g in grams)
        if score: ranked.append((score,row))
    ranked.sort(key=lambda pair:pair[0],reverse=True)
    key=digest([RULES,issue,rows,store.task(tid)['prefs']])
    record={'issue_id':issue['issue_id'],'terms':terms,'scope':{'sources':[s['id'] for s in store.sources(tid)],'items_scanned':len(rows),'includes_all_saved_text':True},'hits':[{k:v for k,v in r.items() if k!='fact'} for _,r in ranked],'findings':[],'status':'searching','complete':False}
    def save():
        with store.db() as c: c.execute('INSERT OR REPLACE INTO evidence_searches VALUES(?,?,?)',(tid,key,store.dumps(record)))
    save()
    # Every candidate is assessed in bounded batches; truncation is never absence.
    limit=max(0,config['context_budget']-min(config['output_tokens'],OUTPUT_LIMITS['证据判断'])-len(store.dumps(issue).encode())-6500)
    batches=[]; batch=[]; size=0
    for _,row in ranked:
        n=len(store.dumps(row).encode())
        if n>limit:
            record['status']='context_limited'; save()
            raise InvalidOutput('内部检索候选超出上下文；已保留命中，不属于证据缺口')
        if size+n>limit and batch: batches.append(batch); batch=[]; size=0
        batch.append(row); size+=n
    if batch: batches.append(batch)
    for index,batch in enumerate(batches):
        allowed={r['hit_id'] for r in batch}
        def validate(value):
            if value['status'] not in ('sufficient','partial','gap') or not set(value['hit_ids'])<=allowed: raise InvalidOutput('检索判断状态或引用错误')
            if value['status']!='gap' and not value['hit_ids']: raise InvalidOutput('有依据的结论必须引用命中原文')
            if value['status']=='sufficient' and value['unresolved']: raise InvalidOutput('仍有未解决项不能判证据充分')
            return value
        finding=editorial.structured(tid,f'证据判断:{key}:{index}',Finding,'针对单个问题判断这些原文能支持什么。status=sufficient/partial/gap。不要把关键词命中当支持，不从审核意见推断事实；必须核对起终点、整段与子段耗时、日期、同名归属与适用条件。别名不能证明同一地点。gap表示本批不能支持，程序会继续检查其他批次。',{'issue':issue,'preferences':store.task(tid)['prefs'],'candidates':batch},config,validate)
        record['findings'].append(finding); save()
    statuses={f['status'] for f in record['findings']}
    # Conflicting findings remain partial, never silently picked as conclusive.
    record['status']='sufficient' if statuses=={'sufficient'} else 'partial' if statuses & {'sufficient','partial'} else 'gap'
    record['complete']=True
    save()
    return record


def classify(tid,version,config):
    with store.db() as c: content=json.loads(c.execute('SELECT content FROM guides WHERE task_id=? AND version=?',(tid,version)).fetchone()[0])
    known=blocks(content['writing']); prefs=store.task(tid)['prefs']
    key=digest([RULES,version,content['quality_review'],prefs,known])
    def validate(value):
        for issue in value['issues']:
            if issue['kind'] not in ('text','day','cross_day','global') or not issue['targets'] or not set(issue['targets'])<=known.keys(): raise InvalidOutput('问题修改目标越界')
            if any(type(d)!=int or not 1<=d<=prefs['days'] for d in issue['days']): raise InvalidOutput('问题日期越界')
            if issue['kind']!='text' and not issue['days']: raise InvalidOutput('路线问题必须说明受影响日期')
            if not issue['basis'] or not issue['action'] or not issue['impact']: raise InvalidOutput('问题缺少依据、动作或影响范围')
        return value
    issues=editorial.structured(tid,'问题范围:'+key,Issues,'将审核必须修改项整理成问题，不纳入无需修改项。targets只用输入段落编号；文字text仅改目标段落，单日路线day，住宿或跨日移动cross_day须覆盖次日，全局硬需求冲突才global且说明理由。days列受影响日期，terms给地点、别名、起终点、类别关键词。basis引用具体审核原句；审核不是事实，include_arrival只管预算是否含往返费用，预算完整性不扣分。',{'blocks':known,'review':content['quality_review'],'preferences':prefs},config,validate)['issues']
    rows=corpus(tid); records=[]
    for issue in issues:
        issue['issue_id']='I'+digest([version,issue])[:16]
        if issue['kind']=='cross_day':
            issue['days']=sorted(set(issue['days'])|{d+1 for d in issue['days'] if d<prefs['days']})
        if issue['kind']!='text':
            issue['targets']=sorted(set(issue['targets'])|{f'day:{d}:{p}' for d in issue['days'] for p in PERIODS})
        with store.db() as c: c.execute('INSERT OR REPLACE INTO revision_issues VALUES(?,?,?,?)',(tid,version,issue['issue_id'],store.dumps(dict(issue,status='searching'))))
        evidence=search_existing(tid,issue,config,rows)
        issue.update(status=evidence['status'],evidence=evidence)
        records.append(issue)
        with store.db() as c: c.execute('INSERT OR REPLACE INTO revision_issues VALUES(?,?,?,?)',(tid,version,issue['issue_id'],store.dumps(issue)))
    gaps=[i for i in records if i['status']=='gap']
    queries=[prefs['destination']+' '+' '.join(i['terms'][:3]) for i in gaps][:5]
    return {'repairable':[i['description'] for i in records], 'evidence_gaps':[i['description'] for i in gaps], 'queries':queries,'query_issues':[i['issue_id'] for i in gaps][:5],'issues':records,'search_complete':True,'version':version,'rules_version':RULES}


def apply_patch(prose,result,allowed,known_facts):
    """Apply data by fixed IDs only; never evaluate a path or expression."""
    if not isinstance(result,dict) or set(result)!={'patches','unresolved'} or not isinstance(result['patches'],list) or not isinstance(result['unresolved'],list) or any(not isinstance(x,str) for x in result['unresolved']): raise InvalidOutput('正文补丁结构错误')
    updated=copy.deepcopy(prose); before=blocks(prose); seen=set()
    for patch in result['patches']:
        if not isinstance(patch,dict) or set(patch)!={'target','block','reason'}: raise InvalidOutput('正文补丁字段错误')
        target=patch['target']
        if not isinstance(target,str) or target not in allowed or target not in before or target in seen: raise InvalidOutput('补丁目标越界或重复')
        block=patch['block']
        if not isinstance(block,dict) or not isinstance(block.get('fact_ids'),list) or any(not isinstance(f,str) or f not in known_facts for f in block['fact_ids']): raise InvalidOutput('补丁未知引用')
        if not isinstance(patch['reason'],str) or not patch['reason']: raise InvalidOutput('补丁缺少修改说明')
        seen.add(target)
        if target=='overview': updated['overview']=block
        elif target.startswith('day:'):
            _,day,period=target.split(':'); updated['days'][int(day)-1][period]=block
        else:
            _,heading,index=target.split(':'); updated['sections'][heading][int(index)]=block
    if any(digest(value)!=digest(blocks(updated)[key]) for key,value in before.items() if key not in allowed): raise InvalidOutput('未涉及段落哈希改变')
    return updated


def select_baseline(tid,c=None):
    """Compare chronological candidates under the SAME preferences and rubric."""
    if c is None:
        with store.db() as c: return select_baseline(tid,c)
    task=c.execute('SELECT prefs FROM tasks WHERE id=?',(tid,)).fetchone()
    if not task: raise KeyError('任务不存在')
    # Only the review fields are needed; full guide content is large.
    rows=c.execute("SELECT version,prefs,json_extract(content,'$.quality_review') review,json_extract(content,'$.revision_comparison') comparison FROM guides WHERE task_id=? ORDER BY version",(tid,)).fetchall()
    current_prefs=json.loads(task['prefs']); baseline=None; latest=None; reason='尚无已审核版本'; uncertain=False
    for row in rows:
        review=json.loads(row['review']) if row['review'] else None
        if not review or json.loads(row['prefs'])!=current_prefs: continue
        latest=(row['version'],review)
        uncertain=False
        if baseline is None or review.get('rubric_version')!=baseline[1].get('rubric_version'):
            baseline=latest; reason='采用当前审核规则下的起始版本'; continue
        previous=baseline[1]
        new_critical=set(review['critical_issues'])-set(previous['critical_issues'])
        comparison=json.loads(row['comparison']) if row['comparison'] else {}
        if comparison.get('uncertain'):
            uncertain=True; reason='复审意见存在矛盾；保留基准并停止自动推进'
        elif new_critical or review['score']<previous['score'] or comparison.get('regressed'):
            reason=f"第 {row['version']} 版 {review['score']} 分出现新增关键错误或退步；保留第 {baseline[0]} 版 {previous['score']} 分为基准"
        elif review['score']>previous['score'] or comparison.get('resolved'):
            baseline=latest; reason='分数改善或具体问题已解决，且无已识别退步'
        else:
            reason=f"第 {row['version']} 版未显示明确改善，保留第 {baseline[0]} 版"
        if comparison.get('uncertain'): uncertain=True; reason='复审意见存在矛盾；保留基准并停止自动推进'
    return {'baseline_version':baseline[0] if baseline else 0,'baseline_score':baseline[1]['score'] if baseline else -1,'latest_version':rows[-1]['version'] if rows else 0,'baseline_reason':reason,'uncertain':uncertain}


def revision_evidence(tid,previous,decision):
    byid={f['fact_id']:f for f in previous['facts']}
    rows={r['hit_id']:r for r in corpus(tid)}
    for issue in decision['issues']:
        for finding in issue['evidence']['findings']:
            for hid in finding['hit_ids']:
                row=rows.get(hid)
                if not row: raise InvalidOutput('证据已变化，需要重新检索')
                if row.get('fact'): byid[hid]=row['fact']; continue
                # A raw excerpt is citable evidence, not a synthesized travel claim.
                byid[hid]={'fact_id':hid,'category':'注意事项','entity_name':'检索原文','claim':row['text'],'evidence_excerpt':row['text'],'source_id':row['source_id'],'chunk_id':hid,'location':None,'amount':None,'currency':None,'stated_date':None,'timing_precision':'text_offset','start':None,'end':None,'issue_flags':[],'raw_location':row['location']}
    return list(byid.values())


def revise_prose(tid,previous,packet,decision,config):
    allowed={target for issue in decision['issues'] for target in issue['targets']}
    before=blocks(previous['writing'])
    data={'targets':{k:before[k] for k in allowed},'issues':[{k:v for k,v in i.items() if k!='evidence'} for i in decision['issues']], 'preferences':packet['preferences'],'facts':packet['facts'],'directory':packet['directory'],'route':packet['route'],'neighbor_context':{k:b for k,b in before.items() if k not in allowed and k.startswith('day:') and any(abs(int(k.split(':')[1])-d)<=1 for i in decision['issues'] for d in i['days'])}}
    def validate(result):
        apply_patch(previous['writing'],result,allowed,{f['fact_id'] for f in packet['facts']})
        return result
    result=editorial.cached_step(tid,'正文补丁:'+digest([RULES,data]),'只返回静态JSON {"patches":[{"target":"输入目标编号","block":{"text":"替换段落","fact_ids":[],"kind":"planning或fact","route_node_ids":[]},"reason":"修改说明"}],"unresolved":["未解决问题"]}。只替换授权目标，不重复返回全文。不改变已检查路线；每日段落关联本日D日-A序号/D日-L序号或D日-start/end节点，按顺序。事实只引用本轮facts；纯规划标planning。无依据时缩小必要移动并明确收益、损失及未满足需求，不虚构交通/房态。遵守include_arrival预算含义，审核意见不作证据。',data,config,validate)
    updated=apply_patch(previous['writing'],result,allowed,{f['fact_id'] for f in packet['facts']})
    return updated,result,allowed


class Comparison(editorial.Strict):
    resolved: list[str]
    unresolved: list[str]
    regressed: list[str]
    uncertain: bool
    reason: str


def compare(tid,previous,candidate,decision,config):
    issue_ids={i['issue_id'] for i in decision.get('issues',[])}
    def validate(value):
        if not set(value['resolved']+value['unresolved'])<=issue_ids or set(value['resolved'])&set(value['unresolved']): raise InvalidOutput('复审问题编号错误')
        if set(value['resolved']+value['unresolved'])!=issue_ids: raise InvalidOutput('复审未覆盖全部问题')
        return value
    return editorial.structured(tid,'退步检查:'+digest([RULES,previous['quality_review'],candidate['quality_review'],decision]),Comparison,'比较一次修订前后，resolved/unresolved必须覆盖输入问题编号。regressed列新关键问题、重现问题或破坏硬约束；审核互相矛盾不能确定时uncertain=true。无需重新评分，不挑高分；分数相同仍可明确解决问题。审核意见不是旅行事实；include_arrival仅控制预算，不表示禁用抵达日，预算完整性不评分。',{'issues':[{k:v for k,v in i.items() if k!='evidence'} for i in decision.get('issues',[])],'before_review':previous['quality_review'],'after_review':candidate['quality_review'],'before':{k:v for k,v in blocks(previous['writing']).items() if k in {t for i in decision.get('issues',[]) for t in i['targets']}},'after':{k:v for k,v in blocks(candidate['writing']).items() if k in {t for i in decision.get('issues',[]) for t in i['targets']}}},config,validate)
