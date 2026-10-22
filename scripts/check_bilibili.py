"""One non-paid live search probe; no cookies, downloads or model calls."""
import json
import tempfile
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from backend import store,media
from backend.models import Settings,Preferences
with tempfile.TemporaryDirectory() as folder:
    store.DATA=Path(folder)/'data'; store.SECRETS=Path(folder)/'secrets'; store.init()
    config=Settings(search_count=1,search_groups=1).model_dump(); tid=store.uid()
    with store.db() as c: c.execute('INSERT INTO tasks(id,status,prefs,limits,created,updated) VALUES(?,?,?,?,?,?)',(tid,'searching',store.dumps(Preferences(destination='成都').model_dump()),'{}',store.now(),store.now()))
    links=media.search('成都',[],tid,config)
    print(json.dumps({'search_results':links},ensure_ascii=False))
    if links:
        try:
            meta,info=media.metadata(links[0],tid,config)
            print(json.dumps({'metadata':meta,'subtitle_languages':list((info.get('subtitles') or {}).keys())},ensure_ascii=False))
        except Exception as exc:
            from backend.security import safe_error
            print(safe_error(exc))
    else:
        with store.db() as c: print(json.dumps([dict(x) for x in c.execute('SELECT stage,message FROM events')],ensure_ascii=False))
