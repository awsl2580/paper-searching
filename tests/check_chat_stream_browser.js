// 真浏览器自检：一轮回答结束后，发送按钮必须回到「发送」。
//
// **不属于 `run_all.py`**：它需要一个真排版引擎 + 真 fetch 流，
// 而且要把"服务端发完 done 之后**不关连接**"这个真实条件造出来。
//
// 存在的理由很具体（用户报的那几个 bug）：
//   1) 回答已经输出完、论文卡片也画出来了，发送按钮却停在「思考中...」，
//      输入框被锁，切到别的对话分支同样发不出去。
//      根因是前端只认"连接被关掉"这个结束信号；真实环境里连接常被内核/代理
//      一直挂着，于是这一轮永远不结束。
//   2) 思考进行中删掉正在回答的会话，聊天里会堆出一串「❌ 会话不存在」
//      （删除不幂等 + 404 被当成聊天错误写进对话）。
//
// 用法：
//   node tests/check_chat_stream_browser.js
//   node tests/check_chat_stream_browser.js --html static/index.html
// 没装 Edge/Chrome 时打印 SKIP 并以 0 退出。
const http = require('http');
const fs = require('fs');
const os = require('os');
const path = require('path');
const { spawn } = require('child_process');

const ROOT = path.join(__dirname, '..');
const args = process.argv.slice(2);
const htmlArg = args.indexOf('--html');
const HTML_PATH = htmlArg >= 0 ? args[htmlArg + 1] : path.join(ROOT, 'static', 'index.html');

const BROWSERS = [
    'C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Microsoft\\Edge\\Application\\msedge.exe',
    'C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe',
    'C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
    '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
];

const browserPath = BROWSERS.find((p) => fs.existsSync(p));
if (!browserPath) {
    console.log('SKIP  没找到 Edge/Chrome，跳过真浏览器自检');
    process.exit(0);
}

let pass = 0, fail = 0;
function ok(name, cond, got) {
    if (cond) { pass++; console.log('PASS  ' + name); }
    else { fail++; console.log('FAIL  ' + name + '  -> got: ' + JSON.stringify(got)); }
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

let DELETED = false;   // 删过之后，GET 这段会话就该 404（模拟真实后端）

// ── 假后端：只实现这个页面会碰到的接口 ───────────────────────────
function makeServer(html, mode) {
    return http.createServer((req, res) => {
        const url = req.url.split('?')[0];
        const json = (o, code) => {
            res.writeHead(code || 200, { 'Content-Type': 'application/json; charset=utf-8' });
            res.end(JSON.stringify(o));
        };
        if (url === '/' || url === '/index.html') {
            res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
            return res.end(html);
        }
        if (url === '/api/chat' && req.method === 'POST') return chat(req, res, mode);
        if (url === '/api/chat/abort') return json({ success: true, aborted: false });
        if (url === '/api/me') return json({ success: true, user: { id: 1, username: 'tester' } });
        if (url === '/api/models') {
            return json({ success: true, backend: 'deepseek',
                models: { catalog: { default: 'deepseek-V4.1-flash',
                    models: [{ id: 'deepseek-V4.1-flash', name: 'DeepSeek-V4.1-Flash',
                               desc: '快速通用模型', default: true }] },
                    roles: [] } });
        }
        if (url === '/api/sources') return json({ success: true, sources: [] });
        if (url === '/api/history') return json({ success: true, history: [] });
        if (url === '/api/favorites') return json({ success: true, favorites: [] });
        if (url === '/api/conversations') {
            if (req.method === 'GET') {
                if (mode === 'switch') {
                    return json({ success: true, conversations: [
                        { id: 99, title: '另一个分支', message_count: 2,
                          updated_at: '2026-10-25T10:00:00' }] });
                }
                if (mode === 'delete') {
                    return json({ success: true, conversations: DELETED ? [] : [
                        { id: 7, title: '这段对话', message_count: 1,
                          updated_at: '2026-10-25T10:00:00' }] });
                }
                return json({ success: true, conversations: [] });
            }
            return json({ success: true, conversation_id: 7 });
        }
        // 删除是幂等的：已经删过也回 success（与真实后端一致）
        if (url === '/api/conversations/7' && req.method === 'DELETE') {
            DELETED = true;
            return json({ success: true, message: '已删除', already: false });
        }
        if (url.startsWith('/api/conversations/')) {
            if (DELETED) return json({ error: '会话不存在' }, 404);
            return json({ success: true, messages: [] });
        }
        return json({ success: true });
    });
}

function chat(req, res, mode) {
    let body = '';
    req.on('data', (d) => { body += d; });
    req.on('end', () => {
        res.writeHead(200, {
            'Content-Type': 'text/event-stream; charset=utf-8',
            'Cache-Control': 'no-cache, no-transform',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        });
        const send = (o) => res.write('data: ' + JSON.stringify(o) + '\n\n');
        send({ type: 'model', model: 'deepseek-V4.1-flash',
               name: 'DeepSeek-V4.1-Flash', thinking: false });
        send({ type: 'conversation_id', conversation_id: 7 });
        send({ type: 'status', text: '正在思考…' });
        setTimeout(() => send({ type: 'token', token: '第一段回答。' }), 120);
        setTimeout(() => send({ type: 'token', token: '第二段回答。' }), 260);
        setTimeout(() => send({ type: 'done' }), mode === 'normal' ? 420 : 9000);
        // ⚠️ 关键：done 之后**故意不关连接**，只每 10 秒发一次心跳。
        // 这正是真实环境里的样子（连接被内核/代理一直挂着）。
        const ping = setInterval(() => { try { res.write(': ping\n\n'); } catch (e) {} }, 10000);
        req.on('close', () => clearInterval(ping));
    });
}

// ── 极简 CDP 客户端（Node 22+ 自带 WebSocket，不用装依赖）──────────
class CDP {
    constructor(ws) {
        this.ws = ws; this.id = 0; this.pending = new Map();
        ws.addEventListener('message', (ev) => {
            const msg = JSON.parse(ev.data);
            const slot = msg.id && this.pending.get(msg.id);
            if (!slot) return;
            this.pending.delete(msg.id);
            msg.error ? slot.reject(new Error(JSON.stringify(msg.error))) : slot.resolve(msg.result);
        });
    }
    send(method, params) {
        const id = ++this.id;
        return new Promise((resolve, reject) => {
            this.pending.set(id, { resolve, reject });
            this.ws.send(JSON.stringify({ id, method, params: params || {} }));
        });
    }
    async eval(expression) {
        const r = await this.send('Runtime.evaluate',
            { expression, awaitPromise: true, returnByValue: true });
        if (r.exceptionDetails) throw new Error(JSON.stringify(r.exceptionDetails));
        return r.result.value;
    }
}

async function launchBrowser(port, profile) {
    const proc = spawn(browserPath, [
        '--headless=new', `--remote-debugging-port=${port}`,
        `--user-data-dir=${profile}`, '--no-first-run', '--no-default-browser-check',
        '--disable-gpu', '--window-size=1280,900', 'about:blank',
    ], { stdio: 'ignore' });
    for (let i = 0; i < 60; i++) {
        try {
            const v = await (await fetch(`http://127.0.0.1:${port}/json/version`)).json();
            if (v.webSocketDebuggerUrl) return proc;
        } catch (e) { /* 还没起来 */ }
        await sleep(250);
    }
    proc.kill();
    throw new Error('浏览器没起来');
}

async function openPage(httpPort, cdpPort) {
    const target = await (await fetch(
        `http://127.0.0.1:${cdpPort}/json/new?url=http://127.0.0.1:${httpPort}/`,
        { method: 'PUT' })).json();
    const ws = new WebSocket(target.webSocketDebuggerUrl);
    await new Promise((r, j) => {
        ws.addEventListener('open', r);
        ws.addEventListener('error', j);
    });
    const cdp = new CDP(ws);
    await cdp.send('Runtime.enable');
    await cdp.send('Page.enable');
    await cdp.send('Page.navigate', { url: `http://127.0.0.1:${httpPort}/` });
    await sleep(2500);          // 等页面脚本跑完 checkLogin 与各接口
    return { cdp, ws };
}

const READ_STATE = `(function(){
    const btn = document.getElementById('sendBtn');
    const inp = document.getElementById('input');
    const bubbles = Array.from(document.querySelectorAll('#messages .bubble'))
        .map(b => b.textContent.trim()).filter(Boolean);
    const items = Array.from(document.querySelectorAll('.conv-item')).map(i => i.dataset.convId);
    return {
        buttonText: btn ? btn.textContent : null,
        inputDisabled: inp ? inp.disabled : null,
        isProcessing: (typeof isProcessing !== 'undefined') ? isProcessing : null,
        historyLen: (typeof messageHistory !== 'undefined') ? messageHistory.length : null,
        activeConv: (typeof activeConversationId !== 'undefined') ? activeConversationId : null,
        answer: bubbles.length ? bubbles[bubbles.length - 1] : null,
        bubbleCount: bubbles.length,
        items: items,
    };
})()`;

const SEND = `(function(){
    const i = document.getElementById('input');
    i.value = '农业';
    i.dispatchEvent(new Event('input', { bubbles: true }));
    document.getElementById('sendBtn').click();
    return true;
})()`;

async function scenario(mode, httpPort, cdpPort) {
    DELETED = false;
    const html = fs.readFileSync(HTML_PATH, 'utf8');
    const server = makeServer(html, mode);
    await new Promise((r) => server.listen(httpPort, '127.0.0.1', r));
    const profile = fs.mkdtempSync(path.join(os.tmpdir(), 'chat-stream-'));
    const browser = await launchBrowser(cdpPort, profile);
    try {
        const { cdp, ws } = await openPage(httpPort, cdpPort);
        await cdp.eval(SEND);

        if (mode === 'switch') {
            // 回答还在流的时候切到另一个分支：新分支必须立刻可用
            await sleep(1200);
            await cdp.eval('openConversation(99)');
            await sleep(1200);
            const mid = await cdp.eval(READ_STATE);
            ok('切到别的分支后按钮立刻可用（不被上一轮锁住）',
               mid.buttonText === '发送' && mid.inputDisabled === false, mid);
            await sleep(9000);   // 等被切走的那一轮也结束
            const end = await cdp.eval(READ_STATE);
            ok('旧分支跑完也不许把内容写进新分支的历史',
               end.historyLen === 0, end);
            ok('旧分支跑完后界面依旧是可用的',
               end.buttonText === '发送' && end.isProcessing === false, end);
        } else if (mode === 'delete') {
            // 思考进行中删掉正在回答的会话（用户报的那个操作）
            await sleep(1200);
            const del = await cdp.eval(`(function(){
                window.confirm = function(){ return true; };
                const b = document.querySelector('.btn-del-conv');
                if (!b) return 'no-delete-button';
                b.click();
                return 'ok';
            })()`);
            ok('侧栏里能点到删除按钮', del === 'ok', del);
            await sleep(3000);
            const st = await cdp.eval(READ_STATE);
            ok('不再往聊天里堆「❌ 会话不存在」',
               !(st.answer || '').includes('会话不存在') && st.bubbleCount === 0, st);
            ok('被删的会话从侧栏消失', st.items.length === 0, st.items);
            ok('当前会话复位（不再挂在一段已删的对话上）', st.activeConv === null, st);
            ok('界面回到可用状态', st.buttonText === '发送' && st.inputDisabled === false, st);

            // 侧栏里如果还挂着一条已经删掉的记录（别的标签页删的 / 列表是旧的），
            // 点它也不能往聊天里堆报错 —— 这条是「❌ 会话不存在」的直接回归
            await cdp.eval(`(function(){
                conversations.push({ id: 7, title: '已经删掉的对话', message_count: 2 });
                renderConvList();
                return true;
            })()`);
            await sleep(300);
            await cdp.eval('openConversation(7)');
            await sleep(800);
            const ghost = await cdp.eval(READ_STATE);
            ok('点开一条已删记录：聊天里不出现「会话不存在」',
               !(ghost.answer || '').includes('会话不存在') && ghost.bubbleCount === 0, ghost);
            ok('...那条记录会被从侧栏移除', ghost.items.length === 0, ghost.items);
        } else {
            // done 之后连接一直不关：旧代码会永远卡在「思考中...」
            await sleep(3000);
            const st = await cdp.eval(READ_STATE);
            ok('done 到达后按钮回到「发送」（不等连接关掉）',
               st.buttonText === '发送', st);
            ok('done 到达后输入框解锁', st.inputDisabled === false, st);
            ok('done 到达后 isProcessing 复位', st.isProcessing === false, st);
            ok('正文完整保留', st.answer === '第一段回答。第二段回答。', st.answer);
        }
        ws.close();
    } finally {
        browser.kill();
        server.close();
        await sleep(300);
    }
}

(async () => {
    console.log('='.repeat(68));
    console.log('场景 1：服务端发完 done 但连接不关');
    await scenario('normal', 8151, 9351);
    console.log('\n场景 2：回答还在流时切到别的分支');
    await scenario('switch', 8152, 9352);
    console.log('\n场景 3：思考进行中删掉正在回答的会话');
    await scenario('delete', 8153, 9353);
    console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
    process.exit(fail ? 1 : 0);
})().catch((e) => {
    console.error('ERROR ' + e.message);
    process.exit(1);
});
