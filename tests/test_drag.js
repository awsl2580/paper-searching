// 回归测试：面板标题栏拖动。
//
// 这条用例为三个**真实故障**而写：
//
//   A) 抓取光标出现了但面板一动不动 —— 因为边界是按"弹窗列"算的：
//        maxLeft = 容器宽 - 面板宽
//      而面板是 flex:1 子项，展开时被拉伸到和容器一样大 → 上限为 0 →
//      left/top 永远被 clamp 在 0。指针在动、代码在跑，面板纹丝不动。
//      （旧用例用 380×500 的面板放在 1200×900 的容器里，所以一直是绿的。）
//      现在边界按**视口**算，下面第 2 段专门造一个"面板和容器一样大"的场景来卡这个回归。
//   B) move/up 只监听标题栏 → 指针一移出那 44px 就收不到事件。
//     现在挂 document，并且下面把指针移到标题栏外面验证。
//   C) 依赖 initDraggablePanels() 那一次绑定 → 那次没跑到就永远拖不动，
//      且没有任何报错。现在用 document 级事件委托，下面**故意不初始化**面板。
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
        children: [], style: {}, dataset: {}, _cls: new Set(), _listeners: {},
        _rect: { left: x, top: y, width: w, height: h },
        offsetWidth: w, offsetHeight: h, offsetParent: {},
        classList: {
            add: (c) => el._cls.add(c),
            remove: (c) => el._cls.delete(c),
            contains: (c) => el._cls.has(c),
            toggle: (c, on) => (on ? el._cls.add(c) : el._cls.delete(c)),
        },
        get className() { return Array.from(el._cls).join(' '); },
        set className(v) { el._cls = new Set(String(v).split(/\s+/).filter(Boolean)); },
        appendChild(c) { el.children.push(c); c.parentElement = el; return c; },
        addEventListener(t, fn) { (el._listeners[t] = el._listeners[t] || []).push(fn); },
        removeEventListener(t, fn) {
            el._listeners[t] = (el._listeners[t] || []).filter((f) => f !== fn);
        },
        fire(t, ev) { (el._listeners[t] || []).slice().forEach((f) => f(ev)); },
        setPointerCapture() { throw new Error('本实现不应依赖 setPointerCapture'); },
        releasePointerCapture() {},
        closest: () => null,
        querySelectorAll: () => [],
        getBoundingClientRect: () => ({
            left: el._rect.left, top: el._rect.top,
            width: el._rect.width, height: el._rect.height,
        }),
    };
    return el;
}

const docListeners = {};
const documentStub = {
    documentElement: { clientWidth: 1200, clientHeight: 900 },
    addEventListener(t, fn) { (docListeners[t] = docListeners[t] || []).push(fn); },
    removeEventListener(t, fn) {
        docListeners[t] = (docListeners[t] || []).filter((f) => f !== fn);
    },
    body: { classList: { add() {}, remove() {} } },
    getElementById: () => null,
    querySelectorAll: () => [],
};
const windowStub = {
    PointerEvent: function () {},
    innerWidth: 1200, innerHeight: 900,
    addEventListener(t, fn) { (docListeners['win:' + t] = docListeners['win:' + t] || []).push(fn); },
};
function fireDoc(t, ev) { (docListeners[t] || []).slice().forEach((f) => f(ev)); }

// 弹窗列（1200×900）+ 一个"被拉伸到和容器一样大"的面板（这正是线上形态）
const parent = mkEl(0, 0, 1200, 900);
const panel = mkEl(0, 0, 1200, 900);
const header = mkEl(0, 0, 1200, 44);
header.className = 'panel-header';
panel.className = 'open';
panel.appendChild(header);
parent.appendChild(panel);

// 第二个面板（验"相邻面板被钉住"）
const other = mkEl(0, 0, 1200, 300);
const otherHeader = mkEl(0, 0, 1200, 40);
otherHeader.className = 'panel-header';
other.className = 'open';
other.appendChild(otherHeader);
parent.appendChild(other);

const dragStateDecl = (script.match(/const panelDrag = \{[^}]*\};/) || [''])[0];
const sandbox = new Function(
    'document', 'window', 'PANEL_MARGIN',
    dragStateDecl + '\n' +
    grabFn('panelHome') + '\n' +
    grabFn('pinPanel') + '\n' +
    grabFn('pointerPoint') + '\n' +
    grabFn('viewportSize') + '\n' +
    grabFn('panelHeaderHeight') + '\n' +
    grabFn('panelDragBounds') + '\n' +
    grabFn('clampPanel') + '\n' +
    grabFn('panelDragMove') + '\n' +
    grabFn('panelDragEnd') + '\n' +
    grabFn('panelDragStart') + '\n' +
    grabFn('installPanelDragging') + '\n' +
    grabFn('makePanelDraggable') + '\n' +
    'return {panelDrag, installPanelDragging, makePanelDraggable, panelDragStart, panelDragBounds};'
)(documentStub, windowStub, 8);

// ── 1. 只装委托（故意不初始化每个面板）也要能拖 ──────────────────
sandbox.installPanelDragging();
ok('装上 document 级 pointerdown 委托', (docListeners['pointerdown'] || []).length === 1,
   Object.keys(docListeners));

function press(target, x, y, button) {
    fireDoc('pointerdown', {
        target, button: button === undefined ? 0 : button,
        clientX: x, clientY: y, pointerId: 1,
        cancelable: true, preventDefault() {},
    });
}
const headerTarget = { closest: (sel) => (sel === '.panel-header' ? header : null) };

press(headerTarget, 600, 130);
ok('按下后进入拖动状态', panel.classList.contains('dragging'));
ok('按下后把面板钉成内联绝对定位（压得住 id 规则）',
   panel.style.position === 'absolute', panel.style.position);
ok('按下后写入初始坐标与尺寸',
   panel.style.left === '0px' && panel.style.top === '0px'
   && panel.style.width === '1200px',
   [panel.style.left, panel.style.top, panel.style.width]);
ok('按下后把 move/up 挂到 document',
   (docListeners['pointermove'] || []).length === 1
   && (docListeners['pointerup'] || []).length === 1,
   Object.keys(docListeners));

// ── 2. 核心回归 A：面板和容器/视口一样大时，也必须能移动 ──────────
// 旧实现 maxLeft = 1200-1200 = 0、maxTop = 900-900 = 0 → 永远不动。
const bounds = sandbox.panelDragBounds(panel);
ok('可移动范围不再是 0（这是"完全拖不动"的根因）',
   bounds.maxLeft - bounds.minLeft > 200 && bounds.maxTop - bounds.minTop > 200, bounds);
ok('纵向范围至少留出标题栏高度（保证能抓回来）',
   bounds.minTop <= -44 && bounds.maxTop >= 44, bounds);

// ── 3. 核心回归 B：指针移出标题栏后仍然要动 ─────────────────────
fireDoc('pointermove', { clientX: 300, clientY: 60, pointerId: 1, cancelable: true, preventDefault() {} });
ok('指针移出标题栏后仍能拖动（原 bug 就在这）',
   panel.style.left === '-300px' && panel.style.top === '-70px',
   [panel.style.left, panel.style.top]);

fireDoc('pointermove', { clientX: 400, clientY: 200, pointerId: 1, cancelable: true, preventDefault() {} });
ok('继续移动仍跟随（相对按下点 600,130 移动 -200,+70）',
   panel.style.left === '-200px' && panel.style.top === '70px',
   [panel.style.left, panel.style.top]);

// 面板比视口宽时也不能把标题栏拖出屏幕（clamp 下限兜底）
fireDoc('pointermove', { clientX: 5000, clientY: 5000, pointerId: 1, cancelable: true, preventDefault() {} });
ok('拖到屏幕外时被夹住（左上角仍可见）',
   parseFloat(panel.style.left) <= 1200 && parseFloat(panel.style.top) <= 900,
   [panel.style.left, panel.style.top]);

// ── 4. 松手后的清理 ───────────────────────────────────────────
const kept = panel.style.left;
fireDoc('pointerup', { pointerId: 1 });
ok('松手后清除 document 监听（不留悬挂监听）',
   (docListeners['pointermove'] || []).length === 0,
   (docListeners['pointermove'] || []).length);
ok('松手后退出拖动状态', !panel.classList.contains('dragging'));
ok('松手后位置保留', panel.style.left === kept, panel.style.left);
fireDoc('pointermove', { clientX: -500, clientY: -500, pointerId: 1, cancelable: true, preventDefault() {} });
ok('松手后移动不再影响位置', panel.style.left === kept, panel.style.left);
ok('实现不依赖 setPointerCapture', true);

// ── 5. 点关闭按钮不触发拖动 ────────────────────────────────────
press({ closest: (sel) => (sel === '.panel-close' ? {} : (sel === '.panel-header' ? header : null)) },
      600, 100);
ok('点标题栏里的关闭按钮不会开始拖动', !panel.classList.contains('dragging'));

// ── 6. 未展开的面板不响应拖动 ──────────────────────────────────
const closed = mkEl(0, 0, 380, 400);
const closedHeader = mkEl(0, 0, 380, 40);
closedHeader.className = 'panel-header';
closed.appendChild(closedHeader);
parent.appendChild(closed);
fireDoc('pointerdown', {
    target: { closest: (sel) => (sel === '.panel-header' ? closedHeader : null) },
    button: 0, clientX: 100, clientY: 100, cancelable: true, preventDefault() {},
});
ok('未展开（无 .open）的面板不会被拖动', !closed.classList.contains('dragging'));

// ── 7. 拖一个面板时，同列其它面板要被钉住（否则会突然上跳）───────
press(headerTarget, 600, 130);
ok('拖动时相邻面板被固化成绝对定位',
   other.classList.contains('panel-moved') && other.style.top === '0px',
   [other.style.top, other.className]);
fireDoc('pointerup', { pointerId: 1 });

// ── 8. 兼容旧入口：makePanelDraggable 仍然给标题栏加提示 ────────
const el2 = mkEl(0, 0, 300, 300);
const h2 = mkEl(0, 0, 300, 40);
h2.className = 'panel-header';
el2.appendChild(h2);
sandbox.makePanelDraggable(el2);
ok('makePanelDraggable 仍标记 dragBound 并写 title',
   h2.dataset.dragBound === '1' && /拖动/.test(h2.title || ''),
   [h2.dataset.dragBound, h2.title]);

// ── 9. 只认左键 ────────────────────────────────────────────────
press(headerTarget, 600, 130);
fireDoc('pointerup', { pointerId: 1 });
press(headerTarget, 100, 100, 2);
ok('右键不触发拖动', !panel.classList.contains('dragging'));

// ── 10. 触屏事件源：没有 PointerEvent 时走 touchstart ───────────
const docListeners2 = {};
const docStub2 = Object.assign({}, documentStub, {
    addEventListener(t, fn) { (docListeners2[t] = docListeners2[t] || []).push(fn); },
    removeEventListener(t, fn) {
        docListeners2[t] = (docListeners2[t] || []).filter((f) => f !== fn);
    },
});
const winStub2 = { PointerEvent: undefined, innerWidth: 1200, innerHeight: 900, addEventListener() {} };
new Function('document', 'window', 'PANEL_MARGIN',
    (script.match(/const panelDrag = \{[^}]*\};/) || [''])[0] + '\n' +
    grabFn('panelHome') + '\n' + grabFn('pinPanel') + '\n' + grabFn('pointerPoint') + '\n' +
    grabFn('viewportSize') + '\n' + grabFn('panelHeaderHeight') + '\n' +
    grabFn('panelDragBounds') + '\n' + grabFn('clampPanel') + '\n' +
    grabFn('panelDragMove') + '\n' + grabFn('panelDragEnd') + '\n' +
    grabFn('panelDragStart') + '\n' + grabFn('installPanelDragging') + '\n' +
    'installPanelDragging();'
)(docStub2, winStub2, 8);
ok('没有 PointerEvent 时回退到 touchstart/mousedown',
   (docListeners2['touchstart'] || []).length === 1
   && (docListeners2['mousedown'] || []).length === 1,
   Object.keys(docListeners2));

// ── 11. 五个面板都能打开（曾经漏过某个面板的 .open 规则，导致永远打不开）
['#favoritesPanel.open', '#wfPanel.open', '#tablePanel.open',
 '#termPanel.open', '#polishPanel.open'].forEach(function (sel) {
    ok('CSS 里有 ' + sel + ' 的显示规则',
       html.indexOf('.side-panels > ' + sel) >= 0);
});

// ── 12. 窄窗口下面板不能缩成"右上角一条"（缩成一条就抓不住、拖不动）
const css = html.match(/<style>([\s\S]*?)<\/style>/)[1];
ok('宽屏用"右侧留白列"布局',
   /\.side-panels \{[\s\S]{0,400}left: calc\(50% \+ min\(590px/.test(css));
ok('窄屏有右侧浮窗兜底（锚右 + 给足宽度）',
   /@media \(max-width: 1830px\) \{[\s\S]{0,260}\.side-panels \{[\s\S]{0,220}right: 14px;[\s\S]{0,160}width: min\(390px, calc\(100vw - 28px\)\)/.test(css));
ok('浮窗模式把顶边让到工具栏下面（不盖住「功能/设置」）',
   /@media \(max-width: 1830px\) \{[\s\S]{0,260}\.side-panels \{[\s\S]{0,220}top: 108px/.test(css));
ok('面板被挤成一条时自愈（清掉拖动留下的内联几何）',
   /function ensurePanelUsable\(id\)[\s\S]{0,1600}resetPanelGeometry\(id\)/.test(script)
   && /setTimeout\(function \(\) \{ ensurePanelUsable\(id\); \}, 180\)/.test(script));
ok('尺寸退化重开前也会先清几何（不然还是同一块坏框）',
   /function ensurePanelVisible\(id\)[\s\S]{0,600}resetPanelGeometry\(id\)[\s\S]{0,120}dockPanel\(el, 'window'\)/.test(script));
ok('内嵌面板不因为"宽高小"被搬回小窗',
   /function ensurePanelVisible\(id\)[\s\S]{0,500}closest\('#inlineHost'\)[\s\S]{0,40}return false;/.test(script));

// ── 13. 面板跑到对话区左上角（宿位/内联定位不对）时的兜底 ─────
ok('小窗打开前把面板归一化到弹窗列（必要时重新挂载）',
   /function normalizeWindowPanel\(id\)[\s\S]{0,600}el\.parentElement !== home[\s\S]{0,200}home\.appendChild\(el\)/.test(script));
ok('归一化在 applyFeatureMode 里、只对小窗生效',
   /function applyFeatureMode\(id, mode\)[\s\S]{0,300}mode !== 'inline'\) normalizeWindowPanel\(id\)/.test(script));
ok('跑出弹窗列左右很远也算"坏位"并自愈',
   /function ensurePanelUsable\(id\)[\s\S]{0,900}r\.right < hr\.left - 60 \|\| r\.left > hr\.right \+ 60/.test(script));
ok('量不到真实尺寸时不钉绝对定位（否则会飞到左上角）',
   /function applyPanelSize\(el, w, h, allowPin\)[\s\S]{0,2200}r\.width >= 50 && r\.height >= 50/.test(script));
ok('恢复尺寸时不钉绝对定位（避免开成横跨半屏的巨窗）',
   /applyPanelSize\(el, saved\.w, saved\.h, false\)/.test(script)
   && /allowPin === false && !moved/.test(script));
ok('右边缘超出屏幕也算坏位并自愈', /r\.right > viewportSize\(\)\.w \+ 20/.test(script));
ok('面板打开后分两次体检（180ms / 700ms）',
   /ensurePanelUsable\(id\); \}, 180\)/.test(script) && /ensurePanelUsable\(id\); \}, 700\)/.test(script));

// ── 14. 首次打开：旧的保存尺寸不能把面板开成"横跨半屏的巨窗" ──
(function () {
    const colRect = { left: 1200, top: 108, width: 390, height: 700 };
    const style = {};
    const cls = new Set();
    const el = {
        style,
        classList: {
            contains: (c) => cls.has(c),
            add: (c) => cls.add(c),
            remove: (c) => cls.delete(c),
        },
        parentElement: { getBoundingClientRect: () => colRect },
        getBoundingClientRect: () => ({ left: 1200, top: 108, width: 390, height: 700 }),
    };
    const fn = new Function('window',
        'const PANEL_MARGIN = 8; const PANEL_MIN_W = 260; const PANEL_MIN_H = 160;' +
        grabFn('panelHome') + grabFn('pinPanel') + grabFn('viewportSize') + grabFn('applyPanelSize') +
        'return applyPanelSize;')({ innerWidth: 1640, innerHeight: 900 });
    fn(el, 900, 930, false);
    ok('打开时不钉绝对定位（避免巨窗）',
       !style.left && !style.top && !cls.has('panel-moved'),
       { left: style.left, top: style.top });
    ok('宽度被夹到弹窗列宽', style.width === '390px', style.width);
    ok('高度被夹到弹窗列高', style.height === '700px', style.height);
})();

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
