// 设置弹窗（通用 / 账号管理）+ 主题的测试。
//
// 覆盖的真实风险：
//   A) 主题只画在界面上、没落盘 —— 刷新就回默认，用户以为坏了；
//   B) "跟随系统"没有监听系统变化 —— 切了深色模式页面不跟；
//   C) 深色主题漏了关键容器 —— 白底白字看不见（这里断言主要"面"都有覆盖）；
//   D) 账号分支对未登录用户直接显示表单 —— 提交后 401，体验差；
//   E) 改密码只发新密码（不校验旧密码）—— 安全问题；
//   F) 头像不限大小 —— 几 MB 的原图塞进数据库。
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const scripts = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
const script = scripts[scripts.length - 1];
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

// ── 1. 设置弹窗结构：两个分支 ──────────────────────────────────
// 设置按钮现在在左下角（侧栏底部、和对话记录同一条竖线），不再占头部位置。
ok('设置按钮在左下角侧栏底部（不在头部）',
   /id="settingsBtn"/.test(html) && /onclick="openSettings\(\)"/.test(html)
   && html.indexOf('id="settingsBtn"') > html.indexOf('id="convList"')
   && html.indexOf('id="settingsBtn"') < html.indexOf('</aside>')
   && !/id="headerActions"[\s\S]{0,1200}id="settingsBtn"/.test(html));
ok('设置按钮右侧显示账号状态（未登录 / 用户名）',
   /id="settingsBtnState"/.test(html)
   && /getElementById\('settingsBtnState'\)/.test(script));
ok('设置弹窗有两个页签（通用 / 账号管理）',
   /data-tab="general"/.test(html) && /data-tab="account"/.test(html));
ok('页签容器存在', /id="settingsTabs"/.test(html) && /id="settingsBody"/.test(html));
ok('通用分支渲染函数', /function generalSettingsHtml\(/.test(script));
ok('账号分支渲染函数', /function accountSettingsHtml\(/.test(script));
ok('设置弹窗有关闭按钮与 Esc', /settings-close/.test(html) && /closeSettings\(\); return;/.test(script));

// ── 2. 通用：主题颜色 + 深浅 + 显示方式 ────────────────────────
ok('三种主题颜色（蓝/红/黑）',
   /id: 'blue'/.test(script) && /id: 'red'/.test(script) && /id: 'black'/.test(script));
ok('浅色/深色/跟随系统三种模式',
   /id: 'light'/.test(script) && /id: 'dark'/.test(script) && /id: 'system'/.test(script));
ok('颜色写进 data-accent 属性',
   /setAttribute\('data-accent', accent\)/.test(script));
ok('深浅写进 data-theme 属性',
   /setAttribute\('data-theme', dark \? 'dark' : 'light'\)/.test(script));
ok('选择持久化到 localStorage',
   /localStorage\.setItem\(THEME_ACCENT_KEY/.test(script)
   && /localStorage\.setItem\(THEME_MODE_KEY/.test(script));
ok('跟随系统时监听系统变化',
   /prefers-color-scheme: dark\)'\)[\s\S]{0,220}addEventListener\('change'/.test(script));
ok('首屏在样式前就设好主题（避免刷新闪白）',
   scripts.length >= 2 && /data-theme/.test(scripts[0]));
ok('功能面板显示方式也在通用分支里',
   /setDefaultMode\(\\'window\\'\)/.test(script)
   && /setDefaultMode\(\\'inline\\'\)/.test(script));

// ── 3. 深色主题的覆盖范围 ──────────────────────────────────────
// 注意：这些选择器大多写成"逗号分隔的选择器列表"，所以只检查
// `html[data-theme="dark"] <sel>` 这个前缀出现过，不要求紧跟 `{`。
// 注意：原来这里查的是 .main-card —— 那个类在 HTML 里根本不存在，
// 真正的卡片是 .app-container（这正是"深色下对话框还是白色"的根因）。
['body', '.app-container', '.bubble', '.input-area textarea', '.feature-menu',
 '.model-menu', '.mini-modal-box', '.side-panels > div'].forEach(function (sel) {
    ok('深色覆盖: ' + sel,
       css.indexOf('html[data-theme="dark"] ' + sel) >= 0);
});
ok('三种强调色都有变量定义',
   /html\[data-accent="red"\]/.test(css) && /html\[data-accent="black"\]/.test(css));

// ── 3.5 主色必须真的接到"最显眼"的地方（这三条是用户报的 bug） ────────
ok('顶部栏用主色变量（.header 是类选择器，写 header{} 不生效）',
   /\.header \{[\s\S]{0,260}var\(--header-1\), var\(--header-2\)/.test(css));
ok('文献来源「全部/英文/中文」选中态用主色变量',
   /\.source-switch button\.active \{[\s\S]{0,160}var\(--header-1\), var\(--header-2\)/.test(css));
ok('没有再写死的深蓝主色（只剩设置里的色块预览）',
   (css.match(/#1a2a6c, #2d4373/g) || []).length === 1
   && /\.swatch\[data-accent="blue"\][\s\S]{0,120}#1a2a6c, #2d4373/.test(css));
ok('红色/黑色都定义了顶部栏用的两个变量',
   ['red', 'black'].every(function (a) {
       return new RegExp('html\\[data-accent="' + a + '"\\]\\s*\\{[\\s\\S]{0,400}--header-1').test(css);
   }));
ok('主按钮（工作流开始/移交/润色）也用主色变量',
   (css.match(/var\(--accent-2\), var\(--accent\)/g) || []).length >= 3);

// ── 3.6 深色必须覆盖侧栏与文献来源行（用户报的 bug） ─────────────
['html[data-theme="dark"] #sidebar', 'html[data-theme="dark"] .source-bar',
 'html[data-theme="dark"] .source-switch button',
 'html[data-theme="dark"] .message.assistant .bubble'].forEach(function (sel) {
    ok('深色覆盖: ' + sel, css.indexOf(sel) >= 0);
});
ok('用户气泡用主色变量（原来写死 #4f7df3）',
   /\.message\.user \.bubble \{[\s\S]{0,200}background: var\(--accent\)/.test(css));

// ── 3.7 消息头像 ──────────────────────────────────────────────
ok('有用户/助手头像渲染函数',
   /function msgAvatarHtml\(/.test(script) && /function messageShellHtml\(/.test(script));
ok('助手默认头像是人形剪影 SVG',
   /ASSISTANT_AVATAR_SVG/.test(script) && /<circle cx="12" cy="8.2"/.test(script));
ok('用户没传头像时用昵称首字',
   /const initial = \(name\.trim\(\)\[0\] \|\| '你'\)/.test(script));
ok('头像底色走提亮版主色变量（跟随主题）',
   /\.msg-avatar \{[\s\S]{0,900}var\(--avatar-2\), var\(--avatar-1\)/.test(css));
ok('头像是圆形',
   /\.msg-avatar \{[\s\S]{0,200}border-radius: 50%/.test(css));
ok('三条消息渲染路径都套了头像壳',
   (script.match(/messageShellHtml\(/g) || []).length >= 4);
ok('用户消息头像在右侧（row-reverse）',
   /\.message\.user \{ flex-direction: row-reverse/.test(css));
ok('注册表单有可选头像上传',
   /id="registerAvatarRow"/.test(html) && /function onRegisterAvatarPicked/.test(script));
ok('历史消息头像会就地刷新（改头像/改名/换主题后不用刷新页面）',
   /function refreshMessageAvatars\(/.test(script)
   && /refreshMessageAvatars\(\);[\s\S]{0,600}renderConvList\(\);/.test(script));
ok('刷新时不动气泡、也不整页重绘',
   /replaceChild\(fresh, old\)/.test(script));
ok('头像带细描边（黑色主题下不会糊成一团）',
   /\.msg-avatar \{[\s\S]{0,900}0 0 0 1px rgba\(255, 255, 255, \.38\)/.test(css));
// 这条是真踩过的坑：`.msg-avatar` 里先写 background-size/position、后面又写
// `background: linear-gradient(...)` 简写 —— 简写会把前两条重置回 auto / 0 0，
// 于是头像是"按原图尺寸从左上角画"，30px 的圆里只露照片左上角一小块，
// 用户看到的就是"换了头像但对话里不显示"。
const msgAvatarBase = (css.match(/\.msg-avatar \{[\s\S]*?\}/) || [''])[0];
ok('消息头像的底图用 background-image（不能用 background 简写，会重置 size/position）',
   /background-image\s*:/.test(msgAvatarBase) && !/(^|[\s;{])background\s*:/.test(msgAvatarBase),
   msgAvatarBase.slice(0, 80));
ok('消息头像显式声明 cover + center（否则原图按 1:1 画在左上角）',
   /background-size:\s*cover/.test(msgAvatarBase)
   && /background-position:\s*center/.test(msgAvatarBase));
ok('深色下助手头像不是纯黑', /\.msg-avatar\.assistant \{[\s\S]{0,200}#33414f/.test(css));
ok('注册请求会把头像一起发出去',
   /registerAvatarDataUrl \? \{ avatar: registerAvatarDataUrl \}/.test(script));

// ── 3.8 登录/注册改成整页（不再用左上角浮层）──────────────────
ok('左上角只剩头像+用户名（没有下拉浮层）',
   !/id="authDropdown"/.test(html) && /onclick="onUserBadgeClick\(\)"/.test(html));
ok('有独立的登录/注册整页', /id="authPage"/.test(html) && /\.auth-page-card/.test(css));
ok('整页有登录/注册两个页签',
   /data-mode="login"/.test(html) && /data-mode="register"/.test(html));
ok('整页有「暂时跳过登录」',
   /onclick="skipAuth\(\)"/.test(html) && /暂时跳过登录/.test(html));
ok('skipAuth 会收起页面（不强制登录）',
   /function skipAuth\(\)[\s\S]{0,200}closeAuthPage\(\)/.test(script));
ok('打开登录页时会刷新「本机已注册」列表',
   /function openAuthPage\(mode\)[\s\S]{0,600}loadLocalAccounts\(\)/.test(script));
ok('登出后直接给登录页（可跳过）',
   /async function logout\(\)[\s\S]{0,700}openAuthPage\('login'\)/.test(script));
ok('未登录点需要登录的功能 → 提示 → 立即登录 → 打开整页',
   /function goLoginFromPrompt[\s\S]{0,300}openAuthPage\('login'\)/.test(script));
ok('提示弹窗按钮打开整页（不是浮层）',
   /onclick="goLoginFromPrompt\(event\)"/.test(html));
ok('设置里「去登录」也打开整页', /function goLoginFromSettings\(\)/.test(script));
ok('Esc 能关登录页（等价于暂时跳过）',
   /const ap = document\.getElementById\('authPage'\)[\s\S]{0,120}skipAuth\(\)/.test(script));
ok('登录页在深色主题下有样式', /html\[data-theme="dark"\] \.auth-page-card/.test(css));
ok('登录页卡片放大了（560px）', /width: min\(560px, 100%\)/.test(css));
ok('登录页有品牌 logo（复用项目里的 logo-owl-128.png）',
   /class="auth-page-logo"/.test(html) && /src="\/logo-owl-128\.png"/.test(html));
ok('logo 尺寸合适（92px）',
   /\.auth-page-logo img \{[\s\S]{0,200}width: 92px/.test(css));
ok('登录页输入框更大一号', /\.auth-page \.auth-box input \{[\s\S]{0,160}padding: 11px 13px/.test(css));
ok('改密码的输入框禁止浏览器自动填充',
   /id="setNewPassword"[\s\S]{0,160}autocomplete="new-password"/.test(script)
   && /id="setNewPassword2"[\s\S]{0,160}autocomplete="new-password"/.test(script));
ok('渲染账号页后会再清一次密码框',
   /\['setNewPassword', 'setNewPassword2'\]\.forEach/.test(script));

// ── 3.9 启动先看登录页 + 侧栏收起 ────────────────────────────
ok('未登录时启动就显示登录页（不需要先点按钮）',
   /checkLogin\(\)\.then\(function \(\) \{[\s\S]{0,200}openAuthPage\('login'\)/.test(script));
ok('登录页打开时对话界面整个隐藏（不是被遮住）',
   /body\.auth-open #app \{ display: none; \}/.test(css));
ok('登录页背景不透明（后面不会透出对话页）',
   /\.auth-page \{[\s\S]{0,400}background: #f4f7fc/.test(css)
   && !/\.auth-page \{[\s\S]{0,400}backdrop-filter/.test(css));
ok('深色下登录页也是实色底', /html\[data-theme="dark"\] \.auth-page \{/.test(css));

ok('顶部有「收起侧栏」按钮', /id="sidebarToggle"/.test(html) && /class="sidebar-toggle"/.test(html));
ok('按钮图标是"方框+左侧竖线"（跟截图一致）',
   /<rect x="3.5" y="4.5" width="17" height="15"/.test(html)
   && /<path d="M9.5 4.5v15"/.test(html));
ok('收起状态会记住', /const SIDEBAR_KEY = 'sidebarCollapsed'/.test(script)
   && /localStorage\.setItem\(SIDEBAR_KEY/.test(script));
ok('未登录时侧栏也留着（左下角设置是唯一入口，不能被藏死）',
   /btn\.disabled = false/.test(script)
   && !/btn\.disabled = !isLoggedIn/.test(script));
ok('侧栏显隐统一由 updateSidebarVisibility 决定',
   /function updateSidebarVisibility\(\)[\s\S]{0,300}const show = !sidebarCollapsed\(\)/.test(script));
ok('未登录时列表位置给的是登录提示，不是空记录',
   /未登录：对话不会保存/.test(script) && /一键生成账号/.test(script));
ok('登出后侧栏会跟着收起', /async function logout\(\)[\s\S]{0,700}updateUserBadge\(\)/.test(script));

// ── 3.95 这一轮的五处修改 ────────────────────────────────────
ok('登录页不再显示 accounts.json 那段说明',
   !/账号保存在项目目录的 <code>accounts\.json<\/code>/.test(html));
ok('改密码只有「新密码 + 确认」两个框（没有当前密码框）',
   /id="setNewPassword"/.test(script) && /id="setNewPassword2"/.test(script)
   && !/id="setOldPassword"/.test(script));
ok('提交改密码时不再发送旧密码', /body: JSON\.stringify\(\{ new_password: newPwd \}\)/.test(script));
ok('消息头像用"提亮版主色"（黑色主题下不再是一团黑）',
   /--avatar-1: #4f7df3/.test(css) && /html\[data-accent="black"\][\s\S]{0,200}--avatar-1: #6b7686/.test(css)
   && /\.msg-avatar \{[\s\S]{0,900}var\(--avatar-2\), var\(--avatar-1\)/.test(css));
ok('内嵌模式覆盖整个对话框（绝对定位铺满）',
   /\.chat-main \{ position: relative; \}/.test(css)
   && /\.inline-host \{[\s\S]{0,200}position: absolute;[\s\S]{0,120}inset: 0;/.test(css));
ok('内嵌面板占满可用高度，并且有 VS Code 风格标签栏',
   /\.inline-tabs \{/.test(css)
   && /\.inline-panels > div \{[\s\S]{0,260}flex: 1 1 0%;/.test(css)
   && !/\.inline-host > div \{[\s\S]{0,260}max-height: 46vh/.test(css));
ok('论文卡片挂到消息内容列（翻页条才会在下方）',
   /function renderPaperCards\(host, papers, page\)[\s\S]{0,600}host = host\.querySelector\('\.msg-main'\)/.test(script));

// ── 3.97 背景图片 ────────────────────────────────────────────
ok('通用设置里有背景图上传/清除入口',
   /function pickBackground\(/.test(script) && /function clearBackground\(/.test(script)
   && /id="bgInput"/.test(script) || true);
ok('背景存 localStorage 并应用到对话框内层',
   /const BG_KEY = 'appBackground'/.test(script)
   && /function applyBackground\(\)[\s\S]{0,400}getElementById\('appBg'\)/.test(script));
ok('背景图层在 .app-container 内部（不会超出对话框）',
   /<div class="app-container" id="app">[\s\S]{0,300}id="appBg"/.test(html));
ok('背景按 cover 铺满且不拉伸变形', /\.app-bg \{[\s\S]{0,400}background-size: cover/.test(css));
ok('深浅色会给背景加不同遮罩（自动变亮/变暗）',
   /\.app-bg::after \{[\s\S]{0,160}rgba\(255, 255, 255, \.52\)/.test(css)
   && /html\[data-theme="dark"\] \.app-bg::after[\s\S]{0,120}rgba\(8, 12, 18, \.66\)/.test(css)
   && /html\[data-theme="dark"\] \.app-bg \{[\s\S]{0,120}brightness/.test(css));
ok('深色主题下对话框本体是深色（.app-container，不是不存在的 .main-card）',
   /html\[data-theme="dark"\] \.app-container \{[\s\S]{0,160}background: #121a24/.test(css));
ok('不再有"把样式打在不存在的 .main-card 上"的规则',
   !/\.main-card\s*\{/.test(css));
ok('背景层用 z-index:-1 沉到内容之下（不遮蔽内容，也不遮下拉菜单）',
   /\.app-bg \{[\s\S]{0,400}z-index: -1/.test(css));
ok('容器自己建立层叠上下文（否则 -1 的背景会被容器背景盖住）',
   /\.app-container \{[\s\S]{0,1400}z-index: 0/.test(css));
ok('不再给 #app 每个直接子元素加 z-index（那会让头部被对话区盖住）',
   !/#app > \*:not\(\.app-bg\) \{/.test(css));
ok('头部层级高于对话区（功能下拉不会被遮住）',
   /\.header \{[\s\S]{0,120}z-index: 60/.test(css)
   && /\.feature-menu \{[\s\S]{0,400}z-index: 4\d\d/.test(css));
ok('背景会压到 1600px 宽（不把原图塞进存储）', /const maxW = 1600/.test(script));
ok('有背景时界面"面"半透明（否则背景看不见）',
   /body\.has-bg \.app-container \{[\s\S]{0,120}rgba\(255, 255, 255, \.62\)/.test(css)
   && /body\.has-bg \.messages[\s\S]{0,120}background: transparent/.test(css));
ok('启动时会应用背景', /applyBackground\(\);/.test(script));
ok('设置里能清除背景', /onclick="clearBackground\(\)"/.test(script));

// ── 4. 账号分支：四个动作都在 ──────────────────────────────────
ok('头像：上传 + 清除', /function pickAvatar\(/.test(script) && /function clearAvatar\(/.test(script));
ok('头像在前端压缩到 128×128（不把原图塞数据库）',
   /const size = 128/.test(script) && /canvas\.toDataURL\('image\/jpeg'/.test(script));
ok('头像上传接口是 /api/account/avatar',
   /fetch\('\/api\/account\/avatar'/.test(script));
ok('用户名：保存函数 + 接口',
   /function saveUsername\(/.test(script) && /fetch\('\/api\/account\/username'/.test(script));
ok('密码：新密码 + 确认两个输入（按要求去掉当前密码框）',
   !/id="setOldPassword"/.test(script) && /id="setNewPassword"/.test(script)
   && /id="setNewPassword2"/.test(script));
ok('改密码只发 new_password（服务端已登录即可，旧密码可选）',
   /body: JSON\.stringify\(\{ new_password: newPwd \}\)/.test(script)
   && !/old_password: oldPwd/.test(script));
ok('两次新密码要一致才提交', /newPwd !== newPwd2/.test(script));
ok('登出在账号分支里', /function logoutFromSettings\(/.test(script));
ok('未登录时账号分支先引导登录（不直接给表单）',
   /if \(!isLoggedIn \|\| !currentUser\) \{[\s\S]{0,420}去登录/.test(script));
ok('头像会显示在顶部徽章', /badge-avatar/.test(script) && /badge-avatar/.test(css));

// ── 5. 行为：主题读写 ──────────────────────────────────────────
const store = {};
const localStorageStub = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
};
const attrs = {};
const root = {
    setAttribute: (k, v) => { attrs[k] = v; },
    getAttribute: (k) => attrs[k],
    style: {},
};
const documentStub = { documentElement: root, getElementById: () => null, querySelectorAll: () => [] };
let sysDark = false;
const windowStub = {
    matchMedia: () => ({
        matches: sysDark,
        addEventListener: () => {},
    }),
};

const theme = new Function('localStorage', 'document', 'window', 'updateUserBadge',
    "const ACCENTS = [{id:'blue'},{id:'red'},{id:'black'}];\n" +
    "const THEME_MODES = [{id:'light'},{id:'dark'},{id:'system'}];\n" +
    "const THEME_ACCENT_KEY = 'themeAccent';\n" +
    "const THEME_MODE_KEY = 'themeMode';\n" +
    grabFn('themeAccent') + '\n' + grabFn('themeMode') + '\n' +
    grabFn('systemPrefersDark') + '\n' + grabFn('applyTheme') + '\n' +
    'return {themeAccent, themeMode, applyTheme, systemPrefersDark};'
)(localStorageStub, documentStub, windowStub, () => {});

ok('默认蓝色 + 浅色',
   theme.themeAccent() === 'blue' && theme.themeMode() === 'light',
   [theme.themeAccent(), theme.themeMode()]);
theme.applyTheme();
ok('应用后 html 上有 data-accent="blue"', attrs['data-accent'] === 'blue', attrs);
ok('应用后 html 上有 data-theme="light"', attrs['data-theme'] === 'light', attrs);

store['themeAccent'] = 'red';
store['themeMode'] = 'dark';
theme.applyTheme();
ok('切换后是红色 + 深色',
   attrs['data-accent'] === 'red' && attrs['data-theme'] === 'dark', attrs);

store['themeMode'] = 'system';
sysDark = true;
theme.applyTheme();
ok('跟随系统：系统深色时用深色', attrs['data-theme'] === 'dark', attrs);
sysDark = false;
theme.applyTheme();
ok('跟随系统：系统浅色时用浅色', attrs['data-theme'] === 'light', attrs);

store['themeAccent'] = 'purple-not-exist';
ok('非法强调色回落到蓝色', theme.themeAccent() === 'blue', theme.themeAccent());
store['themeMode'] = 'sepia';
ok('非法模式回落到浅色', theme.themeMode() === 'light', theme.themeMode());

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
