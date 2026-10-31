// 论文卡片里划词 → 浮出「解释」/ 右键直接解释（名词解释的第二个入口）。
//
// 这条链路全是 DOM 事件，没有浏览器就只验：接线在不在、选区判定对不对、
// 上下文怎么取、面板怎么被带起来。真机效果由用户刷新页面确认。
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const _blocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
const script = _blocks.reduce((a, b) => (b[1].length > a[1].length ? b : a))[1];
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

// ── 1. 事件接线 ───────────────────────────────────────────────
ok('划词后浮出的按钮由脚本懒创建（#termQuickBtn）',
   /function ensureTermQuickBtn\(\)/.test(script) && /id = 'termQuickBtn'/.test(script));
ok('按钮样式存在（.term-quick，fixed 定位）',
   /\.term-quick \{[\s\S]{0,200}position: fixed/.test(css));
ok('mouseup 时判定选区是不是在论文卡片里',
   /addEventListener\('mouseup'[\s\S]{0,700}selectedTextInPaperCard\(\)/.test(script));
ok('右键：卡片内有选区才接管，否则保留系统菜单',
   /addEventListener\('contextmenu'[\s\S]{0,300}selectedTextInPaperCard\(\)[\s\S]{0,200}if \(!hit\) return;[\s\S]{0,200}preventDefault\(\)/
     .test(script));
ok('点别处 / 滚动 / 改窗口会收起浮层',
   /function hideTermQuick\(\)/.test(script)
   && /addEventListener\('scroll', hideTermQuick, true\)/.test(script)
   && /addEventListener\('resize', hideTermQuick\)/.test(script));

// ── 2. 接到名词解释面板 ───────────────────────────────────────
ok('选中词会填进输入框与上下文框',
   /function explainSelection\(text, context\)[\s\S]{0,700}inputEl\.value = term[\s\S]{0,300}ctxEl\.value = String\(context \|\| ''\)\.slice\(0, 1200\)/
     .test(script));
ok('面板没开时走统一的功能打开流程（保留首次的显示方式询问）',
   /function explainSelection\(text, context\)[\s\S]{0,1200}toggleFeaturePanel\('termPanel'\)/.test(script));
ok('面板一开就自动解释一次（_termPendingAuto）',
   /_termPendingAuto = true/.test(script)
   && /if \(_termPendingAuto\) \{[\s\S]{0,160}explainTerm\(\)/.test(script));
ok('面板已经开着时直接解释，不重复打开',
   /function explainSelection[\s\S]{0,900}isSidePanelOpen\('termPanel'\)[\s\S]{0,200}explainTerm\(\)/.test(script));
ok('选区过长不当作一个术语（上限 60 字）',
   /const TERM_SEL_MAX = 60/.test(script)
   && /text\.length > TERM_SEL_MAX/.test(script));

// ── 3. 行为：选区判定 / 上下文截取 ───────────────────────────
const fakeCard = {
    querySelector: function (sel) {
        if (sel === '.pc-title') return { textContent: '1. Graph Neural Networks for RS' };
        if (sel === '.pc-abstract') return { textContent: '这是一段摘要。'.repeat(300) };
        return null;
    },
};
const insideNode = { nodeType: 1, closest: (s) => (s === '.paper-card' ? fakeCard : null) };
const outsideNode = { nodeType: 1, closest: () => null };

function makeApi(text, node) {
    const src = [
        'const TERM_SEL_MAX = 60;',
        grabFn('selectedTextInPaperCard'),
        grabFn('cardContextSnippet'),
        'return {selectedTextInPaperCard, cardContextSnippet};',
    ].join('\n');
    const win = {
        getSelection: () => ({
            isCollapsed: false,
            rangeCount: 1,
            anchorNode: node,
            toString: () => text,
        }),
    };
    return new Function('window', src)(win);
}

let api = makeApi('对比学习', insideNode);
const hit = api.selectedTextInPaperCard();
ok('卡片内的选区能被识别', hit && hit.text === '对比学习' && hit.card === fakeCard, hit && hit.text);

api = makeApi('对比学习', outsideNode);
ok('卡片外的选区不触发', api.selectedTextInPaperCard() === null);

api = makeApi('这个词'.repeat(40), insideNode);
ok('超长选区不触发（不当成术语）', api.selectedTextInPaperCard() === null);

api = makeApi('对比学习', insideNode);
const ctx = api.cardContextSnippet(fakeCard);
ok('上下文带论文标题（去掉序号）', ctx.indexOf('论文《Graph Neural Networks for RS》') === 0, ctx.slice(0, 40));
ok('上下文带摘要', ctx.indexOf('摘要：') > 0);
ok('上下文被截到 1200 字以内', ctx.length <= 1200, ctx.length);

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
