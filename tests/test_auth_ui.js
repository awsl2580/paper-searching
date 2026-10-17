// 未登录点「润色/收藏/工作流」：必须弹"登录后才能用"，并且
//   * 提示里的「去登录」要真的把登录框打开（以前会被全局点击监听立刻收掉，表现为"点了没反应"）；
//   * 登录成功后自动打开用户原本想用的那个面板（不用再点一次按钮）。
//
// 函数从 static/index.html 里现抠，测的是线上代码。
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

let pass = 0, fail = 0;
function ok(name, cond, got) {
    if (cond) { pass++; console.log('PASS  ' + name); }
    else { fail++; console.log('FAIL  ' + name + '  -> got: ' + JSON.stringify(got)); }
}

// ── DOM 桩 ───────────────────────────────────────────────────
function el(extra) {
    const node = Object.assign({
        _cls: new Set(),
        textContent: '',
        value: '',
        children: [],
        style: {},
        classList: {
            add: (c) => node._cls.add(c),
            remove: (c) => node._cls.delete(c),
            contains: (c) => node._cls.has(c),
            toggle: (c, on) => (on ? node._cls.add(c) : node._cls.delete(c)),
        },
        appendChild: (c) => { node.children.push(c); return c; },
        focus: () => { node.focused = true; },
        addEventListener: () => {},
        querySelectorAll: () => [],
    }, extra || {});
    return node;
}

const nodes = {
    authPromptModal: el(),
    authPromptFeature: el(),
    authDropdown: el(),
    authError: el(),
    authSuccess: el(),
    authTitle: el(),
    authSub: el(),
    authSubmitBtn: el(),
    authSwitchText: el(),
    authSwitchLink: el(),
    authEmail: el(),
    authConfirmPassword: el(),
    authUsername: el(),
    authPassword: el(),
    authRememberHint: el(),
};

const opened = [];
const documentStub = {
    getElementById: (id) => nodes[id] || null,
    addEventListener: () => {},
};

// 需要注入的小工具（页面里的，但不在被测函数里）
const helpers = {
    openSidePanel: (id) => opened.push('open:' + id),
    showFavorites: () => opened.push('showFavorites'),
};

const src = [
    'let isLoggedIn = false;',
    'let isRegisterMode = false;',
    'let pendingPanelId = null;',
    'let currentUser = null;',
    // 页面里这些是模块级的 DOM 常量，这里按同名注入
    'const authPromptModal = document.getElementById("authPromptModal");',
    'const authPromptFeature = document.getElementById("authPromptFeature");',
    'const authDropdown = document.getElementById("authDropdown");',
    'const authError = document.getElementById("authError");',
    'const authSuccess = document.getElementById("authSuccess");',
    'const authTitle = document.getElementById("authTitle");',
    'const authSub = document.getElementById("authSub");',
    'const authSubmitBtn = document.getElementById("authSubmitBtn");',
    'const authSwitchText = document.getElementById("authSwitchText");',
    'const authSwitchLink = document.getElementById("authSwitchLink");',
    'const authEmail = document.getElementById("authEmail");',
    'const authConfirmPassword = document.getElementById("authConfirmPassword");',
    'const authUsername = document.getElementById("authUsername");',
    'const authPassword = document.getElementById("authPassword");',
    'const authRememberHint = document.getElementById("authRememberHint");',
    grabFn('requireLogin'),
    grabFn('showAuthPrompt'),
    grabFn('closeAuthPrompt'),
    grabFn('openAuthDropdown'),
    grabFn('closeAuthDropdown'),
    grabFn('setAuthMode'),
    grabFn('toggleAuthMode'),
    grabFn('goLoginFromPrompt'),
    grabFn('openLoginFromPrompt'),
    grabFn('resumePendingPanel'),
    'return { requireLogin, showAuthPrompt, setAuthMode, toggleAuthMode, goLoginFromPrompt,',
    '         openLoginFromPrompt, resumePendingPanel,',
    '         setLoggedIn: (v) => { isLoggedIn = v; },',
    '         setPending: (v) => { pendingPanelId = v; },',
    '         getPending: () => pendingPanelId, getMode: () => isRegisterMode };',
].join('\n');

const api = new Function(
    'document', 'openSidePanel', 'showFavorites', 'renderPaperTable', 'setTimeout',
    src
)(documentStub, helpers.openSidePanel, helpers.showFavorites, () => {}, (fn) => fn());

// ══════════════════════════════════════════════════════════════
//  ① 未登录点润色：弹提示 + 记住面板
// ══════════════════════════════════════════════════════════════
ok('未登录时 requireLogin 返回 true（拦住）', api.requireLogin('摘要润色', 'polishPanel') === true);
ok('提示弹窗被打开', nodes.authPromptModal.classList.contains('show'));
ok('提示里写明是哪个功能', nodes.authPromptFeature.textContent === '摘要润色',
   nodes.authPromptFeature.textContent);
ok('记住登录后要打开的面板', api.getPending() === 'polishPanel', api.getPending());
ok('提示文案说明"点击去登录"与"登录后自动打开"',
   /去登录/.test(html) && /登录成功后会自动帮你打开/.test(html));

// ══════════════════════════════════════════════════════════════
//  ② 点「去登录」：关提示、开登录框、默认登录模式、聚焦用户名
// ══════════════════════════════════════════════════════════════
nodes.authDropdown.classList.remove('open');
api.goLoginFromPrompt();
ok('点去登录会收起提示', !nodes.authPromptModal.classList.contains('show'));
ok('点去登录会打开登录框', nodes.authDropdown.classList.contains('open'));
ok('默认是登录模式（老用户不用先切注册）', api.getMode() === false, api.getMode());
ok('焦点落在用户名输入框', nodes.authUsername.focused === true);
ok('登录框提示"记住 30 天"',
   /30 天/.test(nodes.authRememberHint.textContent), nodes.authRememberHint.textContent);

ok('切到注册模式：表单换成注册文案', (() => {
    api.toggleAuthMode();
    return api.getMode() === true && nodes.authSubmitBtn.textContent === '注册';
})(), [api.getMode(), nodes.authSubmitBtn.textContent]);
ok('注册模式提示"自动登录"',
   /自动登录/.test(nodes.authRememberHint.textContent), nodes.authRememberHint.textContent);
api.toggleAuthMode();
ok('还能切回登录模式', api.getMode() === false && nodes.authSubmitBtn.textContent === '登录');

// ══════════════════════════════════════════════════════════════
//  ③ 登录成功后自动打开
// ══════════════════════════════════════════════════════════════
api.resumePendingPanel();
ok('登录成功后打开原来的面板（润色）', opened.includes('open:polishPanel'), opened);
ok('用完就清掉，避免下次登录乱开面板', api.getPending() === null);

// 收藏面板走 showFavorites（要顺带拉列表）
api.setPending('favoritesPanel');
opened.length = 0;
api.resumePendingPanel();
ok('收藏面板登录后走 showFavorites（同时刷新列表）', opened.includes('showFavorites'), opened);

// 没有待办面板时什么都不做
opened.length = 0;
api.resumePendingPanel();
ok('没有待办时不会乱开面板', opened.length === 0, opened);

// 已登录就不该再弹提示
api.setLoggedIn(true);
ok('已登录时 requireLogin 直接放行', api.requireLogin('摘要润色', 'polishPanel') === false);
nodes.authPromptModal.classList.remove('show');
api.setLoggedIn(false);

// ══════════════════════════════════════════════════════════════
//  ④ 接线检查（HTML/脚本层）
// ══════════════════════════════════════════════════════════════
ok('润色按钮带登录门槛并记住面板',
   /toggleSidePanel\('polishPanel',\s*'摘要润色'\)/.test(script) &&
   /requireLogin\(feature,\s*id\)/.test(script));
ok('收藏/工作流也把面板 id 传进去',
   /requireLogin\('收藏功能',\s*'favoritesPanel'\)/.test(script) &&
   /requireLogin\('科研工作流',\s*'wfPanel'\)/.test(script));
ok('「去登录」按钮绑定了 goLoginFromPrompt',
   /class="btn-go-login"[^>]*onclick="goLoginFromPrompt\(\)"/.test(html));
ok('全局点击监听不会把刚打开的登录框收掉',
   /closest\('#authPromptModal'\)[\s\S]{0,60}return;/.test(script));
ok('登录/注册成功都走 onAuthSuccess（注册后自动登录）',
   /function onAuthSuccess\(/.test(script) && /onAuthSuccess\(data\.user\)/.test(script));
ok('登录框有"记住 30 天"的提示元素', /id="authRememberHint"/.test(html));

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
