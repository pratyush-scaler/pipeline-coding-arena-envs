/* a class cannot be in two lessons at the same time */
const test = require('node:test');
const assert = require('node:assert');
const { fresh, add, lessons, span, codes, req } = require('./data/harness.js');

test('a period already covered by that class is refused', () => {
  const st = fresh();
  const n = lessons(st, 'H-A2', 'Thursday').length;
  const r = add(st, req({ classId: 'H-A2', day: 'Thursday', period: 4 }));
  assert.strictEqual(r.ok, false, 'that class is already in a lesson at 11:05');
  assert.ok(codes(r).includes('class-busy'), 'codes=' + codes(r).join(','));
  assert.strictEqual(lessons(st, 'H-A2', 'Thursday').length, n, 'a refused lesson must not be stored');
});

test('free time of that class and the same period of another class are fine', () => {
  let st = fresh();
  let r = add(st, req({ classId: 'H-A2', day: 'Thursday', period: 5 }));
  assert.strictEqual(r.ok, true, 'that class is free at 13:30, codes=' + codes(r).join(','));
  assert.strictEqual(span(r.lesson), '13:30-14:10');

  st = fresh();
  r = add(st, req({ classId: 'H-A1', day: 'Thursday', period: 4 }));
  assert.strictEqual(r.ok, true, 'another class is free then, codes=' + codes(r).join(','));
});
