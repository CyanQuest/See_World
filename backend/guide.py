import hashlib
import json
import re
from datetime import datetime
from decimal import Decimal
from . import store
from .media import check
from .providers import llm_json
from .security import InvalidOutput

PROMPT_VERSION = 'evidence-v1.1'
CATEGORIES = ['景点','美食','住宿','交通','费用','预约','注意事项']

def normalize_issue_flags(value):
    if value is None: return []
    rows=value if isinstance(value,list) else [value]
    result=[]
    for row in rows:
        text=row if isinstance(row,str) else json.dumps(row,ensure_ascii=False,sort_keys=True)
        if text.strip() and text not in result: result.append(text)
    return result

def split_text(transcript, budget):
    # UTF-8 byte ceiling is conservative across Chinese and Latin tokenizers.
    max_bytes = max(600, (budget - 2500) // 3)
    result, buf, start, end = [], '', None, None
    for segment in transcript['segments']:
        text = segment['text']
        pieces, piece = [], ''
        for char in text:
            if len((piece+char).encode()) > max_bytes: pieces.append(piece); piece = ''
            piece += char
        if piece: pieces.append(piece)
        for piece in pieces:
            if buf and len((buf+'\n'+piece).encode()) > max_bytes:
                result.append({'text':buf,'start':start,'end':end}); buf=''; start=None
            if start is None: start=segment['start']
            end=segment['end']; buf += ('\n' if buf else '')+piece
    if buf: result.append({'text':buf,'start':start,'end':end})
    for index, part in enumerate(result): part['chunk_id'] = f'T{index+1}'
    return result

def compact_text(value): return re.sub(r'\s+', '', value)

def numbers(value): return {Decimal(n) for n in re.findall(r'\d+(?:\.\d+)?',value)}

def passages(text):
    # Contiguous original passages; no rewriting of punctuation, names or prices.
    pieces=re.split(r'(?<=[。！？!?\n])',text)
    result=[]; current=''
    for piece in pieces:
        if current and len(current)+len(piece)>280:
            result.append({'id':f'P{len(result)+1}','text':current});current=''
        while len(piece)>400:
            if current: result.append({'id':f'P{len(result)+1}','text':current});current=''
            result.append({'id':f'P{len(result)+1}','text':piece[:400]});piece=piece[400:]
        current+=piece
    if current: result.append({'id':f'P{len(result)+1}','text':current})
    return result


def validate_facts(result, segment, source_id, precision):
    rows = result.get('facts')
    if not isinstance(rows, list) or len(rows) > 50: raise InvalidOutput('事实结构错误')
    facts = []
    for row in rows:
        if not isinstance(row, dict): raise InvalidOutput('事实条目格式错误')
        excerpt = row.get('evidence_excerpt')
        if 'passages' in segment:
            evidence=next((p for p in segment['passages'] if p['id']==row.get('evidence_id')),None)
            if not evidence: raise InvalidOutput('事实引用了不存在的原文段落')
            excerpt=evidence['text']
        if not isinstance(excerpt,str) or len(excerpt.strip()) < 4 or compact_text(excerpt) not in compact_text(segment['text']): raise InvalidOutput('事实摘录无法对应原始资料')
        if row.get('category') not in CATEGORIES or not isinstance(row.get('entity_name'),str): raise InvalidOutput('事实类别或名称错误')
        if not isinstance(row.get('claim'),str) or not row['claim'].strip(): raise InvalidOutput('事实陈述为空')
        if not numbers(row['claim']).issubset(numbers(excerpt)): raise InvalidOutput('事实陈述包含原文摘录没有支持的数字')
        for field in ('location','currency','stated_date'):
            if row.get(field) is not None and not isinstance(row[field],str): raise InvalidOutput('事实字段类型错误')
        row['issue_flags']=normalize_issue_flags(row.get('issue_flags',[]))
        amount = row.get('amount')
        if amount is not None:
            if not isinstance(amount, (int,float)) or amount < 0 or Decimal(str(amount)) not in numbers(excerpt): amount = None
        core=re.sub(r'（[^）]*）|\([^)]*\)', '', row['entity_name']).removesuffix('区域')
        if not core or compact_text(core) not in compact_text(excerpt): row.setdefault('issue_flags',[]).append('名称未在摘录中明确出现，需核实')
        if row.get('stated_date') and compact_text(row['stated_date']) not in compact_text(excerpt): row['stated_date']=None
        if row.get('location') and compact_text(row['location']) not in compact_text(excerpt): row['location']=None
        fid = 'F' + hashlib.sha256((source_id+segment['chunk_id']+row['category']+row['entity_name']+row['claim']+excerpt).encode()).hexdigest()[:12]
        facts.append({'fact_id':fid,'category':row['category'],'entity_name':row['entity_name'][:100],'claim':row['claim'][:600],'source_id':source_id,'chunk_id':segment['chunk_id'],'evidence_excerpt':excerpt,'location':row.get('location'),'amount':amount,'currency':row.get('currency') if amount is not None else None,'stated_date':row.get('stated_date'),'timing_precision':precision,'start':segment['start'],'end':segment['end'],'issue_flags':row.get('issue_flags',[])})
    return facts

def extract(source, task_id, config):
    transcript = source['transcript']
    saved = source.get('facts') or {}
    if saved.get('transcript_hash') != transcript.get('hash') or saved.get('prompt_version')!=PROMPT_VERSION:
        saved = {'segments':{}, 'facts':[], 'summary':None,'transcript_hash':transcript.get('hash'),'prompt_version':PROMPT_VERSION}
    for seg in split_text(transcript, config['context_budget'] - config['output_tokens']):
        check(task_id)
        seg['passages']=passages(seg['text'])
        if seg['chunk_id'] in saved['segments']: continue
        store.event(task_id, '整理资料', '正在提取有原文支持的旅行信息', source['id'])
        instruction = '提取目的地相关旅行事实，最多20条。返回 {"facts":[{"category":"景点/美食/住宿/交通/费用/预约/注意事项之一","entity_name":"名称","claim":"忠实于原文的简短陈述，数字写法保持原样","evidence_id":"支持该事实的原文段落ID，例如P1","location":null,"amount":null,"currency":null,"stated_date":null,"issue_flags":[]}]}。每条事实必须由一个真实的passages段落支持。只返回evidence_id，不复制或改写原文摘录。资料没有旅行事实时返回空列表。未知字段为null，不从标题推断。口语里的数字不要改写成阿拉伯数字；不能确定名称时标注未确认。'
        error = ''
        for attempt in range(2):
            try:
                obj = llm_json(config,task_id,instruction + (' 上次错误：'+error+'，请修复。' if attempt else ''), {'destination':store.task(task_id)['prefs']['destination'],'source_id':source['id'],**{k:v for k,v in seg.items() if k!='text'}})
                rows=obj.get('facts')
                if not isinstance(rows,list) or len(rows)>50: raise InvalidOutput('事实结构错误')
                found=[]; rejected=[]
                for row in rows:
                    try: found.extend(validate_facts({'facts':[row]},seg,source['id'],transcript['precision']))
                    except InvalidOutput as exc: rejected.append(str(exc))
                if rows and not found: raise InvalidOutput('所有事实均未通过校验：'+ '；'.join(sorted(set(rejected))))
                if rejected:
                    store.event(task_id,'整理资料',f'保留 {len(found)} 条合格事实，排除 {len(rejected)} 条不合格条目；原因：'+'；'.join(sorted(set(rejected))),source['id'],level='warning')
                break
            except InvalidOutput as exc:
                error = str(exc)
                if attempt: raise
        saved['segments'][seg['chunk_id']] = found
        saved['facts'] = [f for group in saved['segments'].values() for f in group]
        store.update_source(source['id'], facts=saved)
    for fact in saved['facts']:
        flags=normalize_issue_flags(fact.get('issue_flags',[]))
        # Recheck cached facts under the same display/scheduling quality rules.
        core=re.sub(r'（[^）]*）|\([^)]*\)', '', fact['entity_name']).removesuffix('区域')
        if core and compact_text(core) in compact_text(fact['evidence_excerpt']):
            flags[:]=[x for x in flags if x!='名称未在摘录中明确出现，需核实']
        if re.search(r'([^\s，,。！？]{1,8}[，,、\s]*)\1{7,}',fact['evidence_excerpt']):
            flags.append('摘录含异常重复转写，暂不作为推荐')
        if any(word in fact['claim'] for word in ('最受欢迎','最推荐','米其林','媲美','不会踩雷','不踩雷')):
            flags.append('宣传或主观背书未经核实，暂不作为推荐')
        if fact['category']=='交通' and ('直达' in fact['claim'] or '双流机场' in fact['claim']):
            flags.append('线路直达与机场接驳说法需按现行交通信息核实')
        fact['issue_flags']=list(dict.fromkeys(flags))
    # Preserve all cards, while summary is a list of IDs, never a lossy rewrite.
    unique = {}; ordered=[]
    for fact in saved['facts']:
        key = (fact['entity_name'],fact.get('location'),fact['claim'])
        if key not in unique: unique[key]=fact; ordered.append(fact['fact_id'])
    saved['summary'] = {'fact_ids':ordered,'missing_categories':[c for c in CATEGORIES if not any(f['category']==c for f in saved['facts'])]}
    store.update_source(source['id'],facts=saved,status='completed' if source['complete'] else 'partial',error='')
    return saved['facts']

def validate_plan(result, facts, days, preferences=None):
    known = {f['fact_id'] for f in facts}
    by_id={f['fact_id']:f for f in facts}
    notes=(preferences or {}).get('notes','')
    itinerary = result.get('days')
    if not isinstance(itinerary,list) or len(itinerary)!=days: raise InvalidOutput('行程天数不符合偏好')
    for i, day in enumerate(itinerary):
        if not isinstance(day,dict) or day.get('day')!=i+1: raise InvalidOutput('行程序号错误')
        for period in ('上午','下午','晚上'):
            ids = day.get(period)
            if not isinstance(ids,list) or len(ids)>5 or any(not isinstance(x,str) or x not in known for x in ids): raise InvalidOutput('行程引用了无效事实')
            for fid in ids:
                claim=by_id[fid].get('claim','')
                if any(x in notes for x in ('不自驾','不想开车','不开车')) and any(x in claim for x in ('自驾','开车','租车')) and not any(x in claim for x in ('无需自驾','不用自驾','不建议自驾')):
                    raise InvalidOutput('行程与不自驾要求冲突，请移除相应安排')
                if any(x in notes for x in ('少走路','少步行','不能爬山')) and any(x in claim for x in ('徒步','爬山','登山','暴走')):
                    raise InvalidOutput('行程与少步行要求冲突，请移除相应安排')
    conflicts = result.get('conflicts',[])
    if not isinstance(conflicts,list): raise InvalidOutput('冲突结构错误')
    for group in conflicts:
        if not isinstance(group,list) or len(group)<2 or any(x not in known for x in group): raise InvalidOutput('冲突引用无效')
    # Payment methods and fares belong in the transport chapter, not as visits.
    for day in itinerary:
        seen=set()
        for period in ('上午','下午','晚上'):
            kept=[]
            for fid in day[period]:
                fact=by_id[fid]
                if fact.get('category','景点') not in ('景点','美食') or fact.get('issue_flags'): continue
                key=(fact.get('source_id'),fact.get('entity_name',fid))
                if key in seen: continue
                seen.add(key); kept.append(fid)
            day[period]=kept
    return {'days':itinerary,'conflicts':conflicts}

def escape(value):
    return re.sub(r'([\\`*{}\[\]()#+.!|<>_~])', r'\\\1', str(value)).replace('\n',' ')

def evidence_preview(text):
    # Display only: keep the original transcript and evidence untouched.
    text = re.sub(r'([^\s，,。！？]{1,8}[，,、\s]*)\1{7,}', '〔重复片段已折叠〕', text)
    return text[:220]+('…' if len(text)>220 else '')

def public_markdown(markdown):
    """Apply current presentation to historical guides without changing evidence."""
    parts=re.split(r'(?m)^## ',markdown)
    result=[parts[0]]
    for part in parts[1:]:
        heading,_,body=part.partition('\n')
        if heading.strip()=='资料冲突与待核实项': continue
        if heading.strip()=='来源列表':
            keep=[]
            for line in body.splitlines():
                if line.startswith('### ') or line.startswith('[在 Bilibili 查看视频]('): keep.append(line)
                elif line.startswith('作者：'): keep.append(line.split(' · 分 P：',1)[0])
            body='\n\n'.join(keep)+'\n\n'
        result.append('## '+heading+'\n'+body)
    return ''.join(result)

def render(prefs, facts, sources, plan, warnings):
    refs = {s['id']:f'S{i+1}' for i,s in enumerate(sources)}
    by_id = {f['fact_id']:f for f in facts}
    lines = [f"# {escape(prefs['destination'])} · {prefs['days']} 日旅行攻略", '', '## 攻略概览', f"生成日期：{datetime.now().strftime('%Y-%m-%d %H:%M')} · 有效来源 {len(sources)} 个", f"{prefs['days']} 天 / {prefs['people']} 人 / {escape(prefs['pace'])}节奏 / 兴趣：{escape('、'.join(prefs['interests']) or '未指定')}", f"出行时间：{escape(prefs['date'] or '未提供，不推断季节')}；出发地：{escape(prefs['origin'] or '未提供，不生成具体往返班次')}", f"预算：{escape(prefs['budget'] or '未指定')}（人民币、人均，{'包含' if prefs['include_arrival'] else '不包含'}往返目的地交通）", f"其他要求：{escape(prefs['notes'] or '无')}，资料是否足以满足仍需逐项核对。", '', '> 本攻略依据视频语音或字幕整理，未分析画面。票价、营业时间与预约政策均需出行前核实。行程为资料编排建议，未验证地图距离、最短路线或通行时间。', '', '## 每日行程']
    coverage = ' / '.join(label+'：'+('有可用资料' if any(f['category']==category and not f.get('issue_flags') for f in facts) else '缺少资料') for label,category in [('住','住宿'),('行','交通'),('玩','景点'),('吃','美食')])
    lines[lines.index('## 每日行程'):lines.index('## 每日行程')] = ['**资料覆盖** · '+coverage, '', '标题仅用于寻找候选；以上按已提取且无疑点标记的资料统计。缺类时应补充来源后再形成完整行程。', '']
    def citation(f):
        note = '（待核实：'+escape('、'.join(str(x) for x in f.get('issue_flags',[])))+'）' if f.get('issue_flags') else ''
        return f"[{refs[f['source_id']]}](#source-{refs[f['source_id']]}){note}"
    for day in plan['days']:
        lines += ['',f"### 第 {day['day']} 天"]
        for period in ('上午','下午','晚上'):
            ids = [fid for fid in day[period] if not by_id[fid].get('issue_flags')]
            if not ids: lines += [f'- **{period}**：资料未覆盖，留待补充。']; continue
            for fid in ids:
                f=by_id[fid]; lines += [f"- **{period} · {escape(f['entity_name'])}**：{escape(f['claim'])} {citation(f)}"]
        lines += ['  安排理由：依据已有来源与出行偏好编排；交通衔接、开放时间和可行性需出行前确认。']
    for heading,category in [('景点推荐','景点'),('美食建议','美食'),('住宿区域建议','住宿'),('交通建议','交通')]:
        lines += ['', '## '+heading]
        matched=[f for f in facts if f['category']==category and not f.get('issue_flags')]
        lines += [f"- **{escape(f['entity_name'])}**：{escape(f['claim'])} {citation(f)}" for f in matched] or ['资料未覆盖。']
    lines += ['', '## 预算参考', '仅列来源中的金额，不将不同口径、不同币种或同一项目的报价相加。完整人均预算未知；以下不是实时价格或已验证总费用。', '', '| 项目 | 来源金额 | 口径 |', '| --- | --- | --- |']
    amounts=[f for f in facts if f['amount'] is not None]
    for f in amounts: lines += [f"| {escape(f['entity_name'])} | {Decimal(str(f['amount']))} {escape(f['currency'] or '币种未知')} {citation(f)} | 按原文核实人数、次数与日期 |"]
    if not amounts: lines += ['| 住宿 / 餐饮 / 交通 / 门票 | 未知 | 资料未覆盖 |']
    lines += ['', '## 避坑与准备事项']
    lines += [f"- {escape(f['claim'])} {citation(f)}" for f in facts if f['category'] in ('预约','注意事项')] or ['资料未覆盖。']
    lines += ['', '## 来源列表']
    for source in sources:
        m=source['meta']; ref=refs[source['id']]
        lines += ['',f"### {ref} · {escape(m['title'])}", f"作者：{escape(m['author'])} · 发布日期：{escape(m.get('published') or '未知')}", f"[在 Bilibili 查看视频]({m['url']})"]
    return '\n'.join(lines)

def generate(task_id, config, quality_pending=False, revision=None):
    task = store.task(task_id); prefs=task['prefs']
    sources=[s for s in store.sources(task_id) if s['selected'] and s['transcript']]
    all_facts=[]; usable=[]
    previous=None; decision=None; patch_result=None; allowed=None
    from . import revision as revisions
    if revision:
        with store.db() as c: previous=json.loads(c.execute('SELECT content FROM guides WHERE task_id=? AND version=?',(task_id,revision)).fetchone()[0])
        decision=__import__('backend.editorial',fromlist=['triage']).triage(task_id,revision,config)
        if not decision.get('search_complete'): raise InvalidOutput('已有证据检索尚未完成，不能修订或补视频')
        all_facts=revisions.revision_evidence(task_id,previous,decision)
        usable=[s for s in store.sources(task_id) if s['id'] in {f['source_id'] for f in all_facts}]
    for source in ([] if previous else sources):
        try: facts=extract(source,task_id,config)
        except InvalidOutput as exc:
            store.update_source(source['id'],status='failed',error=str(exc))
            store.event(task_id,'整理资料',str(exc),source['id'],level='warning')
            continue
        if facts: all_facts.extend(facts); usable.append(source)
    if not all_facts: raise ValueError('所有来源均未提供可验证的旅行事实，请补充视频链接')
    store.set_status(task_id,'generating'); store.event(task_id,'生成攻略','正在编排每日行程并检查多来源差异')
    from . import writing
    from . import editorial
    context={'issues':[{k:v for k,v in i.items() if k!='evidence'} for i in decision['issues']], 'previous_directory':previous.get('editorial',{}).get('directory') if previous.get('editorial',{}).get('rules_version')==revisions.RULES else None, 'previous_route':previous.get('editorial',{}).get('route'),'allowed_days':sorted({d for i in decision['issues'] if i['kind']!='text' for d in i['days']})} if previous else None
    config=dict(config,output_tokens=min(config['output_tokens'],8000))
    packet_config=dict(config,context_budget=config['context_budget']-len(store.dumps(context).encode())-min(12000,config['context_budget']//5))
    packet_config.pop('quality_feedback',None)
    import hashlib
    input_key='写作资料:'+hashlib.sha256(store.dumps({'rules':revisions.RULES,'prompt':writing.PROMPT_PATH.read_text(),'revision':revision,'preferences':prefs,'facts':all_facts,'context':context,'feedback':config.get('quality_feedback')}).encode()).hexdigest()
    with store.db() as c: checkpoint=c.execute('SELECT value FROM editorial_steps WHERE task_id=? AND step_key=?',(task_id,input_key)).fetchone()
    if checkpoint:
        saved_input=json.loads(checkpoint[0]); instruction=saved_input['instruction']; packet=saved_input['packet']
    else:
        instruction, packet = writing.build_packet(prefs,all_facts,usable,packet_config)
        with store.db() as c: c.execute('INSERT INTO editorial_steps VALUES(?,?,?)',(task_id,input_key,store.dumps({'instruction':instruction,'packet':packet})))
    if previous:
        # Preserve old citations distinctly, but always retain their actual evidence.
        cited={f for b in revisions.blocks(previous['writing']).values() for f in b.get('fact_ids',[])}
        cited|={f for p in previous.get('editorial',{}).get('directory',{}).get('places',[]) for f in p['fact_ids']}
        relevant={h for i in decision['issues'] for f in i['evidence']['findings'] for h in f['hit_ids']}
        packet['facts']=[{k:f[k] for k in ('fact_id','category','entity_name','claim','source_id','evidence_excerpt')} for f in all_facts if f['fact_id'] in cited|relevant]
        packet['old_reference_ids']=sorted(cited)
        packet['omitted_count']=len(all_facts)-len(packet['facts'])
    if previous and previous.get('editorial',{}).get('rules_version')==revisions.RULES and not context['allowed_days']:
        route_info=previous['editorial']
        editorial.validate_route(route_info['route'],route_info['directory'],packet)
    else:
        route_info=editorial.route(task_id,packet,config,context)
    packet['directory']=route_info['directory']
    packet['route']=json.loads(store.dumps(route_info['route']))
    places={p['place_id']:p for p in route_info['directory']['places']}
    for day in packet['route'].get('days',[]):
        for activity in day['activities']:
            place=places[activity['place_id']]
            activity.update(name=place['name'],region=place['region'],fact_ids=place['fact_ids'])
    packet['route_check']=route_info['route_check']
    if context: packet['revision_context']=context
    minimal = packet['facts']
    if packet['omitted_count']:
        store.event(task_id,'生成攻略','按住行玩吃与来源均衡选取写作资料；未发送的资料仍保留在来源中',level='warning')
    writer_key='正文完成:'+revisions.digest([input_key,route_info,instruction])
    with store.db() as c: finished=c.execute('SELECT value FROM editorial_steps WHERE task_id=? AND step_key=?',(task_id,writer_key)).fetchone()
    if finished:
        saved_prose=json.loads(finished[0]); plan=saved_prose['plan']; prose=saved_prose['prose']; patch_result=saved_prose.get('patch'); allowed=set(saved_prose['allowed']) if saved_prose.get('allowed') is not None else None
    error=''
    for attempt in ([] if finished else range(2)):
        try:
            if previous:
                updated,patch_result,allowed=revisions.revise_prose(task_id,previous,packet,decision,config)
                result={'writing':updated}
            else:
                result=llm_json(config,task_id,instruction+(' 上次检查错误：'+error if attempt else ''),packet)
            result=writing.normalize_reference_prefixes(result,packet)
            plan=validate_plan(writing.plan_from_writing(result,packet,prefs['days']),minimal,prefs['days'],prefs)
            validated=writing.validate_writing(result,packet,plan,defer_numeric_review=quality_pending)
            if previous:
                # Validation must not normalize or clean untouched legacy paragraphs.
                cleaned=revisions.blocks(validated)
                normalized={'patches':[dict(p,block=cleaned[p['target']]) for p in patch_result['patches']],'unresolved':patch_result['unresolved']}
                prose=revisions.apply_patch(previous['writing'],normalized,allowed,{f['fact_id'] for f in packet['facts']})
            else: prose=writing.clean_prose(validated)
            writing.validate_route_links(prose,route_info['route'],allowed)
            def check_body(value):
                if not value['valid'] or value['issues']: raise InvalidOutput('正文越过路线约束：'+'；'.join(value['issues']))
                return value
            editorial.structured(task_id,'正文路线核对:'+revisions.digest([prose,route_info]),editorial.Gate,'独立核对正文是否新增核心地点、改变先后顺序/住宿终点/跨日移动，或只写风险提醒仍保留不成立的必要路段。逐段对照路线节点与原文，不能相信自报未改路线。包含概览及主题章节；推荐备选不等于核心行程，但不能暗中改变路线。无法确定或必须整改则valid=false且写issues。核对交通完整线路与子段时长、窗口、重复路段取舍及条件分支实际删减。不推断没有原文支持的地理关系。',{'writing':prose,'route':route_info,'facts':packet['facts'],'preferences':prefs},dict(config,output_tokens=min(config['output_tokens'],2400)),check_body)
            with store.db() as c: c.execute('INSERT INTO editorial_steps VALUES(?,?,?)',(task_id,writer_key,store.dumps({'plan':plan,'prose':prose,'patch':patch_result,'allowed':sorted(allowed) if allowed is not None else None})))
            break
        except InvalidOutput as exc:
            error=str(exc)
            if attempt: raise
    warnings=[]
    missing = [label for label, category in [('住','住宿'),('行','交通'),('玩','景点'),('吃','美食')] if not any(f['category']==category and not f.get('issue_flags') for f in all_facts)]
    if missing: warnings.append('有效原文资料缺少「'+'、'.join(missing)+'」，当前仅为部分攻略，需要补充相关来源。')
    if any(f.get('issue_flags') for f in all_facts): warnings.append('待核实条目保留在资料章节，暂不纳入每日行程。')
    if len(minimal)<sum(not f.get('issue_flags') for f in all_facts): warnings.append('受模型上下文预算限制，每日行程仅依据部分事实编排；完整资料保留在其他章节及来源中。')
    all_sources=store.sources(task_id)
    if len(usable)<3: warnings.append('有效来源少于 3 个，建议补充资料后再安排行程。')
    if any(s['selected'] and (s['status']=='failed' or not s['complete']) for s in all_sources): warnings.append('部分视频或音频片段处理失败，来源详情保留缺失范围。')
    if any('较旧' in s['reason'] or (s['meta'].get('published') and s['meta']['published'] < datetime.fromtimestamp(store.now()-365*86400).strftime('%Y%m%d')) for s in usable): warnings.append('包含较旧来源，其中的政策与价格可能已变化。')
    if len(all_facts) < prefs['days']*3: warnings.append('资料不足以覆盖全部时段；空缺部分不作无依据补全。')
    store.event(task_id,'检查结果','已检查行程天数、事实编号与原文摘录')
    usable=[s for s in store.sources(task_id) if s['id'] in {x['id'] for x in usable}]
    markdown=writing.render_prose(render(prefs,all_facts,usable,plan,warnings),prose,all_facts,usable,escape)
    changes=[]
    if previous:
        if previous['writing'].get('overview')!=prose['overview']: changes.append('攻略概览')
        for old,new in zip(previous['writing'].get('days',[]),prose['days']):
            for period in ('上午','下午','晚上'):
                if old.get(period)!=new[period]: changes.append(f"第{new['day']}天·{period}")
        for heading,blocks in prose['sections'].items():
            if previous['writing'].get('sections',{}).get(heading)!=blocks: changes.append(heading)
    check(task_id)
    with store.db() as c:
        c.execute('BEGIN IMMEDIATE')
        current=c.execute('SELECT status FROM tasks WHERE id=?',(task_id,)).fetchone()
        if not current or current['status']=='cancelled':
            from .security import Cancelled
            raise Cancelled()
        version=c.execute('SELECT COALESCE(MAX(version),0)+1 FROM guides WHERE task_id=?',(task_id,)).fetchone()[0]
        c.execute('INSERT INTO guides VALUES(?,?,?,?,?,?,?,?)',(task_id,version,store.dumps({'plan':plan,'writing':prose,'writing_input':packet,'writing_prompt':instruction,'facts':all_facts,'warnings':warnings,'source_ids':[s['id'] for s in usable],'editorial':route_info,'revision_of':revision,'revision_changes':changes,'revision_context':context,'revision_decision':decision,'revision_patch':patch_result,'block_hashes':{k:revisions.digest(v) for k,v in revisions.blocks(prose).items()}}),markdown,store.dumps(prefs),config['llm_model'],PROMPT_VERSION+'+'+writing.VERSION,store.now()))
        c.execute("UPDATE tasks SET status=?,error='',updated=? WHERE id=?",('generating' if quality_pending else ('completed_with_warnings' if warnings else 'completed'),store.now(),task_id))
        if revision:
            c.execute('UPDATE quality_cycles SET revision_round=revision_round+1 WHERE task_id=?',(task_id,))
        if quality_pending:
            c.execute("UPDATE quality_cycles SET phase='review',version=? WHERE task_id=?",(version,task_id))
    store.event(task_id,'等待审核' if quality_pending else '已完成',f'第 {version} 版攻略已保存'+('，接下来由 GLM 审核' if quality_pending else ''))
    return version
