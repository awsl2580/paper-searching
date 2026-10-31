// 模型下拉列表（输入框下方）的前端测试。
//
// 覆盖的真实风险：
//   A) 清单如果写死在前端，改模型要改两处 —— 这里断言它必须来自 /api/models 的 catalog；
//   B) 选完不落盘，刷新就回到默认 —— 断言存 localStorage；
//   C) 请求不带 model，等于选了个寂寞 —— 断言 /api/chat 的请求体里有 model；
//   D) 没有 toast 组件却调用 toast(...) —— 运行时静默报错，断言不再引用不存在的函数；
//   E) 点菜单外面 / 按 Esc 要能收起。
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

// ── 1. 结构 ────────────────────────────────────────────────────
ok('输入框下方有模型选择按钮', /id="modelPick"/.test(html) && /id="modelPickName"/.test(html));
ok('有一个可弹出的菜单容器', /id="modelMenu"/.test(html));
ok('有一行提示文案位', /id="modelHint"/.test(html));
ok('按钮带 aria-haspopup（可访问性）', /id="modelPick"[\s\S]{0,200}aria-haspopup="listbox"/.test(html));
ok('菜单默认是收起的', /id="modelMenu"[\s\S]{0,100}style="display:none"/.test(html));
ok('默认显示 DeepSeek-V4.1-Flash', /id="modelPickName">DeepSeek-V4\.1-Flash</.test(html));
ok('顶栏品牌不再出现 AI4S（给功能按钮让出宽度）',
   /<h1>[\s\S]{0,500}DeepSeek <span>· 科学智能体<\/span>[\s\S]{0,40}<\/h1>/.test(html));
ok('顶栏强制单行（header / actions / features 都不换行）',
   /\.header \{[\s\S]{0,800}flex-wrap: nowrap/.test(html)
   && /\.header-actions \{[\s\S]{0,400}flex-wrap: nowrap/.test(html)
   && /\.header-features \{[\s\S]{0,300}flex-wrap: nowrap/.test(html)
   && /\.header h1 \{[\s\S]{0,400}white-space: nowrap/.test(html));
ok('空间不足时逐级收起副标题 / 模型徽章',
   /@media \(max-width: 900px\)[\s\S]{0,240}\.header h1 span \{ display: none/.test(html)
   && /@media \(max-width: 760px\)[\s\S]{0,160}\.model-badge \{ display: none/.test(html));
ok('菜单向上弹出（挂在来源行右侧，下面就是输入框）',
   /\.model-menu \{[\s\S]{0,240}bottom: calc\(100% \+ 8px\)/.test(html));
ok('模型下拉箭头是居中的 SVG（不是下沉的字形）',
   /class="caret-ico"/.test(html) && !/&#8964;/.test(html)
   && /\.model-pick[\s\S]{0,200}align-items: center/.test(html));
ok('模型/思考控件挪到了文献来源那一行（右对齐）',
   /class="model-cluster"/.test(html)
   && html.indexOf('class="model-cluster"') > html.indexOf('class="source-bar"')
   && html.indexOf('class="model-cluster"') < html.indexOf('class="input-area"'));
ok('旧的独立一行已移除', !/class="model-row"/.test(html));

// ── 2. 清单必须来自服务端 ──────────────────────────────────────
ok('前端不写死模型文案，而是读 catalog',
   /fetch\('\/api\/models'/.test(script) && /\(data\.models \|\| \{\}\)\.catalog/.test(script));
ok('按 id 查模型（用于显示名）', /function modelById\(id\)/.test(script));
ok('多模态模型标"读图"', /multimodal\) return '<span class="model-item-tag vis">读图/.test(script));
ok('推理模型标"推理"', /thinking\) return '<span class="model-item-tag def">推理/.test(script));
ok('不再凭"没配过"就标注没权限（权限按 token 授予，前端猜不准）',
   !/需 token 授权/.test(script) && !/未授权/.test(script));

// ── 3. 选中逻辑：默认值 + 持久化 + 白名单回退 ────────────────────
const store = {};
global.localStorage = {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
};
const sandbox = new Function('localStorage', 'document',
    "let modelCatalog = {default: 'deepseek-V4.1-flash', models: [\n" +
    "  {id:'deepseek-V4.1-flash', name:'DeepSeek-V4.1-Flash', default:true, configured:true, verified:true},\n" +
    "  {id:'deepseek-v4-pro', name:'DeepSeek-V4-Pro', configured:true, verified:true},\n" +
    "]};\n" +
    "const MODEL_PICK_KEY = 'chatModel';\n" +
    grabFn('currentChatModel') + '\n' +
    grabFn('modelById') + '\n' +
    grabFn('modelDisplayName') + '\n' +
    'return {currentChatModel, modelById, modelDisplayName, setCat(c){modelCatalog=c;}};'
)(global.localStorage, { getElementById: () => null });

ok('没选过时默认 flash', sandbox.currentChatModel() === 'deepseek-V4.1-flash', sandbox.currentChatModel());

store['chatModel'] = 'deepseek-v4-pro';
ok('选过就用存的', sandbox.currentChatModel() === 'deepseek-v4-pro', sandbox.currentChatModel());

store['chatModel'] = 'gpt-4';
ok('存了不存在的模型名 -> 回退默认（不会把垃圾发给服务端）',
   sandbox.currentChatModel() === 'deepseek-V4.1-flash', sandbox.currentChatModel());

ok('显示名取自清单', sandbox.modelDisplayName('deepseek-v4-pro') === 'DeepSeek-V4-Pro',
   sandbox.modelDisplayName('deepseek-v4-pro'));
ok('未知 id 原样回显（便于排查错误配置）',
   sandbox.modelDisplayName('xxx') === 'xxx', sandbox.modelDisplayName('xxx'));
ok('空值给中性文案', sandbox.modelDisplayName('') === '模型', sandbox.modelDisplayName(''));

// ── 4. 请求体必须带 model ──────────────────────────────────────
const bodyBlock = script.slice(script.indexOf("fetch('/api/chat'"), script.indexOf("fetch('/api/chat'") + 900);
ok('/api/chat 请求体里带 model', /model:\s*currentChatModel\(\)/.test(bodyBlock), bodyBlock.slice(0, 200));
ok('pickModel 会写 localStorage',
   /function pickModel\(id\)[\s\S]{0,400}localStorage\.setItem\(MODEL_PICK_KEY, id\)/.test(script));
ok('pickModel 拒绝不认识的模型',
   /function pickModel\(id\)[\s\S]{0,120}if \(!modelById\(id\)\) return;/.test(script));
ok('不再调用不存在的 toast()', !/toast\(/.test(script));
ok('切换后按钮有视觉反馈', /just-picked/.test(script) && /@keyframes modelPicked/.test(html));

// ── 5. 收起行为 ────────────────────────────────────────────────
ok('Esc 会先收模型菜单',
   /function\(e\) \{[\s\S]{0,1200}modelMenu'\)[\s\S]{0,200}closeModelMenu\(\)/.test(script));
ok('点菜单外收起（且不误伤按钮自身）',
   /closest\('#modelMenu, #modelPick'\)/.test(script));
ok('菜单打开时阻止冒泡（否则刚开就关）',
   /function toggleModelMenu\(ev\) \{[\s\S]{0,120}ev\.stopPropagation\(\)/.test(script));

// ── 6. 服务端事件回显 ──────────────────────────────────────────
ok('处理服务端的 model 事件（发图会被切成 VL3.5）',
   /data\.type === 'model'/.test(script) && /function showEffectiveModel/.test(script));
ok('切换原因会写在提示里', /发了图片，自动切换/.test(script));

// ── 7. 深度思考开关 ────────────────────────────────────────────
ok('输入框下方有深度思考开关', /id="thinkToggle"/.test(html) && /深度思考/.test(html));
ok('开关带 aria-pressed（可访问性）', /id="thinkToggle"[\s\S]{0,300}aria-pressed="false"/.test(html));
ok('请求体里带 thinking', /thinking:\s*thinkingOn\(\)/.test(script));
ok('默认关闭（localStorage 没值就是 false）',
   /function thinkingOn\(\)[\s\S]{0,160}=== '1'/.test(script));
ok('选择会持久化到 localStorage',
   /THINK_KEY\s*=\s*'deepThinking'/.test(script)
   && /localStorage\.setItem\(THINK_KEY/.test(script));
ok('两个 DeepSeek 模型都支持深度思考',
   /function thinkingSupported\(\)[\s\S]{0,200}supports_thinking !== false/.test(script)
   && /off-unsupported/.test(html));
ok('开启时提示会切到 DeepSeek-V4-Pro', /深度思考已开：本轮用 DeepSeek-V4-Pro/.test(script));
ok('开关打开会自动选中 pro', /turningOn && currentChatModel\(\) !== THINK_MODEL/.test(script));
ok('服务端回显思考被降级时给原因',
   /ev\.thinking_requested && !ev\.thinking/.test(script) && /thinking_note/.test(script));

const think = new Function('localStorage', 'document',
    "const THINK_KEY = 'deepThinking';\n" +
    "let modelCatalog = {default:'deepseek-V4.1-flash', models:[\n" +
    "  {id:'deepseek-V4.1-flash', name:'DeepSeek-V4.1-Flash', supports_thinking:true, default:true},\n" +
    "  {id:'deepseek-v4-pro', name:'DeepSeek-V4-Pro', supports_thinking:true},\n" +
    "]};\n" +
    "const MODEL_PICK_KEY = 'chatModel';\n" +
    grabFn('currentChatModel') + '\n' + grabFn('modelById') + '\n' +
    grabFn('thinkingOn') + '\n' + grabFn('thinkingSupported') + '\n' +
    'return {thinkingOn, thinkingSupported, setModel(id){localStorage.setItem(MODEL_PICK_KEY, id);}};'
)(global.localStorage, { getElementById: () => null });

ok('思考开关默认关', think.thinkingOn() === false, think.thinkingOn());
store['deepThinking'] = '1';
ok('开关状态能读回来', think.thinkingOn() === true, think.thinkingOn());
store['deepThinking'] = '0';
ok('关掉后是 false', think.thinkingOn() === false, think.thinkingOn());
store['deepThinking'] = '1';
think.setModel('deepseek-v4-pro');
ok('选 pro 时支持思考', think.thinkingSupported() === true,
   think.thinkingSupported());
think.setModel('deepseek-V4.1-flash');
ok('选 flash 时也支持（开关会切到 pro）', think.thinkingSupported() === true,
   think.thinkingSupported());

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
