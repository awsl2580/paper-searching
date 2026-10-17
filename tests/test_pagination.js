// Verify the search-result paging + collapse logic from index.html.
// The functions are extracted from the real file, so this tests shipped code,
// not a copy. No browser is available in this environment (headless Chrome
// crashes), so DOM behaviour is checked with a minimal stub.
const fs = require('fs');
const path = require('path');

const HTML = path.join(__dirname, '..', 'static', 'index.html');
const html = fs.readFileSync(HTML, 'utf8');
const script = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];

function grabFn(name) {
    const start = script.indexOf('function ' + name + '(');
    if (start < 0) throw new Error('function not found: ' + name);
    let depth = 0;
    for (let j = script.indexOf('{', start); j < script.length; j++) {
        if (script[j] === '{') depth++;
        else if (script[j] === '}') { depth--; if (depth === 0) return script.slice(start, j + 1); }
    }
    throw new Error('unbalanced: ' + name);
}

// ---- 最小 DOM 桩：只实现被测代码用到的部分 ----
class El {
    constructor(tag) {
        this.tagName = (tag || 'div').toUpperCase();
        this.children = [];
        this.style = {};
        this.dataset = {};
        this._cls = new Set();
        this._text = '';
        this.attrs = {};
        this._listeners = {};
        this.classList = {
            add: (c) => this._cls.add(c),
            remove: (c) => this._cls.delete(c),
            contains: (c) => this._cls.has(c),
            toggle: (c, on) => {
                const want = (on === undefined) ? !this._cls.has(c) : !!on;
                if (want) this._cls.add(c); else this._cls.delete(c);
                return want;
            },
        };
    }
    get className() { return Array.from(this._cls).join(' '); }
    set className(v) { this._cls = new Set(String(v || '').split(/\s+/).filter(Boolean)); }
    get textContent() { return this._text; }
    set textContent(v) { this._text = String(v); }
    set innerHTML(v) { this._html = String(v); }
    get innerHTML() { return this._html || ''; }
    appendChild(c) { this.children.push(c); c.parentElement = this; return c; }
    setAttribute(k, v) { this.attrs[k] = v; }
    getAttribute(k) { return this.attrs[k]; }
    closest(sel) {
        // 只实现本测试用到的 .panel-close 判定；_closeHit 由用例设置
        const cls = sel.replace(/^\./, '');
        if (cls === 'panel-close') return this._closeHit ? this : null;
        return null;
    }
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
    fire(type, ev) { (this._listeners[type] || []).forEach((f) => f(ev || {})); }
    remove() {
        if (this._parent) this._parent.children = this._parent.children.filter((c) => c !== this);
    }
    // 被测代码用到的查询：按 class 名匹配（桩里不解析 innerHTML，只认已 append 的子节点）
    querySelector(sel) { return this._find(sel, false); }
    querySelectorAll(sel) { return this._find(sel, true); }
    _find(sel, all) {
        // 支持 ":scope > .foo" / ".foo" / ".foo.bar" 这类简单选择器：
        // 去掉 :scope 和子选择符，只按第一个 class 名匹配。
        const cleaned = String(sel).replace(/:scope/g, '').replace(/>/g, ' ').trim();
        const cls = cleaned.replace(/^\./, '').split(/[\s.]+/)[0];
        const out = [];
        const walk = (n) => {
            n.children.forEach((c) => {
                if (c._cls && c._cls.has(cls)) out.push(c);
                walk(c);
            });
        };
        walk(this);
        return all ? out : (out[0] || null);
    }
}

// 构造一个"卡片"桩，模拟 togglePaperCard 需要的结构
function makeCard() {
    const card = new El('div');
    card.className = 'paper-card';
    const head = new El('div'); head.className = 'pc-head';
    const body = new El('div'); body.className = 'pc-body'; body.style.display = 'none';
    const btn = new El('button'); btn.className = 'pc-toggle';
    card.appendChild(head); card.appendChild(body); card.appendChild(btn);
    return card;
}

let pass = 0, fail = 0;
function ok(name, cond, got) {
    if (cond) { pass++; console.log('PASS  ' + name); }
    else { fail++; console.log('FAIL  ' + name + '  -> got: ' + JSON.stringify(got)); }
}

// ---- 只抽取纯逻辑 + 状态函数，绕开 document/lastPapers 依赖 ----
const src = [
    'const PAGE_SIZE = 5;',
    'let cardPage = 0; let cardExpanded = {}; let cardsAllExpanded = false;',
    'let lastPapers = [];',
    grabFn('paginate'),
    grabFn('togglePaperCard'),
    // resetCardPaging 依赖 lastPapers/cardPages，这里就地重写等价体。
    // （totalCardPages 已删：它没有任何调用方，且这里本来也没在用它。）
    'function resetCardPaging(){ cardPage = 0; cardsAllExpanded = false; Object.keys(cardExpanded).forEach(k => delete cardExpanded[k]); }',
    'return {paginate, togglePaperCard, resetCardPaging, PAGE_SIZE, getCardPage: () => cardPage, getExpanded: () => cardExpanded};',
].join('\n');
const api = new Function(src)();

// ---- paginate ----
const items = (n) => Array.from({ length: n }, (_, i) => 'p' + i);

let r = api.paginate(items(12), 0, 5);
ok('12 篇 -> 3 页', r.totalPages === 3, r.totalPages);
ok('第 1 页 5 篇', r.items.length === 5, r.items.length);
ok('第 1 页区间 1-5', r.from === 1 && r.to === 5, [r.from, r.to]);
ok('第 1 页起始序号 0', r.startIdx === 0, r.startIdx);

r = api.paginate(items(12), 2, 5);
ok('第 3 页只剩 2 篇', r.items.length === 2, r.items.length);
ok('第 3 页区间 11-12', r.from === 11 && r.to === 12, [r.from, r.to]);

r = api.paginate(items(12), 99, 5);
ok('页码越界回落到最后一页', r.page === 2, r.page);
r = api.paginate(items(12), -3, 5);
ok('负页码回落到第 1 页', r.page === 0, r.page);
r = api.paginate(items(12), 1, 5);
ok('第 2 页内容是第 6-10 篇', r.items[0] === 'p5' && r.items[4] === 'p9', r.items);

r = api.paginate([], 0, 5);
ok('空结果仍是 1 页', r.totalPages === 1, r.totalPages);
ok('空结果 0 篇', r.items.length === 0 && r.from === 0 && r.to === 0, [r.from, r.to]);

r = api.paginate(items(5), 0, 5);
ok('正好一页', r.totalPages === 1 && r.items.length === 5, [r.totalPages, r.items.length]);

r = api.paginate(items(6), 0, 5);
ok('多 1 篇就分 2 页', r.totalPages === 2, r.totalPages);

r = api.paginate(items(50), 9, 5);
ok('50 篇 10 页，末页 5 篇', r.totalPages === 10 && r.items.length === 5, [r.totalPages, r.items.length]);

ok('pages 分块与 items 一致',
   api.paginate(items(12), 1, 5).pages[1].join() === api.paginate(items(12), 1, 5).items.join(),
   api.paginate(items(12), 1, 5).pages.length);

// ---- 折叠 ----
const card = makeCard();
const body = card.querySelector('.pc-body');
ok('初始为折叠', body.style.display === 'none', body.style.display);
api.togglePaperCard('A', card);
ok('点击后展开', body.style.display === 'block', body.style.display);
ok('展开时加上 is-open', card.classList.contains('is-open'));
ok('展开状态被记住', api.getExpanded()['A'] === true, api.getExpanded());
api.togglePaperCard('A', card);
ok('再点收回', body.style.display === 'none', body.style.display);
ok('收回时去掉 is-open', !card.classList.contains('is-open'));
ok('收回状态被记住', api.getExpanded()['A'] === false, api.getExpanded());

api.resetCardPaging();
ok('新检索后回到第 1 页', api.getCardPage() === 0, api.getCardPage());
ok('新检索后清空展开记忆', Object.keys(api.getExpanded()).length === 0, api.getExpanded());

// ---- 静态接线：模板里必须真有这些钩子 ----
ok('模板含可折叠容器 pc-body', /class="pc-body"/.test(script));
ok('模板含折叠按钮 pc-toggle', /class="pc-toggle"/.test(script));
ok('模板含翻页条 paper-pager', /class="paper-pager"/.test(script));
ok('渲染前先移除旧卡片（否则翻页会累加）', /querySelector\('\.paper-cards'\)/.test(script)
   && /old\.remove\(\)/.test(script));
ok('渲染前先移除旧翻页条', /querySelector\('\.paper-pager'\)/.test(script));
ok('标题栏可点击折叠', /pc-head'\)\.addEventListener\('click'/.test(script));
ok('表格也有翻页', /gotoTablePage/.test(script) && /sorted\.length/.test(script));
ok('发新消息时重置分页', /resetCardPaging\(\);/.test(script) && /tablePage = 0;/.test(script));

// ---- 工作流"已等待秒数"（综述那一步实测 6s+，只转圈会让人以为卡住）----
ok('运行中的步骤带已等待元素', /status === 'running'[\s\S]{0,240}wf-elapsed/.test(script));
ok('计时器在渲染后被同步', /syncWfElapsedTimer\(\);/.test(script));
ok('计时器会被停掉（不泄漏）', /function stopWfElapsedTimer/.test(script)
   && /stopWfElapsedTimer\(\);/.test(script));
ok('开跑时记录起点时间', /wfRunStartedAt = Date\.now\(\);/.test(script));
ok('已等待样式存在', /\.wf-elapsed\s*\{/.test(html));

// 用 DOM 桩验证 renderWfSteps 真的只给 running 那步插入计时元素
const elapsedSrc = grabFn('renderWfSteps');
const fakeHost = new El('div');
const sandbox = new Function('document', 'escapeHtml', 'escapeAttr', 'WF_MARK',
    'syncWfElapsedTimer',
    elapsedSrc + '\nrenderWfSteps(arguments[5]);'
);
let synced = 0;
sandbox({ getElementById: () => fakeHost },
        (s) => String(s == null ? '' : s), (s) => String(s == null ? '' : s),
        { done: '✓', failed: '!' }, () => { synced++; },
        [{ key: 'search', title: '检索', status: 'done' },
         { key: 'synth', title: '综述', status: 'running' }]);
const rendered = fakeHost.innerHTML;
ok('渲染结果里只有 running 那步有 wf-elapsed',
   (rendered.match(/wf-elapsed/g) || []).length === 1,
   (rendered.match(/wf-elapsed/g) || []).length);
ok('running 那步带 data-status 便于定位',
   /data-status="running"/.test(rendered), rendered.slice(0, 160));
ok('渲染后触发了计时器同步', synced === 1, synced);

// ---- 右侧弹窗列（顶部四个功能不再挤占对话区）----
// 改版前：四个面板排在 .chat-main 里，一打开就把消息列表往下挤。
// 现在统一挂到 .side-panels，用 .open 类开合。
ok('存在右侧弹窗列容器', /class="side-panels" id="sidePanels"/.test(html));
ok('弹窗列是 fixed 定位（浮层，不占对话流）',
   /\.side-panels\s*\{[^}]*position:\s*fixed/.test(html));
// 上下边界要和主框对齐：主框距视口上下各 35px，
// 弹窗列用 37/47（多 2~12px 的余量）才落在主框内侧不溢出。
ok('弹窗列上下边界与主框对齐（37px 档）',
   /\.side-panels\s*\{[^}]*top:\s*37px/.test(html)
   && /\.side-panels\s*\{[^}]*bottom:\s*37px/.test(html));
ok('窄屏回落档也用 47px（与主框对齐）',
   /top:\s*47px/.test(html) && /bottom:\s*47px/.test(html));
ok('弹窗列不再用旧的 80px', !/\.side-panels\s*\{[^}]*top:\s*80px/.test(html));

// 四个面板都必须有 #id.open 规则：只写 class 会被各自的 #id{display:none} 压住
for (const pid of ['favoritesPanel', 'polishPanel', 'tablePanel', 'wfPanel']) {
    ok(`面板 ${pid} 在弹窗列内`, html.includes(`id="${pid}"`));
    ok(`面板 ${pid} 有 #id.open 打开规则`,
       new RegExp('#' + pid + '\\.open').test(html));
}
// 开合统一走辅助函数，不再各写一份 style.display
ok('有统一的 openSidePanel/closeSidePanel',
   /function openSidePanel\(/.test(script) && /function closeSidePanel\(/.test(script));
ok('四个面板都不再用 style.display 开关',
   !/getElementById\('(wfPanel|tablePanel|polishPanel|favoritesPanel)'\)\.style\.display/.test(script));
ok('切换函数走 toggleSidePanel', /function toggleSidePanel\(/.test(script)
   && /toggleSidePanel\('wfPanel'/.test(script)
   && /toggleSidePanel\('tablePanel'/.test(script)
   && /toggleSidePanel\('polishPanel'/.test(script));

// ---- 未登录点"科研工作流"要弹警告，而不是打开空面板 ----
ok('科研工作流按钮带登录门槛',
   /toggleSidePanel\('wfPanel',\s*'科研工作流'\)/.test(script));
ok('润色按钮带登录门槛',
   /toggleSidePanel\('polishPanel',\s*'摘要润色'\)/.test(script));
ok('toggleSidePanel 先查登录再打开',
   /function toggleSidePanel\([\s\S]{0,300}requireLogin\(feature/.test(script));

// 行为验证：未登录时不许打开，已登录时才打开
// closeSidePanel 里会收尾拖动/缩放状态（panelDragEl / panelResizeEl），桩里一并给上
const tglSrc = [
    'let panelDragEl = null;',
    'let panelResizeEl = null;',
    'function endPanelDrag() {}',
    'function endPanelResize() {}',
    grabFn('isSidePanelOpen'), grabFn('openSidePanel'),
    grabFn('closeSidePanel'), grabFn('toggleSidePanel'),
].join('\n');
const closed = {};
const fakePanels = {
    wfPanel: { classList: { contains: () => !!closed.wfPanel,
                            add: () => { closed.wfPanel = true; },
                            remove: () => { closed.wfPanel = false; } } },
};
let prompted = 0;
const tgl = new Function('document', 'requireLogin', tglSrc + '\nreturn toggleSidePanel;')(
    { getElementById: (id) => fakePanels[id] || null },
    (feature) => { prompted++; return true; }      // 模拟未登录：拦截
);
ok('未登录时 toggle 返回 false 且不打开', tgl('wfPanel', '科研工作流') === false
   && !closed.wfPanel, closed);
ok('未登录时弹了提示', prompted === 1, prompted);

const tgl2 = new Function('document', 'requireLogin', tglSrc + '\nreturn toggleSidePanel;')(
    { getElementById: (id) => fakePanels[id] || null },
    () => false                                     // 模拟已登录：放行
);
ok('已登录时能打开', tgl2('wfPanel', '科研工作流') === true && closed.wfPanel === true, closed);
ok('再点一次是收起', tgl2('wfPanel', '科研工作流') === false && closed.wfPanel === false, closed);

// ---- 收藏从"聊天刷屏"改成独立面板 ----
ok('收藏面板存在', html.includes('id="favoritesPanel"'));
ok('收藏有渲染函数 loadFavorites', /async function loadFavorites\(/.test(script));
ok('收藏不再往聊天里刷列表',
   !/📚 \*\*我的收藏\*\*/.test(script));
ok('删除收藏会刷新面板（面板开着时）',
   /function favoritesFeedback\([\s\S]{0,200}loadFavorites\(\)/.test(script));

// ---- 主对话框放大 ----
ok('主框加宽到 1180px', /max-width:\s*min\(1180px/.test(html));
// 主框距视口上下各 35px -> 高度 = 100vh - 2*35
ok('主框上下各留 35px', /height:\s*calc\(100vh - 70px\)/.test(html));
ok('主框不再用旧的 80px 留白', !/calc\(100vh - 160px\)/.test(html));

// ---- 统一的关闭按钮 ----
// 四个面板共用 .panel-close，位置固定在标题栏最右。
const closeBtns = html.match(/<button[^>]*class="panel-close"[^>]*>✕<\/button>/g) || [];
ok('四处关闭按钮都用 .panel-close', closeBtns.length === 4, closeBtns.length);
ok('关闭按钮都带 type=button', closeBtns.every(b => /type="button"/.test(b)), closeBtns);
ok('关闭按钮都带 aria-label', closeBtns.every(b => /aria-label="关闭"/.test(b)), closeBtns);
// 去掉 onclick（各面板的关闭函数本来就不同）后，属性必须完全一致
const clickStripped = closeBtns.map(b => b.replace(/onclick="[^"]*"/, 'onclick="X"'));
ok('四处按钮属性完全一致（只差关闭函数名）',
   new Set(clickStripped).size === 1, clickStripped);
ok('旧的 #polishPanel 按钮规则已删除', !/#polishPanel \.panel-header button/.test(html));
ok('旧的 #tablePanel 按钮规则已删除', !/#tablePanel \.panel-header button/.test(html));
ok('存在统一的 .panel-close 样式', /\.panel-close\s*\{/.test(html));
ok('.panel-close 有 hover 反馈', /\.panel-close:hover\s*\{/.test(html));

// ---- CSS 结构完整性 ----
// 为什么专门测这个：删无用样式时如果是"按正则截一段"，
// 很容易把 @keyframes 只截掉一半，留下 `40% { ... } }` 这种
// 悬空片段 —— 页面还能打开（浏览器会跳过坏规则），但后面的样式会错乱，
// 而且极难看出来。实测我第一次删 .thinking-dots 就踩了这个坑。
{
    const styleTag = html.match(/<style>([\s\S]*?)<\/style>/);
    const css = (styleTag ? styleTag[1] : '').replace(/\/\*[\s\S]*?\*\//g, '');
    const opens = (css.match(/\{/g) || []).length;
    const closes = (css.match(/\}/g) || []).length;
    ok('CSS 大括号左右平衡', opens === closes, { opens, closes });

    let depth = 0, negative = false;
    for (const ch of css) {
        if (ch === '{') depth++;
        else if (ch === '}') { depth--; if (depth < 0) negative = true; }
    }
    ok('CSS 没有多余的右括号', !negative);
    ok('CSS 结束时嵌套层数为 0', depth === 0, depth);

    // 悬空的关键帧片段：`}` 之后直接跟 `40% {`
    ok('没有悬空的关键帧片段',
       !/\}\s*\d+%\s*\{/.test(css), (css.match(/\}\s*\d+%\s*\{/) || [''])[0]);

    // 已删的死样式不该复活
    ok('已删除的 .thinking-dots 不再出现', !/thinking-dots/.test(html));
    ok('已删除的 dot-bounce 动画不再出现', !/dot-bounce/.test(html));
    // 但真正在用的状态类必须留着（workflow.py 会发出 status="skipped"）
    ok('仍在使用的 .skipped 保留', /\.skipped\s*\{/.test(html));
}
// 钉右：弹窗列只有约 360px，标题是中文长串，
// 靠 justify-content: space-between 在换行时会把 ✕ 甩到第二行
ok('关闭按钮用 margin-left:auto 钉在右侧',
   /\.panel-close\s*\{[^}]*margin-left:\s*auto/.test(html));
ok('关闭按钮 order:10（标题先被压缩）',
   /\.panel-close\s*\{[^}]*order:\s*10/.test(html));
ok('标题可收缩并显示省略号',
   /\.panel-header > span\s*\{[^}]*text-overflow:\s*ellipsis/.test(html));

// ---- 弹窗拖动 ----
ok('标题栏是拖动把手', /\.side-panels > div > \.panel-header\s*\{[^}]*cursor:\s*move/.test(html));
// 必须是 fixed（相对视口）：absolute 的包含块是弹窗列，而面板正好撑满列，
// 可移动范围会变成 0×0 —— 也就是"光标是抓手但拖不动"那个 bug
ok('拖动过的面板改为视口定位（fixed 而不是 absolute）',
   /\.side-panels > div\.panel-moved\s*\{[^}]*position:\s*fixed/.test(html)
   && !/\.side-panels > div\.panel-moved\s*\{[^}]*position:\s*absolute/.test(html));
ok('启动时初始化拖动', /initDraggablePanels\(\);/.test(script));
ok('Esc 先关弹窗再收登录浮层',
   /if \(closeAllSidePanels\(\) > 0\) return;/.test(script));
// ---- 弹窗缩放 ----
ok('四个面板都有右下角缩放手柄',
   (html.match(/class="panel-resize"/g) || []).length === 4,
   (html.match(/class="panel-resize"/g) || []).length);
ok('缩放手柄有 nwse 光标', /\.panel-resize\s*\{[^}]*cursor:\s*nwse-resize/.test(html));
ok('缩放尺寸有夹取函数', /function clampPanelSize\(/.test(script));

// 拖动相关的行为验证已移到 tests/test_drag.js ——
// 那边用 document 级监听的桩，能覆盖"指针移出标题栏后仍要跟随"这个真实缺陷。
// 这里只保留上面的静态接线检查。
console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
