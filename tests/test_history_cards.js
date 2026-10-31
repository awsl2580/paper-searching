// 历史对话里的论文卡片（修复：切走再切回，论文变成纯文本、点不开原文）。
//
// 现象：检索出论文 → 点另一段历史对话 → 再切回来，卡片没了，只剩一串标题文字。
// 根因：卡片是拿 SSE 里的 papers 现画的，而历史消息只存了正文。
// 现在 papers 跟 assistant 消息一起落库，打开历史对话时按它重建卡片。
//
// 这个文件只查"前端接线有没有断"，真正的存取/迁移在 tests/test_history_papers.py
// 里用真 SQLite 跑。
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const _blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
const script = _blocks.reduce((a, b) => (b[1].length > a[1].length ? b : a))[1];

let pass = 0, fail = 0;
function ok(name, cond, got) {
    if (cond) { pass++; console.log('PASS  ' + name); }
    else { fail++; console.log('FAIL  ' + name + '  -> got: ' + JSON.stringify(got)); }
}

// 取一个函数的完整源码（按花括号配平），用于"在函数体内断言"。
// 比 `从函数头往下数 N 个字符` 稳：函数里加几行注释不会让用例误报。
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

// ── 1. 历史渲染 ───────────────────────────────────────────────
ok('renderHistory 会给带 papers 的助手消息重建卡片',
   /function renderHistory\(\)[\s\S]{0,2000}renderPaperCards\(div, msg\.papers, 0\)/.test(script));
ok('只有 assistant 且确有论文时才重建（用户消息不带卡片）',
   /msg\.role === 'assistant' && msg\.papers && msg\.papers\.length/.test(script));
ok('历史渲染时把文献表同步成"这段对话最近一轮检索"',
   /function renderHistory\(\)[\s\S]{0,2200}lastPapers = lastWithPapers \? lastWithPapers\.papers : \[\]/.test(script)
   && /function renderHistory\(\)[\s\S]{0,2400}renderPaperTable\(\)/.test(script));

// ── 2. 打开历史对话：papers 要跟着消息进内存 ─────────────────
ok('openConversation 读消息时保留 papers',
   // 取函数体再断言：窗口式（{0,900}）在函数里多几行注释时就会误报
   /papers: Array\.isArray\(m\.papers\) \? m\.papers : \[\]/.test(grabFn('openConversation')));
ok('流式结束后把本轮 papers 留在内存消息里（删别的对话重画也不丢）',
   /messageHistory\.push\(\{ role: 'assistant', content: fullContent, papers: turnPapers \}\)/.test(script));
ok('流式过程中记下本轮 papers',
   /turnPapers = data\.papers \|\| \[\]/.test(script));

// ── 3. 请求体：别把几十 KB 摘要发给模型 ──────────────────────
ok('发给 /api/chat 的 history 只带 role/content',
   /history: messageHistory\.map\(function \(m\) \{\s*return \{ role: m\.role, content: m\.content \};/.test(script));

// ── 4. 卡片分页：每条消息各记各的页码 ────────────────────────
ok('renderPaperCards 把论文与页码记在这块卡片区上',
   /host\._papers = papers;/.test(script)
   && /host\.dataset\.cardPage = String\(pg\.page\)/.test(script));
ok('翻页/全展开都以"这块卡片区"为单位',
   /function gotoCardPage\(host, page\)/.test(script)
   && /function toggleAllCards\(host\)/.test(script)
   && /buildPager\(pg, host, papers\.length\)/.test(script));
ok('翻页条不再用全局 lastPapers 算总数',
   !/共 <b>' \+\s*lastPapers\.length/.test(script));

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
