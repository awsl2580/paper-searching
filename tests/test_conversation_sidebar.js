// The conversation must appear in the left sidebar as soon as it starts,
// not when the answer finishes (that could be 10+ seconds later).
//
// Reported behaviour: the record only showed up after clicking 新对话.
// Actual cause: the client waited for the whole SSE stream to finish before
// calling loadConversations(), even though the server creates the conversation
// row at request time and sends its id immediately.
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const script = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];

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

function mkEl() {
    const el = {
        _cls: new Set(), innerHTML: '', style: {},
        classList: {
            add: (c) => el._cls.add(c),
            remove: (c) => el._cls.delete(c),
            contains: (c) => el._cls.has(c),
            toggle: (c, on) => { if (on) el._cls.add(c); else el._cls.delete(c); },
        },
    };
    return el;
}

// 被测函数依赖的模块级状态
const deps = {
    sidebar: mkEl(), convList: mkEl(), sidebarFoot: mkEl(),
    conversations: [], activeConversationId: null, isLoggedIn: true,
};
const src = [
    grabFn('renderConvList'),
    grabFn('addConversationPlaceholder'),
    'return {renderConvList, addConversationPlaceholder,' +
    ' get conversations(){return conversations;},' +
    ' set conversations(v){conversations=v;},' +
    ' get activeConversationId(){return activeConversationId;}};',
].join('\n');

function build(state) {
    return new Function(
        'sidebar', 'convList', 'sidebarFoot', 'conversations', 'activeConversationId',
        'isLoggedIn', 'escapeHtml', 'formatConvTime',
        'let _c = arguments[3], _a = arguments[4];' +
        'Object.defineProperty(this, "conversations", {get:()=>_c, set:v=>{_c=v;}});' +
        '\n' + src
    ).call({}, state.sidebar, state.convList, state.sidebarFoot,
           state.conversations, state.activeConversationId, state.isLoggedIn,
           (s) => String(s == null ? '' : s), () => '刚刚');
}

// ---- 1. 未登录：侧栏隐藏，不显示任何记录 ----
{
    const st = { sidebar: mkEl(), convList: mkEl(), sidebarFoot: mkEl(),
                 conversations: [], activeConversationId: null, isLoggedIn: false };
    const api = build(st);
    api.renderConvList();
    ok('未登录时侧栏隐藏', st.sidebar.classList.contains('hidden'));
    ok('未登录时不渲染记录', st.convList.innerHTML === '', st.convList.innerHTML);
}

// ---- 2. 已登录但还没对话：侧栏就要显示出来 ----
{
    const st = { sidebar: mkEl(), convList: mkEl(), sidebarFoot: mkEl(),
                 conversations: [], activeConversationId: null, isLoggedIn: true };
    const api = build(st);
    api.renderConvList();
    ok('已登录时空列表也显示侧栏', !st.sidebar.classList.contains('hidden'));
    ok('空列表给出引导文案', /还没有对话记录/.test(st.convList.innerHTML),
       st.convList.innerHTML);
    ok('空列表提示"提问后会立刻出现"', /立刻出现/.test(st.convList.innerHTML),
       st.convList.innerHTML);
}

// ---- 3. 核心：会话一开始就插入左侧列表 ----
{
    const st = { sidebar: mkEl(), convList: mkEl(), sidebarFoot: mkEl(),
                 conversations: [], activeConversationId: null, isLoggedIn: true };
    const api = build(st);
    api.addConversationPlaceholder(7, '农业');
    ok('占位项进入列表', api.conversations.length === 1, api.conversations.length);
    ok('占位项标题取用户那句话', api.conversations[0].title === '农业',
       api.conversations[0].title);
    ok('占位项被标为当前会话', api.activeConversationId === 7, api.activeConversationId);
    ok('列表里渲染出标题', /农业/.test(st.convList.innerHTML), st.convList.innerHTML);
    ok('当前会话高亮 active', /class="conv-item active"/.test(st.convList.innerHTML),
       st.convList.innerHTML);
    ok('占位期间提示"正在回答…"', /正在回答/.test(st.convList.innerHTML),
       st.convList.innerHTML);
    ok('侧栏可见', !st.sidebar.classList.contains('hidden'));
}

// ---- 4. 幂等：同一个 id 不重复插入 ----
{
    const st = { sidebar: mkEl(), convList: mkEl(), sidebarFoot: mkEl(),
                 conversations: [], activeConversationId: null, isLoggedIn: true };
    const api = build(st);
    api.addConversationPlaceholder(7, '农业');
    api.addConversationPlaceholder(7, '农业');
    ok('重复同一 id 不会插两条', api.conversations.length === 1, api.conversations.length);
}

// ---- 5. 未登录时不插占位（游客不存记录）----
{
    const st = { sidebar: mkEl(), convList: mkEl(), sidebarFoot: mkEl(),
                 conversations: [], activeConversationId: null, isLoggedIn: false };
    const api = build(st);
    api.addConversationPlaceholder(7, '农业');
    ok('未登录不插占位项', api.conversations.length === 0, api.conversations.length);
}

// ---- 6. 回答结束后服务端数据覆盖占位（不再显示"正在回答…"）----
{
    const st = { sidebar: mkEl(), convList: mkEl(), sidebarFoot: mkEl(),
                 conversations: [], activeConversationId: null, isLoggedIn: true };
    const api = build(st);
    api.addConversationPlaceholder(7, '农业');
    api.conversations = [{ id: 7, title: '农业', message_count: 2,
                           updated_at: '2026-09-21 07:00:00' }];
    api.renderConvList();
    ok('服务端数据覆盖后不再显示占位文案',
       !/正在回答/.test(st.convList.innerHTML), st.convList.innerHTML);
    ok('覆盖后显示真实条数', /2 条/.test(st.convList.innerHTML), st.convList.innerHTML);
}

// ---- 7. 静态接线：conversation_id 事件里就要插占位 ----
ok('收到 conversation_id 时插入占位',
   /data\.type === 'conversation_id'[\s\S]{0,400}addConversationPlaceholder\(/.test(script));
ok('插入前判断不是当前会话（避免续聊时重复插）',
   /newConversationId !== activeConversationId[\s\S]{0,200}addConversationPlaceholder/.test(script));
ok('流结束后仍用服务端数据刷新一次',
   /newConversationId !== activeConversationId[\s\S]{0,120}loadConversations\(\)/.test(script));

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
