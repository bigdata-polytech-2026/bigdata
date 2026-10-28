'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const {classify} = require('../analysis/version_constraints/classifier.cjs');

test('classifies the principal npm constraint families', () => {
  const cases = new Map([
    ['1.2.3', 'exact'], ['=1.2.3', 'exact'], ['^1.2.3', 'caret'], ['^0.2', 'caret'],
    ['~1.2.3', 'tilde'], ['~1.2', 'tilde'], ['1', 'wildcard'], ['1.2', 'wildcard'],
    ['1.x', 'wildcard'], ['*', 'wildcard'], ['>=1.0.0 <2.0.0', 'range'],
    ['1.0.0 - 2.0.0', 'range'], ['^1.0.0 || ~2.0.0', 'range'],
  ]);
  for (const [input, expected] of cases) assert.equal(classify(input).category, expected, input);
});

test('separates prerelease, unsupported, and invalid requirements', () => {
  assert.equal(classify('^1.2.3-beta.1').category, 'prerelease');
  assert.equal(classify('latest').category, 'unsupported');
  assert.equal(classify('npm:other@^1').category, 'unsupported');
  assert.equal(classify('https://example.test/a.tgz').category, 'unsupported');
  assert.equal(classify('definitely not a range !!!').category, 'invalid');
});
