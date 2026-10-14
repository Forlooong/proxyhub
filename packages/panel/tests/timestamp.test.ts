import assert from 'node:assert/strict';
import test from 'node:test';
import { resolveTimestamp, timestampView } from '../src/timestamp.ts';

test('unchanged timestamps retain original seconds and zero', () => {
  for (const seconds of [0, 1700000001, 1700000021, 1791953999]) {
    assert.equal(resolveTimestamp(seconds, '', false, '时间'), seconds);
    const view = timestampView(seconds);
    assert.equal(view.unsafe, false);
    assert.match(view.value, /:\d{2}$/);
    assert.equal(resolveTimestamp(seconds, view.value, true, '时间'), seconds);
  }
});
test('timestamps outside the calendar range remain intact without throwing', () => {
  const seconds = 8640000000001;
  const view = timestampView(seconds);
  assert.equal(view.value, '');
  assert.equal(view.unsafe, false);
  assert.ok(view.warning);
  assert.equal(resolveTimestamp(seconds, '', false, '时间'), seconds);
});
test('unsafe JSON integers require an explicit replacement or clearing', () => {
  const seconds = Number(2n ** 63n - 1n);
  assert.equal(timestampView(seconds).unsafe, true);
  assert.throws(() => resolveTimestamp(seconds, '', false, '时间'), /无法精确处理/);
  assert.equal(resolveTimestamp(seconds, '', true, '时间'), null);
  const replacement = timestampView(1700000001).value;
  assert.equal(resolveTimestamp(seconds, replacement, true, '时间'), 1700000001);
});
test('absent values and explicit clearing stay distinct from unedited values', () => {
  assert.equal(timestampView(undefined).value, '');
  assert.equal(timestampView(null).value, '');
  assert.equal(resolveTimestamp(null, '', false, '时间'), null);
  assert.equal(resolveTimestamp(undefined, '', false, '时间'), null);
  assert.equal(resolveTimestamp(1700000001, '', true, '时间'), null);
});
test('invalid dates and fractional second input are rejected without normalization', () => {
  for (const value of ['2026-02-30T12:00:00', '2026-01-01T25:00:00', '2026-01-01T00:00:00.001', 'not-a-date']) {
    assert.throws(() => resolveTimestamp(null, value, true, '时间'));
  }
});
