/* who the teacher is comes from the chinese part of the written name */
const test = require('node:test');
const assert = require('node:assert');
const { fresh, add, lessons, blocks, span, codes, shape, req } = require('./data/harness.js');
const LIUYAN = '\u5218\u4dae Liu Yan';
const SUNLI = '\u5b59\u4e3d Sun Li';
const SUNLI_KEY = '\u5b59\u4e3d';

test('the teacher named by the chinese characters is the one who is busy', () => {
  const st = fresh();
  const n = lessons(st, 'H-A2', 'Friday').length;
  const r = add(st, req({ classId: 'H-A2', day: 'Friday', period: 2, teacher: LIUYAN }));
  assert.strictEqual(r.ok, false, 'that teacher already has a lesson at 09:25');
  assert.ok(codes(r).includes('teacher-busy'), 'codes=' + codes(r).join(','));
  assert.strictEqual(lessons(st, 'H-A2', 'Friday').length, n, 'a refused lesson must not be stored');
});

test('his own imported week belongs to him too', () => {
  const st = fresh();
  const n = lessons(st, 'H-A2', 'Thursday').length;
  const r = add(st, req({ classId: 'H-A2', day: 'Thursday', period: 2, teacher: LIUYAN }));
  assert.strictEqual(r.ok, false, 'he is on lab duty 09:25-10:05 that day');
  assert.ok(codes(r).includes('teacher-busy'), 'codes=' + codes(r).join(','));
  assert.strictEqual(lessons(st, 'H-A2', 'Thursday').length, n, 'a refused lesson must not be stored');
});

test('two teachers with different chinese names are two people', () => {
  const st = fresh();
  const r = add(st, req({ classId: 'H-A2', day: 'Friday', period: 3, teacher: '\u674e\u5efa Li Jian' }));
  assert.strictEqual(r.ok, true, 'a different teacher is free then, codes=' + codes(r).join(','));
  assert.strictEqual(span(r.lesson), '10:15-10:55');
  assert.strictEqual(blocks(st, '\u674e\u5efa\u56fd', 'Friday', 'all').length, 0, 'the other teacher week must stay empty');
});

test('the imported week is hers however the export spells her out', () => {
  let st = fresh();
  const n = lessons(st, 'H-A2', 'Tuesday').length;
  let r = add(st, req({ classId: 'H-A2', day: 'Tuesday', period: 2, teacher: SUNLI }));
  assert.strictEqual(r.ok, false, 'she is on library duty 09:25-10:05');
  assert.ok(codes(r).includes('teacher-busy'), 'codes=' + codes(r).join(','));
  assert.strictEqual(lessons(st, 'H-A2', 'Tuesday').length, n, 'a refused lesson must not be stored');

  st = fresh();
  r = add(st, req({ classId: 'H-A2', day: 'Tuesday', period: 6, teacher: SUNLI }));
  assert.strictEqual(r.ok, true, 'she is free in the afternoon, codes=' + codes(r).join(','));
  const tue = blocks(st, SUNLI_KEY, 'Tuesday', 'all').map(shape);
  assert.ok(tue.includes('09:25-10:05'), 'her duty must still be there, her Tuesday reads ' + tue.join(' | '));
  assert.ok(tue.length > 1, 'her own week should hold the new lesson too, her Tuesday reads ' + tue.join(' | '));
});
