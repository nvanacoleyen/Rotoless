"""Local browser session: click-to-prompt with live preview, then progress.

Resolve's free edition has no panel API, and a script launched from the Scripts
menu blocks Resolve's UI for the whole run -- so Resolve cannot show progress
even in principle. The browser tab can: it is a separate process and stays
responsive while Resolve is frozen.

So this server does not shut down once it has the points. It stays up for the
whole run and streams progress to the page, which makes the browser the UI and
Resolve merely the launcher. That also removes the reason people launch the
script twice.

Binds to 127.0.0.1 on an ephemeral port; nothing leaves the machine.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import tempfile
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from rotoless.segment import PALETTE


class NotReadyError(RuntimeError):
    """The model is still loading; the page should say so rather than fail."""


PAGE = """<!doctype html><meta charset=utf-8><title>Rotoless</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#15171a;color:#e8eaed;font:14px/1.5 -apple-system,BlinkMacSystemFont,sans-serif;
      display:flex;flex-direction:column;height:100vh}
 header{padding:10px 14px;background:#1e2126;border-bottom:1px solid #2c3037;display:flex;
        gap:10px;align-items:center;flex-wrap:wrap}
 button{background:#2c3037;color:#e8eaed;border:1px solid #3a3f47;border-radius:6px;
        padding:6px 12px;font:inherit;cursor:pointer}
 button:hover{background:#363b43}
 button:disabled{opacity:.5;cursor:default}
 button.go{background:#2f6fed;border-color:#2f6fed;font-weight:600}
 button.go:hover{background:#4480f5}
 .chip{display:inline-flex;align-items:center;gap:6px;padding:5px 10px;border-radius:14px;
       border:1px solid #3a3f47;background:#2c3037;cursor:pointer;font-size:13px}
 .chip.on{border-color:#e8eaed;background:#3a3f47}
 .chip .sw{width:11px;height:11px;border-radius:50%}
 .chip .rm{opacity:.55;font-weight:700}
 .chip .rm:hover{opacity:1}
 .sep{width:1px;height:22px;background:#3a3f47}
 .hint{opacity:.65;font-size:13px}
 #wrap{flex:1;overflow:auto;padding:14px}
 canvas{max-width:100%;cursor:crosshair;border-radius:6px;display:block;margin:0 auto}
 #prog{display:none;padding:22px;max-width:760px;margin:0 auto;width:100%}
 .bar{height:12px;background:#2c3037;border-radius:6px;overflow:hidden;margin:14px 0 8px}
 .fill{height:100%;width:0;background:linear-gradient(90deg,#2f6fed,#5b9dff);transition:width .3s}
 .stat{display:flex;justify-content:space-between;font-variant-numeric:tabular-nums;opacity:.85}
 pre{background:#1a1d21;border:1px solid #2c3037;border-radius:6px;padding:12px;
     max-height:260px;overflow:auto;font-size:12px;white-space:pre-wrap;margin-top:18px}
 h2{margin:0;font-size:17px}
 .ok{color:#3ddc84}.err{color:#ff5c5c}
</style>
<header id=bar1>
  <strong>Objects</strong>
  <span id=chips></span>
  <button onclick=addObj()>+ Object</button>
  <span class=sep></span>
  <span class=hint>click = include &nbsp;·&nbsp; shift-click = exclude</span>
  <span class=sep></span>
  <button onclick=undo()>Undo</button>
  <button onclick=clearObj()>Clear</button>
  <button id=prev onclick=preview()>Preview mask</button>
  <button class=go onclick=go()>Run &rarr;</button>
  <span id=count class=hint></span>
</header>
<div id=wrap><canvas id=c></canvas></div>
<div id=prog>
  <h2 id=phase>Starting…</h2>
  <div class=bar><div class=fill id=fill></div></div>
  <div class=stat><span id=pct>0%</span><span id=eta></span></div>
  <pre id=log></pre>
</div>
<script>
const PALETTE = __PALETTE__;
const img=new Image(), c=document.getElementById('c'), x=c.getContext('2d');
let objs=[{id:1,pts:[]}], active=0, overlay=null, nextId=2;

function colorOf(id){const p=PALETTE[(id-1)%PALETTE.length];return 'rgb('+p[0]+','+p[1]+','+p[2]+')';}

img.onload=()=>{c.width=img.naturalWidth;c.height=img.naturalHeight;draw();};
img.src="data:image/png;base64,__IMG__";

function chips(){
  const box=document.getElementById('chips'); box.innerHTML='';
  objs.forEach((o,i)=>{
    const el=document.createElement('span');
    el.className='chip'+(i===active?' on':'');
    el.onclick=()=>{active=i;chips();draw();};
    el.innerHTML='<span class=sw style="background:'+colorOf(o.id)+'"></span>Object '+o.id+
                 ' <span class=hint>'+o.pts.length+'</span>';
    if(objs.length>1){
      const rm=document.createElement('span');
      rm.className='rm'; rm.textContent='×'; rm.title='remove';
      rm.onclick=(e)=>{e.stopPropagation();objs.splice(i,1);
                       if(active>=objs.length)active=objs.length-1;
                       overlay=null;chips();draw();};
      el.appendChild(rm);
    }
    box.appendChild(el);
  });
}
function addObj(){
  if(objs.length>=PALETTE.length){alert('Maximum '+PALETTE.length+' objects.');return;}
  objs.push({id:nextId++,pts:[]}); active=objs.length-1; chips(); draw();
}

function draw(){
  x.drawImage(img,0,0);
  if(overlay) x.drawImage(overlay,0,0);
  objs.forEach((o)=>{
    const col=colorOf(o.id);
    for(const p of o.pts){
      x.beginPath(); x.arc(p.x,p.y,8,0,7);
      if(p.label){ x.fillStyle=col; x.fill(); x.lineWidth=2; x.strokeStyle='#000'; x.stroke(); }
      else {       x.fillStyle='#15171a'; x.fill(); x.lineWidth=3; x.strokeStyle=col; x.stroke();
                   x.beginPath(); x.moveTo(p.x-4,p.y); x.lineTo(p.x+4,p.y);
                   x.lineWidth=3; x.strokeStyle=col; x.stroke(); }
    }
  });
  const total=objs.reduce((n,o)=>n+o.pts.length,0);
  document.getElementById('count').textContent =
    total? total+' point(s) across '+objs.length+' object(s)' : 'no points yet';
  chips();
}

c.onclick=e=>{
  const r=c.getBoundingClientRect();
  objs[active].pts.push({x:(e.clientX-r.left)*(c.width/r.width),
                         y:(e.clientY-r.top)*(c.height/r.height),
                         label:e.shiftKey?0:1});
  overlay=null;                     // stale the moment the points change
  draw();
};
function undo(){objs[active].pts.pop();overlay=null;draw();}
function clearObj(){objs[active].pts=[];overlay=null;draw();}

function payload(){
  return objs.filter(o=>o.pts.length).map(o=>({obj_id:o.id,points:o.pts}));
}
function anyPoints(){return objs.some(o=>o.pts.length);}

function preview(){
  const b=document.getElementById('prev');
  if(!anyPoints()){alert('Add at least one include point first.');return;}
  b.disabled=true; b.textContent='Computing…';
  fetch('/preview',{method:'POST',body:JSON.stringify(payload())}).then(r=>{
    if(!r.ok) return r.text().then(t=>{throw new Error(t||('HTTP '+r.status))});
    return r.blob();
  }).then(blob=>{
    const im=new Image();
    im.onload=()=>{overlay=im;draw();URL.revokeObjectURL(im.src);
                   b.disabled=false;b.textContent='Preview mask';};
    im.src=URL.createObjectURL(blob);
  }).catch(e=>{
    b.disabled=false; b.textContent='Preview mask';
    document.getElementById('count').textContent=String(e.message||e).slice(0,90);
  });
}

function go(){
  if(!anyPoints()){alert('Add at least one include point first.');return;}
  fetch('/submit',{method:'POST',body:JSON.stringify(payload())});
  document.getElementById('bar1').style.display='none';
  document.getElementById('wrap').style.display='none';
  document.getElementById('prog').style.display='block';
  poll();
}
function fmt(s){s=Math.round(s);return s<60?s+'s':Math.floor(s/60)+'m '+(s%60)+'s';}
function poll(){
  fetch('/progress').then(r=>r.json()).then(s=>{
    const pc = s.total ? Math.round(100*s.done/s.total) : 0;
    document.getElementById('fill').style.width=pc+'%';
    document.getElementById('pct').textContent =
      pc+'%'+(s.total?'  ('+s.done+'/'+s.total+' frames)':'');
    document.getElementById('phase').textContent=s.phase||'Working…';
    if(s.eta!=null && s.state==='running')
      document.getElementById('eta').textContent='about '+fmt(s.eta)+' left';
    else document.getElementById('eta').textContent = s.elapsed?('elapsed '+fmt(s.elapsed)):'';
    document.getElementById('log').textContent=(s.lines||[]).join('\\n');
    if(s.state==='done'){
      document.getElementById('phase').innerHTML='<span class=ok>Done — '+s.done+' frames written.</span> You can close this tab and return to Resolve.';
      document.getElementById('fill').style.width='100%';
      return;
    }
    if(s.state==='error'){
      document.getElementById('phase').innerHTML='<span class=err>Failed.</span> See the log below and Resolve\\'s console.';
      return;
    }
    setTimeout(poll,400);
  }).catch(()=>setTimeout(poll,1000));
}
chips();
</script>"""


def _extract_preview(video: Path, frame: int, dest: Path) -> None:
    """Pull a single frame at native resolution for clicking."""
    from rotoless.decode import tool
    cmd = [tool("ffmpeg"), "-v", "error", "-y", "-i", str(video),
           "-vf", f"select=gte(n\\,{frame})", "-frames:v", "1", str(dest)]
    done = subprocess.run(cmd, capture_output=True, text=True)
    if done.returncode != 0:
        raise RuntimeError(f"ffmpeg preview failed: {done.stderr.strip()}")


def _normalise(raw) -> list[dict]:
    """Wire format -> {"obj_id", "points": [(x, y)], "labels": [int]} per object."""
    objects = []
    for entry in raw:
        pts = entry.get("points") or []
        objects.append({
            "obj_id": int(entry.get("obj_id", 1)),
            "points": [(float(p["x"]), float(p["y"])) for p in pts],
            "labels": [int(p.get("label", 1)) for p in pts],
        })
    return objects


class Session:
    """Serves the picker, then the progress view, for one run."""

    MAX_LINES = 200

    def __init__(self, video: Path, frame: int = 0):
        self.video = Path(video)
        self.frame = frame
        self._lock = threading.Lock()
        self._objects: list[dict] = []
        self._submitted = threading.Event()
        self._state = {"state": "picking", "phase": "Waiting for your selection…",
                       "done": 0, "total": 0, "lines": [], "elapsed": 0, "eta": None}
        self._started_at: float | None = None
        self._server: ThreadingHTTPServer | None = None
        self.url = ""
        # Set by the caller: (objects) -> PNG bytes. May raise.
        self.preview_fn = None

    # -- state updates, called from the worker ---------------------------
    def log(self, message: str) -> None:
        with self._lock:
            lines = self._state["lines"]
            lines.append(str(message))
            del lines[:-self.MAX_LINES]
        print(message, flush=True)

    def set_total(self, total: int) -> None:
        with self._lock:
            self._state["total"] = int(total)
            self._state["state"] = "running"
            self._state["phase"] = "Tracking…"
            self._started_at = time.monotonic()

    def advance(self, done: int) -> None:
        with self._lock:
            self._state["done"] = int(done)
            if self._started_at and done:
                elapsed = time.monotonic() - self._started_at
                self._state["elapsed"] = elapsed
                total = self._state["total"] or done
                self._state["eta"] = max(0.0, elapsed / done * (total - done))

    def finish(self, ok: bool, phase: str = "") -> None:
        with self._lock:
            self._state["state"] = "done" if ok else "error"
            self._state["phase"] = phase or ("Done." if ok else "Failed.")
            if self._started_at:
                self._state["elapsed"] = time.monotonic() - self._started_at
            self._state["eta"] = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        handle, tmp = tempfile.mkstemp(prefix="mm_preview_", suffix=".png")
        os.close(handle)
        preview = Path(tmp)
        try:
            _extract_preview(self.video, self.frame, preview)
            encoded = base64.b64encode(preview.read_bytes()).decode()
        finally:
            preview.unlink(missing_ok=True)

        html = (PAGE.replace("__PALETTE__", json.dumps([list(c) for c in PALETTE]))
                    .replace("__IMG__", encoded).encode())
        session = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def _send(self, payload: bytes, ctype: str):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def _text(self, code: int, message: str):
                payload = message.encode()
                self.send_response(code)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path.startswith("/progress"):
                    with session._lock:
                        body = json.dumps(session._state).encode()
                    self._send(body, "application/json")
                else:
                    self._send(html, "text/html; charset=utf-8")

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                objects = _normalise(json.loads(body))

                if self.path.startswith("/preview"):
                    if session.preview_fn is None:
                        self._text(503, "preview is not available for this run")
                        return
                    try:
                        png = session.preview_fn(objects)
                    except NotReadyError as exc:
                        self._text(503, str(exc))
                        return
                    except Exception as exc:
                        self._text(500, f"preview failed: {exc!r}")
                        return
                    self._send(png, "image/png")
                    return

                with session._lock:
                    session._objects = objects
                    session._state["phase"] = "Decoding frames…"
                session._submitted.set()
                self.send_response(204)
                self.end_headers()

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self._server.server_port}/"
        print(f"picker open at {self.url}", flush=True)
        webbrowser.open(self.url)

    def wait_for_objects(self, timeout: float = 600.0) -> list[dict]:
        if not self._submitted.wait(timeout):
            raise TimeoutError("picker timed out with no selection")
        with self._lock:
            return list(self._objects)

    def stop(self, linger: float = 0.0) -> None:
        """Keep serving briefly so the page can render the final state."""
        if linger:
            time.sleep(linger)
        if self._server is not None:
            self._server.shutdown()
            self._server = None
