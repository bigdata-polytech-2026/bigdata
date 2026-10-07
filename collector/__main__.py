from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
from typing import List, Optional, Sequence

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
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.command != "collect" or args.source != "npm":
        raise AssertionError("argparse accepted an unsupported command")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
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
    print(json.dumps(summary.to_dict(), ensure_ascii=False, sort_keys=True))
    return 0 if summary.available >= summary.target else 1


if __name__ == "__main__":
    raise SystemExit(main())
