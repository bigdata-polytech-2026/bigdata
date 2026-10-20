const fs = require('node:fs');
const semver = require('semver');
const versions = JSON.parse(fs.readFileSync(0, 'utf8'));
process.stdout.write(JSON.stringify(versions.map(version => {
  const valid = semver.valid(version, {loose: false}) !== null;
  return {version, version_valid: valid,
    is_prerelease: valid && semver.prerelease(version, {loose: false}) !== null};
})));
