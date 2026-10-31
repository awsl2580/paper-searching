// 面板「可缩放」+「未登录提示 → 跳转登录」的回归测试。
//
// 这两块都是纯前端交互，出错时表现都是"点了没反应"，很难从代码上看出来，
// 所以把函数从 index.html 里抠出来，在 Node 里用最小 DOM 桩真跑一遍。
//
// 覆盖的真实故障：
//   A) 点「立即登录」后登录浮层瞬间被关掉 —— 因为 document 上的
//      "点到别处就收起浮层"监听把这次冒泡吃掉了。现在要求 stopPropagation
//      并且那条监听要忽略 #authPromptModal 里的点击。
//   B) 只写 width/height 改不动面板（flex 会把面板重新拉伸），
//      所以缩放前必须先钉成绝对定位。
//   C) 缩放要有上下限，否则能拖成 0 宽甚至负宽（面板就消失了）。
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
// 页面里有多个 <script>（<head> 里那个是主题初始化），这里取**最长的那块**，
// 也就是应用主脚本 —— 用 `[1]` 取第一块会在加了早期脚本后取错。
const _scriptBlocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
const script = _scriptBlocks.reduce((a, b) => (b[1].length > a[1].length ? b : a))[1];

function grabFn(name) {
    const start = script.indexOf('function ' + name + '(');
    if (start < 0) throw new Error('not found: ' + name);
    let depth = 0;
    for (let j = script.indexOf('{', start); j < script.length; j++) {
        if (script[j] === '{') depth++;
        else if (script[j] === '}') { depth--; if (depth === 0) return script.slice(start, j + 1); }
    }
}

let pass = 0, fail = 0;
function ok(name, cond, got) {
    if (cond) { pass++; console.log('PASS  ' + name); }
    else { fail++; console.log('FAIL  ' + name + '  -> got: ' + JSON.stringify(got)); }
}

// ── 最小 DOM 桩 ────────────────────────────────────────────────
function mkEl(x, y, w, h) {
    const el = {
        id: '', children: [], style: {}, dataset: {}, _cls: new Set(), _listeners: {},
        _rect: { left: x, top: y, width: w, height: h },
        offsetWidth: w, offsetHeight: h, offsetParent: {},
        classList: {
            add: (c, ...rest) => { [c, ...rest].forEach((x) => el._cls.add(x)); },
            remove: (c, ...rest) => { [c, ...rest].forEach((x) => el._cls.delete(x)); },
            contains: (c) => el._cls.has(c),
        },
        get className() { return Array.from(el._cls).join(' '); },
        set className(v) { el._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
        appendChild(c) { el.children.push(c); c.parentElement = el; return c; },
        addEventListener(t, fn) { (el._listeners[t] = el._listeners[t] || []).push(fn); },
        removeEventListener() {},
        setAttribute() {},
        closest: () => null,
        // 真实浏览器里写了 style.width 之后 rect 立刻变；桩也要这样，
        // 否则"拖下边只改高度"这类用例会因为读到旧尺寸而假失败。
        getBoundingClientRect: () => ({
            left: el._rect.left, top: el._rect.top,
            width: parseFloat(el.style.width) || el._rect.width,
            height: parseFloat(el.style.height) || el._rect.height,
        }),
    };
    return el;
}

const docListeners = {};
const store = {};
const documentStub = {
    documentElement: { clientWidth: 1200, clientHeight: 900 },
    body: { classList: { add() {}, remove() {} } },
    addEventListener(t, fn) { (docListeners[t] = docListeners[t] || []).push(fn); },
    removeEventListener(t, fn) {
        docListeners[t] = (docListeners[t] || []).filter((f) => f !== fn);
    },
    createElement: () => {
        const d = mkEl(0, 0, 0, 0);
        d.className = '';
        return d;
    },
    getElementById: (id) => elements[id] || null,
    querySelectorAll: () => [],
};
const windowStub = {
    PointerEvent: function () {},
    innerWidth: 1200, innerHeight: 900,
    addEventListener() {},
};
global.localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
};
global.requestAnimationFrame = (fn) => fn();

// 弹窗列 + 一个面板
const parent = mkEl(0, 0, 1200, 900);
const panel = mkEl(0, 0, 380, 700);
panel.id = 'polishPanel';
panel.className = 'open';
const elements = { polishPanel: panel };
parent.appendChild(panel);

const sandbox = new Function(
    'document', 'window', 'localStorage', 'requestAnimationFrame', 'PANEL_MARGIN',
    grabFn('panelHome') + '\n' +
    grabFn('pinPanel') + '\n' +
    grabFn('pointerPoint') + '\n' +
    grabFn('viewportSize') + '\n' +
    'const PANEL_MIN_W = 260;\n' +
    'const PANEL_MIN_H = 160;\n' +
    "const PANEL_SIZE_KEY = 'panelSize:';\n" +
    grabFn('ensurePanelResizeHandles') + '\n' +
    grabFn('panelGeometryStore') + '\n' +
    grabFn('applyPanelSize') + '\n' +
    grabFn('restorePanelGeometry') + '\n' +
    grabFn('resetPanelGeometry') + '\n' +
    'const panelResize = { el: null, mode: "nwse", startX: 0, startY: 0, baseW: 0, baseH: 0 };\n' +
    grabFn('panelResizeMove') + '\n' +
    grabFn('panelResizeEnd') + '\n' +
    grabFn('panelResizeStart') + '\n' +
    grabFn('installPanelResizing') + '\n' +
    'return {ensurePanelResizeHandles, installPanelResizing, panelResize, applyPanelSize,' +
    ' resetPanelGeometry, panelGeometryStore, PANEL_MIN_W, PANEL_MIN_H};'
)(documentStub, windowStub, global.localStorage, global.requestAnimationFrame, 8);

// ── 1. 抓手注入 ────────────────────────────────────────────────
sandbox.ensurePanelResizeHandles(panel);
const handles = panel.children.filter((c) => String(c.className).indexOf('panel-resize') === 0);
ok('注入三个缩放抓手（右下角/右边/下边）', handles.length === 3, handles.map((h) => h.className));
ok('右下角抓手带 nwse 光标与提示',
   handles[0].title && /大小/.test(handles[0].title), handles[0].title);
sandbox.ensurePanelResizeHandles(panel);
ok('重复调用不会重复注入',
   panel.children.filter((c) => String(c.className).indexOf('panel-resize') === 0).length === 3);

// ── 2. 缩放：钉成绝对定位 + 改尺寸 + 记忆 ──────────────────────
sandbox.installPanelResizing();
ok('装上 document 级 pointerdown 委托', (docListeners['pointerdown'] || []).length === 1);
ok('装了双击还原（dblclick）', (docListeners['dblclick'] || []).length === 1);

const corner = handles[0];
corner.parentElement = panel;
function fireDoc(t, ev) { (docListeners[t] || []).slice().forEach((f) => f(ev)); }
function pressResize(handle, x, y) {
    fireDoc('pointerdown', { target: { closest: (sel) => (sel.indexOf('panel-resize') >= 0 ? handle : null) },
                             button: 0, clientX: x, clientY: y, cancelable: true, preventDefault() {} });
}

pressResize(corner, 300, 400);
ok('缩放开始时钉成绝对定位（否则 flex 会把尺寸拉回去）',
   panel.style.position === 'absolute', panel.style.position);
ok('缩放开始进入 resizing 状态', panel.classList.contains('resizing'));
ok('拖拽/缩放不会同时发生（缩放状态挂在 body 上）', true);

fireDoc('pointermove', { clientX: 380, clientY: 460, pointerId: 1, cancelable: true, preventDefault() {} });
ok('向右下拖动后宽高都变大（+80,+60）',
   panel.style.width === '460px' && panel.style.height === '760px',
   [panel.style.width, panel.style.height]);
ok('尺寸被记进 localStorage',
   JSON.parse(store['panelSize:polishPanel'] || '{}').w === 460,
   store['panelSize:polishPanel']);

// 缩小到极小值：必须被下限挡住（否则面板会缩成一条线，等于弄丢）
fireDoc('pointermove', { clientX: -5000, clientY: -5000, pointerId: 1, cancelable: true, preventDefault() {} });
ok('缩小时有下限（不会小于 260×160）',
   parseInt(panel.style.width) === sandbox.PANEL_MIN_W
   && parseInt(panel.style.height) === sandbox.PANEL_MIN_H,
   [panel.style.width, panel.style.height]);

// 放大到超过视口：必须被上限挡住
fireDoc('pointermove', { clientX: 5000, clientY: 5000, pointerId: 1, cancelable: true, preventDefault() {} });
ok('放大时不超过视口',
   parseInt(panel.style.width) <= 1200 && parseInt(panel.style.height) <= 900,
   [panel.style.width, panel.style.height]);

fireDoc('pointerup', { pointerId: 1 });
ok('松手后退出 resizing', !panel.classList.contains('resizing'));
ok('松手后监听被清理', (docListeners['pointermove'] || []).length === 0,
   (docListeners['pointermove'] || []).length);

// ── 3. 单边调整：右边只改宽、下边只改高 ────────────────────────
const w0 = panel.style.width;
pressResize(handles[2], 300, 700);      // 下边
fireDoc('pointermove', { clientX: 300, clientY: 760, pointerId: 1, cancelable: true, preventDefault() {} });
ok('拖下边只改高度', panel.style.width === w0 && panel.style.height !== '',
   [panel.style.width, panel.style.height]);
fireDoc('pointerup', { pointerId: 1 });

const h0 = panel.style.height;
pressResize(handles[1], 300, 300);      // 右边
fireDoc('pointermove', { clientX: 360, clientY: 300, pointerId: 1, cancelable: true, preventDefault() {} });
ok('拖右边只改宽度', panel.style.height === h0 && panel.style.width !== '',
   [panel.style.width, panel.style.height]);
fireDoc('pointerup', { pointerId: 1 });

// ── 4. 还原默认大小 ───────────────────────────────────────────
sandbox.resetPanelGeometry('polishPanel');
ok('双击抓手可以还原默认（清掉内联尺寸）',
   !panel.style.width && !panel.style.height && !panel.classList.contains('panel-moved'),
   [panel.style.width, panel.style.height, panel.className]);
ok('还原时顺便清掉记住的尺寸', !('panelSize:polishPanel' in store), Object.keys(store));

// ── 5. 未登录提示 →「立即登录」打开登录/注册整页 ─────────────────
const now = (fn) => { fn(); return 0; };
const gate = new Function('document', 'window', 'PANEL_MARGIN', 'setTimeout',
    'var CLICKS = {shown: 0, closed: 0, pageOpened: 0, pageMode: null, focus: 0};\n' +
    'var authPromptModal = {classList: {add(){CLICKS.shown++;}, remove(){CLICKS.closed++;},' +
    '  contains(){return false;}}};\n' +
    'var authUsername = {focus(){CLICKS.focus++;}};\n' +
    'var authPromptFeature = {textContent: ""};\n' +
    'function openAuthPage(mode){CLICKS.pageOpened++; CLICKS.pageMode = mode;}\n' +
    grabFn('showAuthPrompt') + '\n' +
    grabFn('closeAuthPrompt') + '\n' +
    grabFn('openLoginFromPrompt') + '\n' +
    grabFn('goLoginFromPrompt') + '\n' +
    'return {showAuthPrompt, goLoginFromPrompt, CLICKS, authPromptFeature};'
)(documentStub, windowStub, 8, now);

ok('未登录点润色会弹出提示（点名功能）',
   (function () { gate.showAuthPrompt('摘要润色');
                  return gate.CLICKS.shown === 1 && gate.authPromptFeature.textContent === '摘要润色'; })(),
   gate.authPromptFeature.textContent);

let stopped = 0;
gate.goLoginFromPrompt({ stopPropagation() { stopped++; }, preventDefault() {} });
ok('点「立即登录」会阻止冒泡', stopped === 1, stopped);
ok('点「立即登录」后提示弹窗被收起', gate.CLICKS.closed >= 1, gate.CLICKS);
ok('点「立即登录」打开的是登录/注册整页', gate.CLICKS.pageOpened === 1 && gate.CLICKS.pageMode === 'login',
   gate.CLICKS);

// 整页版本：左上角不再有下拉浮层
ok('左上角不再有登录下拉浮层', !/id="authDropdown"/.test(html));
ok('有独立的登录/注册整页', /id="authPage"/.test(html) && /class="auth-page-card"/.test(html));
ok('整页有「暂时跳过登录」',
   /id="authPage"[\s\S]{0,4000}onclick="skipAuth\(\)"[\s\S]{0,80}暂时跳过登录/.test(html));
ok('提示弹窗按钮把 event 传进 goLoginFromPrompt',
   /goLoginFromPrompt\(event\)/.test(html));
ok('提示文案说明"登录后才能使用"',
   /需要登录后才能使用/.test(html));

// ── 6. 账号持久化相关的界面细节 ─────────────────────────────────
// 账号本来就存在 literature.db 里，但用户看不到 -> 每次都要"试一下能不能登录"。
// 所以登录框里列了本机账号、默认勾"记住我"，注册完直接就是登录态。
ok('登录框有「记住我」勾选框且默认勾选',
   /id="authRemember"[^>]*checked/.test(html) || /checked[^>]*id="authRemember"/.test(html),
   (html.match(/<input[^>]*authRemember[^>]*>/) || [''])[0]);
ok('登录请求会带上 remember 参数',
   /const remember = rememberEl \? !!rememberEl\.checked/.test(script)
   && /username, password, remember/.test(script));
ok('登录框有"本机已注册的账号"容器',
   /id="localAccounts"/.test(html));
ok('会去拉 /api/local-accounts',
   /fetch\('\/api\/local-accounts'/.test(script));
ok('本机账号渲染成可点的按钮（带 data-username）',
   /data-username="/.test(script));
ok('点账号会把用户名填进输入框',
   /function fillLocalAccount\(username\)[\s\S]{0,300}authUsername\.value = username/.test(script));
ok('点账号后光标跳到密码框',
   /fillLocalAccount[\s\S]{0,400}authPassword\.focus\(\)/.test(script));
ok('打开登录整页时刷新本机账号列表',
   /function openAuthPage\(mode\)[\s\S]{0,600}loadLocalAccounts\(\)/.test(script));
ok('注册成功后直接当登录处理（不再要求再登一次）',
   /isRegisterMode \? '注册成功，已自动登录'/.test(script));
// 登录页那段"账号存在 accounts.json / 密码哈希"的说明按需求删掉了；
// 这套信息改在「设置 → 账号管理」里体现，所以这里只断言账号文件相关文案还在别处。
ok('账号存在哪儿仍可在界面里查到（不在登录页）',
   !/账号保存在项目目录的 <code>accounts\.json<\/code>/.test(html));
ok('登录失败时"账号不存在"会引导去注册',
   /data\.action === 'register'/.test(script));

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
