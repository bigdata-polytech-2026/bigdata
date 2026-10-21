'use strict';

const fs = require('node:fs');
const readline = require('node:readline');
const {once} = require('node:events');
const {calculateLag, prepareDependencyHistory} = require('./core.cjs');

async function writeLine(stream, value) {
  if (!stream.write(`${JSON.stringify(value)}\n`)) await once(stream, 'drain');
}

async function loadHistories(path, diagnosticsPath) {
  const histories = new Map();
  const diagnostics = fs.createWriteStream(diagnosticsPath, {encoding: 'utf8'});
  const lines = readline.createInterface({input: fs.createReadStream(path, {encoding: 'utf8'}), crlfDelay: Infinity});
  for await (const line of lines) {
    if (!line) continue;
    const source = JSON.parse(line);
    if (histories.has(source.package_name)) throw new Error(`duplicate dependency history: ${source.package_name}`);
    const history = prepareDependencyHistory(source);
    histories.set(source.package_name, history);
    await writeLine(diagnostics, {
      package_name: source.package_name,
      provenance: source.provenance,
      ...history.diagnostics,
    });
  }
  diagnostics.end();
  await once(diagnostics, 'finish');
  return histories;
}

async function main() {
  const [historiesPath, diagnosticsPath] = process.argv.slice(2);
  if (!historiesPath || !diagnosticsPath) {
    throw new Error('usage: node runner.cjs HISTORIES_JSONL DIAGNOSTICS_JSONL < candidates.jsonl > results.jsonl');
  }
  const histories = await loadHistories(historiesPath, diagnosticsPath);
  const lines = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
  let count = 0;
  for await (const line of lines) {
    if (!line) continue;
    const row = JSON.parse(line);
    const dependency = row.dependency_package_matched ? histories.get(row.dependency_name) : null;
    if (row.dependency_package_matched && !dependency) {
      throw new Error(`matched dependency has no history: ${row.dependency_name}`);
    }
    const result = calculateLag({
      consumer: {
        package_name: row.source_package,
        version: row.source_version,
        published_at: row.observation_time,
        provenance: {retrieved_at: row.consumer_retrieved_at},
      },
      dependency,
      requirement: row.requirement,
      dependency_type: row.dependency_type,
      shadowed_by_optional: row.shadowed_by_optional,
      observation_time: row.observation_time,
    });
    await writeLine(process.stdout, {
      source_package: row.source_package,
      source_version: row.source_version,
      dependency_type: row.dependency_type,
      dependency_name: row.dependency_name,
      effective_dependency_type: result.effective_dependency_type,
      status: result.status,
      exclusion_reason: result.exclusion_reason,
      v_req: result.v_req,
      v_latest: result.v_latest,
      available_versions: result.available_versions,
      version_lag: result.version_lag,
      time_lag_days: result.time_lag_days,
      time_lag_nanoseconds: result.time_lag_nanoseconds,
      update_type: result.update_type,
      nonmonotone_publish_order: result.nonmonotone_publish_order,
    });
    count++;
    if (count % 100000 === 0) process.stderr.write(`technical-lag rows evaluated: ${count}\n`);
  }
}

main().catch(error => {
  process.stderr.write(`${error.stack || error}\n`);
  process.exitCode = 1;
});
