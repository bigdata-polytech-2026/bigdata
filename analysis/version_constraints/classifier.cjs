'use strict';

const readline = require('node:readline');
const semver = require('../../processing/technical_lag/node_modules/semver');

const OPTIONS = Object.freeze({loose: false, includePrerelease: false});

function unsupportedSubtype(value) {
  if (/^(?:npm|file|link|workspace):/i.test(value)) return 'alias_or_local';
  if (/^(?:https?|git(?:\+\w+)?|ssh):/i.test(value)) return 'url_or_git';
  if (/^[a-z][a-z0-9._-]*$/i.test(value)) return 'tag';
  return 'other_non_registry';
}

function classify(input) {
  if (typeof input !== 'string') return {category: 'invalid', subtype: 'non_string'};
  const value = input.trim();
  if (/^[a-z][a-z0-9+.-]*:/i.test(value) || /[/\\#]/.test(value) || value.startsWith('.')) {
    return {category: 'unsupported', subtype: unsupportedSubtype(value)};
  }
  try {
    new semver.Range(value, OPTIONS);
  } catch {
    return /^[a-z][a-z0-9._-]*$/i.test(value)
      ? {category: 'unsupported', subtype: 'tag'}
      : {category: 'invalid', subtype: 'invalid_semver'};
  }
  for (const token of value.split(/[\s<>=~^|]+/)) {
    const parsed = semver.parse(token, OPTIONS);
    if (parsed && parsed.prerelease.length) return {category: 'prerelease', subtype: 'explicit_prerelease'};
  }
  if (/^=?\s*v?\d+\.\d+\.\d+(?:\+[0-9A-Za-z.-]+)?$/.test(value)) {
    return {category: 'exact', subtype: value.startsWith('=') ? 'equals' : 'bare'};
  }
  if (/^\^\s*v?\d+(?:\.\d+){0,2}$/.test(value)) return {category: 'caret', subtype: 'caret'};
  if (/^~\s*v?\d+(?:\.\d+){0,2}$/.test(value)) return {category: 'tilde', subtype: 'tilde'};
  if (value === '' || /^(?:[v=]?\s*)?(?:\*|x|X|\d+)(?:\.(?:\*|x|X|\d+))?(?:\.(?:\*|x|X|\d+))?$/.test(value)) {
    return {category: 'wildcard', subtype: /[xX*]/.test(value) || value === '' ? 'wildcard' : 'shorthand'};
  }
  let subtype = 'comparators';
  if (value.includes('||')) subtype = 'union';
  else if (/\s+-\s+/.test(value)) subtype = 'hyphen';
  else if (/^[<>]=?/.test(value)) subtype = 'comparator';
  else if (/\s/.test(value)) subtype = 'mixed';
  return {category: 'range', subtype};
}

if (require.main === module) {
  const input = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
  input.on('line', line => {
    try {
      const requirement = JSON.parse(line);
      const result = classify(requirement);
      process.stdout.write(`${JSON.stringify({requirement, ...result})}\n`);
    } catch (error) {
      process.stderr.write(`${error.stack || error}\n`);
      process.exitCode = 1;
    }
  });
}

module.exports = {classify};
