"""Versioned GLM editorial prompt, evidence packet and output validation."""
import json
import re
from decimal import Decimal
from pathlib import Path
from .security import InvalidOutput

PROMPT_PATH = Path(__file__).resolve().parent.parent/'prompts/glm_travel_writer.md'
VERSION = 'travel-writer-v3'
HEADINGS = ('景点推荐','美食建议','住宿区域建议','交通建议','预算参考','避坑与准备事项')
EMPTY = '资料未覆盖，建议留作休息或自由活动。'

def normalize_reference_prefixes(result, packet):
    """Repair only a duplicated F prefix of an otherwise exact known ID."""
    known={f['fact_id'] for f in packet['facts']}
    def visit(value):
        if isinstance(value,dict):
            for key,item in value.items():
                if key=='fact_ids' and isinstance(item,list):
                    value[key]=['F'+i[2:] if isinstance(i,str) and i not in known and i.startswith(('FF','Ff')) and 'F'+i[2:] in known else i for i in item]
                else: visit(item)
        elif isinstance(value,list):
            for item in value: visit(item)
    visit(result)
    return result

def focus_evidence(fact):
    """Select original sentences for the writing packet; never rewrite evidence."""
    original=fact['evidence_excerpt']
    sentences=[m.group() for m in re.finditer(r'[^。！？!?\n]+[。！？!?\n]*',original)]
    if len(sentences)<2: return original
    claim=fact['entity_name']+fact['claim']
    grams={claim[i:i+2] for i in range(len(claim)-1)}
    ranked=sorted(range(len(sentences)),key=lambda i:sum(g in sentences[i] for g in grams),reverse=True)
    chosen=set(ranked[:2])
    # Keep the sentence containing each written number to retain numerical context.
    for n in re.findall(r'\d+(?:\.\d+)?',fact['claim']):
        found=next((i for i in ranked if n in sentences[i]),None)
        if found is not None: chosen.add(found)
    return ' […] '.join(sentences[i].strip() for i in sorted(chosen))

def build_packet(prefs, facts, sources, config):
    prompt=PROMPT_PATH.read_text()
    packet={'preferences':dict(prefs,budget_unit='人民币/人',budget_scope='表单预算为人均上限；只有用户其他要求明确另述时才按其要求处理'),'sources':[{'source_id':s['id'],'title':s['meta']['title'],'published':s['meta'].get('published'),'url':s['meta']['url'],'author':s['meta'].get('author','未知')} for s in sources], 'facts':[], 'coverage':{},'omitted_count':0}
    if config.get('quality_feedback'): packet['previous_review']=config['quality_feedback']
    good=[f for f in facts if not f.get('issue_flags')]
    categories=('住宿','交通','景点','美食','预约','注意事项','费用')
    groups=[[f for f in good if f['category']==c] for c in categories]
    # Rotate categories AND sources; food-heavy inputs cannot consume every slot.
    for group in groups:
        buckets=[[f for f in group if f['source_id']==s['id']] for s in sources]
        group[:]=[]
        while any(buckets):
            for bucket in buckets:
                if bucket: group.append(bucket.pop(0))
    for c in categories: packet['coverage'][c]=any(f['category']==c for f in good)
    limit=config['context_budget']-config['output_tokens']-len(prompt.encode())-1200
    schedule=[groups[0],groups[1],groups[2],groups[3],groups[2],*groups[4:]]
    while any(groups):
        for group in schedule:
            if not group: continue
            f=group.pop(0)
            row={k:f[k] for k in ('fact_id','category','entity_name','claim','source_id')}
            row['evidence_excerpt']=focus_evidence(f)
            packet['facts'].append(row)
            if len(json.dumps(packet,ensure_ascii=False).encode())>limit: packet['facts'].pop()
    packet['omitted_count']=len(good)-len(packet['facts'])
    if not packet['facts']: raise InvalidOutput('写作提示词与资料超过上下文预算，请增加上下文预算')
    for c in ('住宿','交通','景点','美食'):
        if packet['coverage'][c] and not any(f['category']==c for f in packet['facts']):
            raise InvalidOutput('上下文预算无法同时容纳住行玩吃的原文证据，请增加上下文预算')
    return prompt,packet

def validate_writing(result, packet, plan, defer_numeric_review=False):
    value=result.get('writing')
    if not isinstance(value,dict): raise InvalidOutput('缺少完整攻略正文 writing')
    facts={f['fact_id']:f for f in packet['facts']}
    def block(b, allowed=None):
        if not isinstance(b,dict) or not isinstance(b.get('text'),str) or not 1<=len(b['text'])<=1200: raise InvalidOutput('正文段落格式或长度错误')
        ids=b.get('fact_ids')
        if not isinstance(ids,list): raise InvalidOutput('fact_ids 必须是字符串数组')
        unknown=[str(i)[:60] for i in ids if not isinstance(i,str) or i not in facts]
        if unknown:
            raise InvalidOutput('正文引用了未发送的事实编号：'+','.join(unknown[:8])+'。请只使用本轮 facts 中的 fact_id，不要照抄 previous_review 中的审核别名')
        kind=b.get('kind','fact')
        if kind not in ('fact','planning'): raise InvalidOutput('段落类型错误')
        if kind=='planning' and not any(t in b['text'] for t in ('建议','估算','待确认','默认','未提供','未覆盖')):
            b=dict(b,text='建议：'+b['text'])
        if not ids and kind!='planning':
            # No uncited factual prose reaches the guide. Normalize free-form
            # missing-information notices instead of failing the whole output.
            raise InvalidOutput('事实段落缺少引用；缺项请返回空章节，规划请标注 kind=planning')
        evidence=' '.join(facts[i]['claim']+' '+facts[i]['evidence_excerpt'] for i in ids)
        evidence+=' '+json.dumps(packet.get('preferences',{}),ensure_ascii=False)
        nums=lambda text:{Decimal(n) for n in re.findall(r'\d+(?:\.\d+)?',text)}
        # Citation IDs are identifiers, never travel quantities. The renderer
        # already supplies source links, so remove redundant inline IDs.
        text=b['text']
        for fid in sorted(ids,key=len,reverse=True):
            text=re.sub(r'(?<![A-Za-z0-9])'+re.escape(fid)+r'(?![A-Za-z0-9])','',text)
        text=re.sub(r'[（(]\s*[）)]','',text)
        unmatched=nums(text)-nums(evidence)
        if unmatched and not defer_numeric_review and kind=='fact': raise InvalidOutput('正文包含引用没有支持的数字')
        clean={'text':text,'fact_ids':list(dict.fromkeys(ids)), 'kind':kind}
        if 'route_node_ids' in b: clean['route_node_ids']=b['route_node_ids']
        if unmatched and defer_numeric_review:
            clean['numeric_checks']=[str(n) for n in sorted(unmatched)]
        return clean
    overview=block(value.get('overview'))
    days=value.get('days')
    if not isinstance(days,list) or len(days)!=len(plan['days']): raise InvalidOutput('正文天数错误')
    clean_days=[]
    for d,p in zip(days,plan['days']):
        if not isinstance(d,dict) or d.get('day')!=p['day']: raise InvalidOutput('正文日期顺序错误')
        clean_days.append({'day':p['day'],**{period:block(d.get(period),p[period]) for period in ('上午','下午','晚上')}})
    sections=value.get('sections')
    if not isinstance(sections,dict) or any(h not in sections for h in HEADINGS): raise InvalidOutput('攻略正文章节不完整')
    clean={}
    for h in HEADINGS:
        if not isinstance(sections[h],list) or len(sections[h])>6: raise InvalidOutput('正文章节段落过多或格式错误')
        clean[h]=[block(b) for b in sections[h]]
    for h,c in [('景点推荐','景点'),('美食建议','美食'),('住宿区域建议','住宿'),('交通建议','交通')]:
        if not defer_numeric_review and any(f['category']==c for f in facts.values()) and not any(facts[i]['category']==c for b in clean[h] for i in b['fact_ids']):
            raise InvalidOutput('正文未覆盖已有资料：'+h)
    return {'overview':overview,'days':clean_days,'sections':clean}

def plan_from_writing(result, packet, day_count):
    value=result.get('writing',{})
    days=value.get('days') if isinstance(value,dict) else None
    if not isinstance(days,list) or len(days)!=day_count: raise InvalidOutput('正文天数错误')
    facts={f['fact_id']:f for f in packet['facts']}
    plan=[]
    for index,d in enumerate(days,1):
        if not isinstance(d,dict) or d.get('day')!=index: raise InvalidOutput('正文日期顺序错误')
        row={'day':index}
        for p in ('上午','下午','晚上'):
            block=d.get(p)
            if not isinstance(block,dict) or not isinstance(block.get('fact_ids'),list): raise InvalidOutput('每日正文格式错误')
            row[p]=list(dict.fromkeys(i for i in block['fact_ids'] if isinstance(i,str) and i in facts and facts[i]['category'] in ('景点','美食')))[:5]
        plan.append(row)
    return {'days':plan,'conflicts':result.get('conflicts',[])}

def render_prose(base, prose, facts, sources, escape):
    byid={f['fact_id']:f for f in facts}; refs={s['id']:f'S{i+1}' for i,s in enumerate(sources)}
    def paragraph(b):
        labels=list(dict.fromkeys(refs[byid[i]['source_id']] for i in b['fact_ids']))
        return escape(b['text'])+' '+ ' '.join(f'[{r}](#source-{r})' for r in labels)
    parts=re.split(r'(?m)^## ',base)
    output=[parts[0]]
    for part in parts[1:]:
        heading,_,body=part.partition('\n')
        if heading=='攻略概览': body=body.rstrip()+'\n\n'+paragraph(prose['overview'])+'\n\n'
        elif heading=='每日行程':
            lines=[]
            for d in prose['days']:
                lines+=['',f"### 第 {d['day']} 天",'']
                for p in ('上午','下午','晚上'): lines += [f'**{p}** · '+paragraph(d[p]),'']
            body='\n'.join(lines)+'\n'
        elif heading in HEADINGS:
            body='\n\n'.join(paragraph(b) for b in prose['sections'][heading]) or '资料未覆盖，需补充来源。'
            body+='\n\n'
        output.append('## '+heading+'\n'+body)
    return ''.join(output)


def clean_prose(prose):
    """Only remove exact duplicate reminders and known placeholders before audit."""
    for heading, blocks in prose['sections'].items():
        seen=set(); kept=[]
        for b in blocks:
            if b['text'].strip() in (EMPTY,'资料未覆盖，留待补充。'): continue
            key=b['text'].strip()
            if key not in seen: kept.append(b); seen.add(key)
        prose['sections'][heading]=kept
    return prose


def validate_route_links(prose,route,required_targets=None):
    from .revision import blocks
    byday={d['day']:d for d in route['days']}
    for target,block in blocks(prose).items():
        if not target.startswith('day:') or required_targets is not None and target not in required_targets: continue
        day=byday.get(int(target.split(':')[1]))
        if day is None: raise InvalidOutput('正文日期不在已检查路线')
        allowed={a['node_id'] for a in day['activities']}|{l['node_id'] for l in day['legs']}|{f"D{day['day']}-start",f"D{day['day']}-end"}
        ids=block.get('route_node_ids')
        if not isinstance(ids,list) or not ids or any(not isinstance(i,str) or i not in allowed for i in ids) or len(ids)!=len(set(ids)): raise InvalidOutput('每日正文缺少路线关联或引用了其他日期节点')
    return prose
