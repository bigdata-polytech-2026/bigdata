from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Sequence

from collector.depsdev.collector import DEFAULT_API_URL as DEPSDEV_API_URL
from collector.depsdev.collector import DepsDevCollector
from collector.npm.collector import DEFAULT_QUERY, NpmCollector


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("value must be at least 1")
    return parsed


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("value must be at least 0")
    return parsed


def _package_names(explicit: Sequence[str], packages_file: Optional[Path]) -> Optional[List[str]]:
    names = list(explicit)
    if packages_file is not None:
        for line in packages_file.read_text(encoding="utf-8").splitlines():
            name = line.strip()
            if name and not name.startswith("#"):
                names.append(name)

    if not names:
        return None

    deduplicated = []
    seen = set()
    for name in names:
        if name not in seen:
            deduplicated.append(name)
            seen.add(name)
    return deduplicated


def _package_versions(values: Sequence[str], input_file: Optional[Path]) -> List[tuple[str, str]]:
    entries = list(values)
    if input_file is not None:
        entries.extend(line.strip() for line in input_file.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#"))
    parsed = []
    for entry in entries:
        name, separator, version = entry.rpartition("@")
        if not separator or not name or not version:
            raise argparse.ArgumentTypeError(f"expected PACKAGE@VERSION, got {entry!r}")
        parsed.append((name, version))
    if not parsed:
        raise argparse.ArgumentTypeError("provide --package-version or --input-file")
    return list(dict.fromkeys(parsed))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="bigdata", description="Technical-lag data collectors")
    commands = parser.add_subparsers(dest="command", required=True)

    collect = commands.add_parser("collect", help="collect source data")
    sources = collect.add_subparsers(dest="source", required=True)

    npm = sources.add_parser("npm", help="collect full npm package metadata")
    npm.add_argument("--limit", type=_positive_int, default=1000, help="successful packages required")
    npm.add_argument(
        "--query",
        default=os.environ.get("NPM_SEARCH_QUERY", DEFAULT_QUERY),
        help="npm search query used when package names are not supplied",
    )
    npm.add_argument("--package", action="append", default=[], help="collect an explicit package (repeatable)")
    npm.add_argument("--packages-file", type=Path, help="UTF-8 file with one package name per line")
    npm.add_argument("--workers", type=_positive_int, default=8, help="concurrent package requests")
    npm.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    npm.add_argument("--retries", type=_non_negative_int, default=3, help="retries after a failed request")
    npm.add_argument(
        "--data-dir",
        type=Path,
        default=Path(os.environ.get("BIGDATA_DATA_DIR", "data")),
        help="root for raw, normalized, state, manifests, and logs",
    )
    npm.add_argument(
        "--registry-url",
        default=os.environ.get("NPM_REGISTRY_URL", "https://registry.npmjs.org"),
        help="npm Registry base URL",
    )
    npm.add_argument("--refresh", action="store_true", help="download packages even when a valid success marker exists")

    depsdev = sources.add_parser("depsdev", help="collect resolved dependency graphs for npm package versions")
    depsdev.add_argument("--package-version", action="append", default=[], help="npm package and version as PACKAGE@VERSION (repeatable)")
    depsdev.add_argument("--input-file", type=Path, help="UTF-8 file with one PACKAGE@VERSION per line")
    depsdev.add_argument("--limit", type=_positive_int, help="process only the first N unique package versions")
    depsdev.add_argument("--workers", type=_positive_int, default=8, help="concurrent deps.dev requests")
    depsdev.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout in seconds")
    depsdev.add_argument("--retries", type=_non_negative_int, default=3, help="retries after 429, 5xx, or network errors")
    depsdev.add_argument("--data-dir", type=Path, default=Path(os.environ.get("BIGDATA_DATA_DIR", "data")), help="root for raw, normalized, state, manifests, and logs")
    depsdev.add_argument("--api-url", default=os.environ.get("DEPSDEV_API_URL", DEPSDEV_API_URL), help="deps.dev API base URL")
    depsdev.add_argument("--refresh", action="store_true", help="download graphs even when a valid success marker exists")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command != "collect":
        raise AssertionError("argparse accepted an unsupported command")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.source == "npm":
        names = _package_names(args.package, args.packages_file)
        target = args.limit if names is None else min(args.limit, len(names))
        collector = NpmCollector(
            data_dir=args.data_dir,
            registry_url=args.registry_url,
            workers=args.workers,
            timeout=args.timeout,
            retries=args.retries,
            refresh=args.refresh,
        )
        summary = collector.run(limit=target, query=args.query, package_names=names)
        success = summary.available >= summary.target
    elif args.source == "depsdev":
        package_versions = _package_versions(args.package_version, args.input_file)
        if args.limit is not None:
            package_versions = package_versions[: args.limit]
        summary = DepsDevCollector(
            data_dir=args.data_dir,
            api_url=args.api_url,
            workers=args.workers,
            timeout=args.timeout,
            retries=args.retries,
            refresh=args.refresh,
        ).run(package_versions)
        success = summary.api_failures == 0
    else:
        raise AssertionError("argparse accepted an unsupported source")
    print(json.dumps(summary.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
