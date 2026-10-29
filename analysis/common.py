from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Iterable, Sequence


ANALYSIS_VERSION = "1.0.0"


def sql_string(value: Path | str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def digest(path: Path) -> str:
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def verify_manifest(root: Path, filename: str, output_key: str = "outputs") -> dict[str, Any]:
    manifest_path = root / filename
    if not manifest_path.is_file():
        raise ValueError(f"manifest not found: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    for item in manifest.get(output_key, []):
        path = root / item["path"]
        if not path.is_file() or path.stat().st_size != item["bytes"] or digest(path) != item["sha256"]:
            raise ValueError(f"manifest verification failed: {path}")
    return manifest


def atomic_directory(destination: Path):
    destination = destination.resolve()
    if destination.exists():
        raise ValueError(f"analysis output already exists: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))


def finalize_directory(staging: Path, destination: Path) -> None:
    os.rename(staging, destination.resolve())


def cleanup_directory(staging: Path) -> None:
    if staging.exists():
        shutil.rmtree(staging)


def write_csv(path: Path, columns: Sequence[str], rows: Iterable[Sequence[Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows(rows)


def json_safe(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(item) for item in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if hasattr(value, "item"):
        return json_safe(value.item())
    return value


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(json_safe(value), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                    encoding="utf-8")


def output_records(root: Path) -> list[dict[str, Any]]:
    return [
        {"path": path.relative_to(root).as_posix(), "sha256": digest(path), "bytes": path.stat().st_size}
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "analysis-manifest.json"
    ]


def configure_plotting():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "figure.dpi": 140,
        "savefig.dpi": 180,
        "axes.spines.top": False,
        "axes.spines.right": False,
        "axes.grid": True,
        "grid.alpha": 0.22,
        "font.size": 10,
    })
    return plt


def materialize_enriched(db, normalized_dir: Path, lag_dir: Path, destination: Path) -> None:
    """Create one compact analysis table with time-local consumer characteristics."""
    db.execute(f"CREATE OR REPLACE VIEW lag AS SELECT * FROM read_parquet({sql_string(lag_dir / 'technical_lag.parquet')})")
    db.execute(f"CREATE OR REPLACE VIEW packages AS SELECT * FROM read_parquet({sql_string(normalized_dir / 'packages/*.parquet')})")
    db.execute(f"CREATE OR REPLACE VIEW quality AS SELECT * FROM read_parquet({sql_string(normalized_dir / 'version_quality.parquet')})")
    db.execute("""CREATE OR REPLACE TEMP VIEW version_features AS
        SELECT package_name, version,
            count(*) OVER package_release AS stable_releases_to_date,
            first_value(try_cast(published_at AS TIMESTAMPTZ)) OVER package_release AS first_stable_release_at
        FROM quality
        WHERE version_valid AND NOT is_prerelease AND try_cast(published_at AS TIMESTAMPTZ) IS NOT NULL
        WINDOW package_release AS (
            PARTITION BY package_name ORDER BY try_cast(published_at AS TIMESTAMPTZ), version
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)""")
    db.execute("""CREATE OR REPLACE TEMP VIEW dependency_counts AS
        SELECT source_package, source_version, count(DISTINCT dependency_name) AS production_dependency_count
        FROM lag
        WHERE dependency_type='dependencies' AND NOT shadowed_by_optional
        GROUP BY 1, 2""")
    db.execute(f"""COPY (
        SELECT l.source_package, l.source_version, l.dependency_name, l.requirement,
            l.observation_time, l.status, l.exclusion_reason, l.v_req, l.v_latest,
            l.version_lag, l.time_lag_days, l.update_type,
            greatest(0.0, date_diff('second', try_cast(p.created_at AS TIMESTAMPTZ),
                try_cast(l.observation_time AS TIMESTAMPTZ)) / (365.2425 * 86400.0)) AS package_age_years,
            vf.stable_releases_to_date,
            CASE WHEN vf.stable_releases_to_date > 1 THEN
                (vf.stable_releases_to_date - 1) /
                nullif(date_diff('second', vf.first_stable_release_at,
                    try_cast(l.observation_time AS TIMESTAMPTZ)) / (365.2425 * 86400.0), 0)
            END AS releases_per_year,
            coalesce(dc.production_dependency_count, 0) AS production_dependency_count
        FROM lag l
        LEFT JOIN packages p ON p.package_name=l.source_package
        LEFT JOIN version_features vf ON vf.package_name=l.source_package AND vf.version=l.source_version
        LEFT JOIN dependency_counts dc USING(source_package, source_version)
    ) TO {sql_string(destination)} (FORMAT PARQUET, COMPRESSION ZSTD)""")


def quantile_summary(db, relation: str, column: str, where: str = "TRUE") -> tuple[Any, ...]:
    return db.execute(f"""SELECT count({column}), avg({column}), stddev_samp({column}), min({column}),
        quantile_cont({column}, 0.25), quantile_cont({column}, 0.5), quantile_cont({column}, 0.75),
        quantile_cont({column}, 0.9), quantile_cont({column}, 0.95), quantile_cont({column}, 0.99), max({column})
        FROM {relation} WHERE {where} AND {column} IS NOT NULL""").fetchone()


def spearman_with_bootstrap(x, y, clusters, repetitions: int, seed: int) -> dict[str, Any]:
    import numpy as np
    from scipy.stats import spearmanr

    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    clusters = np.asarray(clusters)
    mask = np.isfinite(x) & np.isfinite(y)
    x, y, clusters = x[mask], y[mask], clusters[mask]
    unique, inverse = np.unique(clusters, return_inverse=True)
    # Associations are estimated at consumer-package level. This prevents packages
    # with many historical releases from dominating both the estimate and bootstrap.
    # Aggregate all packages in one pass. Re-scanning the full observation array
    # once per package is quadratic and is prohibitive for full-v1.
    counts = np.bincount(inverse)
    package_x = np.bincount(inverse, weights=x) / counts
    package_y = np.bincount(inverse, weights=y) / counts
    rho, p_value = spearmanr(package_x, package_y)
    estimates = []
    if repetitions and unique.size > 1:
        rng = np.random.default_rng(seed)
        for _ in range(repetitions):
            indices = rng.integers(0, unique.size, unique.size)
            estimate = spearmanr(package_x[indices], package_y[indices]).statistic
            if np.isfinite(estimate):
                estimates.append(estimate)
    low, high = (np.quantile(estimates, [0.025, 0.975]) if estimates else (float("nan"), float("nan")))
    return {"n": int(unique.size), "unit": "consumer_package", "rho": float(rho), "p_value": float(p_value),
            "ci_low": float(low), "ci_high": float(high), "bootstrap_repetitions": len(estimates)}
