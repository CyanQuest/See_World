#!/usr/bin/env python3
"""Local supervisor: durable worker + loopback-only web server."""
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
os.chdir(ROOT)
if not (ROOT/'frontend/dist/index.html').exists():
    sys.exit('前端尚未构建，请先运行 ./install.command')
children=[]
def stop(*args):
    for p in children:
        if p.poll() is None: p.terminate()
    for p in children:
        try: p.wait(timeout=8)
        except subprocess.TimeoutExpired: p.kill(); p.wait()
    raise SystemExit(0)
signal.signal(signal.SIGINT,stop); signal.signal(signal.SIGTERM,stop)
web=subprocess.Popen([sys.executable,'-m','uvicorn','backend.app:app','--host','127.0.0.1','--port','8765','--no-access-log'])
children.append(web)
worker=subprocess.Popen([sys.executable,'-m','backend.worker']); children.append(worker)
print('见世界已启动：http://127.0.0.1:8765  ·  按 Ctrl+C 停止',flush=True)
try:
    while web.poll() is None:
        if worker.poll() is not None:
            # A second worker exits because the first holds the OS lock.
            print('后台工作进程停止；请确认没有同时运行其他应用实例。',flush=True)
            break
        time.sleep(1)
finally: stop()
