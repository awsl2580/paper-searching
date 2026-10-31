"""真浏览器自检：弹窗面板的「拖动 / 缩放 / 登录门槛」三件事。

**不属于 `run_all.py`**：它需要一个真实排版引擎（Edge / Chrome）。
存在的理由很具体 —— 这一类 bug 只有真浏览器量得出来：

* 拖动：面板是 flex 子项，展开时被拉伸成和容器一模一样大（实测 380×711），
  旧代码用 (容器 - 面板) 当可移动上限 → 上限恒为 0 → "怎么拖都不动"。
* 缩放：只写 width/height 会被 flex 的拉伸覆盖，必须先钉成绝对定位。
* 登录门槛：点「立即登录」时事件继续冒泡到 document，被"点到别处收起浮层"
  的监听立刻关掉 → 表现是"点了没反应"。

用法：
    python tests/check_panels_browser.py                 # 自己起临时服务
    python tests/check_panels_browser.py --url http://127.0.0.1:8000/

没装 Edge/Chrome 或缺少 websockets 时打印 SKIP 并以 0 退出。
"""
import argparse
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
]

CHECKS = []


def ok(name, cond, got=None):
    CHECKS.append((name, bool(cond), got))


def find_browser():
    for p in BROWSERS:
        if os.path.isfile(p):
            return p
    return None


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_http(url, timeout=40):
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=3) as r:
                if r.status == 200:
                    return True
        except Exception:
            time.sleep(0.4)
    return False


async def drive(ws_url):
    import websockets
    # 单条 CDP 消息最多等 30 秒。没有这个超时的话，页面里任一 await 卡住
    # （比如某个 fetch 没回来）就会让整个脚本静默挂死 —— 实测挂过 10 分钟。
    RECV_TIMEOUT = 30
    async with websockets.connect(ws_url, max_size=32 * 1024 * 1024) as ws:
        seq = {"i": 0}

        async def call(method, params=None):
            seq["i"] += 1
            mid = seq["i"]
            await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
            while True:
                msg = json.loads(await asyncio.wait_for(ws.recv(), RECV_TIMEOUT))
                if msg.get("id") == mid:
                    if "error" in msg:
                        raise RuntimeError(f"{method}: {msg['error']}")
                    return msg.get("result", {})

        async def js(expr):
            res = await call("Runtime.evaluate", {"expression": expr,
                                                 "returnByValue": True,
                                                 "awaitPromise": True})
            if res.get("exceptionDetails"):
                return {"__error": str(res["exceptionDetails"].get("text"))}
            return res.get("result", {}).get("value")

        await call("Page.enable")
        await call("Runtime.enable")
        # 点功能现在会先弹「小窗 / 对话框」询问框；这里钉成「小窗」并关掉询问，
        # 以便量面板本身的拖动/缩放（否则 toggleFeaturePanel 只会弹出询问框）。
        await js("try{localStorage.setItem('panelMode','window');"
                 "localStorage.setItem('panelModeAsk','off');}catch(e){}")
        for _ in range(60):
            if await js("!!(window.__panelDragInstalled && window.__panelResizeInstalled)"):
                break
            await asyncio.sleep(0.25)

        # 首页默认盖着登录页（本机已注册账号时尤其如此）。面板在登录页下面，
        # 不跳过登录的话它们量出来的宽高是 0，"拖动/缩放"必然失败。
        await js("try{ skipAuth(); }catch(e){}")
        await asyncio.sleep(0.4)

        ok("拖动与缩放的委托都装上了",
           bool(await js("window.__panelDragInstalled"))
           and bool(await js("window.__panelResizeInstalled")))

        # ══════════════════════════════════════════════════════
        # 1) 四个面板：打开 → 拖 120×60 → 量位移
        # ══════════════════════════════════════════════════════
        for pid, label in (("favoritesPanel", "收藏"),
                           ("polishPanel", "润色"),
                           ("tablePanel", "文献表"),
                           ("wfPanel", "科研工作流")):
            res = await js("""(function(){
                var id = %s;
                closeAllSidePanels();
                var el = document.getElementById(id);
                el.classList.add('open');
                var header = el.querySelector('.panel-header');
                if (!header) return {error: 'no header'};
                var r0 = el.getBoundingClientRect();
                var b = header.getBoundingClientRect();
                function fire(type, x, y) {
                    var ev = new PointerEvent(type, {bubbles: true, cancelable: true,
                        clientX: x, clientY: y, button: 0, pointerId: 1,
                        pointerType: 'mouse'});
                    header.dispatchEvent(ev);
                    if (type !== 'pointerdown') document.dispatchEvent(ev);
                }
                fire('pointerdown', b.left + 30, b.top + 10);
                fire('pointermove', b.left + 30 - 120, b.top + 10 - 60);
                var r1 = el.getBoundingClientRect();
                document.dispatchEvent(new PointerEvent('pointerup',
                    {bubbles: true, pointerId: 1}));
                return {dx: Math.round(r1.left - r0.left),
                        dy: Math.round(r1.top - r0.top),
                        w: el.offsetWidth, h: el.offsetHeight,
                        position: getComputedStyle(el).position,
                        stillDragging: el.classList.contains('dragging')};
            })()""" % json.dumps(pid))
            ok(f"{label}面板可以打开", res and not res.get("error"), res)
            if res and not res.get("error"):
                ok(f"{label}面板拖动时真的移动（-120,-60）",
                   res["dx"] == -120 and res["dy"] == -60, res)
                ok(f"{label}面板拖动后是绝对定位",
                   res["position"] == "absolute", res["position"])
                ok(f"{label}面板松手后退出拖动状态",
                   res["stillDragging"] is False, res["stillDragging"])

        # ══════════════════════════════════════════════════════
        # 2) 缩放：拖右下角抓手，宽高要跟着变（并且记住）
        # ══════════════════════════════════════════════════════
        await js("closeAllSidePanels(); document.getElementById('polishPanel').classList.add('open');")
        await asyncio.sleep(0.1)
        resize = await js("""(function(){
            var el = document.getElementById('polishPanel');
            var grip = el.querySelector('.panel-resize');
            if (!grip) return {error: 'no resize handle'};
            var r0 = el.getBoundingClientRect();
            var g = grip.getBoundingClientRect();
            function fire(type, x, y) {
                var ev = new PointerEvent(type, {bubbles: true, cancelable: true,
                    clientX: x, clientY: y, button: 0, pointerId: 1,
                    pointerType: 'mouse'});
                grip.dispatchEvent(ev);
                if (type !== 'pointerdown') document.dispatchEvent(ev);
            }
            fire('pointerdown', g.left + 6, g.top + 6);
            fire('pointermove', g.left + 6 + 90, g.top + 6 + 70);
            var r1 = el.getBoundingClientRect();
            document.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerId: 1}));
            var saved = null;
            try { saved = JSON.parse(localStorage.getItem('panelSize:polishPanel') || 'null'); } catch (e) {}
            return {w0: Math.round(r0.width), h0: Math.round(r0.height),
                    w1: Math.round(r1.width), h1: Math.round(r1.height),
                    dw: Math.round(r1.width - r0.width), dh: Math.round(r1.height - r0.height),
                    position: getComputedStyle(el).position,
                    stillResizing: el.classList.contains('resizing'),
                    saved: saved};
        })()""")
        ok("润色面板有右下角缩放抓手", resize and not resize.get("error"), resize)
        if resize and not resize.get("error"):
            ok("拖抓手后宽度变化约 +90",
               abs(resize["dw"] - 90) <= 2, resize["dw"])
            ok("拖抓手后高度变化约 +70",
               abs(resize["dh"] - 70) <= 2, resize["dh"])
            ok("缩放后是绝对定位", resize["position"] == "absolute", resize["position"])
            ok("松手后退出缩放状态", resize["stillResizing"] is False, resize["stillResizing"])
            ok("尺寸记进了 localStorage",
               resize["saved"] and abs(resize["saved"]["w"] - resize["w1"]) <= 1,
               resize["saved"])

        # 单边抓手：拖右边只改宽度
        edge = await js("""(function(){
            var el = document.getElementById('polishPanel');
            var e = el.querySelector('.panel-resize-e');
            if (!e) return {error: 'no edge handle'};
            var r0 = el.getBoundingClientRect();
            var b = e.getBoundingClientRect();
            function fire(type, x, y) {
                var ev = new PointerEvent(type, {bubbles: true, cancelable: true,
                    clientX: x, clientY: y, button: 0, pointerId: 1, pointerType: 'mouse'});
                e.dispatchEvent(ev);
                if (type !== 'pointerdown') document.dispatchEvent(ev);
            }
            fire('pointerdown', b.left + 3, b.top + 20);
            fire('pointermove', b.left + 3 + 40, b.top + 20);
            var r1 = el.getBoundingClientRect();
            document.dispatchEvent(new PointerEvent('pointerup', {bubbles: true, pointerId: 1}));
            return {dw: Math.round(r1.width - r0.width), dh: Math.round(r1.height - r0.height)};
        })()""")
        ok("右边抓手只改宽度（高度不动）",
           edge and abs(edge.get("dw", 0) - 40) <= 2 and edge.get("dh") == 0, edge)

        # 双击抓手还原默认
        reset = await js("""(function(){
            var el = document.getElementById('polishPanel');
            var grip = el.querySelector('.panel-resize');
            grip.dispatchEvent(new MouseEvent('dblclick', {bubbles: true}));
            return {w: el.style.width, h: el.style.height,
                    moved: el.classList.contains('panel-moved'),
                    saved: localStorage.getItem('panelSize:polishPanel')};
        })()""")
        ok("双击右下角抓手可以还原默认大小",
           reset and not reset["w"] and not reset["h"] and not reset["moved"], reset)

        # ══════════════════════════════════════════════════════
        # 3) 未登录：点润色要提示登录；点「立即登录」要跳到登录入口
        # ══════════════════════════════════════════════════════
        gate = await js("""(function(){
            closeAllSidePanels(); closeAuthPage();
            var logged = isLoggedIn;
            togglePolishPanel();
            var modal = document.getElementById('authPromptModal');
            var shown = modal.classList.contains('show');
            var feature = document.getElementById('authPromptFeature').textContent;
            var panelOpened = document.getElementById('polishPanel').classList.contains('open');
            var btn = modal.querySelector('.btn-go-login');
            btn.click();
            var dd = document.getElementById('authPage');
            return {logged: logged, shown: shown, feature: feature,
                    panelOpened: panelOpened,
                    modalAfterClose: modal.classList.contains('show'),
                    pageOpen: dd.style.display !== 'none'};
        })()""")
        # 焦点是延后 80ms 落的（openAuthPage 里），等一拍再读
        await asyncio.sleep(0.35)
        focus_id = await js("document.activeElement && document.activeElement.id")
        gate["focused"] = focus_id
        ok("当前处于未登录状态", gate.get("logged") is False, gate.get("logged"))
        ok("未登录点润色会弹出登录提示", gate.get("shown") is True, gate.get("shown"))
        ok("提示里点名了「摘要润色」", gate.get("feature") == "摘要润色", gate.get("feature"))
        ok("未登录时润色面板不会打开", gate.get("panelOpened") is False, gate.get("panelOpened"))
        ok("点「立即登录」后提示弹窗关闭", gate.get("modalAfterClose") is False,
           gate.get("modalAfterClose"))
        ok("点「立即登录」后登录/注册整页真的打开了", gate.get("pageOpen") is True,
           gate.get("pageOpen"))
        ok("焦点落在用户名输入框", gate.get("focused") == "authUsername", gate.get("focused"))

        # ══════════════════════════════════════════════════════
        # 4) 「显示在对话框」= 整面覆盖对话区 + VS Code 风格标签页
        # ══════════════════════════════════════════════════════
        tabs = await js("""(function(){
            try{ skipAuth(); }catch(e){}
            closeAllSidePanels();
            localStorage.setItem('panelMode','inline');
            localStorage.setItem('panelModeAsk','off');   // 关掉询问，直接开
            openFeaturePanel('favoritesPanel','inline');
            openFeaturePanel('tablePanel','inline');
            var tabsEl = document.getElementById('inlineTabs');
            var panelsEl = document.getElementById('inlinePanels');
            var btns = tabsEl.querySelectorAll('.inline-tab');
            var firstCount = btns.length;
            var activeBefore = panelsEl.querySelector('.inline-active');
            btns[0].click();
            var activeAfter = panelsEl.querySelector('.inline-active');
            tabsEl.querySelector('.inline-tab .inline-tab-close').click();
            var countAfterClose = tabsEl.querySelectorAll('.inline-tab').length;
            var favOpen = document.getElementById('favoritesPanel').classList.contains('open');
            var host = document.getElementById('inlineHost').getBoundingClientRect();
            var main = document.querySelector('.chat-main').getBoundingClientRect();
            return {firstCount: firstCount,
                    activeBefore: activeBefore && activeBefore.id,
                    activeAfter: activeAfter && activeAfter.id,
                    countAfterClose: countAfterClose,
                    favOpen: favOpen,
                    hostW: Math.round(host.width), hostH: Math.round(host.height),
                    mainW: Math.round(main.width), mainH: Math.round(main.height),
                    hostDisplay: getComputedStyle(document.getElementById('inlineHost')).display};
        })()""")
        ok("内嵌模式出现标签栏（两个面板 = 两个标签）",
           tabs and tabs.get("firstCount") == 2, tabs)
        ok("同一时间只有一个激活面板",
           tabs and tabs.get("activeBefore") == "tablePanel", tabs)
        ok("点标签能切换激活面板",
           tabs and tabs.get("activeAfter") == "favoritesPanel", tabs)
        ok("关标签后标签数减少、面板也关了",
           tabs and tabs.get("countAfterClose") == 1 and tabs.get("favOpen") is False, tabs)

        # 整面覆盖：内嵌容器的宽高 == 对话区（不是只占上面一半）
        ok("内嵌面板铺满整个对话区（不是上下分栏）",
           tabs and tabs.get("hostW") == tabs.get("mainW")
           and tabs.get("hostH") == tabs.get("mainH"), tabs)

        # ══════════════════════════════════════════════════════
        # 4.1) 点功能时先弹「小窗 / 对话框」询问框（含"不再询问"）
        # ══════════════════════════════════════════════════════
        ask = await js("""(function(){
            closeAllSidePanels();
            localStorage.removeItem('panelModeAsk');       // 恢复"每次询问"
            toggleFeaturePanel('favoritesPanel');
            var m = document.getElementById('panelModeModal');
            var shown = m.classList.contains('show');
            var choices = m.querySelectorAll('.mode-choice').length;
            var openedBeforeChoice = document.getElementById('favoritesPanel').classList.contains('open');
            document.getElementById('panelModeDontAsk').checked = true;
            m.querySelector('.mode-choice[data-mode="inline"]').click();
            var savedAsk = localStorage.getItem('panelModeAsk');
            var savedMode = localStorage.getItem('panelMode');
            var inDock = document.getElementById('favoritesPanel').parentElement.id === 'inlinePanels';
            closeAllSidePanels();
            toggleFeaturePanel('favoritesPanel');          // 不再询问 -> 直接开
            var askedAgain = m.classList.contains('show');
            var reopened = document.getElementById('favoritesPanel').classList.contains('open');
            return {shown: shown, choices: choices, openedBeforeChoice: openedBeforeChoice,
                    savedAsk: savedAsk, savedMode: savedMode, inDock: inDock,
                    askedAgain: askedAgain, reopened: reopened};
        })()""")
        ok("点功能会先弹出显示方式询问框", ask and ask.get("shown") is True, ask)
        ok("询问框有两个选项（小窗 / 对话框）", ask and ask.get("choices") == 2, ask)
        ok("选之前面板不会先打开", ask and ask.get("openedBeforeChoice") is False, ask)
        ok("勾「不再询问」后记住这次的选择",
           ask and ask.get("savedAsk") == "off" and ask.get("savedMode") == "inline"
           and ask.get("inDock") is True, ask)
        ok("勾过之后点功能直接打开、不再询问",
           ask and ask.get("askedAgain") is False and ask.get("reopened") is True, ask)

        # ══════════════════════════════════════════════════════
        # 4.2) 长内容由面板内部滚动（不是被裁掉、滚轮也滚不动）
        # ══════════════════════════════════════════════════════
        scroll = await js("""(function(){
            applyFeatureMode('wfPanel','inline');          // 绕过登录门槛，只量布局
            var body = document.getElementById('wfBody');
            var one = '这是一段用来把面板撑长的实验结论。';
            var html = '';
            for (var i = 0; i < 40; i++) {
                html += '<div class="wf-card"><h4>步骤 ' + i + '</h4><p>' + one + one + one + '</p></div>';
            }
            body.innerHTML = html;
            var el = document.getElementById('wfPanel');
            var sc = el.querySelector('.panel-scroll');
            var panel = el.getBoundingClientRect();
            var box = el.parentElement.getBoundingClientRect();
            sc.scrollTop = sc.scrollHeight;                // 能滚到底 = 真的能看全
            var bottom = sc.scrollTop;
            sc.scrollTop = 0;
            return {panelH: Math.round(panel.height),
                    scrollH: sc.scrollHeight, clientH: sc.clientHeight,
                    scrollable: sc.scrollHeight > sc.clientHeight + 4,
                    scrolledTo: Math.round(bottom),
                    insideBox: Math.round(panel.bottom) <= Math.round(box.bottom) + 1};
        })()""")
        ok("内嵌面板有确定高度（不是被内容撑开再裁掉）",
           scroll and scroll.get("scrollable") is True, scroll)
        ok("长内容能在面板内部滚到底",
           scroll and scroll.get("scrolledTo", 0) > 100, scroll)
        ok("面板没有溢出自己的容器",
           scroll and scroll.get("insideBox") is True, scroll)

        # ══════════════════════════════════════════════════════
        # 4.3) 收起内嵌面板后对话区恢复整面（覆盖层不再挡历史对话）
        # ══════════════════════════════════════════════════════
        clear = await js("""(function(){
            var host = document.getElementById('inlineHost');
            var before = getComputedStyle(host).display;
            clearInlinePanels();
            var msg = document.getElementById('messages').getBoundingClientRect();
            var main = document.querySelector('.chat-main').getBoundingClientRect();
            return {before: before,
                    after: getComputedStyle(host).display,
                    anyOpen: !!document.querySelector('#inlinePanels .open'),
                    msgW: Math.round(msg.width), mainW: Math.round(main.width),
                    msgH: Math.round(msg.height)};
        })()""")
        ok("收起内嵌面板后覆盖层消失", clear and clear.get("after") == "none"
           and clear.get("anyOpen") is False, clear)
        ok("对话区恢复整面（宽度 == 对话区，消息可见）",
           clear and clear.get("msgW") == clear.get("mainW")
           and clear.get("msgH", 0) > 100, clear)

        # 收尾：把拖动/缩放留下的内联样式和记忆清掉，别影响用户实际使用
        await js("""(function(){
            localStorage.setItem('panelMode','window');
            localStorage.removeItem('panelModeAsk');
            document.getElementById('wfBody').innerHTML = '';
            allSidePanelIds().forEach(function(id){
                var el = document.getElementById(id);
                ['position','left','top','width','height','zIndex','margin','flex']
                    .forEach(function(k){ el.style[k] = ''; });
                el.classList.remove('panel-moved','dragging','resizing','open');
                try { localStorage.removeItem('panelSize:' + id); } catch (e) {}
            });
            closeAuthPage(); closeAuthPrompt();
        })()""")
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="", help="已有服务的地址；不给就自己起一个")
    args = ap.parse_args()

    browser = find_browser()
    if not browser:
        print("SKIP  没有找到 Edge / Chrome，跳过真浏览器自检")
        return 0
    try:
        import websockets  # noqa: F401
    except ImportError:
        print("SKIP  缺少 websockets（pip install websockets），跳过真浏览器自检")
        return 0

    proc_srv = None
    url = args.url
    if not url:
        port = free_port()
        env = dict(os.environ)
        env.setdefault("FLASK_SECRET_KEY", "0" * 64)
        env.setdefault("MCP_TRANSPORT", "inproc")
        env.setdefault("APP_PORT", str(port))
        env.setdefault("APP_HOST", "127.0.0.1")
        proc_srv = subprocess.Popen([sys.executable, os.path.join(ROOT, "web", "app.py")],
                                    cwd=ROOT, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        url = f"http://127.0.0.1:{port}/"
        if not wait_http(url):
            print("SKIP  临时服务没起来（先手动 python web\\app.py，再用 --url 指定）")
            proc_srv.terminate()
            return 0

    dbg_port = free_port()
    # 注意：user-data-dir 不要放 %TEMP% —— 实测 Edge 在那里会因
    # "Failed to create a ProcessSingleton" 直接退出（rc=21），表现为端口起不来。
    profile = os.path.join(ROOT, f"_edge_check_{os.getpid()}")
    shutil.rmtree(profile, ignore_errors=True)
    proc_b = subprocess.Popen([
        browser, "--headless=new", "--no-sandbox", "--disable-gpu",
        "--disable-dev-shm-usage", "--disable-software-rasterizer",
        "--no-first-run", "--no-default-browser-check", "--disable-extensions",
        "--disable-background-networking",
        f"--remote-debugging-port={dbg_port}", f"--user-data-dir={profile}",
        "--window-size=1280,900", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    code = 1
    try:
        t0 = time.time()
        ws_url = None
        ready = False
        while time.time() - t0 < 30:
            try:
                with urllib.request.urlopen(
                        f"http://127.0.0.1:{dbg_port}/json/version", timeout=3) as r:
                    json.loads(r.read().decode())
                ready = True
                break
            except Exception:
                time.sleep(0.5)
        while ready and time.time() - t0 < 40:
            try:
                req = urllib.request.Request(
                    f"http://127.0.0.1:{dbg_port}/json/new?{url}", method="PUT")
                with urllib.request.urlopen(req, timeout=10) as r:
                    ws_url = json.loads(r.read().decode())["webSocketDebuggerUrl"]
                break
            except Exception:
                time.sleep(0.6)
        if not ws_url:
            print("SKIP  浏览器调试端口没起来（沙箱可能禁止 headless），跳过")
            return 0
        try:
            asyncio.run(asyncio.wait_for(drive(ws_url), 240))
        except asyncio.TimeoutError:
            print("FAIL  浏览器自检超时（240s）—— 页面里可能有请求没返回，"
                  "先手动打开页面看控制台报错")
            return 1
        bad = [c for c in CHECKS if not c[1]]
        for name, cond, got in CHECKS:
            print(("PASS  " if cond else "FAIL  ") + name
                  + ("" if cond else f"  -> got: {got!r}"))
        print(f"\n{len(CHECKS) - len(bad)}/{len(CHECKS)} checks passed")
        code = 0 if not bad else 1
    finally:
        for p in (proc_b, proc_srv):
            if p is not None:
                try:
                    p.terminate()
                    time.sleep(0.4)
                    p.kill()
                except Exception:
                    pass
        shutil.rmtree(profile, ignore_errors=True)
    return code


if __name__ == "__main__":
    sys.exit(main())
