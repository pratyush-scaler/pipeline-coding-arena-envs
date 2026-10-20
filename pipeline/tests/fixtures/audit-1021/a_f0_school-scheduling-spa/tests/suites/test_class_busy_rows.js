/* a class is in a lesson for as long as that lesson really runs that day */
const test = require('node:test');
const assert = require('node:assert');
const { fresh, add, lessons, span, codes, req } = require('./data/harness.js');

test('the lesson already on that class day holds its period', () => {
  const st = fresh();
  const n = lessons(st, 'H-B2', 'Monday').length;
  const r = add(st, req({ classId: 'H-B2', day: 'Monday', period: 3 }));
  assert.strictEqual(r.ok, false, 'that class is in a lesson for the whole of that period');
  assert.ok(codes(r).includes('class-busy'), 'codes=' + codes(r).join(','));
  assert.strictEqual(lessons(st, 'H-B2', 'Monday').length, n, 'a refused lesson must not be stored');
});

test('a double running on into it is refused too', () => {
  const st = fresh();
  const n = lessons(st, 'H-B2', 'Monday').length;
  const r = add(st, req({ classId: 'H-B2', day: 'Monday', period: 2, double: true }));
  assert.strictEqual(r.ok, false, 'the second half of that double runs while the class is already busy');
  assert.ok(codes(r).includes('class-busy'), 'codes=' + codes(r).join(','));
  assert.strictEqual(lessons(st, 'H-B2', 'Monday').length, n, 'a refused lesson must not be stored');
});

test('the time that class is not in that lesson is free', () => {
  let st = fresh();
  let r = add(st, req({ classId: 'H-B2', day: 'Monday', period: 2 }));
  assert.strictEqual(r.ok, true, 'the class is free before it, codes=' + codes(r).join(','));
  assert.strictEqual(span(r.lesson), '09:30-10:20');

  st = fresh();
  r = add(st, req({ classId: 'H-B2', day: 'Tuesday', period: 3 }));
  assert.strictEqual(r.ok, true, 'that lesson is on the Monday, codes=' + codes(r).join(','));
  assert.strictEqual(span(r.lesson), '10:20-11:10');
});
