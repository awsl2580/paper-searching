// 右上角「功能」下拉 + 显示方式（单独小窗 / 显示在对话框）的测试。
//
// 当前产品行为（2026-10 第二轮调整）：
//   * 点功能**先弹询问框**：「单独小窗」还是「显示在对话框」，选完才打开；
//   * 询问框里有「不再询问」勾选：勾了就把选择记下来，下次直接按它打开；
//     想恢复询问去「设置 → 通用」点「恢复询问」（或调 setPanelAsk(true)）；
//   * 登录门槛仍然优先于询问框（未登录连框都不弹）；
//   * 内嵌面板**整面覆盖**对话区（不是上下分栏，也没有拖动横条）；
//   * 内嵌面板必须能被压进容器高度（否则长内容会被裁掉、滚轮失效）。
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const _scriptBlocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
const script = _scriptBlocks.reduce((a, b) => (b[1].length > a[1].length ? b : a))[1];
const css = html.match(/<style>([\s\S]*?)<\/style>/)[1];

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

// ── 1. 头部结构 ────────────────────────────────────────────────
ok('头部右侧是功能按钮组，不再有「📚 功能」下拉',
   /id="headerFeatures"/.test(html) && !/id="featureBtn"/.test(html)
   && !/id="featureMenu"/.test(html));
ok('功能按钮按 FEATURES 渲染（加功能只改一处）',
   /function renderHeaderFeatures\(\)[\s\S]{0,900}FEATURES\.map/.test(script));
ok('点头部按钮仍走 openFeatureFromMenu（登录门槛/显示方式都不变）',
   /class="hf-btn[\s\S]{0,500}openFeatureFromMenu\('/.test(script));
ok('按钮带"已打开"状态（is-open）',
   /\.hf-btn\.is-open/.test(css) && /is-open/.test(script));
ok('原来的四个按钮已合并（不再单独占位）',
   ['btnFavorites', 'btnWorkflow', 'btnTable', 'btnPolish']
     .every((id) => !new RegExp('id="' + id + '"').test(html)));
ok('对话框里有内嵌容器', /id="inlineHost"/.test(html));
ok('内嵌容器在对话区内部（不是塞进消息列表里）',
   html.indexOf('id="inlineHost"') > 0
   && html.indexOf('id="inlineHost"') < html.indexOf('class="messages" id="messages"'));
ok('内嵌是整面覆盖：没有拖动横条 / 不做上下分栏',
   !/id="inlineResize"/.test(html)
   && !/\.inline-resize/.test(css)
   && !/function initInlineResize/.test(script)
   && !/inlineHeight/.test(script));
ok('点历史记录会自动收起内嵌面板（覆盖整面也不挡对话）',
   /function focusChatArea\(\)[\s\S]{0,200}clearInlinePanels\(\)/.test(script)
   // 用"取出函数体"代替"从函数头往下数 N 个字符"：窗口式断言每次加几行注释
   // 就会误报（openConversation 里已经因为 detachActiveTurn 加过一次），
   // 而这里真正关心的只是 focusChatArea() 在 openConversation 里被调过。
   && /focusChatArea\(\)/.test(grabFn('openConversation')));
ok('有显示方式询问框（两个选项 + 不再询问）',
   /id="panelModeModal"/.test(html)
   && /data-mode="window"/.test(html) && /data-mode="inline"/.test(html)
   && /id="panelModeDontAsk"/.test(html)
   && /不再询问/.test(html));
ok('询问框有两个明确的按钮文案',
   /单独小窗/.test(html) && /显示在对话框/.test(html));
ok('有设置弹窗（通用 / 账号管理两个页签）',
   /id="settingsModal"/.test(html)
   && /data-tab="general"/.test(html) && /data-tab="account"/.test(html));

// 面板"打开却空白"的兜底
ok('打开后会检查面板是否真的可见', /function ensurePanelVisible\(id\)/.test(script));
ok('不可见就自动搬回小窗重开',
   /function ensurePanelVisible\(id\)[\s\S]{0,500}dockPanel\(el, 'window'\)/.test(script));
ok('打开的面板有最小高度（不会被压成 0）',
   /\.side-panels > #favoritesPanel\.open[\s\S]{0,300}min-height: 240px/.test(css));
ok('点功能先看询问开关',
   /function toggleFeaturePanel\(id\)[\s\S]{0,800}panelAskEnabled\(\)[\s\S]{0,200}openPanelModeModal\(id\)/.test(script));
ok('不再询问时直接按记住的方式打开',
   /function toggleFeaturePanel\(id\)[\s\S]{0,900}openFeaturePanel\(id, panelMode\(\)\)/.test(script));
ok('有 openFeaturePanel 这层"跳过询问"的入口',
   /function openFeaturePanel\(id, mode\)/.test(script));
ok('「重置面板」按钮已删除（按需求移掉）',
   !/function resetAllPanels\(/.test(script) && !/onclick="resetAllPanels\(\)"/.test(script));
ok('面板标题栏是 flex 且关闭按钮靠右（margin-left:auto 才生效）',
   /\.side-panels > div > \.panel-header,[\s\S]{0,120}\.inline-panels > div > \.panel-header \{[\s\S]{0,220}display: flex;/.test(css)
   && /justify-content: space-between/.test(css)
   && /\.panel-close \{[\s\S]{0,120}margin-left: auto/.test(css));
ok('下拉箭头用 SVG（字形基线会往下掉）',
   /class="caret-ico"/.test(html) && /<path d="M2\.6 4\.6 L6 8 L9\.4 4\.6">/.test(html)
   && !/&#8964;/.test(html));
ok('打开功能出错会提示（不再静默空白）',
   /function openFeatureFromMenu\(id\)[\s\S]{0,400}showToast/.test(script));

// ── 2. 功能注册表 ──────────────────────────────────────────────
ok('注册表里四个功能都在',
   ['favoritesPanel', 'tablePanel', 'polishPanel', 'wfPanel']
     .every((id) => script.indexOf("id: '" + id + "'") >= 0));
ok('名词解释是注册表里的第五个功能',
   /id: 'termPanel'[\s\S]{0,120}name: '名词解释'/.test(script));
ok('名词解释不设登录门槛（读论文时随手查一个词）',
   (function () {
       const i = script.indexOf("id: 'termPanel'");
       const j = script.indexOf("id: '", i + 15);
       return i > 0 && script.slice(i, j > 0 ? j : i + 400).indexOf('needLogin') < 0;
   })());
ok('名词解释面板有输入框与结果区',
   /id="termPanel"/.test(html) && /id="termInput"/.test(html)
   && /id="termResult"/.test(html) && /function explainTerm\(/.test(script));
ok('名词解释走和润色同一套接口读取（readApiResponse）',
   /async function explainTerm\([\s\S]{0,3000}readApiResponse\(resp\)/.test(script));
ok('头部五个按钮就是注册表里的五个功能',
   (function () {
       const m = script.match(/const FEATURES = \[([\s\S]*?)\n        \];/);
       if (!m) return false;
       return (m[1].match(/id: '/g) || []).length === 5
           && /id: 'termPanel'/.test(m[1]) && /id: 'wfPanel'/.test(m[1]);
   })());
ok('菜单里只有功能（设置不在下拉里）',
   !/<div class="feature-sep">/.test(script) && !/data-feature="__settings__"/.test(script));
ok('设置搬到左下角侧栏底部，不再占头部位置',
   /id="settingsBtn"[\s\S]{0,200}openSettings\(\)/.test(html)
   && html.indexOf('id="settingsBtn"') > html.indexOf('id="convList"')
   && html.indexOf('id="settingsBtn"') < html.indexOf('</aside>')
   && !/id="headerActions"[\s\S]{0,1200}id="settingsBtn"/.test(html));

// ── 2.5 新对话空白区：「猜你想搜」四个随机词（点一下直接发）────
ok('空白页有「猜你想搜」的容器', /id="guessRow"/.test(html));
ok('重渲染用的空白模板里也有这个容器',
   /const EMPTY_STATE_HTML = `[\s\S]{0,700}id="guessRow"/.test(script));
ok('词条由脚本随机抽 4 条（不写死在模板里）',
   /const GUESS_POOL = \[/.test(script)
   && /function guessPicks\(n\)/.test(script)
   && /guessPicks\(4\)/.test(script));
ok('词池里有"类器官、虚拟细胞"这类示例',
   /类器官、虚拟细胞/.test(script));
ok('初始化时就渲染好（头部按钮 + 猜你想搜）',
   /renderHeaderFeatures\(\);[\s\S]{0,240}renderGuessChips\(\);/.test(script));
ok('renderHistory 注入空白模板后补渲染',
   /messagesDiv\.innerHTML = EMPTY_STATE_HTML;[\s\S]{0,240}renderGuessChips\(\);/.test(script));
ok('点一下会把词填进输入框并直接发送',
   /function searchGuess\(btn\)[\s\S]{0,700}input\.value = q[\s\S]{0,400}sendMessage\(\)/.test(script));
ok('正在回答时不硬发（只填进输入框并提示）',
   /function searchGuess\(btn\)[\s\S]{0,700}if \(isProcessing\)[\s\S]{0,240}return;/.test(script));
ok('四个词条固定 2×2（两列等宽，不按文字长短折成 3+1）',
   /\.guess-row \{[\s\S]{0,240}display: grid;[\s\S]{0,160}grid-template-columns: repeat\(2, minmax\(0, 1fr\)\)/.test(css)
   && /\.guess-label \{[\s\S]{0,140}grid-column: 1 \/ -1/.test(css));
ok('猜你想搜的样式齐全（含深色）',
   /\.guess-chip \{/.test(css) && /html\[data-theme="dark"\] \.guess-chip \{/.test(css)
   && /\.guess-label \{/.test(css));
ok('空白页高度受控：图标/文字收紧 + 插画有高度上限（不撑出滚动条）',
   /\.messages \.empty \.icon \{[\s\S]{0,80}font-size: 36px/.test(css)
   && /\.welcome-art \{[\s\S]{0,500}max-height: 26vh/.test(css));
ok('空白页不再摆功能按钮（功能已搬到头部）',
   !/id="emptyFeatures"/.test(html) && !/\.empty-feature \{/.test(css));
ok('下拉菜单的旧结构已彻底移除',
   !/class="feature-wrap"/.test(html) && !/id="featureMenu"/.test(html));
ok('润色有五种风格（含"生动有画面感"）',
   /value="vivid"/.test(html) && /生动有画面感/.test(html)
   && /value="logic"/.test(html) && /强化逻辑/.test(html));
ok('润色结果会展示「改动说明」与「改写幅度」',
   /data\.notes/.test(script) && /polish-notes/.test(script)
   && /polish-ratio/.test(script) && /改写幅度/.test(script));
ok('几乎没改时会提示换风格（而不是假装改好了）',
   /polish-warn/.test(script) && /几乎没改/.test(script));
ok('润色带登录门槛', /id: 'polishPanel'[\s\S]{0,140}needLogin: '摘要润色'/.test(script));
ok('科研工作流带登录门槛', /id: 'wfPanel'[\s\S]{0,140}needLogin: '科研工作流'/.test(script));
ok('登录检查优先于询问框（没登录不弹选择框）',
   /function toggleFeaturePanel\(id\)[\s\S]{0,700}requireLogin\(f\.needLogin\)[\s\S]{0,200}panelAskEnabled/.test(script));
ok('openFeaturePanel 自己也会再查一次登录',
   /function openFeaturePanel\(id, mode\)[\s\S]{0,260}requireLogin\(f\.needLogin\)/.test(script));
ok('内嵌面板禁止拖动', /function panelDragStart\(ev, header\)[\s\S]{0,400}closest\("#inlineHost"\)/.test(script));
ok('内嵌面板不注入缩放抓手',
   /function ensurePanelResizeHandles\(el\)[\s\S]{0,300}closest\("#inlineHost"\)/.test(script));
ok('Esc 先关询问框、再关设置、再收功能菜单',
   /closePanelModeModal\(\); return;/.test(script)
   && /closeSettings\(\); return;/.test(script)
   && /closeFeatureMenu\(\); return;/.test(script));
ok('Esc 关询问框排在关设置之前',
   script.indexOf('closePanelModeModal(); return;') < script.indexOf('closeSettings(); return;'));

// ── 3. 样式：内嵌是整面覆盖 ───────────────────────────────────
ok('内嵌容器铺满整个对话区（absolute + inset:0）',
   /\.inline-host \{[\s\S]{0,120}position: absolute;/.test(css)
   && /\.inline-host \{[\s\S]{0,200}inset: 0;/.test(css));
ok('内嵌容器盖在对话区之上（z-index 抬起来）',
   /\.inline-host \{[\s\S]{0,260}z-index: 40;/.test(css));
ok('内嵌面板可以被压进容器（修"滚轮滚不动"）',
   /\.inline-panels > #wfPanel\.open\.inline-active[\s\S]{0,300}flex-shrink: 1;/.test(css)
   && /\.inline-panels > #wfPanel\.open\.inline-active[\s\S]{0,300}min-height: 0;/.test(css));
ok('内嵌面板内容区自己滚',
   /\.inline-host \.panel-scroll \{[\s\S]{0,160}overflow-y: auto;/.test(css));
ok('询问框样式在（两个选项卡片 + 不再询问行）',
   /\.mode-choice \{/.test(css) && /\.mode-remember \{/.test(css) && /\.mode-box \{/.test(css));

// ── 4. 行为：模式 / 询问状态存取 ───────────────────────────────
const store = {};
const localStorageStub = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
};

function mkEl(id) {
    const el = {
        id: id, children: [], dataset: {}, style: {}, _cls: new Set(),
        _style: null, checked: false, textContent: '',
        parentElement: null,
        classList: {
            add: (...c) => c.forEach((x) => el._cls.add(x)),
            remove: (...c) => c.forEach((x) => el._cls.delete(x)),
            contains: (c) => el._cls.has(c),
            toggle: (c, on) => (on ? el._cls.add(c) : el._cls.delete(c)),
        },
        get className() { return Array.from(el._cls).join(' '); },
        appendChild(c) {
            const old = c.parentElement;
            if (old && old.children) old.children = old.children.filter((x) => x !== c);
            el.children.push(c);
            c.parentElement = el;
            return c;
        },
        getAttribute(k) { return k === 'style' ? (el._style || null) : null; },
        setAttribute(k, v) { if (k === 'style') el._style = v; },
        removeAttribute(k) { if (k === 'style') el._style = null; },
        closest: () => null,
        focus() {},
        querySelectorAll: () => [],
        querySelector: () => null,
        getBoundingClientRect: () => ({ width: 300, height: 260, left: 0, top: 0, right: 300, bottom: 260 }),
    };
    Object.defineProperty(el.style, 'cssText', {
        get() { return el._style || ''; },
        set(v) {
            el._style = v;
            Object.keys(el.style).forEach((k) => { if (k !== 'cssText') delete el.style[k]; });
        },
    });
    return el;
}

const host = mkEl('inlineHost');
const panelsHost = mkEl('inlinePanels');
const tabsHost = mkEl('inlineTabs');
const home = mkEl('sidePanels');
const polish = mkEl('polishPanel');
const favorites = mkEl('favoritesPanel');
const wf = mkEl('wfPanel');
const table = mkEl('tablePanel');
const modal = mkEl('panelModeModal');
const modalTitle = mkEl('panelModeTitle');
const dontAsk = mkEl('panelModeDontAsk');
home.appendChild(polish);
home.appendChild(favorites);
home.appendChild(wf);
home.appendChild(table);

const els = {
    inlineHost: host, inlinePanels: panelsHost, inlineTabs: tabsHost,
    favoritesPanel: favorites, tablePanel: table,
    polishPanel: polish, wfPanel: wf,
    panelModeModal: modal, panelModeTitle: modalTitle, panelModeDontAsk: dontAsk,
};
const documentStub = {
    // 必须按 id 记忆：modal 被 add('show') 之后，下一次 getElementById 得拿到同一个节点
    _cache: {},
    getElementById(id) {
        if (els[id]) return els[id];
        if (!this._cache[id]) this._cache[id] = mkEl(id);
        return this._cache[id];
    },
    querySelector: (sel) => (sel === '.side-panels' ? home : null),
    querySelectorAll: () => [],
};

const sandbox = new Function('localStorage', 'document', 'escapeAttr', 'escapeHtml', 'setTimeout',
    "const FEATURES = [\n" +
    "  {id:'favoritesPanel', emoji:'⭐', name:'收藏', desc:'a'},\n" +
    "  {id:'tablePanel', emoji:'📊', name:'文献表', desc:'b'},\n" +
    "  {id:'polishPanel', emoji:'✍️', name:'润色', desc:'c', needLogin:'摘要润色'},\n" +
    "  {id:'wfPanel', emoji:'🤖', name:'科研工作流', desc:'d', needLogin:'科研工作流'},\n" +
    "];\n" +
    "const FEATURE_MODE_KEY = 'panelMode';\n" +
    "const FEATURE_ASK_KEY = 'panelModeAsk';\n" +
    "let inlineActiveId = null;\n" +
    "let pendingFeatureId = null;\n" +
    "let loginAsked = null;\n" +
    "let loginBlock = false;\n" +
    "function requireLogin(f){ loginAsked = f; return loginBlock; }\n" +
    "function isSidePanelOpen(id){ const e = document.getElementById(id); return !!(e && e.classList.contains('open')); }\n" +
    "function loadFavorites(){}\n" +
    "function ensurePanelResizeHandles(){}\n" +
    "function restorePanelGeometry(){}\n" +
    "function ensurePanelVisible(){}\n" +
    "function ensurePanelUsable(){}\n" +
    "function normalizeWindowPanel(){}\n" +
    "function syncSettingsRadios(){}\n" +
    "function applyInlineHeight(){}\n" +
    grabFn('featureById') + '\n' + grabFn('panelMode') + '\n' + grabFn('setPanelMode') + '\n' +
    grabFn('panelAskEnabled') + '\n' + grabFn('setPanelAsk') + '\n' +
    grabFn('openPanelModeModal') + '\n' + grabFn('closePanelModeModal') + '\n' +
    grabFn('choosePanelMode') + '\n' +
    grabFn('inlinePanelsHost') + '\n' + grabFn('renderInlineTabs') + '\n' +
    grabFn('activateInlineTab') + '\n' + grabFn('closeInlineTab') + '\n' +
    grabFn('openSidePanel') + '\n' + grabFn('closeSidePanel') + '\n' +
    grabFn('toggleFeaturePanel') + '\n' + grabFn('openFeaturePanel') + '\n' +
    grabFn('applyFeatureMode') + '\n' + grabFn('dockPanel') + '\n' +
    grabFn('setDefaultMode') + '\n' +
    'return {panelMode, setPanelMode, panelAskEnabled, setPanelAsk, dockPanel, toggleFeaturePanel,' +
    ' openFeaturePanel, applyFeatureMode, setDefaultMode, activateInlineTab, closeInlineTab,' +
    ' choosePanelMode, openPanelModeModal, closePanelModeModal,' +
    ' setLoginBlock: (v) => { loginBlock = v; },' +
    ' getTabs: () => document.getElementById("inlineTabs").innerHTML,' +
    ' getLoginAsked: () => loginAsked};'
)(localStorageStub, documentStub, (x) => x, (x) => x, (fn) => { try { fn(); } catch (e) {} });

ok('默认显示方式是单独小窗', sandbox.panelMode() === 'window', sandbox.panelMode());
sandbox.setPanelMode('inline');
ok('能记住内嵌模式', sandbox.panelMode() === 'inline', sandbox.panelMode());
sandbox.setPanelMode('window');
ok('能切回小窗模式', sandbox.panelMode() === 'window', sandbox.panelMode());
ok('默认会先询问显示方式', sandbox.panelAskEnabled() === true, sandbox.panelAskEnabled());

// ── 5. 点功能 → 先弹询问框 ────────────────────────────────────
sandbox.toggleFeaturePanel('favoritesPanel');
ok('点功能先弹出询问框', modal.classList.contains('show'));
ok('选之前面板不会打开', !favorites.classList.contains('open'));
ok('询问框标题点名了功能', /收藏/.test(modalTitle.textContent), modalTitle.textContent);
ok('点「显示在对话框」后面板进内嵌区',
   sandbox.choosePanelMode('inline') === true
   && favorites.classList.contains('open')
   && favorites.parentElement === panelsHost,
   favorites.parentElement && favorites.parentElement.id);
ok('询问框选完就关掉', !modal.classList.contains('show'));
ok('选择被记住（下次默认内嵌）', sandbox.panelMode() === 'inline', sandbox.panelMode());
ok('内嵌容器被显式显示', host.style.display === 'flex', host.style.display);
ok('标签栏列出当前打开的面板', /inline-tab/.test(sandbox.getTabs())
   && /收藏/.test(sandbox.getTabs()));
ok('新打开的面板是激活标签', favorites.classList.contains('inline-active'));

sandbox.toggleFeaturePanel('favoritesPanel');
ok('再点一次直接收起（不再问）', !favorites.classList.contains('open'));

sandbox.toggleFeaturePanel('favoritesPanel');
ok('没勾「不再询问」时下次还会问', modal.classList.contains('show'));
ok('选「单独小窗」后面板回到侧栏',
   sandbox.choosePanelMode('window') === true
   && favorites.parentElement === home && favorites.classList.contains('open'),
   favorites.parentElement && favorites.parentElement.id);
ok('内嵌容器空了会收起', host.style.display === 'none', host.style.display);

// ── 6. 不再询问：记住这次的选择 ───────────────────────────────
sandbox.toggleFeaturePanel('favoritesPanel');       // 收起
sandbox.toggleFeaturePanel('tablePanel');           // 打开询问框
ok('勾选前确实在等选择', modal.classList.contains('show'));
dontAsk.checked = true;                             // 用户在这个框里勾上
sandbox.choosePanelMode('inline');
ok('勾了「不再询问」后开关变成 off', sandbox.panelAskEnabled() === false);
ok('这次照样按选择打开', table.classList.contains('open') && table.parentElement === panelsHost);

sandbox.toggleFeaturePanel('tablePanel');           // 收起
sandbox.toggleFeaturePanel('favoritesPanel');
ok('不再询问时直接打开，不弹框',
   !modal.classList.contains('show') && favorites.classList.contains('open'));
ok('直接打开时用记住的方式（内嵌）', favorites.parentElement === panelsHost);

// 恢复询问：重新点功能又该弹框，且勾选是干净的
sandbox.toggleFeaturePanel('favoritesPanel');       // 收起
sandbox.setPanelAsk(true);
ok('setPanelAsk(true) 能恢复询问', sandbox.panelAskEnabled() === true);
sandbox.toggleFeaturePanel('favoritesPanel');
ok('恢复询问后又弹框', modal.classList.contains('show'));
ok('每次打开询问框都会重置勾选', dontAsk.checked === false, dontAsk.checked);
sandbox.closePanelModeModal();

// ── 7. 标签页切换 / 设置里搬家 ────────────────────────────────
sandbox.openFeaturePanel('favoritesPanel', 'inline'); // 绕过询问，直接开两个标签
sandbox.openFeaturePanel('tablePanel', 'inline');
ok('两个面板会变成两个标签', /收藏/.test(sandbox.getTabs())
   && /文献表/.test(sandbox.getTabs()), sandbox.getTabs());
sandbox.activateInlineTab('tablePanel');
ok('点标签能切到另一个面板',
   table.classList.contains('inline-active') && !favorites.classList.contains('inline-active'));
sandbox.closeInlineTab('favoritesPanel');
ok('关掉非激活标签不影响当前面板',
   table.classList.contains('inline-active') && !favorites.classList.contains('open'));

sandbox.setDefaultMode('window');
ok('设置切回小窗后，已打开的面板回到侧栏',
   table.classList.contains('open') && table.parentElement === home,
   table.parentElement && table.parentElement.id);
sandbox.setDefaultMode('inline');
ok('设置切到内嵌后，已打开的面板搬进对话框',
   table.parentElement === panelsHost, table.parentElement && table.parentElement.id);
ok('内嵌容器重新显示', host.style.display === 'flex', host.style.display);

// ── 8. 登录门槛优先于询问框 ───────────────────────────────────
sandbox.closePanelModeModal();
sandbox.toggleFeaturePanel('tablePanel');           // 收起
sandbox.setLoginBlock(true);
ok('未登录点润色不打开面板',
   sandbox.toggleFeaturePanel('polishPanel') === false && !polish.classList.contains('open'));
ok('未登录时连询问框都不弹', !modal.classList.contains('show'));
ok('先弹登录而不是先问显示方式', sandbox.getLoginAsked() === '摘要润色', sandbox.getLoginAsked());
sandbox.setLoginBlock(false);

// ── 9. dockPanel 本身仍支持两种宿主 ───────────────────────────
sandbox.dockPanel(polish, 'inline');
ok('dockPanel 能把面板搬进内嵌面板区', polish.parentElement === panelsHost);
sandbox.dockPanel(polish, 'window');
ok('dockPanel 能把面板搬回侧栏', polish.parentElement === home);

polish.setAttribute('style', 'position:absolute;left:120px;top:40px;width:380px');
polish.classList.add('panel-moved');
sandbox.dockPanel(polish, 'inline');
ok('搬进内嵌时清掉了绝对定位（否则会错位）',
   polish.style.cssText === '' && !polish.classList.contains('panel-moved'),
   [polish.style.cssText, polish.className]);
sandbox.dockPanel(polish, 'window');
ok('搬回小窗时还原原来的定位', /position:absolute/.test(polish.getAttribute('style') || ''),
   polish.getAttribute('style'));

// ── 10. 设置里的文案与入口 ────────────────────────────────────
ok('设置里说明"点功能会先问一句"',
   /点功能时会先问一句「小窗还是对话框」，选完才打开/.test(script));
ok('设置里有关闭 / 恢复询问的按钮',
   /function togglePanelAsk\(\)/.test(script)
   && /togglePanelAsk\(\)">' \+\s*\(panelAskEnabled\(\) \? '不再询问' : '恢复询问'\)/.test(script));
ok('设置里有两个模式按钮',
   /setDefaultMode\(\\'window\\'\)/.test(script) && /setDefaultMode\(\\'inline\\'\)/.test(script));

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
