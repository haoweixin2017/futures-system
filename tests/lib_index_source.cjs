// 前端源码聚合读取: index.html + frontend/js/*.js (按文件名序) 拼接。
// index.html 的内联脚本已拆分为 js 模块 (scripts/split_index_html.py, 拆分时逐字节搬移),
// 检查脚本以拼接源码做标记切片/正则断言, 行为与拆分前一致; live.html/login.html 未拆分。
const fs = require('node:fs');
const path = require('node:path');

function frontendDir() {
    return path.join(__dirname, '..', 'frontend');
}

function readIndexSource() {
    const dir = frontendDir();
    const parts = [fs.readFileSync(path.join(dir, 'index.html'), 'utf8')];
    const jsDir = path.join(dir, 'js');
    for (const f of fs.readdirSync(jsDir).filter(f => f.endsWith('.js')).sort()) {
        parts.push(fs.readFileSync(path.join(jsDir, f), 'utf8'));
    }
    return parts.join('\n');
}

function frontendJsSources() {
    const jsDir = path.join(frontendDir(), 'js');
    return fs.readdirSync(jsDir).filter(f => f.endsWith('.js')).sort()
        .map(f => fs.readFileSync(path.join(jsDir, f), 'utf8'));
}

module.exports = { readIndexSource, frontendJsSources };
