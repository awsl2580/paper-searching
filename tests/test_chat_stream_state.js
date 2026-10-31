// 「思考中」必须由这一轮自己解锁（用户报的现象：回答输出完了，按钮还在思考中，
// 而且切到别的分支也发不出消息）。
//
// 两层根因，这个文件两层都锁住：
//   1) 结束后解锁写在函数末尾，靠"顺序执行"保证 —— 中间任何一步抛异常，
//      setLoading(false) 就永远不执行。现在放在 finally 里，并且认身份
//      （releaseChatLoading(requestId)）：被切走的那一轮跑完时不许动新一轮的界面。
//   2) 读取循环只认"连接关掉"，不认服务端的 done —— 连接不断就永远等下去。
//      现在收到 done 立刻跳出循环，另外配一个静默看门狗兜底。
const fs = require('fs');

const html = fs.readFileSync(require('path').join(__dirname, '..', 'static', 'index.html'), 'utf8');
// 页面里有多个 <script>，取**最长的那块**（应用主脚本），与其它前端用例一致
const _blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
const script = _blocks.reduce((a, b) => (b[1].length > a[1].length ? b : a))[1];

function grabFn(name) {
    const start = script.indexOf('function ' + name + '(');
    if (start < 0) throw new Error('not found: ' + name);
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

// ── 1. 静态接线：done / finally / 分支守卫 / 看门狗 ──────────────
ok('聊天流收到 done 就结束这一轮（不等连接自己关掉）',
   /data\.type === 'done'[\s\S]{0,400}finished = true;/.test(script));
ok('跳出的是最外层读取循环',
   // need_api_key 与 done 都在最外层读取循环里 break；后者之后直接进入收尾。
   /if \(needKey\) \{ try \{ await reader\.cancel\(\); \} catch \(e\) \{\} break; \}[\s\S]{0,160}if \(finished\) break;/.test(script));
ok('reset 后重新挂加载卡（工具/推理阶段不空白）',
   /data\.type === 'reset'[\s\S]{0,900}streamStarted = false;[\s\S]{0,300}loader = startLoading\(bubble, abortChat\)/.test(script));
ok('解锁在 finally 里（任何异常路径都必须解锁）',
   /finally \{[\s\S]{0,1200}releaseChatLoading\(requestId\)/.test(script));
ok('解锁要校验这一轮的身份（旧轮不许动新一轮）',
   /function releaseChatLoading\(requestId\) \{\s*if \(!isCurrentTurn\(requestId\)\) return false;/.test(script));
ok('sendMessage 结束时不再直接 setLoading(false)（必须走释放函数）',
   !/async function sendMessage\(\)[\s\S]*?\n            setLoading\(false\);/.test(script));

ok('切到别的对话时把在跑的一轮卸下来',
   /async function openConversation\(conversationId\) \{[\s\S]{0,400}detachActiveTurn\(\);/.test(script));
ok('新建对话时同样卸下来',
   /function startNewConversation\(\) \{[\s\S]{0,200}detachActiveTurn\(\);/.test(script));
ok('卸下来只解锁、不中止（服务端跑完还能落库）',
   /function detachActiveTurn\(\) \{[\s\S]{0,400}activeChat\.detached = true;[\s\S]{0,200}activeChat = null;\s*setLoading\(false\);/.test(script));

ok('只往"当前分支"的 messageHistory 里写（不许串到别的分支）',
   /const sameBranch = !chat\.detached &&[\s\S]{0,200}if \(!aborted && fullContent && sameBranch\)/.test(script));
ok('切换分支时不再抢回 activeConversationId',
   /if \(!chat\.detached && newConversationId !== activeConversationId\)/.test(script));

ok('有静默看门狗（连接断了也要结束等待）',
   /function armSilenceWatch\(\)[\s\S]{0,600}timedOut = true;[\s\S]{0,200}controller\.abort\(\)/.test(script));
ok('收到字节（含心跳）就重置看门狗',
   /armSilenceWatch\(\);\s*if \(!line\.startsWith\('data: '\)\) continue;/.test(script));
ok('服务端出错时保留已经流出的正文（只追加一行说明）',
   /if \(fullContent\) \{\s*appendBubbleNote\(bubble, '❌ ' \+ data\.message\);/.test(script));
ok('连接中断/超时也保留正文',
   /appendBubbleNote\(bubble, '⏱ 连接长时间没有数据/.test(script)
   && /appendBubbleNote\(bubble, '⚠️ 连接中断：' \+ e\.message\);/.test(script));
ok('提示样式是追加块而不是覆盖正文',
   /\.msg-note \{/.test(html) && /function appendBubbleNote\(bubble, text\)/.test(script));

// ── 2. 行为：解锁必须有身份，切走必须解开 ──────────────────────
function mkEl() {
    const el = { textContent: '', title: '', disabled: false, style: {}, attrs: {} };
    el.classList = {
        _s: new Set(),
        add: (c) => el.classList._s.add(c),
        remove: (c) => el.classList._s.delete(c),
        contains: (c) => el.classList._s.has(c),
        toggle: (c, on) => { if (on) el.classList._s.add(c); else el.classList._s.delete(c); },
    };
    el.setAttribute = (k, v) => { el.attrs[k] = v; };
    return el;
}

function build() {
    const src = [
        'let isProcessing = false;',
        'let activeChat = null;',
        'const input = { disabled: false, value: "", focus: function () {} };',
        'const sendBtn = __sendBtn;',
        grabFn('setLoading'),
        grabFn('isCurrentTurn'),
        grabFn('releaseChatLoading'),
        grabFn('detachActiveTurn'),
        'return {',
        '  setLoading: setLoading, isCurrentTurn: isCurrentTurn,',
        '  releaseChatLoading: releaseChatLoading, detachActiveTurn: detachActiveTurn,',
        '  startTurn: function (id) { activeChat = { requestId: id, detached: false, aborted: false }; return activeChat; },',
        '  get activeChat() { return activeChat; },',
        '  get isProcessing() { return isProcessing; },',
        '  get input() { return input; },',
        '};',
    ].join('\n');
    const sendBtn = mkEl();
    const api = new Function('__sendBtn', src)(sendBtn);
    api.sendBtn = sendBtn;
    return api;
}

// 2.1 发消息：按钮变「思考中」，输入框锁住
{
    const api = build();
    api.setLoading(true);
    ok('思考中时按钮文案变成「思考中...」', api.sendBtn.textContent === '思考中...', api.sendBtn.textContent);
    ok('思考中时输入框被禁用', api.input.disabled === true, api.input.disabled);
    ok('思考中时 isProcessing 为真', api.isProcessing === true, api.isProcessing);
    ok('按钮不做 disabled（它同时是中止按钮）', api.sendBtn.disabled === false, api.sendBtn.disabled);
}

// 2.2 正常结束：这一轮自己解锁
{
    const api = build();
    const chat = api.startTurn('A');
    api.setLoading(true);
    ok('当前轮认得出自己', api.isCurrentTurn('A') === true);
    ok('解锁成功', api.releaseChatLoading('A') === true);
    ok('按钮恢复「发送」', api.sendBtn.textContent === '发送', api.sendBtn.textContent);
    ok('输入框恢复可用', api.input.disabled === false);
    ok('解锁后 isProcessing 为假', api.isProcessing === false, api.isProcessing);
    ok('解锁后不再持有 activeChat', api.activeChat === null, api.activeChat);
    ok('chat 对象仍记得自己（不会影响别的轮）', chat.requestId === 'A');
}

// 2.3 用户切走：立刻解锁，但不假装成功
{
    const api = build();
    const chat = api.startTurn('A');
    api.setLoading(true);
    ok('切走时确实卸下了一轮', api.detachActiveTurn() === true);
    ok('切走后按钮恢复「发送」', api.sendBtn.textContent === '发送', api.sendBtn.textContent);
    ok('切走后输入框可用', api.input.disabled === false);
    ok('被卸下的那一轮带 detached 标记', chat.detached === true, chat.detached);
    ok('没有请求被中止（不 abort，服务端跑完会落库）', chat.aborted === false, chat.aborted);
    ok('再切一次是空操作', api.detachActiveTurn() === false);
}

// 2.4 关键回归：被切走的那一轮跑完，不许把新一轮的「思考中」抹掉
{
    const api = build();
    api.startTurn('A');
    api.setLoading(true);
    api.detachActiveTurn();            // 用户切到别的分支
    api.startTurn('B');                // 在新分支发了一条
    api.setLoading(true);
    ok('旧轮解锁请求被拒绝', api.releaseChatLoading('A') === false);
    ok('新一轮仍然是「思考中...」', api.sendBtn.textContent === '思考中...', api.sendBtn.textContent);
    ok('新一轮仍然锁着输入框', api.input.disabled === true);
    ok('新一轮自己解锁才生效', api.releaseChatLoading('B') === true);
    ok('解锁后回到「发送」', api.sendBtn.textContent === '发送', api.sendBtn.textContent);
}

// 2.5 没有活动轮时解锁是空操作（不能在空闲时把按钮写坏）
{
    const api = build();
    api.setLoading(false);
    ok('空闲时解锁返回 false', api.releaseChatLoading('X') === false);
    ok('按钮文案仍是「发送」', api.sendBtn.textContent === '发送', api.sendBtn.textContent);
}

// ── 3. 删 / 开一个"已经不存在"的会话（思考进行中删会话时踩到的坑）──
// 现象：聊天里堆出一串「助手：❌ 会话不存在」。
// 根因：会话没了被当成"聊天错误"塞进对话里；删除也不幂等。
ok('打开会话遇到 404：把它从侧栏移除，而不是往聊天里写报错',
   /if \(resp\.status === 404\) \{[\s\S]{0,400}forgetConversation\(conversationId\);\s*return;/.test(script));
ok('删除会话遇到 404 也当成功（幂等）',
   /if \(!data\.success && resp\.status !== 404\)/.test(script));
ok('删除"正在回答"的那个会话：连这一轮一起停掉',
   /if \(activeChat && activeChat\.conversationId === conversationId\) abortChat\(\);/.test(script));
ok('清空全部会话时也停掉在跑的一轮',
   /async function clearAllConversations\(\) \{[\s\S]{0,400}if \(activeChat\) abortChat\(\);/.test(script));
ok('切回标签页时刷新会话列表（别的标签页可能删过）',
   /addEventListener\('visibilitychange'[\s\S]{0,300}loadConversations\(\)/.test(script));

function buildForget() {
    const src = [
        'let conversations = __conversations;',
        'let activeConversationId = null;',
        'let messageHistory = [];',
        'const calls = { rendered: 0, reloaded: 0, history: 0 };',
        'function renderConvList() { calls.rendered++; }',
        'function loadConversations() { calls.reloaded++; }',
        'function renderHistory() { calls.history++; }',
        grabFn('forgetConversation'),
        'return {',
        '  forgetConversation: forgetConversation,',
        '  get conversations() { return conversations; },',
        '  get activeConversationId() { return activeConversationId; },',
        '  set activeConversationId(v) { activeConversationId = v; },',
        '  get messageHistory() { return messageHistory; },',
        '  set messageHistory(v) { messageHistory = v; },',
        '  get calls() { return calls; },',
        '};',
    ].join('\n');
    return new Function('__conversations', src)(
        [{ id: 1, title: 'A' }, { id: 2, title: 'B' }]);
}

// 3.1 只把它从列表里移掉
{
    const api = buildForget();
    api.forgetConversation(1);
    ok('会话从列表里消失', JSON.stringify(api.conversations) === JSON.stringify([{ id: 2, title: 'B' }]),
       api.conversations);
    ok('重画了侧栏', api.calls.rendered === 1, api.calls);
    ok('顺带向服务端要了一次权威列表', api.calls.reloaded === 1, api.calls);
    ok('不影响别的会话的当前视图', api.activeConversationId === null);
}

// 3.2 删掉的正是当前打开的：视图要清空
{
    const api = buildForget();
    api.activeConversationId = 2;
    api.messageHistory = [{ role: 'user', content: 'hi' }, { role: 'assistant', content: 'yo' }];
    api.forgetConversation(2);
    ok('当前会话被复位（不然界面还挂在一段已删的对话上）', api.activeConversationId === null);
    ok('内存里的消息也清空', api.messageHistory.length === 0, api.messageHistory);
    ok('重画了对话区', api.calls.history === 1, api.calls);
}

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
