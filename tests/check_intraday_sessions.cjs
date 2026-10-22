const assert = require('node:assert/strict');
const fs = require('node:fs');
const html = require('./lib_index_source.cjs').readIndexSource();
const code = html.slice(html.indexOf('        function intradaySessions('), html.indexOf('        // 休市两侧在压缩时间轴'));
const intradaySessions = new Function(code + '\nreturn intradaySessions;')();
const bar = bar_time => ({ bar_time });

const normal = [bar('2026-09-11T21:01:00'), bar('2026-09-14T09:06:00')];
const fu = intradaySessions('FU2611', '20260914', normal);
const sc = intradaySessions('SC2611', '20260914', normal);
assert.deepEqual(fu.map(s => s.startLabel), ['21:00', '09:00', '10:30', '13:30']);
assert.equal(new Date(sc[0].end * 1000).toISOString(), '2026-09-12T02:30:00.000Z');

const holidayReopen = [bar('2026-09-28T09:00:00'), bar('2026-09-28T10:30:00')];
const fullDay = intradaySessions('FU2611', '20260928', holidayReopen);
assert.deepEqual(fullDay.map(s => s.startLabel), ['09:00', '10:30', '13:30']);
assert.equal(fullDay[fullDay.length - 1].endLabel, '15:00');
assert.deepEqual(intradaySessions('FU2611', '20260928', [...holidayReopen, bar('2026-09-28T13:31:00')]).map(s => s.startLabel), ['09:00', '10:30', '13:30']);
assert.deepEqual(intradaySessions('AP2701', '20260928', [holidayReopen[0]]).map(s => s.startLabel), ['09:00', '10:30', '13:30']);
assert.deepEqual(intradaySessions('IF2612', '20260928', [bar('2026-09-28T09:31:00')]).map(s => s.startLabel), ['09:30', '13:00']);
assert.deepEqual(intradaySessions('FU2611', '20260928', []).map(s => s.startLabel), ['09:00', '10:30', '13:30']);
console.log('Intraday axis keeps the full day and includes night only when it has bars');

