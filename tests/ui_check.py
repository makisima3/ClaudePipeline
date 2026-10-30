"""Проверка интерфейса в headless Edge на отдельной базе, без моделей."""
import json
import os
import subprocess
import threading
import time
from pathlib import Path

import test_pipeline as fixture


def main():
    edge = Path(os.environ.get("ProgramFiles(x86)", "C:/Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe"
    if not edge.exists():
        raise SystemExit("Для этой необязательной проверки нужен Microsoft Edge")
    root = fixture.SANDBOX
    fixture.cr.INFO["models"] = [{"model": "gpt-test", "displayName": "Codex Test", "isDefault": True, "supportedReasoningEfforts": [{"reasoningEffort": "high"}]},
                                {"model": "gpt-other", "displayName": "Other", "supportedReasoningEfforts": [{"reasoningEffort": "low"}]}]
    p = root / "web/index.html"
    s = p.read_text(encoding="utf-8")
    probe = '''<script>
localStorage.setItem('view', JSON.stringify('settings'));
localStorage.setItem('chatOpen', 'false');
window.__errors = [];
window.addEventListener('error', e => window.__errors.push(e.message));
window.addEventListener('unhandledrejection', e => window.__errors.push(String(e.reason)));
window.EventSource = class { static CLOSED = 2; };
setInterval(() => {
 document.body.setAttribute('data-probe', JSON.stringify({errors:window.__errors, width:innerWidth,
 overflow:document.documentElement.scrollWidth>innerWidth, loaded:!!document.querySelector('option[value="gpt-test"]')}));
}, 200);
</script>'''
    p.write_text(s.replace('<script src="/app.js">', probe + '<script src="/app.js">'), encoding="utf-8")
    srv = fixture.main.Server(("127.0.0.1", 0), fixture.main.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    probe_js = root / "ui-probe.js"
    probe_js.write_text('''const fs = require('fs');
(async () => {
 const [port,width,height,target,screenshot] = process.argv.slice(2);
 const pages = await (await fetch(`http://127.0.0.1:${port}/json/list`)).json();
 const ws = new WebSocket(pages.find(p=>p.type==='page').webSocketDebuggerUrl);
 await new Promise(r=>ws.addEventListener('open',r,{once:true}));
 let id=0; const pending=new Map();
 ws.addEventListener('message',e=>{const m=JSON.parse(e.data); const p=pending.get(m.id); if(p){pending.delete(m.id);m.error?p.reject(m.error):p.resolve(m.result)}});
 const call=(method,params={})=>new Promise((resolve,reject)=>{const rid=++id;pending.set(rid,{resolve,reject});ws.send(JSON.stringify({id:rid,method,params}))});
 await call('Emulation.setDeviceMetricsOverride',{width:+width,height:+height,deviceScaleFactor:1,mobile:+width<600});
 await call('Page.navigate',{url:target});
 let state;
 for(let n=0;n<60;n++){
  await new Promise(r=>setTimeout(r,100));
  const out=await call('Runtime.evaluate',{expression:'document.body?.getAttribute("data-probe")',returnByValue:true});
  if(out.result.value){state=JSON.parse(out.result.value);if(state.loaded)break;}
 }
 const efforts=await call('Runtime.evaluate',{expression:'JSON.stringify({first:effortList("gpt-test"),other:effortList("gpt-other")})',returnByValue:true});
 state.efforts=JSON.parse(efforts.result.value);
 const shot=await call('Page.captureScreenshot',{format:'png'});
 fs.writeFileSync(screenshot,Buffer.from(shot.data,'base64'));
 console.log(JSON.stringify(state));await call('Browser.close');ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
''', encoding="utf-8")
    try:
        for width, height in ((1280, 900), (390, 844)):
            screenshot = root / f"ui-{width}.png"
            profile = root / f"edge-{width}"
            cmd = [str(edge), "--headless", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
                   "--user-data-dir=" + str(profile), "--remote-debugging-port=0", "about:blank"]
            browser = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                devtools = profile / "DevToolsActivePort"
                deadline = time.time() + 15
                while not devtools.exists() and time.time() < deadline:
                    time.sleep(0.1)
                port = devtools.read_text().splitlines()[0]
                out = subprocess.run(["node", str(probe_js), port, str(width), str(height), f"http://127.0.0.1:{srv.server_port}", str(screenshot)],
                                     capture_output=True, timeout=30, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                assert out.returncode == 0, out.stderr.decode("utf-8", "replace")
                state = json.loads(out.stdout)
            finally:
                subprocess.run(["taskkill", "/PID", str(browser.pid), "/T", "/F"], capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                browser.wait(timeout=10)
            print(json.dumps({"requested_width": width, **state, "screenshot": str(screenshot)}, ensure_ascii=False))
            assert not state["errors"] and state["loaded"], state
            assert not state["overflow"], state
            assert state["width"] == width, state
            assert state["efforts"]["first"][-1][0] == "high" and state["efforts"]["other"][-1][0] == "low", state
    finally:
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
