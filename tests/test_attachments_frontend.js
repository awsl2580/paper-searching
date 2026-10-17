// 前端附件逻辑：📎 选择 / 拖拽 / 粘贴 -> pendingFiles -> 随请求发出。
//
// 为什么要有这一份：后端测试能证明"给了 files 就能处理"，
// 但证明不了"用户选完文件后前端真的会把它带上"。中间这段
// （FileReader 读取、校验、预览、清空时机）才是最容易静默失效的地方。
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const script = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];

let pass = 0, fail = 0;
function ok(name, cond, got) {
    if (cond) { pass++; console.log('PASS  ' + name); }
    else { fail++; console.log('FAIL  ' + name + '  -> got: ' + JSON.stringify(got)); }
}

function grabFn(name) {
    // 注意要把前面的 async 一起抓进来：只从 "function name(" 开始截，
    // 异步函数会丢掉 async 关键字，函数体里的 await 直接变成语法错误
    // （第一版就栽在这：报 "await is only valid in async functions"）。
    const at = script.indexOf('function ' + name + '(');
    if (at < 0) throw new Error('not found: ' + name);
    let start = at;
    const prefix = script.slice(Math.max(0, at - 6), at);
    if (/async\s$/.test(prefix)) start = at - 6;
    let depth = 0;
    for (let j = script.indexOf('{', at); j < script.length; j++) {
        if (script[j] === '{') depth++;
        else if (script[j] === '}') { depth--; if (depth === 0) return script.slice(start, j + 1); }
    }
}

function mkEl() {
    const el = {
        innerHTML: '', style: {}, _cls: new Set(), value: '',
        classList: { add: (c) => el._cls.add(c), remove: (c) => el._cls.delete(c),
                     contains: (c) => el._cls.has(c) },
        querySelector: () => null, appendChild: () => {},
    };
    return el;
}

// ── 静态接线 ────────────────────────────────────────────────
ok('有 📎 附件按钮', /id="attachBtn"/.test(html) && /onclick="pickFiles\(\)"/.test(html));
ok('有隐藏的 file input',
   /id="fileInput"/.test(html) && /type="file"/.test(html));
ok('input 允许多选', /id="fileInput"[^>]*multiple/.test(html));
ok('input 限定可接受的类型（含 PDF）',
   /accept="[^"]*application\/pdf/.test(html) && /accept="[^"]*image\/png/.test(html));
ok('有附件预览条容器', /id="attachBar"/.test(html));
ok('输入框提示里说明了可以粘贴/拖入', /可粘贴截图/.test(html));
ok('pendingFiles 初始为空数组', /let pendingFiles = \[\];/.test(script));
ok('前端限制与后端一致：5 个',
   /ATTACH_MAX_FILES = 5/.test(script));
ok('前端限制与后端一致：12MB（实测 14.7MB 会被静默拒绝）',
   /ATTACH_MAX_BYTES = 12 \* 1024 \* 1024/.test(script));
ok('支持粘贴上传', /addEventListener\('paste'/.test(script));
ok('支持拖拽上传', /initDropZone/.test(script) && /'drop'/.test(script));
ok('拖拽时加了落点高亮', /drop-active/.test(html));
ok('发送时清空附件预览', /clearAttachments\(\);/.test(script));
ok('发送请求里带上 files 字段', /files: filesToSend\.map/.test(script));
ok('没有附件时不带 files 字段（保持请求体不变）',
   /filesToSend\.length \? \{/.test(script));

// ── 行为：用桩把 addFiles 跑起来 ───────────────────────────
async function buildHarness() {
    const bar = mkEl();
    const deps = { bar, feedback: [] };
    const src = [
        // 注入真实的转义函数（而不是自己写一个假的）：
        // 这样"名字里带引号会不会逃出属性"测的才是线上那份逻辑
        grabFn('escapeAttr'),
        grabFn('fileToBase64'),
        grabFn('fileToDataURL'),
        grabFn('addFiles'),
        grabFn('removeAttach'),
        grabFn('clearAttachments'),
        grabFn('renderAttachBar'),
    ].join('\n');

    // 模块级状态由函数体内部声明（不能和形参重名）。
    // 必须用 AsyncFunction：被测的 addFiles 是 async 的、里面有 await，
    // 普通 Function 体在**解析阶段**就会报
    // "await is only valid in async functions"，连函数都构造不出来。
    const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
    const api = await new AsyncFunction(
        'document', 'escapeHtml', 'attachFeedback', 'FileReader',
        'let pendingFiles = [];\n' +
        'const ATTACH_MAX_FILES = 5, ATTACH_MAX_BYTES = 12 * 1024 * 1024;\n' +
        "const ATTACH_ACCEPT = ['image/png','image/jpeg','image/jpg'," +
        "'image/webp','image/gif','application/pdf'];\n" +
        src +
        '\nreturn {addFiles, removeAttach, clearAttachments, renderAttachBar,' +
        ' get pendingFiles(){return pendingFiles;}};'
    )({ getElementById: (id) => (id === 'attachBar' ? bar : null) },
      (s) => String(s == null ? '' : s),
      (t) => deps.feedback.push(t),
      // FileReader 桩：把原始内容当作 base64 返回
      class {
          readAsDataURL(file) {
              this.result = 'data:' + (file.type || '') + ';base64,' + file._b64;
              if (this.onload) this.onload();
          }
      });
    return { api, deps, bar };
}

function fakeFile(name, type, b64, size) {
    return { name, type, _b64: b64, size: size == null ? b64.length : size };
}

(async () => {
    // 正常图片
    {
        const h = await buildHarness();
        await h.api.addFiles([fakeFile('shot.png', 'image/png', 'QUJD')]);
        ok('选入一张 PNG 后进入待发送列表', h.api.pendingFiles.length === 1,
           h.api.pendingFiles.length);
        ok('记录了文件名', h.api.pendingFiles[0].name === 'shot.png');
        ok('记录了 mime', h.api.pendingFiles[0].mime === 'image/png');
        ok('base64 去掉了 data URL 前缀（后端只收纯 base64）',
           h.api.pendingFiles[0].data === 'QUJD', h.api.pendingFiles[0].data);
        ok('图片生成了缩略图预览',
           String(h.api.pendingFiles[0].preview).startsWith('data:'), h.api.pendingFiles[0].preview);
        ok('预览条被显示', h.bar.style.display === 'flex', h.bar.style.display);
        ok('预览条渲染了移除按钮', /attach-remove/.test(h.bar.innerHTML));
    }

    // PDF：不做 <img> 预览（避免把整份 PDF 塞进内存）
    {
        const h = await buildHarness();
        await h.api.addFiles([fakeFile('paper.pdf', 'application/pdf', 'JVBERi0=')]);
        ok('PDF 也能加入', h.api.pendingFiles.length === 1, h.api.pendingFiles.length);
        ok('PDF 的 mime 归一成 application/pdf',
           h.api.pendingFiles[0].mime === 'application/pdf', h.api.pendingFiles[0].mime);
        ok('PDF 不做图片预览（preview 为空）',
           h.api.pendingFiles[0].preview === '', h.api.pendingFiles[0].preview);
        ok('PDF 显示成文档图标', /attach-doc/.test(h.bar.innerHTML), h.bar.innerHTML.slice(0, 120));
    }

    // type 为空的 PDF（某些系统会这样）
    {
        const h = await buildHarness();
        await h.api.addFiles([fakeFile('thesis.PDF', '', 'JVBERi0=')]);
        ok('按扩展名识别出 type 为空的 PDF', h.api.pendingFiles.length === 1,
           h.api.pendingFiles.length);
    }

    // 不支持的格式要拒绝，并且不能进列表
    {
        const h = await buildHarness();
        await h.api.addFiles([fakeFile('notes.txt', 'text/plain', 'aGk=')]);
        ok('不支持的格式不进列表', h.api.pendingFiles.length === 0, h.api.pendingFiles.length);
        ok('并且给出可读提示',
           h.deps.feedback.some((t) => t.includes('格式不支持')), h.deps.feedback);
    }

    // 超大文件要拒绝
    {
        const h = await buildHarness();
        await h.api.addFiles([fakeFile('huge.png', 'image/png', 'x', 13 * 1024 * 1024)]);
        ok('超 12MB 的文件被拒', h.api.pendingFiles.length === 0, h.api.pendingFiles.length);
        ok('提示里说明了大小上限',
           h.deps.feedback.some((t) => t.includes('上限')), h.deps.feedback);
    }

    // 数量上限：第 6 个要被挡住
    {
        const h = await buildHarness();
        const many = [];
        for (let i = 0; i < 7; i++) many.push(fakeFile('f' + i + '.png', 'image/png', 'QQ=='));
        await h.api.addFiles(many);
        ok('最多只收 5 个', h.api.pendingFiles.length === 5, h.api.pendingFiles.length);
        ok('超出时给出提示',
           h.deps.feedback.some((t) => t.includes('最多')), h.deps.feedback);
    }

    // 移除与清空
    {
        const h = await buildHarness();
        await h.api.addFiles([fakeFile('a.png', 'image/png', 'QQ=='),
                              fakeFile('b.png', 'image/png', 'Qg==')]);
        h.api.removeAttach(0);
        ok('移除后剩 1 个', h.api.pendingFiles.length === 1, h.api.pendingFiles.length);
        ok('移除的是指定那个', h.api.pendingFiles[0].name === 'b.png',
           h.api.pendingFiles[0].name);
        h.api.clearAttachments();
        ok('清空后为 0', h.api.pendingFiles.length === 0, h.api.pendingFiles.length);
        ok('清空后预览条隐藏', h.bar.style.display === 'none', h.bar.style.display);
    }

    // XSS：文件名会进 title="..." 属性，必须做**属性**转义
    {
        const h = await buildHarness();
        // 关键载荷是名字里的引号——它能从 title="..." 里逃出来。
        // escapeHtml 只处理 < >，不处理引号，用错就会注入。
        await h.api.addFiles([fakeFile('a" onmouseover="alert(1)" x=".png',
                                       'image/png', 'QQ==')]);
        ok('文件名里的引号被转义（不能从属性逃逸）',
           h.bar.innerHTML.includes('&quot;')
           && !/title="a" onmouseover/.test(h.bar.innerHTML),
           h.bar.innerHTML.slice(0, 170));
        // 注意别把断言写成"页面上不许出现 onmouseover 这个词"——
        // 转义后的形态里它**应该**还在（只是被关在 title 值里），
        // 那样写会误报。要断言的是"引号没原样出现"。
        ok('没有产生原样的 onmouseover 属性',
           !/" onmouseover="/.test(h.bar.innerHTML), h.bar.innerHTML.slice(0, 170));
    }
    {
        const h = await buildHarness();
        await h.api.addFiles([fakeFile('<img src=x onerror=alert(1)>.png',
                                       'image/png', 'QQ==')]);
        ok('文件名里的尖括号被转义',
           !/<img src=x onerror/.test(h.bar.innerHTML), h.bar.innerHTML.slice(0, 160));
    }
    // 消息气泡里的附件也要转义（同一条路径的第二个出口）
    {
        const addMessageSrc = grabFn('addMessage');
        ok('消息附件用 escapeAttr 而不是 escapeHtml',
           /escapeAttr\(f\.name\)/.test(addMessageSrc) &&
           !/title="\$\{escapeHtml\(f\.name\)\}"/.test(addMessageSrc),
           addMessageSrc.slice(0, 200));
        ok('图片 src 也做了属性转义', /src="\$\{escapeAttr\(f\.preview\)\}"/.test(addMessageSrc));
    }
    // 预览条同样
    {
        const barSrc = grabFn('renderAttachBar');
        ok('预览条的 title 用 escapeAttr', /title="\$\{escapeAttr\(f\.name\)\}"/.test(barSrc),
           barSrc.slice(0, 200));
    }

    console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
    process.exit(fail ? 1 : 0);
})();
