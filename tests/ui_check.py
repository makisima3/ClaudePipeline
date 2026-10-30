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
    git_calls = []
    original_git_check = fixture.main.check_git_readiness

    def git_check(path, mode, auth):
        git_calls.append((mode, auth))
        missing = mode == "commit_push" and auth == "gh"
        return {"status": "needs_setup" if missing else "local_ready", "write_access": "not_checked", "checks": [
            {"key": "auth", "status": "error" if missing else "ok", "message": "gh не найден в PATH" if missing else "Локальная проверка без push"}]}

    fixture.main.check_git_readiness = git_check
    original_telegram_start = fixture.main.telegram.start

    def telegram_start(operation):
        fixture.main.telegram.auth("ready")
        if operation == "test":
            fixture.db.set_setting("telegram_test_at", fixture.db.now())

    fixture.main.telegram.start = telegram_start
    pid = fixture.db.create_project("Демо Telegram", "C:/Projects/Demo")
    fixture.db.ex("DELETE FROM projects WHERE id<>?", (pid,))
    demo = fixture.db.create_task(pid, "Результат с отдельной ошибкой доставки", telegram_notify=True)
    fixture.pl.finish_task(demo, "Изменения готовы, проверки пройдены на заглушках.", "Демонстрационные данные.")
    fixture.db.ex("UPDATE telegram_deliveries SET state='error', error='Войдите в Telegram заново, затем повторите доставку.' WHERE task_id=?", (demo,))
    fixture.db.set_setting("git_mode", "commit_push")
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
 overflow:document.documentElement.scrollWidth>innerWidth, loaded:!!document.querySelector('option[value="gpt-test"]') && document.querySelector('#git-result')?.textContent.includes('Локальная проверка без push')}));
}, 200);
</script>'''
    probe = probe.replace("window.__errors = [];", "window.__sandboxPath = " + json.dumps(str(root)) + "; window.__errors = [];")
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
 await call('Network.setUserAgentOverride',{userAgent:+width<600?'Mozilla/5.0 (Linux; Android 15) AppleWebKit/537.36 Chrome/130.0.0.0 Mobile Safari/537.36':'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0.0.0 Safari/537.36'});
 await call('Page.navigate',{url:target});
 let state;
 for(let n=0;n<60;n++){
  await new Promise(r=>setTimeout(r,100));
  const out=await call('Runtime.evaluate',{expression:'document.body?.getAttribute("data-probe")',returnByValue:true});
  if(out.result.value){state=JSON.parse(out.result.value);if(state.loaded)break;}
 }
 const efforts=await call('Runtime.evaluate',{expression:'JSON.stringify({first:effortList("gpt-test"),other:effortList("gpt-other")})',returnByValue:true});
 state.efforts=JSON.parse(efforts.result.value);
 const evaluate=async expression=>(await call('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true})).result.value;
 const waitFor=async expression=>{for(let n=0;n<60;n++){if(await evaluate(expression))return;await new Promise(r=>setTimeout(r,100));}throw new Error('UI timeout: '+expression)};
 const gitMode=async value=>{await evaluate(`App.setSetting('git_mode','${value}')`)};
 state.wizardOffer=await evaluate(`!document.querySelector('#welcome').classList.contains('hidden') === ${+width===1280}`);
 state.gitAuto=await evaluate('document.querySelector("#git-result").textContent.includes("Локальные предпосылки настроены")');
 await gitMode('off');
 state.gitOff=await evaluate('!document.querySelector("#git-check") && document.querySelector("#git-settings").textContent.includes("Git выключен")');
 await gitMode('commit');
 await evaluate('App.checkGitReadiness()');
 state.gitCommit=await evaluate('!document.querySelector("#git-auth") && document.querySelector("#git-result").textContent.includes("Локальная проверка без push")');
 await gitMode('commit_push');
 await waitFor('document.querySelector("#git-result").textContent.includes("Локальная проверка без push")');
 await evaluate('document.querySelector("#git-auth").value="gh"; App.gitAuthChanged("gh")');
 await waitFor('document.querySelector("#git-result").textContent.includes("gh не найден")');
 state.gitMissingGh=await evaluate('document.querySelector("#git-result").textContent.includes("Нужна настройка")');
 await evaluate('document.querySelector("#git-auth").value="existing"; App.gitAuthChanged("existing")');
 await waitFor('document.querySelector("#git-result").textContent.includes("Локальная проверка без push")');
 await evaluate('App.openGitSettings()');
 await waitFor('document.querySelector("#git-result").textContent.includes("Локальная проверка без push")');
 state.gitReopen=await evaluate('document.querySelector("#git-settings").getBoundingClientRect().top >= -2');
 state.telegramInitial=await evaluate(`telegramDefault() === ${+width<600}`);
 state.telegramChatControl=await evaluate('document.querySelector("#chat-telegram input").checked === telegramDefault()');
 state.telegramNotReady=await evaluate('document.querySelector("#telegram-status").textContent.includes("Не подключён") && [...document.querySelectorAll("#telegram-settings button")].find(b=>b.textContent.includes("Тест в Избранное")).disabled');
 await evaluate(`App.setTelegramDefault(${+width>=600})`);
 await call('Page.reload');
 await waitFor('document.querySelector("#telegram-status")');
 state.telegramRemembered=await evaluate(`telegramDefault() === ${+width>=600}`);
 await evaluate('App.telegramAction("check")');
 await evaluate('App.telegramAction("test")');
 state.telegramReady=await evaluate('document.querySelector("#telegram-status").textContent.includes("Авторизация проверена") && document.querySelector("#telegram-settings").textContent.includes("Последний успешный тест")');
 await evaluate('App.setTelegramDefault(false); App.setView("board")');
 await waitFor('S.board && document.querySelector(".board")');
 await evaluate('App.newTask()');
 state.telegramFormDefault=await evaluate('!document.querySelector("#nt-telegram").checked');
 await evaluate('document.querySelector("#nt-title").value="Задача из интерфейса";document.querySelector("#nt-telegram").checked=true;App.createTask()');
 const created=await evaluate('(async()=>{const b=await api("GET",`/api/projects/${S.projectId}/board`);return b.tasks.find(t=>t.title==="Задача из интерфейса").id})()');
 state.telegramCreated=await evaluate(`(async()=>!!(await api('GET','/api/tasks/${created}')).telegram_notify)()`);
 await evaluate(`App.openTask(${created})`);
 await evaluate('App.setTelegramDefault(true); App.setTaskTelegram(false)');
 state.telegramIndependent=await evaluate(`(async()=>telegramDefault() && !(await api('GET','/api/tasks/${created}')).telegram_notify)()`);
 await evaluate('document.querySelector("#tt-desc").value="Скинь результат в ТГ";App.saveTask()');
 state.telegramExplicit=await evaluate('document.querySelector("#tt-telegram").checked');
 await evaluate('App.closeModal(); App.setTelegramDefault(false)');
 const failed=await evaluate('(async()=>{const b=await api("GET",`/api/projects/${S.projectId}/board`);return b.tasks.find(t=>t.title==="Результат с отдельной ошибкой доставки").id})()');
 await evaluate(`App.openTask(${failed})`);
 state.telegramDeliveryError=await evaluate('S.task.status==="done" && document.querySelector("#tt-dyn").textContent.includes("ошибка доставки") && document.querySelector("#tt-dyn").textContent.includes("Повторить доставку")');
 await evaluate('document.querySelectorAll(".toast").forEach(e=>e.remove())');
 const taskShot=await call('Page.captureScreenshot',{format:'png'});
 fs.writeFileSync(screenshot.replace('.png','-task.png'),Buffer.from(taskShot.data,'base64'));
 await evaluate('App.retryTelegram()');
 state.telegramRetried=await evaluate('S.task.telegram_delivery.state==="pending" && S.task.status==="done"');
 await evaluate('App.closeModal(); App.openTelegramSettings()');
 await evaluate('App.toggleChat(); App.openTelegramSettings()');
 state.telegramChatSetup=await evaluate(`S.view === "settings" && S.chatOpen === ${+width>=900}`);
 await evaluate('if(S.chatOpen)App.toggleChat()');
 state.telegramOverflow=await evaluate('document.documentElement.scrollWidth>innerWidth');
 await evaluate('App.finishOnboarding("skipped")');
 await call('Page.reload');
 await waitFor('S.state && document.querySelector("#view").children.length');
 state.wizardSkip=await evaluate('S.state.settings.onboarding_status==="skipped" && document.querySelector("#welcome").classList.contains("hidden") && S.view!=="onboarding"');
 await evaluate('App.setView("settings"); document.querySelector("#onboarding-settings button").click()');
 await waitFor('document.querySelector("#onboarding-title")');
 state.wizardReopen=await evaluate('S.view==="onboarding" && S.state.settings.onboarding_status==="skipped" && document.querySelector("#onboarding-title").textContent.includes("От задачи")');
 state.wizardLayouts=true;
 const wizardShot=async step=>{
  state.wizardLayouts=state.wizardLayouts && await evaluate('(()=>{const b=document.querySelector(".onboarding-actions .primary").getBoundingClientRect(); return b.top>=0 && b.bottom<=innerHeight && document.documentElement.scrollWidth<=innerWidth})()');
  await evaluate('document.querySelectorAll(".toast").forEach(e=>e.remove())');
  const s=await call('Page.captureScreenshot',{format:'png'});fs.writeFileSync(screenshot.replace('.png',`-wizard-${step}.png`),Buffer.from(s.data,'base64'))
 };
 await wizardShot(1);
 await evaluate('document.querySelector(".onboarding-actions .ghost").click()');
 await waitFor('document.querySelector("#git-result")?.textContent.includes("Локальные предпосылки")');
 state.wizardStepSkip=await evaluate('document.querySelector("#onboarding-git-mode") && document.querySelector("#git-result").textContent.includes("Локальные предпосылки")');
 await evaluate('App.setSetting("git_mode","off")');
 state.wizardGitOff=await evaluate('!document.querySelector("#git-check") && document.querySelector(".onboarding-status").textContent.includes("Git выключен")');
 await evaluate('App.setSetting("git_mode","commit")');
 await waitFor('document.querySelector("#git-result")?.textContent.includes("Локальные предпосылки")');
 await wizardShot(2);
 await evaluate('document.querySelector(".onboarding-body > button").click()');
 state.wizardGitShared=await evaluate('S.view==="settings" && S.state.settings.git_mode==="commit" && !!document.querySelector("#git-settings")');
 await evaluate('App.openOnboarding(true)');
 state.wizardReturn=await evaluate('S.view==="onboarding" && !!document.querySelector("#onboarding-git-mode")');
 await evaluate('App.onboardingMove(2)');
 await waitFor('document.querySelector("#onboarding-telegram")?.textContent.includes("Авторизация проверена")');
 state.wizardTelegramReady=await evaluate('document.querySelector("#onboarding-telegram").textContent.includes("Последний успешный тест")');
 await wizardShot(3);
 await evaluate('document.querySelector(".onboarding-body > button").click()');
 state.wizardTelegramShared=await evaluate('S.view==="settings" && !!document.querySelector("#telegram-settings") && document.querySelector("#onboarding-settings button").textContent.includes("Вернуться")');
 await evaluate('App.openOnboarding(true)');
 // Потеря авторизации и ошибка запроса: мастер не должен показывать старую готовность.
 await evaluate('window.realFetch=window.fetch; window.fetch=(url,init)=>url==="/api/telegram"?Promise.resolve(new Response(JSON.stringify({auth:"auth_required",test_at:0}),{headers:{"Content-Type":"application/json"}})):window.realFetch(url,init); App.onboardingMove(2)');
 await waitFor('document.querySelector("#onboarding-telegram")?.textContent.includes("Нужно войти заново")');
 state.wizardTelegramFresh=await evaluate('!document.querySelector("#onboarding-telegram").textContent.includes("Авторизация проверена")');
 await evaluate('window.fetch=(url,init)=>url==="/api/telegram"?Promise.reject(new Error("offline")):window.realFetch(url,init); App.onboardingMove(2)');
 await waitFor('document.querySelector("#onboarding-telegram")?.textContent.includes("Не удалось получить")');
 state.wizardError=await evaluate('!document.querySelector("#onboarding-telegram").textContent.includes("Авторизация проверена")');
 await evaluate('window.fetch=window.realFetch; App.onboardingMove(0); S.state.agent="claude"; S.state.agent_name="Claude"; S.state.projects=[]; renderOnboarding()');
 state.wizardClaudeEmpty=await evaluate('document.querySelector(".onboarding-body").textContent.includes("Вход Claude Code на этом экране не проверялся") && document.querySelector(".onboarding-body").textContent.includes("Проект ещё не создан")');
 await evaluate('App.openOnboarding()');
 const demoProject=await evaluate('S.projectId');
 await evaluate('document.querySelector(".onboarding-body > button").click(); document.querySelector("#np-name").value="Первый проект из мастера"; document.querySelector("#np-path").value=window.__sandboxPath; App.createProject()');
 await waitFor('!document.querySelector("#np-name") && document.querySelector(".onboarding-status")?.textContent.includes("Первый проект из мастера")');
 state.wizardCreateProject=await evaluate('S.view==="onboarding" && project().path===window.__sandboxPath');
 const wizardProject=await evaluate('S.projectId');
 await evaluate(`App.selectProject(${demoProject})`);
 state.wizardSelectProject=await evaluate(`S.view==="onboarding" && S.projectId===${demoProject}`);
 await evaluate(`api('DELETE','/api/projects/${wizardProject}').then(()=>loadState())`);
 await evaluate('App.openOnboarding(); App.onboardingMove(2); document.querySelector(".onboarding-actions .primary").click()');
 await waitFor('S.state.settings.onboarding_status==="completed" && S.view==="board"');
 await call('Page.reload');
 await waitFor('S.state && document.querySelector("#view").children.length');
 state.wizardComplete=await evaluate('S.state.settings.onboarding_status==="completed" && document.querySelector("#welcome").classList.contains("hidden") && S.view!=="onboarding"');
 await evaluate('App.openOnboarding()');
 state.wizardCompleteReopen=await evaluate('S.view==="onboarding" && S.state.settings.onboarding_status==="completed"');
 state.wizardOverflow=await evaluate('document.documentElement.scrollWidth>innerWidth');
 state.errors=await evaluate('window.__errors');
 state.overflow=await evaluate('document.documentElement.scrollWidth>innerWidth');
 const shot=await call('Page.captureScreenshot',{format:'png'});
 fs.writeFileSync(screenshot,Buffer.from(shot.data,'base64'));
 console.log(JSON.stringify(state));await call('Browser.close');ws.close();
})().catch(e=>{console.error(e);process.exit(1)});
''', encoding="utf-8")
    try:
        for width, height in ((1280, 900), (390, 844)):
            fixture.db.set_setting("git_mode", "commit_push")
            fixture.db.set_setting("telegram_auth", "unconfigured")
            fixture.db.set_setting("telegram_test_at", "0")
            fixture.db.ex("DELETE FROM tasks WHERE title='Задача из интерфейса'")
            fixture.db.ex("UPDATE telegram_deliveries SET state='error' WHERE task_id=?", (demo,))
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
                                     capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
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
            assert all(state[k] for k in ("gitAuto", "gitOff", "gitCommit", "gitMissingGh", "gitReopen")), state
            assert all(state[k] for k in ("telegramInitial", "telegramNotReady", "telegramRemembered", "telegramReady", "telegramFormDefault",
                                         "telegramCreated", "telegramIndependent", "telegramExplicit", "telegramDeliveryError", "telegramRetried", "telegramChatControl", "telegramChatSetup")), state
            assert not state["telegramOverflow"], state
            assert all(state[k] for k in ("wizardOffer", "wizardSkip", "wizardReopen", "wizardStepSkip", "wizardGitOff", "wizardGitShared", "wizardReturn", "wizardLayouts",
                                         "wizardTelegramReady", "wizardTelegramShared", "wizardTelegramFresh", "wizardError", "wizardClaudeEmpty", "wizardCreateProject", "wizardSelectProject", "wizardComplete", "wizardCompleteReopen")), state
            assert not state["wizardOverflow"], state
        assert ("commit", "existing") in git_calls and ("commit_push", "gh") in git_calls, git_calls
        assert not any(mode == "off" for mode, auth in git_calls), git_calls
    finally:
        fixture.main.check_git_readiness = original_git_check
        fixture.main.telegram.start = original_telegram_start
        srv.shutdown()
        srv.server_close()


if __name__ == "__main__":
    main()
