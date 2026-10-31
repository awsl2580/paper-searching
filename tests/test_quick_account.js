// 一键生成账号：随机用户名 + 随机密码 → 直接建号并登录。
//
// 覆盖的真实风险：
//   A) 随机密码不满足后端的校验规则（8-16 位、大小写 + 数字）—— 点一下必然失败，
//      而用户根本没机会改，功能等于坏的；
//   B) 生成的名字里带 0/o/1/l 这类字符 —— 抄不下来、登录不上（用户要抄它）；
//   C) 撞名后不重试 —— 概率虽小，但一旦发生就是"点了没反应"；
//   D) 建完不把账号密码显示出来 —— 退出登录/换设备就永久丢了；
//   E) 绕过既有注册流程自己写一套 —— 记住我、本机账号列表、昵称校验全都会漏。
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const scriptBlocks = [...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)];
const script = scriptBlocks.reduce((a, b) => (b[1].length > a[1].length ? b : a))[1];

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

// ── 1. 真实跑一遍两个生成函数 ──────────────────────────────────
const generators = new Function(
    grabFn('randomAccountName') + '\n' + grabFn('randomAccountPassword') +
    '\nreturn { randomAccountName, randomAccountPassword };'
)();

const USER_RE = /^user_[a-z0-9]{6}$/;
// 与 handleAuth 里的注册校验、以及后端 /api/register 的规则一致
const PASS_RE = /^(?=.*[a-z])(?=.*[A-Z])(?=.*\d).{8,16}$/;

const names = new Set();
const passes = new Set();
let badName = null, badPass = null, confusing = null;
for (let i = 0; i < 400; i++) {
    const n = generators.randomAccountName();
    const p = generators.randomAccountPassword();
    names.add(n);
    passes.add(p);
    if (!USER_RE.test(n) && !badName) badName = n;
    if (!PASS_RE.test(p) && !badPass) badPass = p;
    // 用户名和密码都不该出现容易看错的字符（0/1/o/l/I/O）
    if (/[01olIO]/.test(n) || /[01olIO]/.test(p)) confusing = n + ' / ' + p;
}

ok('随机用户名符合 user_+6 位规则（400 次全过）', badName === null, badName);
ok('随机密码满足后端规则（8-16 位、大小写 + 数字，400 次全过）', badPass === null, badPass);
ok('不含 0/1/o/l/I 这类抄错就要命的字符', confusing === null, confusing);
ok('用户名足够随机（400 次至少 380 个不同）', names.size >= 380, names.size);
ok('密码足够随机（400 次至少 395 个不同）', passes.size >= 395, passes.size);
ok('密码长度稳定', [...passes].every((p) => p.length === 12),
   [...passes].slice(0, 3).join(','));

// ── 2. 接线：按钮、流程、凭证卡片 ──────────────────────────────
ok('登录页有一键生成账号按钮',
   /id="quickAccountBtn"/.test(html) && /onclick="quickCreateAccount\(\)"/.test(html));
ok('按钮在登录页表单里（不是浮在外面的）',
   html.indexOf('id="quickAccountBtn"') > html.indexOf('id="authSubmitBtn"')
   && html.indexOf('id="quickAccountBtn"') < html.indexOf('onclick="skipAuth()"'));
ok('走的是既有注册流程（切到注册模式 + 复用 handleAuth）',
   /function quickCreateAccount\(\)[\s\S]{0,1400}switchAuthMode\('register'\)[\s\S]{0,400}await handleAuth\(\)/.test(script));
ok('建号用的还是 /api/register（没有另写一套注册接口）',
   !/fetch\('\/api\/quick/.test(script)
   && /const endpoint = isRegisterMode \? '\/api\/register' : '\/api\/login'/.test(script));
ok('撞名会换个名字重试',
   /for \(let attempt = 0; attempt < 3[\s\S]{0,900}已存在/.test(script));
ok('其它错误不瞎重试（只对"已存在"重来）',
   /if \(!\/已存在\|已被\|exist\|占用\/i\.test\(err\)\) break;/.test(script));
ok('提交期间按钮禁用，防连点',
   /btn\.disabled = true;[\s\S]{0,200}正在创建/.test(script));
ok('建完弹出用户名/密码卡片',
   /function showQuickCredentials\(username, password\)/.test(script)
   && /id="quickAccountModal"/.test(html)
   && /id="qaUser"/.test(html) && /id="qaPass"/.test(html));
ok('卡片能一键复制、也能关掉',
   /function copyQuickCredentials\(\)/.test(script) && /function closeQuickCredentials\(\)/.test(script)
   && /navigator\.clipboard\.writeText/.test(script));
ok('卡片里提醒"退出/换设备要用它"', /退出登录或换设备时要用它/.test(html));

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
