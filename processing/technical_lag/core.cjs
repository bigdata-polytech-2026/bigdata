'use strict';

const semver = require('semver');

const OPTIONS = Object.freeze({loose: false, includePrerelease: false});
const DAY_NS = 86400_000_000_000;
const REQUIREMENT_CACHE = new Map();
const REQUIREMENT_CACHE_LIMIT = 10000;

// Date.parse alone truncates fractional seconds and accepts some invalid dates.
// Use it only for validated whole seconds; retain the fractional part as BigInt.
function parseTimestampNs(value) {
  if (typeof value !== 'string') return null;
  const match = /^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|([+-])(\d{2}):(\d{2}))$/.exec(value);
  if (!match) return null;
  const seconds = `${match[1]}T${match[2]}`;
  const milliseconds = Date.parse(`${seconds}Z`);
  if (!Number.isFinite(milliseconds) || new Date(milliseconds).toISOString().slice(0, 19) !== seconds) return null;
  const hours = Number(match[6] || 0);
  const minutes = Number(match[7] || 0);
  if (hours > 23 || minutes > 59) return null;
  const offsetMinutes = (hours * 60 + minutes) * (match[5] === '-' ? -1 : 1);
  return BigInt(milliseconds) * 1_000_000n + BigInt((match[3] || '').padEnd(9, '0'))
    - BigInt(offsetMinutes) * 60_000_000_000n;
}

function parseStableVersion(value) {
  if (typeof value !== 'string') return null;
  const parsed = semver.parse(value, OPTIONS);
  return parsed && parsed.prerelease.length === 0 ? parsed : null;
}

function parseRequirement(value) {
  const requirement = value.trim();
  const cached = REQUIREMENT_CACHE.get(requirement);
  if (cached) return cached;
  const remember = result => {
    if (REQUIREMENT_CACHE.size >= REQUIREMENT_CACHE_LIMIT) REQUIREMENT_CACHE.clear();
    REQUIREMENT_CACHE.set(requirement, result);
    return result;
  };
  // These identify non-registry specifications; the version/range grammar is
  // exclusively node-semver's, including caret's special rules for 0.x.
  if (/^[a-z][a-z0-9+.-]*:/i.test(requirement) || /[/\\#]/.test(requirement)
      || requirement.startsWith('.')) {
    return remember({reason: 'unsupported_requirement'});
  }
  let range;
  try {
    range = new semver.Range(requirement, OPTIONS);
  } catch {
    const tag = /^[a-z][a-z0-9._-]*$/i.test(requirement);
    return remember({reason: tag ? 'unsupported_requirement' : 'invalid_requirement'});
  }
  // Inspect explicit input tokens, not generated range comparators: e.g. ^1.0.0
  // generates <2.0.0-0, which does not make the original a prerelease constraint.
  for (const token of requirement.split(/[\s<>=~^|]+/)) {
    const parsed = semver.parse(token, OPTIONS);
    if (parsed && parsed.prerelease.length) return remember({reason: 'prerelease_requirement'});
  }
  return remember({range});
}

class DependencyHistory {
  #releases;
  #retrievedAt;

  constructor(dependency) {
    if (!dependency || !Array.isArray(dependency.versions)) {
      throw new TypeError('dependency.versions must contain the full release history');
    }
    this.package_name = dependency.package_name ?? null;
    this.provenance = Object.freeze({...dependency.provenance});
    this.#retrievedAt = parseTimestampNs(this.provenance.retrieved_at);
    if (this.#retrievedAt === null) throw new TypeError('dependency provenance requires a valid retrieved_at');
    const diagnostics = {total_versions: dependency.versions.length,
      invalid_versions: 0, prerelease_versions: 0, stable_versions: 0, stable_versions_missing_time: 0};
    const seen = new Set();
    const releases = [];
    for (const release of dependency.versions) {
      if (!release || typeof release.version !== 'string') throw new TypeError('release.version must be a string');
      if (seen.has(release.version)) throw new TypeError(`duplicate release identifier: ${release.version}`);
      seen.add(release.version);
      const parsed = semver.parse(release.version, OPTIONS);
      if (!parsed) {
        diagnostics.invalid_versions++;
        continue;
      }
      if (parsed.prerelease.length) {
        diagnostics.prerelease_versions++;
        continue;
      }
      const publishedAt = parseTimestampNs(release.published_at);
      diagnostics.stable_versions++;
      if (publishedAt === null) diagnostics.stable_versions_missing_time++;
      releases.push({version: release.version, parsed, publishedAt});
    }
    releases.sort((left, right) => semver.compare(left.parsed, right.parsed, OPTIONS));
    this.#releases = releases;
    this.diagnostics = Object.freeze(diagnostics);
    Object.freeze(this);
  }

  evaluate(t, range, consumerRetrievedAt) {
    if (this.#retrievedAt < t || consumerRetrievedAt < t) return {reason: 'snapshot_before_t'};
    if (this.diagnostics.stable_versions_missing_time) return {reason: 'incomplete_release_history'};
    const available = this.#releases.filter(release => release.publishedAt <= t);
    if (!available.length) return {reason: 'no_available_stable_version_at_t'};
    for (let index = 1; index < available.length; index++) {
      if (semver.compare(available[index - 1].parsed, available[index].parsed, OPTIONS) === 0) {
        return {reason: 'ambiguous_semver_precedence'};
      }
    }
    const latest = available.at(-1);
    let requiredIndex = available.length - 1;
    while (requiredIndex >= 0 && !range.test(available[requiredIndex].parsed)) requiredIndex--;
    if (requiredIndex < 0) return {reason: 'no_satisfying_version_at_t'};
    const required = available[requiredIndex];
    const elapsed = latest.publishedAt - required.publishedAt;
    const updateType = required.parsed.major !== latest.parsed.major ? 'major'
      : required.parsed.minor !== latest.parsed.minor ? 'minor'
        : required.parsed.patch !== latest.parsed.patch ? 'patch' : 'none';
    return {metrics: {
      v_req: required.version,
      v_latest: latest.version,
      available_versions: available.length,
      version_lag: available.length - requiredIndex - 1,
      time_lag_days: Number(elapsed) / DAY_NS,
      time_lag_nanoseconds: elapsed.toString(),
      update_type: updateType,
      nonmonotone_publish_order: elapsed < 0n,
    }};
  }
}

function prepareDependencyHistory(dependency) {
  return new DependencyHistory(dependency);
}

function calculateLag(input) {
  if (!input || typeof input.requirement !== 'string') throw new TypeError('requirement must be a string');
  if (input.shadowed_by_optional !== undefined && typeof input.shadowed_by_optional !== 'boolean') {
    throw new TypeError('shadowed_by_optional must be a boolean');
  }
  const consumer = input.consumer || {};
  const observationTime = input.observation_time === undefined ? consumer.published_at : input.observation_time;
  const effectiveType = input.shadowed_by_optional ? 'optionalDependencies' : (input.dependency_type ?? 'dependencies');
  const result = {
    status: 'excluded', exclusion_reason: null, historical_mode: 'reconstructed_survivors',
    consumer_package: consumer.package_name ?? null, consumer_version: consumer.version ?? null,
    dependency_package: input.dependency?.package_name ?? null,
    requirement: input.requirement, dependency_type: input.dependency_type ?? 'dependencies',
    effective_dependency_type: effectiveType, observation_time: observationTime ?? null,
    consumer_provenance: Object.freeze({...consumer.provenance}),
    dependency_provenance: input.dependency ? Object.freeze({...input.dependency.provenance}) : null,
    dependency_diagnostics: null,
    v_req: null, v_latest: null, available_versions: null, version_lag: null,
    time_lag_days: null, time_lag_nanoseconds: null, update_type: null, nonmonotone_publish_order: null,
  };
  const exclude = reason => ({...result, exclusion_reason: reason});
  const publishedAt = parseTimestampNs(consumer.published_at);
  if (!parseStableVersion(consumer.version) || publishedAt === null) return exclude('invalid_consumer_version_or_time');
  const t = parseTimestampNs(observationTime);
  if (t === null) throw new TypeError('observation_time must be an RFC3339 timestamp');
  if (t < publishedAt) throw new RangeError('observation_time precedes consumer publication');
  if (effectiveType !== 'dependencies') return exclude('unsupported_dependency_type');
  const constraint = parseRequirement(input.requirement);
  if (constraint.reason) return exclude(constraint.reason);
  if (input.dependency == null) return exclude('missing_dependency_snapshot');
  const consumerRetrievedAt = parseTimestampNs(consumer.provenance?.retrieved_at);
  if (consumerRetrievedAt === null) throw new TypeError('consumer provenance requires a valid retrieved_at');
  const history = input.dependency instanceof DependencyHistory ? input.dependency : prepareDependencyHistory(input.dependency);
  result.dependency_provenance = history.provenance;
  result.dependency_diagnostics = history.diagnostics;
  const evaluation = history.evaluate(t, constraint.range, consumerRetrievedAt);
  if (evaluation.reason) return exclude(evaluation.reason);
  return {...result, ...evaluation.metrics, status: 'ok'};
}

function summarizeResults(results) {
  const summary = {total: 0, successful: 0, unsuccessful: 0, exclusion_reasons: {}};
  for (const result of results) {
    if (result.status === 'ok' && result.exclusion_reason === null) summary.successful++;
    else if (result.status === 'excluded' && typeof result.exclusion_reason === 'string') {
      summary.unsuccessful++;
      summary.exclusion_reasons[result.exclusion_reason] = (summary.exclusion_reasons[result.exclusion_reason] || 0) + 1;
    } else throw new TypeError('expected a calculateLag result');
    summary.total++;
  }
  return summary;
}

module.exports = {calculateLag, prepareDependencyHistory, summarizeResults, parseTimestampNs};
