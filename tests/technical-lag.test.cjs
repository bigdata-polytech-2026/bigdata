'use strict';

const {test} = require('node:test');
const assert = require('node:assert/strict');
const {calculateLag, prepareDependencyHistory, summarizeResults, parseTimestampNs}
  = require('../processing/technical_lag/core.cjs');

const PROVENANCE = {snapshot_id: 'f14b28f7-663d-4bb5-b191-12e2b4215bd0',
  request_url: 'https://registry.npmjs.org/lib', retrieved_at: '2026-04-01T00:00:00Z',
  raw_path: 'raw/npm/example.json', raw_sha256: 'a'.repeat(64), parser_version: 'fixture@1'};

function fixture(requirement = '*') {
  return {
    consumer: {package_name: 'app', version: '1.0.0', published_at: '2026-03-01T00:00:00Z',
      provenance: {...PROVENANCE, request_url: 'https://registry.npmjs.org/app'}},
    dependency: {package_name: 'lib', provenance: {...PROVENANCE}, versions: [
      ['1.0.0', '2026-01-01'], ['1.1.0', '2026-01-11'], ['1.1.1', '2026-01-21'],
      ['2.0.0', '2026-02-01'], ['2.1.0', '2026-02-11'], ['2.1.1', '2026-02-21'],
    ].map(([version, day]) => ({version, published_at: `${day}T00:00:00Z`}))},
    requirement, dependency_type: 'dependencies',
  };
}

function excluded(input, reason) {
  const result = calculateLag(input);
  assert.equal(result.status, 'excluded');
  assert.equal(result.exclusion_reason, reason);
  for (const key of ['v_req', 'v_latest', 'available_versions', 'version_lag', 'time_lag_days',
    'time_lag_nanoseconds', 'update_type', 'nonmonotone_publish_order']) assert.equal(result[key], null, key);
  return result;
}

for (const [requirement, required, versionLag, timeLag, update] of [
  ['1.0.0', '1.0.0', 5, 51, 'major'],
  ['^1.0.0', '1.1.1', 3, 31, 'major'],
  ['~1.1.0', '1.1.1', 3, 31, 'major'],
  ['>=1.0.0 <2.1.0', '2.0.0', 2, 20, 'minor'],
  ['*', '2.1.1', 0, 0, 'none'],
  ['2.1.0', '2.1.0', 1, 10, 'patch'],
]) {
  test(`A1 documented example: ${requirement}`, () => {
    const result = calculateLag(fixture(requirement));
    assert.equal(result.status, 'ok');
    assert.equal(result.exclusion_reason, null);
    assert.equal(result.v_req, required);
    assert.equal(result.v_latest, '2.1.1');
    assert.equal(result.version_lag, versionLag);
    assert.equal(result.time_lag_days, timeLag);
    assert.equal(result.update_type, update);
    assert.equal(result.nonmonotone_publish_order, false);
  });
}

for (const [requirement, expected] of [
  ['=1.1.0', '1.1.0'], ['~1.0.0', '1.0.0'], ['1.1.x', '1.1.1'], ['1.x', '1.1.1'],
  ['1', '1.1.1'], ['1.1', '1.1.1'], ['1.0.0 - 2.0.0', '2.0.0'],
  ['^1.0.0 || =2.1.0', '2.1.0'], ['>=1.1.0 <=2.1.0', '2.1.0'],
  ['>1.0.0 <2.0.0', '1.1.1'], ['', '2.1.1'], ['  ^1.0.0  ', '1.1.1'], ['x', '2.1.1'],
]) {
  test(`range supported: ${JSON.stringify(requirement)}`, () => {
    const result = calculateLag(fixture(requirement));
    assert.equal(result.status, 'ok');
    assert.equal(result.v_req, expected);
    assert.equal(result.requirement, requirement); // Preserve the actual declaration.
  });
}

test('caret special cases for 0.x and 0.0.x use node-semver', () => {
  const input = fixture('^0.2.3');
  input.dependency.versions = ['0.0.1', '0.0.2', '0.2.3', '0.2.5', '0.3.0'].map(version =>
    ({version, published_at: '2026-01-01T00:00:00Z'}));
  const result = calculateLag(input);
  assert.equal(result.v_req, '0.2.5');
  assert.equal(result.version_lag, 1);
  assert.equal(result.update_type, 'minor');
  assert.equal(calculateLag({...input, requirement: '^0.0.1'}).v_req, '0.0.1');
});

test('release at t is included; future release is excluded', () => {
  const input = fixture('*');
  input.consumer.published_at = '2026-02-11T00:00:00Z';
  const result = calculateLag(input);
  assert.equal(result.v_latest, '2.1.0');
  assert.equal(result.available_versions, 5);
  excluded({...input, requirement: '2.1.1'}, 'no_satisfying_version_at_t');
});

test('SemVer order, not publication order, determines the newest version', () => {
  const input = fixture('1.0.0');
  input.dependency.versions = [
    {version: '2.0.0', published_at: '2026-01-01T00:00:00Z'},
    {version: '1.0.0', published_at: '2026-02-01T00:00:00Z'},
  ];
  const result = calculateLag(input);
  assert.equal(result.v_latest, '2.0.0');
  assert.equal(result.time_lag_days, -31);
  assert.equal(result.nonmonotone_publish_order, true);
  assert.equal(result.version_lag, 1);
});

test('version lag counts releases rather than numeric gaps', () => {
  const input = fixture('1.0.0');
  input.dependency.versions = ['1.0.0', '1.0.99', '7.0.0'].map(version =>
    ({version, published_at: '2026-01-01T00:00:00Z'}));
  const result = calculateLag(input);
  assert.equal(result.version_lag, 2);
  assert.equal(result.time_lag_days, 0);
  assert.equal(result.update_type, 'major');
});

test('fractional days are retained', () => {
  const input = fixture('1.0.0');
  input.dependency.versions = [
    {version: '1.0.0', published_at: '2026-01-01T00:00:00Z'},
    {version: '1.0.1', published_at: '2026-01-01T12:00:00Z'},
  ];
  assert.equal(calculateLag(input).time_lag_days, 0.5);
});

test('timestamp parsing retains nanoseconds and normalizes offsets for comparisons', () => {
  const utc = '2026-01-01T00:00:00.123456789Z';
  assert.equal(parseTimestampNs(utc), parseTimestampNs('2026-01-01T03:00:00.123456789+03:00'));
  assert.equal(parseTimestampNs('2026-01-01T00:00:00.123456790Z') - parseTimestampNs(utc), 1n);
  for (const invalid of [null, '', '2026-02-30T00:00:00Z', '2026-01-01T24:00:00Z',
    '2026-01-01', '2026-01-01T00:00:00', '2026-01-01T00:00:00+24:00',
    '2026-01-01T00:00:00.1234567890Z']) assert.equal(parseTimestampNs(invalid), null);
});

test('one-nanosecond publication boundary is not rounded away', () => {
  const input = fixture('1.0.0');
  input.consumer.published_at = '2026-01-01T00:00:00.000000001Z';
  input.dependency.versions = [
    {version: '1.0.0', published_at: input.consumer.published_at},
    {version: '1.0.1', published_at: '2026-01-01T00:00:00.000000002Z'},
  ];
  assert.equal(calculateLag(input).v_latest, '1.0.0');
  const later = calculateLag({...input, observation_time: '2026-01-01T00:00:00.000000002Z'});
  assert.equal(later.v_latest, '1.0.1');
  assert.equal(later.time_lag_nanoseconds, '1');
  assert.equal(later.time_lag_days, 1 / 86400_000_000_000);
});

for (const version of ['not-semver', '01.0.0', '1.0.0-rc.1']) {
  test(`exclude consumer version ${version}`, () => {
    const input = fixture();
    input.consumer.version = version;
    excluded(input, 'invalid_consumer_version_or_time');
  });
}

test('missing or invalid consumer time is excluded, never replaced by retrieval time', () => {
  const input = fixture();
  for (const date of [null, '2026-02-30T00:00:00Z']) {
    excluded({...input, consumer: {...input.consumer, published_at: date}}, 'invalid_consumer_version_or_time');
  }
});

for (const type of ['devDependencies', 'peerDependencies', 'optionalDependencies']) {
  test(`exclude dependency type ${type}`, () => excluded({...fixture(), dependency_type: type}, 'unsupported_dependency_type'));
}

test('optional override excludes the main declaration but preserves its original type', () => {
  const result = excluded({...fixture(), shadowed_by_optional: true}, 'unsupported_dependency_type');
  assert.equal(result.dependency_type, 'dependencies');
  assert.equal(result.effective_dependency_type, 'optionalDependencies');
});

for (const requirement of ['latest', 'next', 'npm:lib@^1.0.0', 'git+https://example.test/lib',
  'file:../lib', 'https://example.test/lib.tgz', 'user/lib#v1', 'workspace:*']) {
  test(`unsupported specification ${requirement}`, () => excluded(fixture(requirement), 'unsupported_requirement'));
}

for (const requirement of ['^1.0.0-beta.1', '>=1.0.0-rc.1 <2.0.0', '1.0.0-alpha - 2.0.0']) {
  test(`explicit prerelease requirement ${requirement}`, () => excluded(fixture(requirement), 'prerelease_requirement'));
}

for (const requirement of ['>=not-a-version <2', '01.0.0', '^1.0.0-', '1.2.3.4']) {
  test(`invalid range ${requirement}`, () => excluded(fixture(requirement), 'invalid_requirement'));
}

test('build metadata containing a hyphen is not a prerelease', () => {
  const input = fixture('=1.0.0+build-alpha');
  input.dependency.versions = [{version: '1.0.0+build-alpha', published_at: '2026-01-01T00:00:00Z'}];
  assert.equal(calculateLag(input).v_req, '1.0.0+build-alpha');
});

test('missing dependency snapshot has its own exclusion reason', () => {
  excluded({...fixture(), dependency: null}, 'missing_dependency_snapshot');
});

test('either snapshot retrieved before t excludes the row', () => {
  for (const source of ['consumer', 'dependency']) {
    const input = fixture();
    input[source].provenance.retrieved_at = '2026-02-28T23:59:59.999999999Z';
    excluded(input, 'snapshot_before_t');
  }
  const input = fixture();
  input.consumer.provenance.retrieved_at = input.consumer.published_at;
  input.dependency.provenance.retrieved_at = input.consumer.published_at;
  assert.equal(calculateLag(input).status, 'ok');
});

test('a stable release with unknown time invalidates the entire dependency history', () => {
  const input = fixture();
  input.dependency.versions.push({version: '9.0.0', published_at: null});
  const result = excluded(input, 'incomplete_release_history');
  assert.equal(result.dependency_diagnostics.stable_versions_missing_time, 1);
});

test('invalid and prerelease dependency versions are skipped and counted', () => {
  const input = fixture();
  input.dependency.versions.push({version: '01.0.0', published_at: null},
    {version: '9.0.0-rc.1', published_at: null});
  const result = calculateLag(input);
  assert.equal(result.v_latest, '2.1.1');
  assert.equal(result.dependency_diagnostics.invalid_versions, 1);
  assert.equal(result.dependency_diagnostics.prerelease_versions, 1);
});

test('no stable versions available at t differs from no satisfying version', () => {
  const input = fixture();
  input.consumer.published_at = '2025-01-01T00:00:00Z';
  excluded(input, 'no_available_stable_version_at_t');
  excluded({...fixture(), dependency: {...fixture().dependency, versions: []}}, 'no_available_stable_version_at_t');
  excluded(fixture('>=3'), 'no_satisfying_version_at_t');
});

test('equal SemVer precedence with different strings is ambiguous only once both are available', () => {
  const input = fixture('1.0.0');
  input.dependency.versions.push({version: '1.0.0+build.1', published_at: '2026-04-01T00:00:00Z'});
  assert.equal(calculateLag(input).status, 'ok');
  input.dependency.versions.at(-1).published_at = '2026-02-01T00:00:00Z';
  excluded(input, 'ambiguous_semver_precedence');
});

test('deprecated releases are retained; dist-tags do not select a version', () => {
  const input = fixture();
  input.dependency.versions.at(-1).is_deprecated = true;
  input.dependency.dist_tags = {latest: '1.0.0'};
  assert.equal(calculateLag(input).v_latest, '2.1.1');
});

test('exclusion priority follows A1', () => {
  const input = fixture('npm:lib@latest');
  input.consumer.version = 'bad';
  input.dependency_type = 'devDependencies';
  input.dependency = null;
  excluded(input, 'invalid_consumer_version_or_time');
  input.consumer.version = '1.0.0';
  excluded(input, 'unsupported_dependency_type');
  input.dependency_type = 'dependencies';
  excluded(input, 'unsupported_requirement');
  input.requirement = '*';
  excluded(input, 'missing_dependency_snapshot');
  const history = fixture();
  history.dependency.provenance.retrieved_at = '2020-01-01T00:00:00Z';
  history.dependency.versions[0].published_at = null;
  excluded(history, 'snapshot_before_t');
});

test('prepared history is reusable, isolated from source mutation and preserves source keys/provenance', () => {
  const input = fixture('^1');
  const before = structuredClone(input);
  const history = prepareDependencyHistory(input.dependency);
  const first = calculateLag({...input, dependency: history});
  assert.deepEqual(input, before);
  assert.deepEqual(first, calculateLag(input));
  assert.equal(first.historical_mode, 'reconstructed_survivors');
  assert.equal(first.dependency_provenance.raw_sha256, PROVENANCE.raw_sha256);
  input.dependency.versions.length = 0;
  assert.deepEqual(first, calculateLag({...input, dependency: history}));
  assert.throws(() => { history.package_name = 'changed'; }, TypeError);
  assert.equal(calculateLag({...input, requirement: '*', dependency: history}).v_req, '2.1.1');
});

test('malformed inputs fail explicitly instead of producing fake zero lag', () => {
  assert.throws(() => calculateLag({...fixture(), requirement: null}), TypeError);
  assert.throws(() => calculateLag({...fixture(), shadowed_by_optional: 'true'}), TypeError);
  const input = fixture();
  delete input.consumer.provenance.retrieved_at;
  assert.throws(() => calculateLag(input), TypeError);
  assert.throws(() => prepareDependencyHistory({versions: []}), TypeError);
  assert.throws(() => calculateLag({...fixture(), observation_time: 'not-a-date'}), TypeError);
  assert.throws(() => calculateLag({...fixture(), observation_time: '2025-01-01T00:00:00Z'}), RangeError);
  const duplicate = fixture();
  duplicate.dependency.versions.push({...duplicate.dependency.versions[0]});
  assert.throws(() => calculateLag(duplicate), /duplicate release identifier/);
});

test('summary consumes an iterable without dropping excluded rows', () => {
  function* results() {
    yield calculateLag(fixture());
    yield calculateLag(fixture('latest'));
    yield calculateLag(fixture('>=9'));
  }
  assert.deepEqual(summarizeResults(results()), {total: 3, successful: 1, unsuccessful: 2,
    exclusion_reasons: {unsupported_requirement: 1, no_satisfying_version_at_t: 1}});
  assert.deepEqual(summarizeResults([]), {total: 0, successful: 0, unsuccessful: 0, exclusion_reasons: {}});
  assert.throws(() => summarizeResults([{status: 'unknown'}]), TypeError);
});
