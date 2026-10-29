from __future__ import annotations

import json
import platform
from pathlib import Path
from typing import Any

from analysis.common import (
    ANALYSIS_VERSION,
    atomic_directory,
    cleanup_directory,
    configure_plotting,
    digest,
    finalize_directory,
    materialize_enriched,
    output_records,
    quantile_summary,
    spearman_with_bootstrap,
    sql_string,
    verify_manifest,
    write_csv,
    write_json,
)


SUMMARY_COLUMNS = ("metric", "n", "mean", "stddev", "min", "p25", "median", "p75", "p90", "p95", "p99", "max")
FEATURES = (
    ("package_age_years", "Package age, years"),
    ("releases_per_year", "Stable releases per year"),
    ("production_dependency_count", "Production dependencies"),
)


def _rows(db, query: str):
    cursor = db.execute(query)
    return [item[0] for item in cursor.description], cursor.fetchall()


def _plot_distributions(db, relation: str, figures: Path) -> None:
    import numpy as np

    plt = configure_plotting()
    figures.mkdir(parents=True, exist_ok=True)
    for column, title, filename, xlabel in (
        ("version_lag", "Version lag distribution", "version-lag-distribution.png", "Version lag (log1p scale)"),
        ("time_lag_days", "Time lag distribution", "time-lag-distribution.png", "Time lag, days (log1p scale)"),
    ):
        values = np.asarray([row[0] for row in db.execute(
            f"SELECT {column} FROM {relation} WHERE status='ok' AND {column}>0").fetchall()], dtype=float)
        fig, ax = plt.subplots(figsize=(7.2, 4.2))
        if values.size:
            ax.hist(np.log1p(values), bins=50, color="#2667a8", alpha=0.88)
        ax.set(title=f"{title} among lagged requirements", xlabel=xlabel, ylabel="Requirements")
        fig.tight_layout()
        fig.savefig(figures / filename)
        plt.close(fig)


def _plot_update_types(rows, figures: Path) -> None:
    plt = configure_plotting()
    labels = [row[0] for row in rows]
    counts = [row[1] for row in rows]
    fig, ax = plt.subplots(figsize=(6.4, 4.2))
    ax.bar(labels, counts, color=["#5b8c5a", "#78a1bb", "#f2b134", "#c44536"][:len(labels)])
    ax.set(title="Available update type", xlabel="Update type", ylabel="Requirements")
    fig.tight_layout()
    fig.savefig(figures / "update-type-composition.png")
    plt.close(fig)


def _plot_associations(grouped: dict[str, list[tuple[Any, ...]]], figures: Path) -> None:
    plt = configure_plotting()
    fig, axes = plt.subplots(1, 3, figsize=(13.2, 4.2), sharey=True)
    for ax, (feature, label) in zip(axes, FEATURES):
        rows = grouped[feature]
        ax.plot([row[0] for row in rows], [100 * row[4] for row in rows], marker="o", color="#8b1e3f")
        ax.set(title=label, xlabel="Decile", ylabel="Lagged requirements, %")
    fig.suptitle("Technical lag prevalence by package characteristics")
    fig.tight_layout()
    fig.savefig(figures / "package-characteristics.png")
    plt.close(fig)


def _findings(dataset_id: str, coverage: dict[str, Any], update_rows, summaries, associations) -> str:
    successful = coverage["successful"]
    lagged = coverage["lagged"]
    rate = lagged / successful if successful else 0
    update = {row[0]: row[1] for row in update_rows}
    lines = [
        f"# A6 technical lag: {dataset_id}", "",
        f"Technical lag was calculable for {successful:,} of {coverage['total']:,} requirements ({coverage['success_rate']:.2%}).",
        f"Among calculable requirements, {lagged:,} ({rate:.2%}) had non-zero lag; {successful-lagged:,} had no lag.",
        f"Available updates were patch for {update.get('patch', 0):,}, minor for {update.get('minor', 0):,}, and major for {update.get('major', 0):,} requirements.",
        "",
        f"Median version lag was {summaries['version_lag'][5]:.3g}; median time lag was {summaries['time_lag_days'][5]:.3g} days across all calculable requirements.",
        "",
        "Package-level associations are descriptive, use publication-time characteristics, and do not identify causal effects.",
    ]
    for feature, label in FEATURES:
        result = associations[feature]
        lines.append(f"{label}: Spearman rho={result['rho']:.3f} (95% cluster bootstrap {result['ci_low']:.3f} to {result['ci_high']:.3f}; n={result['n']:,}).")
    lines += ["", "Excluded requirements are reported as coverage failures and are never treated as zero lag.", ""]
    return "\n".join(lines)


def run(dataset_id: str, normalized_dir: Path, lag_dir: Path, output_dir: Path,
        bootstrap_repetitions: int = 2000, seed: int = 20261027) -> dict[str, Any]:
    import duckdb
    import matplotlib
    import numpy as np
    import pyarrow
    import scipy

    if bootstrap_repetitions < 0:
        raise ValueError("bootstrap repetitions must be non-negative")
    normalized_dir, lag_dir = normalized_dir.resolve(), lag_dir.resolve()
    normalized_manifest = verify_manifest(normalized_dir, "normalization-manifest.json")
    lag_manifest = verify_manifest(lag_dir, "technical-lag-manifest.json")
    staging = atomic_directory(output_dir)
    db = duckdb.connect()
    db.execute("SET memory_limit='2GB'")
    db.execute("SET threads=4")
    db.execute(f"SET temp_directory={sql_string(staging / '.spill')}")
    try:
        enriched_path = staging / "tables" / "analysis-observations.parquet"
        enriched_path.parent.mkdir(parents=True)
        materialize_enriched(db, normalized_dir, lag_dir, enriched_path)
        db.execute(f"CREATE VIEW observations AS SELECT * FROM read_parquet({sql_string(enriched_path)})")
        total, successful, lagged = db.execute("""SELECT count(*), count(*) FILTER (WHERE status='ok'),
            count(*) FILTER (WHERE status='ok' AND version_lag>0) FROM observations""").fetchone()
        coverage = {"total": total, "successful": successful, "excluded": total-successful,
                    "success_rate": successful/total if total else 0.0, "lagged": lagged}

        summaries = {}
        summary_rows = []
        for column in ("version_lag", "time_lag_days"):
            values = quantile_summary(db, "observations", column, "status='ok'")
            summaries[column] = values
            summary_rows.append((column, *values))
            positive = quantile_summary(db, "observations", column, f"status='ok' AND {column}>0")
            summaries[f"{column}_positive"] = positive
            summary_rows.append((f"{column}_positive", *positive))
        write_csv(staging / "tables/descriptive-statistics.csv", SUMMARY_COLUMNS, summary_rows)

        update_rows = db.execute("""SELECT update_type, count(*), count(*)*1.0/sum(count(*)) OVER ()
            FROM observations WHERE status='ok' GROUP BY 1
            ORDER BY CASE update_type WHEN 'none' THEN 0 WHEN 'patch' THEN 1 WHEN 'minor' THEN 2 ELSE 3 END""").fetchall()
        write_csv(staging / "tables/update-types.csv", ("update_type", "n", "fraction"), update_rows)
        exclusion_rows = db.execute("""SELECT exclusion_reason, count(*) FROM observations
            WHERE status='excluded' GROUP BY 1 ORDER BY 2 DESC""").fetchall()
        write_csv(staging / "tables/exclusions.csv", ("exclusion_reason", "n"), exclusion_rows)

        package_rows = db.execute("""SELECT source_package, count(*) AS requirements,
            avg((version_lag>0)::INTEGER)::DOUBLE AS lag_fraction, median(version_lag) AS median_version_lag,
            median(time_lag_days) AS median_time_lag_days
            FROM observations WHERE status='ok' GROUP BY 1 HAVING count(*)>=10
            ORDER BY lag_fraction DESC, requirements DESC LIMIT 100""").fetchall()
        write_csv(staging / "tables/packages-with-most-lag.csv",
                  ("source_package", "requirements", "lag_fraction", "median_version_lag", "median_time_lag_days"), package_rows)

        grouped: dict[str, list[tuple[Any, ...]]] = {}
        associations = {}
        db.execute("""CREATE TEMP TABLE package_version AS SELECT source_package, source_version,
            avg(version_lag)::DOUBLE AS mean_version_lag,
            avg((version_lag>0)::INTEGER)::DOUBLE AS lag_fraction,
            max(package_age_years) AS package_age_years,
            max(releases_per_year) AS releases_per_year,
            max(production_dependency_count) AS production_dependency_count
            FROM observations WHERE status='ok' GROUP BY 1,2""")
        for offset, (feature, _) in enumerate(FEATURES):
            rows = db.execute(f"""WITH ranked AS (
                SELECT *, ntile(10) OVER (ORDER BY {feature}) AS decile
                FROM observations WHERE status='ok' AND {feature} IS NOT NULL)
                SELECT decile, min({feature}), max({feature}), count(*),
                    avg((version_lag>0)::INTEGER), median(version_lag), median(time_lag_days)
                FROM ranked GROUP BY 1 ORDER BY 1""").fetchall()
            grouped[feature] = rows
            write_csv(staging / f"tables/{feature}-deciles.csv",
                      ("decile", "min", "max", "n", "lag_fraction", "median_version_lag", "median_time_lag_days"), rows)
            feature_rows = db.execute(f"""SELECT {feature}, mean_version_lag, source_package
                FROM package_version WHERE {feature} IS NOT NULL""").fetchall()
            associations[feature] = spearman_with_bootstrap(
                [row[0] for row in feature_rows], [row[1] for row in feature_rows],
                [row[2] for row in feature_rows], bootstrap_repetitions, seed + offset)
        write_json(staging / "statistical-tests/spearman.json", associations)
        _plot_distributions(db, "observations", staging / "figures")
        _plot_update_types(update_rows, staging / "figures")
        _plot_associations(grouped, staging / "figures")
        (staging / "findings.md").write_text(
            _findings(dataset_id, coverage, update_rows, summaries, associations), encoding="utf-8")
        shutil_path = staging / ".spill"
        if shutil_path.exists():
            import shutil
            shutil.rmtree(shutil_path)
        result = {
            "analysis": "lag-distribution", "analysis_version": ANALYSIS_VERSION, "dataset_id": dataset_id,
            "inputs": {
                "normalization_manifest_sha256": digest(normalized_dir / "normalization-manifest.json"),
                "technical_lag_manifest_sha256": digest(lag_dir / "technical-lag-manifest.json"),
                "normalized_counts": normalized_manifest["report"]["counts"],
                "technical_lag_rows": lag_manifest["report"]["rows"],
            },
            "parameters": {"bootstrap_repetitions": bootstrap_repetitions, "seed": seed},
            "runtime": {"python": platform.python_version(), "duckdb": duckdb.__version__,
                        "pyarrow": pyarrow.__version__, "numpy": np.__version__,
                        "scipy": scipy.__version__, "matplotlib": matplotlib.__version__},
            "coverage": coverage, "associations": associations,
        }
        result["outputs"] = output_records(staging)
        write_json(staging / "analysis-manifest.json", result)
        db.close()
        finalize_directory(staging, output_dir)
        return result
    except BaseException:
        db.close()
        cleanup_directory(staging)
        raise
