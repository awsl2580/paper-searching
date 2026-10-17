// 回归：右侧弹窗（收藏/工作流/文献表/润色）必须能拖动。
//
// 用户报的现象："点收藏/润色弹出来的小对话框拖不动"。
//
// 真因不是事件没绑上（旧实现确实把 pointermove 挂到了 document），而是
// **可移动范围是 0**：旧实现用 position:absolute 相对 .side-panels 那一列定位，
// 而面板在列里是 flex:1 1 auto，宽高正好把列撑满 —— "列尺寸 − 面板尺寸" = 0×0。
// 于是光标变成抓手、也进了拖动状态，但面板纹丝不动。
//
// 所以这个测试里 DOM 桩必须按**真实几何**来搭：面板和弹窗列一样大。
// 旧版 test_drag.js 用的是 380x500 的面板配 1200x900 的容器，桩里能拖动，
// 真实页面却拖不动 —— 测试通过但 bug 还在。这里专门把这种情况钉住。
//
// 函数全部从 static/index.html 里现抠（grabFn），测的是线上代码而不是副本。
const fs = require('fs');
const path = require('path');

const HTML = path.join(__dirname, '..', 'static', 'index.html');
const html = fs.readFileSync(HTML, 'utf8');
const css = (html.match(/<style>([\s\S]*?)<\/style>/) || [, ''])[1];
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

let pass = 0, fail = 0;
function ok(name, cond, got) {
    if (cond) { pass++; console.log('PASS  ' + name); }
    else { fail++; console.log('FAIL  ' + name + '  -> got: ' + JSON.stringify(got)); }
}

const PANEL_EDGE = Number((script.match(/const PANEL_EDGE = (\d+);/) || [])[1]);
const PANEL_MIN_W = Number((script.match(/const PANEL_MIN_W = (\d+);/) || [])[1]);
const PANEL_MIN_H = Number((script.match(/const PANEL_MIN_H = (\d+);/) || [])[1]);
ok('PANEL_EDGE 常量还在', PANEL_EDGE === 8, PANEL_EDGE);
ok('最小尺寸常量还在（缩放下限）', PANEL_MIN_W > 0 && PANEL_MIN_H > 0, [PANEL_MIN_W, PANEL_MIN_H]);
ok('拖动状态变量还在（panelDragEl / panelDragFrom）',
   /let panelDragEl = null;/.test(script) && /let panelDragFrom = null;/.test(script));
ok('缩放状态变量还在（panelResizeEl / panelResizeFrom）',
   /let panelResizeEl = null;/.test(script) && /let panelResizeFrom = null;/.test(script));

// ── 最小 DOM 桩：只实现被测代码用到的部分 ──────────────────────
function simpleMatches(el, sel) {
    if (!sel) return false;
    return sel.split(',').some((one) => {
        one = one.trim();
        if (!one) return false;
        if (one[0] === '.') return el.classList.contains(one.slice(1));
        return el.tagName === one.toUpperCase();
    });
}

class El {
    constructor(tag, cls) {
        this.tagName = (tag || 'div').toUpperCase();
        this.children = [];
        this.parentElement = null;
        this.style = {};
        this._cls = new Set((cls || '').split(/\s+/).filter(Boolean));
        this._listeners = {};
        this._rect = { left: 0, top: 0, width: 0, height: 0 };
        this.classList = {
            add: (c) => { String(c).split(/\s+/).filter(Boolean).forEach((x) => this._cls.add(x)); },
            remove: (c) => { String(c).split(/\s+/).filter(Boolean).forEach((x) => this._cls.delete(x)); },
            contains: (c) => this._cls.has(c),
        };
    }
    // 和浏览器一致：内联 width/height 优先于初始矩形
    // （缩放逻辑会先写 style.width 再读 offsetWidth 来夹位置，桩必须跟上）
    get offsetWidth() {
        const w = parseFloat(this.style.width);
        return isNaN(w) ? this._rect.width : w;
    }
    get offsetHeight() {
        const h = parseFloat(this.style.height);
        return isNaN(h) ? this._rect.height : h;
    }
    get className() { return Array.from(this._cls).join(' '); }
    set className(v) { this._cls = new Set(String(v).split(/\s+/).filter(Boolean)); }
    appendChild(c) { this.children.push(c); c.parentElement = this; return c; }
    addEventListener(t, fn) { (this._listeners[t] = this._listeners[t] || []).push(fn); }
    removeEventListener(t, fn) {
        this._listeners[t] = (this._listeners[t] || []).filter((f) => f !== fn);
    }
    fire(t, ev) {
        // 真实浏览器里事件会从 target 冒泡到 document，委托监听才收得到；
        // 桩里也要冒泡，否则测不出"委托在 document 上"这件事
        const path = [];
        let node = this;
        while (node) { path.push(node); node = node.parentElement; }
        path.forEach((el) => (el._listeners[t] || []).slice().forEach((f) => f(ev)));
        const doc = path[path.length - 1]._doc;
        if (doc) (doc._listeners[t] || []).slice().forEach((f) => f(ev));
        return ev;
    }
    closest(sel) {
        let node = this;
        while (node) {
            if (simpleMatches(node, sel)) return node;
            node = node.parentElement;
        }
        return null;
    }
    contains(other) {
        let node = other;
        while (node) { if (node === this) return true; node = node.parentElement; }
        return false;
    }
    querySelectorAll(sel) {
        if (sel !== '.panel-moved') return [];
        return this.children.filter((c) => c.classList.contains('panel-moved'));
    }
    getBoundingClientRect() {
        return {
            left: this._rect.left, top: this._rect.top,
            width: this._rect.width, height: this._rect.height,
            x: this._rect.left, y: this._rect.top,
        };
    }
    setRect(left, top, width, height) {
        this._rect = { left, top, width, height };
    }
    // 实现不应该依赖 pointer capture（capture 没生效时指针一离开标题栏就收不到事件）
    setPointerCapture() { throw new Error('本实现不应依赖 setPointerCapture'); }
    releasePointerCapture() { throw new Error('本实现不应依赖 setPointerCapture'); }
}

function buildWorld(opts) {
    const vw = opts.vw, vh = opts.vh;
    const colW = opts.colW, colH = opts.colH;

    const container = new El('div', 'side-panels');
    container.id = 'sidePanels';
    container.setRect(opts.colX, opts.colY, colW, colH);

    const panel = new El('div', 'open');
    panel.id = opts.id;
    panel.setRect(opts.colX, opts.colY, colW, colH);   // ← 关键：面板撑满弹窗列
    container.appendChild(panel);

    const header = new El('div', 'panel-header');
    header.setRect(opts.colX, opts.colY, colW, 45);
    panel.appendChild(header);

    const title = new El('span');
    header.appendChild(title);

    const closeBtn = new El('button', 'panel-close');
    header.appendChild(closeBtn);

    const panelBody = new El('div', 'panel-scroll');
    panel.appendChild(panelBody);

    // 右下角缩放手柄
    const resizeHandle = new El('div', 'panel-resize');
    resizeHandle.setRect(opts.colX + colW - 20, opts.colY + colH - 20, 20, 20);
    panel.appendChild(resizeHandle);

    const docListeners = {};
    const documentStub = {
        addEventListener(t, fn) { (docListeners[t] = docListeners[t] || []).push(fn); },
        removeEventListener(t, fn) {
            docListeners[t] = (docListeners[t] || []).filter((f) => f !== fn);
        },
        body: new El('body'),
        getElementById(id) {
            if (id === 'sidePanels') return container;
            return id === opts.id ? panel : null;
        },
    };
    const fireDoc = (t, ev) => (docListeners[t] || []).slice().forEach((f) => f(ev));
    const countDoc = (t) => (docListeners[t] || []).length;
    documentStub._listeners = docListeners;
    container._doc = documentStub;      // 让子元素 fire() 时能冒泡到 document

    const windowStub = {
        innerWidth: vw, innerHeight: vh, _resize: [],
        addEventListener(t, fn) { if (t === 'resize') this._resize.push(fn); },
    };

    const body = [
        'const PANEL_EDGE = ' + PANEL_EDGE + ';',
        'const PANEL_MIN_W = ' + PANEL_MIN_W + ';',
        'const PANEL_MIN_H = ' + PANEL_MIN_H + ';',
        'let panelDragEl = null;',
        'let panelDragFrom = null;',
        'let panelResizeEl = null;',
        'let panelResizeFrom = null;',
        grabFn('panelHeader'),
        grabFn('clampPanelPos'),
        grabFn('clampPanelSize'),
        grabFn('applyPanelPos'),
        grabFn('onPanelResizeMove'),
        grabFn('endPanelResize'),
        grabFn('startPanelResize'),
        grabFn('detachPanel'),
        grabFn('resetPanelPos'),
        grabFn('onPanelPointerMove'),
        grabFn('endPanelDrag'),
        grabFn('initDraggablePanels'),
        grabFn('allSidePanelIds'),
        grabFn('isSidePanelOpen'),
        grabFn('openSidePanel'),
        grabFn('closeSidePanel'),
        'return { clampPanelPos, clampPanelSize, panelHeader, resetPanelPos, initDraggablePanels,',
        '         closeSidePanel, openSidePanel, isSidePanelOpen,',
        '         getDragEl: () => panelDragEl, getResizeEl: () => panelResizeEl };',
    ].join('\n');

    const api = new Function('document', 'window', body)(documentStub, windowStub);
    return { vw, vh, colW, colH, container, panel, header, closeBtn, resizeHandle,
             bodyEl: panelBody, api, documentStub, windowStub, fireDoc, countDoc };
}

function pointerEv(over) {
    return Object.assign({
        button: 0, buttons: 1, pointerType: 'mouse', pointerId: 1,
        cancelable: true, preventDefault() {}, target: null,
    }, over);
}

// ══════════════════════════════════════════════════════════════
//  ① 真实几何（1440x900，面板 = 380x832 撑满右列）：必须能移动
// ══════════════════════════════════════════════════════════════
const W = buildWorld({ vw: 1440, vh: 900, colX: 1052, colY: 47, colW: 380, colH: 832, id: 'favoritesPanel' });
W.api.initDraggablePanels();

// 拖动前先算"可移动范围"：旧实现下这里就是 0
const oldMaxLeft = Math.max(0, W.colW - W.colW);      // 列宽 − 面板宽 = 0
const oldMaxTop = Math.max(0, W.colH - W.colH);       // 列高 − 面板高 = 0
const leftMin = W.api.clampPanelPos(W.vw, W.vh, W.colW, W.colH, 45, -99999, -99999).left;
const leftMax = W.api.clampPanelPos(W.vw, W.vh, W.colW, W.colH, 45, 99999, 99999).left;
ok('回归：面板撑满弹窗列时，横向仍可移动（旧实现是 0）',
   oldMaxLeft === 0 && leftMax - leftMin > 900, { oldRange: oldMaxLeft, newRange: leftMax - leftMin });
ok('回归：纵向也留了余量（能把弹窗挪出列高）',
   oldMaxTop === 0 && (W.api.clampPanelPos(W.vw, W.vh, W.colW, W.colH, 45, 0, 99999).top > 47),
   W.api.clampPanelPos(W.vw, W.vh, W.colW, W.colH, 45, 0, 99999).top);

// 标题栏按下
W.header.fire('pointerdown', pointerEv({ target: W.header, clientX: 1200, clientY: 70 }));
ok('标题栏按下后进入拖动状态', W.panel.classList.contains('dragging'));
ok('标题栏按下后脱离弹窗列（panel-moved）', W.panel.classList.contains('panel-moved'));
ok('按下后把 move/up 挂到 document',
   W.countDoc('pointermove') === 1 && W.countDoc('pointerup') === 1,
   { move: W.countDoc('pointermove'), up: W.countDoc('pointerup') });
ok('按下后固定了宽高（fixed 后 flex 拉伸会失效）',
   W.panel.style.width === '380px' && W.panel.style.height === '832px',
   [W.panel.style.width, W.panel.style.height]);

// 指针移到标题栏外面（左侧 400px 外）—— 旧实现只监听标题栏时会在这里丢事件
W.fireDoc('pointermove', pointerEv({ clientX: 800, clientY: 300 }));
ok('指针移出标题栏后仍然跟随（-400, +230 → 夹到视口内的位置）',
   W.panel.style.left === '652px' && W.panel.style.top === '60px',
   [W.panel.style.left, W.panel.style.top]);

W.fireDoc('pointermove', pointerEv({ clientX: 260, clientY: 780 }));
ok('继续移动按视口夹取（纵向到底 8px 边距、标题栏仍可见）',
   W.panel.style.left === '112px' && W.panel.style.top === '60px',
   [W.panel.style.left, W.panel.style.top]);

// 拖到视口外（左右上下都超）
W.fireDoc('pointermove', pointerEv({ clientX: 99999, clientY: 99999 }));
const clampedRight = parseFloat(W.panel.style.left);
ok('拖到屏幕外会被夹回视口内', clampedRight === W.vw - W.colW - PANEL_EDGE,
   { left: W.panel.style.left, expect: W.vw - W.colW - PANEL_EDGE });

W.fireDoc('pointerup', pointerEv({ clientX: 99999, clientY: 99999, buttons: 0 }));
ok('松手后清掉 document 监听（不留悬挂监听）',
   W.countDoc('pointermove') === 0 && W.countDoc('pointerup') === 0,
   { move: W.countDoc('pointermove'), up: W.countDoc('pointerup') });
ok('松手后退出拖动状态', !W.panel.classList.contains('dragging'));
ok('松手后位置保留', W.panel.style.left === clampedRight + 'px', W.panel.style.left);
ok('松手后拖动状态变量已清空', W.api.getDragEl() === null);

// 松手后移动不该再改变位置
W.fireDoc('pointermove', pointerEv({ clientX: 10, clientY: 10 }));
ok('松手后移动不再影响位置', W.panel.style.left === clampedRight + 'px', W.panel.style.left);

// 鼠标在窗口外松开：pointerup 收不到，靠 buttons === 0 兜底
W.header.fire('pointerdown', pointerEv({ target: W.header, clientX: 1200, clientY: 70 }));
W.fireDoc('pointermove', pointerEv({ clientX: 900, clientY: 200, buttons: 0 }));
ok('鼠标在窗口外松开时能自动收尾', W.api.getDragEl() === null && W.countDoc('pointermove') === 0);

// 双击标题栏复位
W.header.fire('pointerdown', pointerEv({ target: W.header, clientX: 1200, clientY: 70 }));
W.fireDoc('pointerup', pointerEv({ clientX: 1200, clientY: 70, buttons: 0 }));
W.header.fire('dblclick', { target: W.header });
ok('双击标题栏复位（取消 panel-moved 与内联坐标）',
   !W.panel.classList.contains('panel-moved') && !W.panel.style.left && !W.panel.style.width,
   { cls: W.panel.className, style: W.panel.style });

// 点 ✕ 不能触发拖动
W.closeBtn.fire('pointerdown', pointerEv({ target: W.closeBtn, clientX: 1420, clientY: 70 }));
ok('点关闭按钮不会误拖', W.api.getDragEl() === null && !W.panel.classList.contains('panel-moved'));
W.closeBtn.fire('dblclick', { target: W.closeBtn });
ok('双击关闭按钮不会误复位拖动状态', W.api.getDragEl() === null);

// 面板内容区（非标题栏）不能拖：否则选文字/滚轮/点按钮都会变成拖动
W.bodyEl.fire('pointerdown', pointerEv({ target: W.bodyEl, clientX: 1200, clientY: 400 }));
ok('内容区按下不触发拖动（能正常选字/滚动）', W.api.getDragEl() === null);

// 拖过之后关掉面板：位置要**保留**（用户特意挪开的，重开时不该跳回去）
W.header.fire('pointerdown', pointerEv({ target: W.header, clientX: 1200, clientY: 70 }));
W.fireDoc('pointermove', pointerEv({ clientX: 500, clientY: 200 }));
W.fireDoc('pointerup', pointerEv({ clientX: 500, clientY: 200, buttons: 0 }));
ok('拖过之后确实被记住了位置', W.panel.classList.contains('panel-moved'));
const keptLeft = W.panel.style.left, keptTop = W.panel.style.top;
W.api.closeSidePanel('favoritesPanel');
ok('关闭面板保留位置（重开还在原处）',
   !W.panel.classList.contains('open')
   && W.panel.classList.contains('panel-moved')
   && W.panel.style.left === keptLeft && W.panel.style.top === keptTop,
   { cls: W.panel.className, style: W.panel.style });

// 拖动中按 Esc 关掉：不能留下还在改坐标的监听
W.api.openSidePanel('favoritesPanel');
W.header.fire('pointerdown', pointerEv({ target: W.header, clientX: 1200, clientY: 70 }));
W.api.closeSidePanel('favoritesPanel');
ok('拖动中关掉面板会顺手结束拖动（不留悬挂监听）',
   W.api.getDragEl() === null && W.countDoc('pointermove') === 0);

// ══════════════════════════════════════════════════════════════
//  ①b 缩放：拖右下角可以自由改大小
// ══════════════════════════════════════════════════════════════
W.api.openSidePanel('favoritesPanel');
W.header.fire('dblclick', { target: W.header });          // 复位，回到 380x832

// 从右下角手柄按下 → 必须先脱离弹窗列（否则固定尺寸和 flex 打架）
W.resizeHandle.fire('pointerdown', pointerEv({ target: W.resizeHandle, clientX: 1420, clientY: 860 }));
ok('按住右下角手柄进入缩放', W.panel.classList.contains('resizing') && W.api.getResizeEl() === W.panel);
ok('缩放前先把面板从弹窗列里拿出来', W.panel.classList.contains('panel-moved'));
ok('缩放开始挂了 document 监听',
   W.countDoc('pointermove') === 1 && W.countDoc('pointerup') === 1,
   { move: W.countDoc('pointermove'), up: W.countDoc('pointerup') });

// 往右下拖 (+200, +100)：宽度变大，高度被视口上限夹住（面板 832 高，只剩 884 的上限）
W.fireDoc('pointermove', pointerEv({ clientX: 1620, clientY: 960 }));
ok('拖右下角把面板变大了（高度按视口上限夹）',
   parseInt(W.panel.style.width, 10) === 580 && parseInt(W.panel.style.height, 10) === W.vh - 16,
   [W.panel.style.width, W.panel.style.height]);
ok('变大后仍在视口内（右下角贴边时改成向左/向上长）',
   parseFloat(W.panel.style.left) + parseInt(W.panel.style.width, 10) <= W.vw - PANEL_EDGE + 1
   && parseFloat(W.panel.style.top) + parseInt(W.panel.style.height, 10) <= W.vh - PANEL_EDGE + 1,
   { left: W.panel.style.left, top: W.panel.style.top, w: W.panel.style.width, h: W.panel.style.height });

// 缩到比最小尺寸还小：夹在最小值
W.fireDoc('pointermove', pointerEv({ clientX: -9999, clientY: -9999 }));
ok('缩小有下限（不会缩成一条缝）',
   parseInt(W.panel.style.width, 10) === PANEL_MIN_W
   && parseInt(W.panel.style.height, 10) === PANEL_MIN_H,
   [W.panel.style.width, W.panel.style.height]);

W.fireDoc('pointerup', pointerEv({ clientX: -9999, clientY: -9999, buttons: 0 }));
ok('松手后退出缩放并清监听',
   !W.panel.classList.contains('resizing') && W.api.getResizeEl() === null
   && W.countDoc('pointermove') === 0);

// 缩放后的尺寸要保留；双击标题栏才复位
const keptW = W.panel.style.width, keptH = W.panel.style.height;
W.api.closeSidePanel('favoritesPanel');
W.api.openSidePanel('favoritesPanel');
ok('关闭再打开保留大小', W.panel.style.width === keptW && W.panel.style.height === keptH,
   [W.panel.style.width, W.panel.style.height]);
W.header.fire('dblclick', { target: W.header });
ok('双击标题栏把大小也复原', !W.panel.style.width && !W.panel.style.height,
   [W.panel.style.width, W.panel.style.height]);

// 内容区不能误触发缩放；缩放手柄本身不能触发拖标题栏那套逻辑
W.bodyEl.fire('pointerdown', pointerEv({ target: W.bodyEl, clientX: 1200, clientY: 500 }));
ok('内容区按下不会开始缩放', W.api.getResizeEl() === null && W.api.getDragEl() === null);

// 缩放中关掉面板：也要收尾
W.api.openSidePanel('favoritesPanel');
W.resizeHandle.fire('pointerdown', pointerEv({ target: W.resizeHandle, clientX: 1420, clientY: 860 }));
W.api.closeSidePanel('favoritesPanel');
ok('缩放中关掉面板会顺手结束缩放', W.api.getResizeEl() === null && W.countDoc('pointermove') === 0);

// 鼠标在窗口外松开（buttons === 0）→ 自动收尾
W.api.openSidePanel('favoritesPanel');
W.resizeHandle.fire('pointerdown', pointerEv({ target: W.resizeHandle, clientX: 1420, clientY: 860 }));
W.fireDoc('pointermove', pointerEv({ clientX: 1300, clientY: 800, buttons: 0 }));
ok('缩放在窗口外松手也能收尾', W.api.getResizeEl() === null && W.countDoc('pointermove') === 0);

// 纯函数：clampPanelSize
{
    const s1 = W.api.clampPanelSize(1440, 900, 10, 10);
    ok('尺寸夹取：过小取最小值', s1.w === PANEL_MIN_W && s1.h === PANEL_MIN_H, s1);
    const s2 = W.api.clampPanelSize(1440, 900, 99999, 99999);
    ok('尺寸夹取：过大取视口上限', s2.w === 1440 - 16 && s2.h === 900 - 16, s2);
    const s3 = W.api.clampPanelSize(390, 700, 9999, 9999);
    ok('尺寸夹取：手机尺寸也不出屏', s3.w === 390 - 16 && s3.h === 700 - 16, s3);
    const s4 = W.api.clampPanelSize(300, 260, 9999, 9999);
    ok('尺寸夹取：视口比最小尺寸还小时以视口为准', s4.w === 300 - 16 && s4.h === 260 - 16, s4);
}

// ══════════════════════════════════════════════════════════════
//  ② 不同视口/面板尺寸下的夹取（clampPanelPos 是纯函数）
// ══════════════════════════════════════════════════════════════
{
    const W2 = buildWorld({ vw: 1920, vh: 1080, colX: 1552, colY: 37, colW: 360, colH: 1006, id: 'polishPanel' });
    const moveRange = W2.api.clampPanelPos(1920, 1080, 360, 1006, 45, 99999, 0).left
                    - W2.api.clampPanelPos(1920, 1080, 360, 1006, 45, -99999, 0).left;
    ok('宽屏（1920x1080）也能横向拖开', moveRange > 1000, moveRange);
}
{
    const c = (vw, vh, w, h, keep, l, t) => buildWorld(
        { vw, vh, colX: 0, colY: 0, colW: w, colH: h, id: 'tablePanel' }
    ).api.clampPanelPos(vw, vh, w, h, keep, l, t);
    ok('面板比视口高时，标题栏不会被拖出屏幕',
       c(1000, 600, 360, 2000, 45, 100, 99999).top === 600 - 45 - PANEL_EDGE,
       c(1000, 600, 360, 2000, 45, 100, 99999).top);
    ok('面板比视口高时，往上拖也留在顶部边距内',
       c(1000, 600, 360, 2000, 45, 100, -99999).top === PANEL_EDGE,
       c(1000, 600, 360, 2000, 45, 100, -99999).top);
    ok('面板比视口宽时贴左边（不会左右都露一半）',
       c(800, 900, 1200, 400, 45, 300, 100).left === PANEL_EDGE,
       c(800, 900, 1200, 400, 45, 300, 100).left);
    ok('手机尺寸（390x700）也夹得住',
       c(390, 700, 374, 640, 45, 999, 999).left === 390 - 374 - PANEL_EDGE,
       c(390, 700, 374, 640, 45, 999, 999).left);
}

// ══════════════════════════════════════════════════════════════
//  ③ 样式与实现约定（CSS/HTML 侧）
// ══════════════════════════════════════════════════════════════
const movedRule = (css.match(/\.side-panels > div\.panel-moved\s*\{([\s\S]*?)\}/) || [, ''])[1];
ok('.panel-moved 用 position: fixed（相对视口，才能拖到页面任意位置）',
   /position:\s*fixed/.test(movedRule) && !/position:\s*absolute/.test(movedRule), movedRule.trim());
ok('.panel-moved 限制了最大宽高（窗口缩小时不出屏）',
   /max-height:\s*calc\(100vh/.test(movedRule) && /max-width:\s*calc\(100vw/.test(movedRule));
ok('四个面板的标题栏都挂了拖动光标',
   (css.match(/\.side-panels > div > \.panel-header \{[\s\S]*?cursor:\s*(move|grab)/) || []).length > 0);
ok('标题栏有可见的抓手提示（::before）',
   /\.panel-header::before\s*\{/.test(css));
ok('拖动时禁止选中文字（body.panel-dragging）',
   /body\.panel-dragging\s*\{[\s\S]*?user-select:\s*none/.test(css));
ok('四个面板都在弹窗列里、且都有 panel-header',
   ['favoritesPanel', 'wfPanel', 'tablePanel', 'polishPanel'].every((id) => {
       const block = (html.match(new RegExp('<div id="' + id + '"[\\s\\S]*?<div class="panel-header">')) || [])[0];
       return !!block;
   }));
ok('实现不依赖 setPointerCapture',
   !/\.setPointerCapture\(/.test(grabFn('onPanelPointerMove') + grabFn('initDraggablePanels')));
ok('拖动用的是 document 级事件委托（初始化晚一步也不会失效）',
   /document\.addEventListener\('pointerdown'/.test(grabFn('initDraggablePanels')));

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
