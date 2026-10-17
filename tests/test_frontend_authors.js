// Verify the frontend's gbtAuthor/apaAuthors match the Python rule for
// OpenAlex's "汉字+拼音" names. Extracts the real functions from index.html
// so we test the shipped code, not a copy.
const fs = require('fs');
const path = require('path');

const html = fs.readFileSync(path.join(__dirname, '..', 'static', 'index.html'), 'utf8');
const script = html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];

function grab(name) {
    const start = script.indexOf('function ' + name + '(');
    if (start < 0) throw new Error('not found: ' + name);
    let depth = 0, i = script.indexOf('{', start);
    for (let j = i; j < script.length; j++) {
        if (script[j] === '{') depth++;
        else if (script[j] === '}') { depth--; if (depth === 0) return script.slice(start, j + 1); }
    }
    throw new Error('unbalanced: ' + name);
}

const src = [grab('gbtAuthor'), grab('gbtAuthors'), grab('apaAuthors')].join('\n');
const factory = new Function(src + '\nreturn {gbtAuthor, gbtAuthors, apaAuthors};');
const { gbtAuthor, gbtAuthors, apaAuthors } = factory();

let pass = 0, fail = 0;
function ok(name, cond, got) {
    if (cond) { pass++; console.log('PASS  ' + name); }
    else { fail++; console.log('FAIL  ' + name + '  -> got: ' + JSON.stringify(got)); }
}

ok('汉字+拼音只留汉字', gbtAuthor('曾军英ZengJunying') === '曾军英', gbtAuthor('曾军英ZengJunying'));
ok('纯汉字原样', gbtAuthor('张三') === '张三', gbtAuthor('张三'));
ok('汉字段内有空格也去掉', gbtAuthor('张 三') === '张三', gbtAuthor('张 三'));
ok('英文名不受影响', gbtAuthor('Ashish Vaswani') === 'VASWANI A', gbtAuthor('Ashish Vaswani'));
ok('单段英文名照旧', gbtAuthor('Vaswani') === 'VASWANI', gbtAuthor('Vaswani'));
ok('连字符名缩成 H B', gbtAuthor('Hong-Bing Zhang') === 'ZHANG H B', gbtAuthor('Hong-Bing Zhang'));
ok('空值返回空', gbtAuthor('') === '', gbtAuthor(''));

ok('三作者用「等」', gbtAuthors(['曾军英ZengJunying', '邓森耀DengSenyao',
    '秦传波QinChuanbo', '翟懿奎ZhaiYikui']) === '曾军英, 邓森耀, 秦传波, 等',
    gbtAuthors(['曾军英ZengJunying', '邓森耀DengSenyao', '秦传波QinChuanbo', '翟懿奎ZhaiYikui']));
ok('两作者全列', gbtAuthors(['曾军英ZengJunying', '邓森耀DengSenyao']) === '曾军英, 邓森耀',
    gbtAuthors(['曾军英ZengJunying', '邓森耀DengSenyao']));
ok('无作者用佚名', gbtAuthors([]) === '佚名', gbtAuthors([]));

ok('APA 也去掉拼音', apaAuthors(['曾军英ZengJunying']) === '曾军英',
    apaAuthors(['曾军英ZengJunying']));

console.log('\n' + pass + '/' + (pass + fail) + ' checks passed');
process.exit(fail ? 1 : 0);
