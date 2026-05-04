import os
import socket
import time
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

BOOTED_AT = time.time()
HOSTNAME = socket.gethostname()
SANDBOX_ID = os.environ.get("DAYTONA_SANDBOX_ID") or os.environ.get("HOSTNAME") or "unknown"
RUN_ID = os.environ.get("TRUNKS_RUN_ID", "local")
COMMIT = os.environ.get("TRUNKS_COMMIT_OID") or os.environ.get("GITHUB_SHA") or "unknown"
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")

REQUEST_COUNT = 0

app = FastAPI()


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Hello from Trunks CI</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    font: 16px/1.5 ui-monospace, SFMono-Regular, Menlo, monospace;
    background: radial-gradient(circle at 20% 10%, #1d2230, #07090d 60%);
    color: #e6e9ef;
  }}
  .card {{
    max-width: 720px; padding: 40px; border: 1px solid #2a3142;
    background: rgba(20, 24, 32, .8); border-radius: 14px;
    box-shadow: 0 20px 60px rgba(0,0,0,.4);
  }}
  h1 {{ margin: 0 0 8px; font-size: 28px; letter-spacing: -0.5px; }}
  .tag {{ color: #8ab4ff; font-size: 13px; letter-spacing: 1px; text-transform: uppercase; }}
  .row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px dashed #232a3b; }}
  .row:last-child {{ border-bottom: 0; }}
  .k {{ color: #8a93a6; }}
  .v {{ color: #e6e9ef; }}
  .pill {{
    display: inline-block; padding: 2px 10px; border-radius: 999px;
    background: #1d4a2a; color: #7ee787; font-size: 12px; margin-left: 8px;
  }}
  a {{ color: #8ab4ff; }}
  pre {{ background: #0c1018; padding: 12px; border-radius: 8px; overflow-x: auto; }}
</style>
</head>
<body>
  <div class="card">
    <div class="tag">trunks actions / daytona</div>
    <h1>Hello from Trunks CI <span class="pill">live</span></h1>
    <p>This page is being served by a sandbox a Trunks Actions workflow booted moments ago.
       Daytona exposes this port for the duration of the run. When the run ends, the sandbox
       is destroyed and this URL stops resolving.</p>
    <div class="row"><span class="k">sandbox</span><span class="v">{sandbox_id}</span></div>
    <div class="row"><span class="k">hostname</span><span class="v">{hostname}</span></div>
    <div class="row"><span class="k">run id</span><span class="v">{run_id}</span></div>
    <div class="row"><span class="k">commit</span><span class="v">{commit}</span></div>
    <div class="row"><span class="k">uptime</span><span class="v">{uptime}s</span></div>
    <div class="row"><span class="k">requests served</span><span class="v">{count}</span></div>
    <p style="margin-top: 24px;">Try:</p>
    <pre>curl {public}/work
curl {public}/echo/hi</pre>
  </div>
</body>
</html>
"""


@app.get("/", response_class=HTMLResponse)
def root():
    global REQUEST_COUNT
    REQUEST_COUNT += 1
    return PAGE.format(
        sandbox_id=SANDBOX_ID,
        hostname=HOSTNAME,
        run_id=RUN_ID,
        commit=COMMIT[:12],
        uptime=round(time.time() - BOOTED_AT, 1),
        count=REQUEST_COUNT,
        public=PUBLIC_URL or "http://localhost:8080",
    )


@app.get("/echo/{msg}")
def echo(msg: str):
    global REQUEST_COUNT
    REQUEST_COUNT += 1
    return {"echo": msg, "ts": time.time()}


@app.get("/work")
def work():
    global REQUEST_COUNT
    REQUEST_COUNT += 1
    started = time.time()
    total = 0
    for i in range(200_000):
        total += (i * 31) % 7919
    return {"checksum": total, "elapsed_ms": round((time.time() - started) * 1000, 2)}


@app.get("/healthz")
def healthz():
    return {"ok": True, "uptime_s": round(time.time() - BOOTED_AT, 2), "served": REQUEST_COUNT}
