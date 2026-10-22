import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from . import store
from .security import Attention, Cancelled, canonical_video, filter_cookies, get_public, public_url

def check(task_id):
    if task_id and store.task(task_id)['status'] == 'cancelled': raise Cancelled()

def run(args, task_id=None, timeout=300, allow_failure=False):
    check(task_id)
    # Disk-backed output avoids subprocess pipe deadlock for long downloads.
    with tempfile.TemporaryFile() as output:
        proc = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
        start = time.monotonic()
        try:
            while proc.poll() is None:
                check(task_id)
                if time.monotonic() - start > timeout: raise ValueError('本地处理超时，请重试或选择较短视频')
                time.sleep(.2)
        finally:
            if proc.poll() is None:
                import signal
                os.killpg(proc.pid, signal.SIGTERM)
                try: proc.wait(timeout=3)
                except subprocess.TimeoutExpired: os.killpg(proc.pid, signal.SIGKILL); proc.wait()
        output.seek(0)
        text = output.read(25_000_000).decode('utf-8', errors='replace')
    if proc.returncode and not allow_failure:
        if any(x in text.lower() for x in ('403', '412', '429', 'search rejected request')):
            raise Attention('Bilibili 暂时拒绝搜索或媒体请求；已暂停连续重试，请稍后再试。登录状态需单独验证。')
        if any(x in text.lower() for x in ('login', 'cookie', 'premium', '会员', '登录')):
            raise Attention('Bilibili 要求登录或限制访问，请在设置中导入 Cookie，或稍后重试')
        raise ValueError('视频或媒体处理失败，请检查视频是否可访问、网络及环境诊断')
    return text

def ytdlp(args, task_id, config, timeout=180):
    cookie_path = None
    try:
        extra = []
        if config.get('cookies'):
            fd, cookie_path = tempfile.mkstemp(dir=store.DATA, suffix='.cookies')
            with os.fdopen(fd, 'w') as f: f.write(filter_cookies(config['cookies']))
            extra = ['--cookies', cookie_path]
        return run([sys.executable, '-m', 'backend.ytdlp_guard', '--ignore-config', '--no-warnings', '--no-progress', '--socket-timeout', '15', '--retries', '1', '--extractor-retries', '1', '--no-playlist', *extra, *args], task_id, timeout)
    finally:
        if cookie_path: Path(cookie_path).unlink(missing_ok=True)

def metadata(url, task_id, config):
    canonical = canonical_video(url)
    text = ytdlp(['--dump-single-json', '--skip-download', canonical], task_id, config)
    info = json.loads(text)
    if info.get('_type') == 'playlist': raise ValueError('无法确定选中的分 P，请使用带 p 参数的标准视频链接')
    match = re.search(r'BV[0-9A-Za-z]{10}', info.get('webpage_url', '') + ' ' + str(info.get('bvid', '')) + ' ' + str(info.get('id', '')))
    part = int(parse_qs(urlparse(canonical).query)['p'][0])
    if match: canonical = f'https://www.bilibili.com/video/{match[0]}?p={part}'
    return {'url': canonical, 'bvid': match[0] if match else canonical.split('/video/')[1].split('?')[0], 'part': part, 'cid': info.get('id'), 'title': info.get('title') or '未知标题', 'author': info.get('uploader') or '未知', 'published': info.get('upload_date'), 'duration': info.get('duration'), 'views': info.get('view_count'), 'description': (info.get('description') or '')[:10000], 'fetched': store.now()}, info

TRAVEL_TOPICS = {
    '住': ('住宿', '酒店', '民宿', '住哪里', '住哪', '青旅'),
    '行': ('交通', '地铁', '公交', '机场', '高铁', '出行'),
    '玩': ('景点', '游玩', '景区', '游览', '博物馆', '公园'),
    '吃': ('美食', '小吃', '餐厅', '火锅', '面馆'),
}

def source_topics(source):
    # Titles are candidate hints, never evidence that the transcript covers a topic.
    title = source['meta']['title']
    return {topic for topic, words in TRAVEL_TOPICS.items() if any(word in title for word in words)}

def missing_topics(sources):
    covered = set().union(*(source_topics(s) for s in sources)) if sources else set()
    return [topic for topic in TRAVEL_TOPICS if topic not in covered]

def search(destination, interests, task_id, config):
    terms = ['住宿 住哪里', '交通 出行攻略', '景点 游玩攻略', '美食 小吃攻略']
    terms += [x for x in interests if x not in terms][:max(0, config['search_groups']-4)]
    if config.get('evidence_queries'): terms=config['evidence_queries'][:5]
    results, seen = [], set()
    scope = hashlib.sha256(config.get('cookies','').encode()).hexdigest()
    for term in terms:
        check(task_id)
        query = term if config.get('evidence_queries') else destination+' '+term
        key = hashlib.sha256(json.dumps(['wbi-v1',scope,query,config['search_count']]).encode()).hexdigest()
        with store.db() as c:
            cached = c.execute('SELECT urls FROM search_cache WHERE cache_key=? AND created>?',(key,store.now()-21600)).fetchone()
            pacing = c.execute('SELECT next_at FROM search_pacing WHERE scope=?',('bilibili',)).fetchone()
        if cached:
            links=json.loads(cached['urls'])
            store.event(task_id,'搜索视频',f'复用 B 站搜索缓存「{query}」')
        else:
            delay=max(0,(pacing['next_at'] if pacing else 0)-store.now())
            if delay>5:
                store.event(task_id,'搜索视频','B 站请求处于冷却期，请稍后再试；已取得的候选保留。',level='warning')
                continue
            until=time.monotonic()+delay
            while time.monotonic()<until:
                check(task_id); time.sleep(min(.2,max(0,until-time.monotonic())))
            store.event(task_id, '搜索视频', f'正在搜索「{query}」')
            with store.db() as c: c.execute('INSERT OR REPLACE INTO search_pacing VALUES(?,?)',('bilibili',store.now()+2))
            try:
                raw=ytdlp(['--dump-single-json','--flat-playlist',f"bilisearch{config['search_count']}:{query}"],task_id,config)
                links=[]
                for entry in json.loads(raw).get('entries',[]):
                    url=entry.get('url') or entry.get('webpage_url') or entry.get('id','')
                    try: url=canonical_video(url.replace('http://www.bilibili.com/','https://www.bilibili.com/',1))
                    except ValueError: continue
                    if url not in links: links.append(url)
                if links:
                    with store.db() as c: c.execute('INSERT OR REPLACE INTO search_cache VALUES(?,?,?)',(key,store.dumps(links),store.now()))
            except Cancelled: raise
            except Exception as exc:
                from .security import safe_error
                store.event(task_id,'搜索视频',safe_error(exc),level='warning')
                with store.db() as c: c.execute('INSERT OR REPLACE INTO search_pacing VALUES(?,?)',('bilibili',store.now()+300))
                continue
        for url in links:
            if url not in seen: seen.add(url);results.append(url)
    return results

def choose(candidates, destination, config):
    selected, total, authors = [], 0, set()
    cutoff = time.time() - config['recent_months'] * 30.44 * 86400
    def rank(item):
        m = item['meta']; title = m['title']
        topic = sum(word in title for word in ('攻略', '交通', '避坑', '行程', '美食', '住宿', '酒店', '景点', '游玩'))
        return (topic, m.get('published') or '', m.get('views') or 0)
    remaining = sorted(candidates, key=rank, reverse=True)
    reasons = {}
    while remaining:
        # Prefer a new author among equally practical candidates.
        covered = set().union(*(source_topics(x) for x in candidates if x['id'] in selected)) if selected else set()
        # Reserve scarce categories first so food cannot consume every slot.
        eligible = [x for x in remaining if destination.lower() in (x['meta']['title']+x['meta'].get('description','')).lower() and 0 < (x['meta'].get('duration') or 0) <= config['max_video_minutes']*60 and total+(x['meta'].get('duration') or 0)<=config['max_minutes']*60]
        needed = [t for t in TRAVEL_TOPICS if t not in covered and any(t in source_topics(x) for x in eligible)]
        target = min(needed, key=lambda t: sum(t in source_topics(x) for x in eligible)) if needed else None
        pool = [x for x in eligible if target in source_topics(x)] if target else eligible
        best = max(pool, key=lambda x: (len(source_topics(x)-covered), rank(x)[0], x['meta']['author'] not in authors, rank(x)[1:])) if pool else remaining[0]
        i = remaining.index(best)
        item = remaining.pop(i); m = item['meta']; duration = m.get('duration')
        if destination.lower() not in (m['title'] + m.get('description', '')).lower(): reason = '目的地未明确匹配，可手动确认'
        elif not duration: reason = '时长未知，无法校验资源限制'
        elif duration > config['max_video_minutes'] * 60: reason = '超过单条时长上限'
        elif len(selected) >= min(5, config['max_videos']): reason = '达到视频数量上限'
        elif total + duration > config['max_minutes'] * 60: reason = '超过总时长上限'
        else:
            selected.append(item['id']); total += duration; authors.add(m['author']); reason = '主题候选：'+('、'.join(t for t in TRAVEL_TOPICS if t in source_topics(item)) or '综合/未明确')+'；优先补齐住、行、玩、吃，内容待原文验证'
        if m.get('published'):
            try:
                if time.mktime(time.strptime(m['published'], '%Y%m%d')) < cutoff: reason += '；资料可能较旧'
            except ValueError: pass
        reasons[item['id']] = reason
    return selected, reasons

def parse_subtitle(raw, duration, min_chars=100, min_coverage=.5):
    cues = []
    try:
        body = json.loads(raw).get('body', [])
        cues = [{'start': float(x['from']), 'end': float(x['to']), 'text': str(x['content'])} for x in body]
    except (ValueError, KeyError, TypeError, AttributeError):
        def seconds(v):
            bits = v.replace(',', '.').split(':')
            return sum(float(x) * 60 ** i for i, x in enumerate(reversed(bits)))
        for block in re.split(r'\n\s*\n', raw.replace('\r', '')):
            lines = block.strip().splitlines()
            idx = next((i for i, x in enumerate(lines) if '-->' in x), -1)
            if idx < 0: continue
            try:
                a, b = lines[idx].split('-->')
                cues.append({'start': seconds(a.strip()), 'end': seconds(b.strip().split()[0]), 'text': '\n'.join(lines[idx+1:])})
            except ValueError: continue
    cleaned, coverage, previous, union_end = [], 0., -1., 0.
    for cue in cues:
        a, b = cue['start'], cue['end']
        if a < previous or a < 0 or b <= a or (duration and b > duration + 10): raise ValueError('字幕时间顺序或范围无效')
        previous = a
        body = html.unescape(re.sub('<[^>]*>', '', cue['text'])).strip()
        if not body: continue
        coverage += max(0, b - max(a, union_end)); union_end = max(union_end, b)
        cleaned.append({'start': a, 'end': b, 'text': body})
    text = '\n'.join(c['text'] for c in cleaned)
    if len(re.sub(r'\W', '', text)) < min_chars: raise ValueError('字幕有效文字不足')
    if not duration or coverage / duration < min_coverage: raise ValueError('字幕覆盖范围不足')
    return {'raw': raw, 'text': text, 'segments': cleaned, 'type': 'subtitle', 'precision': 'subtitle', 'complete': True, 'hash': hashlib.sha256(raw.encode()).hexdigest()}

def subtitle(info, config):
    tracks = []
    for kind in ('subtitles', 'automatic_captions'):
        for lang, items in (info.get(kind) or {}).items():
            if not any(x in lang.lower() for x in ('zh', 'cn', 'chinese')): continue
            for item in items:
                if item.get('ext') not in ('srt', 'vtt', 'json'): continue
                if any(x in (lang + str(item)).lower() for x in ('danmaku', 'danmu', '弹幕', 'xml')): continue
                tracks.append((kind, lang, item))
    failures = []
    for kind, lang, item in tracks:
        try:
            raw = item.get('data') or get_public(item['url'])
            result = parse_subtitle(raw, info.get('duration'), config['subtitle_chars'], config['subtitle_coverage'])
            result.update(language=lang, subtitle_kind=kind)
            return result
        except Exception as exc: failures.append(type(exc).__name__)
    raise ValueError('没有合格中文字幕' + ('，字幕解析或覆盖检查未通过' if failures else ''))

def ffmpeg():
    binary = shutil.which('ffmpeg')
    if binary: return binary
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception: raise Attention('缺少 FFmpeg，请先运行安装脚本或安装 FFmpeg')

def probe(path, task_id=None):
    binary = shutil.which('ffprobe')
    if binary:
        raw = json.loads(run([binary, '-v', 'error', '-show_format', '-show_streams', '-of', 'json', str(path)], task_id))
        if not any(s['codec_type'] == 'audio' for s in raw['streams']): raise ValueError('媒体没有音轨')
        return float(raw['format']['duration'])
    raw = run([ffmpeg(), '-hide_banner', '-i', str(path)], task_id, allow_failure=True)
    match = re.search(r'Duration: (\d+):(\d+):(\d+(?:\.\d+)?)', raw)
    if not match or 'Audio:' not in raw: raise ValueError('媒体不可解码或没有音轨')
    return float(match[1])*3600 + float(match[2])*60 + float(match[3])

def download_formats(info):
    candidates=[]
    for item in info.get('formats',[]):
        if item.get('acodec')=='none' or not item.get('url'): continue
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',str(item.get('format_id',''))): continue
        try: public_url(item['url'],media=True)
        except ValueError: continue
        candidates.append(item)
    if not candidates: raise ValueError('没有符合安全限制的可用音频格式，请换一个视频或稍后重试')
    audio=[f for f in candidates if f.get('vcodec')=='none'] or candidates
    audio.sort(key=lambda f:float(f.get('abr') or f.get('tbr') or 0),reverse=True)
    return [str(audio[0]['format_id']),str(audio[-1]['format_id'])]


def download(source, info, task_id, config):
    folder = store.DATA / 'media' / task_id / source['id']; folder.mkdir(parents=True, exist_ok=True)
    if shutil.disk_usage(folder).free < 512 * 1024 ** 2: raise Attention('磁盘可用空间不足 512 MB，请先清理')
    formats=download_formats(info)
    existing = list(folder.glob('original.*'))
    for attempt, fmt in enumerate(formats):
        try:
            if not existing:
                ytdlp(['-f', fmt, '--max-filesize', '512M', '--restrict-filenames', '-o', str(folder / 'original.%(ext)s'), source['meta']['url']], task_id, config, timeout=900)
                existing = [p for p in folder.glob('original.*') if p.suffix not in ('.part', '.ytdl')]
            if not existing or existing[0].stat().st_size == 0: raise ValueError('未获得完整媒体文件')
            path = existing[0]; duration = probe(path, task_id)
            expected = source['meta'].get('duration')
            if not expected or duration < expected * .9 or duration > config['max_video_minutes'] * 60 + 5: raise ValueError('实际媒体时长与视频不符，可能仅下载了试听片段')
            # Decode the whole audio to ensure corruption is found before a paid call.
            run([ffmpeg(), '-v', 'error', '-xerror', '-i', str(path), '-map', '0:a:0', '-f', 'null', '-'], task_id, 600)
            return path, duration
        except (Cancelled, Attention): raise
        except Exception:
            for p in existing: p.unlink(missing_ok=True)
            existing = []
            if attempt: raise

def boundaries(duration, size, overlap, silences=()):
    start = 0.
    while start < duration:
        target = min(duration, start + size)
        nearby = [s for s in silences if abs(s-target) <= 8 and s > start + 10]
        end = min(nearby, key=lambda x: abs(x-target)) if target < duration and nearby else target
        yield round(start, 3), round(end, 3)
        if end >= duration: break
        start = end - overlap

def chunks(path, duration, source_id, task_id, config):
    with store.db() as c: existing = c.execute('SELECT * FROM chunks WHERE source_id=? ORDER BY start', (source_id,)).fetchall()
    if existing:
        for row in existing:
            if row['status']=='completed': continue
            target=Path(row['path'])
            if not target.is_file() or target.stat().st_size==0:
                run([ffmpeg(),'-v','error','-y','-ss',str(row['start']),'-i',str(path),'-t',str(row['end']-row['start']),'-vn','-ac','1','-ar','16000','-b:a','64k',str(target)],task_id)
                if target.stat().st_size>config['chunk_mb']*1024**2: raise ValueError('恢复后的分片超过大小限制')
                digest=hashlib.sha256(target.read_bytes()).hexdigest()
                with store.db() as c: c.execute('UPDATE chunks SET hash=? WHERE id=?',(digest,row['id']))
        with store.db() as c: return [dict(x) for x in c.execute('SELECT * FROM chunks WHERE source_id=? ORDER BY start',(source_id,))]
    silence = run([ffmpeg(), '-hide_banner', '-i', str(path), '-af', 'silencedetect=noise=-30dB:d=0.4', '-f', 'null', '-'], task_id, 600)
    points = [float(x) for x in re.findall(r'silence_end: ([\d.]+)', silence)]
    chunk_size = min(config['chunk_seconds'], int(config['chunk_mb'] * 1024 * 1024 / 9000))
    result = []
    for a, b in boundaries(duration, chunk_size, config['overlap_seconds'], points):
        check(task_id); cid = hashlib.sha256(f'{source_id}:{a}:{b}:moss-transcribe-1.0:mp3-v1'.encode()).hexdigest()[:32]
        target = path.parent / f'{cid}.mp3'
        run([ffmpeg(), '-v', 'error', '-y', '-ss', str(a), '-i', str(path), '-t', str(b-a), '-vn', '-ac', '1', '-ar', '16000', '-b:a', '64k', str(target)], task_id)
        if target.stat().st_size > config['chunk_mb'] * 1024 ** 2: raise ValueError('音频分片超过大小上限，请降低分段时长')
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        row = dict(id=cid, source_id=source_id, start=a, end=b, hash=digest, status='pending', text=None, raw=None, error='', path=str(target))
        result.append(row)
    # All segment boundaries are committed together, so a crash cannot omit the tail.
    with store.db() as c:
        for row in result: c.execute('INSERT OR IGNORE INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?)', tuple(row.values()))
    return result

def merge_text(parts):
    result = ''
    for part in parts:
        overlap = 0
        for length in range(min(500, len(result), len(part)), 3, -1):
            if result[-length:] == part[:length]: overlap = length; break
        result += ('\n' if result and not overlap else '') + part[overlap:]
    return result
