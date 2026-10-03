const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// Load the actual page functions without starting page rendering or timers.
const source = fs.readFileSync(path.join(__dirname, '../docs/assets/app.js'), 'utf8');
const context = vm.createContext({ Intl, Date, Number });
vm.runInContext(source.slice(0, source.indexOf('const page = document.body.dataset.page;')), context);
const now = new Date('2026-10-03T04:00:00Z');
const fresh = '2026-10-03T00:00:00Z';
const status = {
  generated_at: fresh, outcome: 'success', email: { status: 'ok', folders: [] },
  counts: { items_in_window: 3, recommended_today: 2 },
};
const health = (changes = {}, timestamp = fresh, at = now) => context.updateHealth({ ...status, ...changes }, timestamp, at);

// Optional enrichment errors must not imply that the literature update failed.
const supplement = health({ outcome: 'partial', abstracts: { errors: 2, metadata_fallback: { errors: 1 } } });
assert.equal(supplement.label, '更新已完成');
assert.equal(supplement.notice, '');
assert.equal(supplement.identified, 3);
assert.equal(supplement.recommended, 2);

const parser = health({ email: { status: 'ok', parser_errors: 1 } });
assert.equal(parser.label, '更新不完整');
assert.match(parser.notice, /1 封邮件解析失败/);
const reading = health({ email: { status: 'partial', read_errors: 1, folders: [{ name: 'INBOX', status: 'partial' }] } });
assert.equal(reading.failedFolders.length, 1);
assert.equal(reading.state, 'partial');

const zero = health({ counts: { items_in_window: 0, recommended_today: 0 }, email: { status: 'ok', unrecognized: 3 } });
assert.equal(zero.state, 'success');
assert.equal(zero.description, '本次没有发现文献。');
assert.equal(zero.notice, '');

// The threshold uses the actual content date, including after a failed attempt.
const boundary = '2026-10-01T16:00:00Z';
assert.equal(health({}, boundary).delayed, false);
assert.equal(health({}, boundary, new Date(now.getTime() + 1)).delayed, true);
assert.equal(health({ freshness_max_age_hours: 48 }, boundary).delayed, false);
const old = '2026-09-30T20:25:10Z';
assert.equal(health({}, old).label, '尚未更新');
const failure = health({ outcome: 'stale', email: { status: 'error' } }, old);
assert.equal(failure.label, '本次更新失败');
assert.ok(failure.notice.includes(context.formatUpdatedAt(old)));
assert.equal(health({ outcome: 'stale', email: { status: 'offline' } }).label, '暂未更新');
assert.equal(health({ outcome: 'pending', generated_at: '' }, '').label, '等待首次更新');
console.log('Status UI checks passed');
