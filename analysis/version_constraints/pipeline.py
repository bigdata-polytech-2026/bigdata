from __future__ import annotations

import json
import math
import platform
import subprocess
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
    sql_string,
    verify_manifest,
    write_csv,
    write_json,
)


ORDER = ("exact", "caret", "tilde", "wildcard", "range", "prerelease", "unsupported", "invalid")
PRIMARY = ("exact", "caret", "tilde", "wildcard", "range")


def _classify(db, observations: Path, destination: Path, work: Path, pa, pq) -> int:
    input_path, output_path = work / ".constraints.in.jsonl", work / ".constraints.out.jsonl"
    cursor = db.execute(f"SELECT DISTINCT requirement FROM read_parquet({sql_string(observations)}) ORDER BY 1")
    count = 0
    with input_path.open("w", encoding="utf-8") as stream:
        while True:
            rows = cursor.fetchmany(10000)
            if not rows:
                break
            for (requirement,) in rows:
                stream.write(json.dumps(requirement, ensure_ascii=False) + "\n")
                count += 1
    classifier = Path(__file__).with_name("classifier.cjs")
    with input_path.open("rb") as stdin, output_path.open("wb") as stdout:
        result = subprocess.run(["node", str(classifier)], stdin=stdin, stdout=stdout,
                                stderr=subprocess.PIPE, check=False)
    if result.returncode:
        raise RuntimeError(f"constraint classifier failed: {result.stderr.decode(errors='replace')}")
    schema = pa.schema([
        ("requirement", pa.string()), ("constraint_category", pa.string()), ("constraint_subtype", pa.string())
    ])
    writer = pq.ParquetWriter(destination, schema, compression="zstd")
    parsed = 0
    batch = []
    try:
        with output_path.open(encoding="utf-8") as stream:
            for line in stream:
                item = json.loads(line)
                batch.append({"requirement": item["requirement"], "constraint_category": item["category"],
                              "constraint_subtype": item["subtype"]})
                parsed += 1
                if len(batch) == 10000:
                    writer.write_table(pa.Table.from_pylist(batch, schema=schema))
                    batch.clear()
            if batch:
                writer.write_table(pa.Table.from_pylist(batch, schema=schema))
    finally:
        writer.close()
        input_path.unlink(missing_ok=True)
        output_path.unlink(missing_ok=True)
    if parsed != count:
        raise ValueError(f"constraint classification accounting mismatch: {count} input, {parsed} output")
    return count


def _cramers_v(table) -> tuple[float, float, float]:
    import numpy as np
    from scipy.stats import chi2_contingency

    matrix = np.asarray(table, dtype=float)
    chi2, p_value, _, _ = chi2_contingency(matrix)
    n = matrix.sum()
    phi2 = chi2 / n
    rows, columns = matrix.shape
    corrected = max(0.0, phi2 - ((columns - 1) * (rows - 1)) / max(n - 1, 1))
    row_corrected = rows - ((rows - 1) ** 2) / max(n - 1, 1)
    column_corrected = columns - ((columns - 1) ** 2) / max(n - 1, 1)
    denominator = min(row_corrected - 1, column_corrected - 1)
    return float(chi2), float(p_value), float(math.sqrt(corrected / denominator) if denominator > 0 else 0.0)


def _cluster_risk_differences(rows, repetitions: int, seed: int) -> dict[str, Any]:
    import numpy as np

    packages = sorted({row[0] for row in rows})
    package_index = {name: index for index, name in enumerate(packages)}
    category_index = {name: index for index, name in enumerate(PRIMARY)}
    totals = np.zeros((len(packages), len(PRIMARY)), dtype=np.int64)
    lagged = np.zeros_like(totals)
    for package, category, n, positive in rows:
        if category in category_index:
            index = (package_index[package], category_index[category])
            totals[index] += n
            lagged[index] += positive
    rates = np.divide(lagged.sum(axis=0), totals.sum(axis=0), out=np.full(len(PRIMARY), np.nan), where=totals.sum(axis=0)>0)
    reference = category_index["caret"]
    rng = np.random.default_rng(seed)
    samples = {category: [] for category in PRIMARY if category != "caret"}
    for _ in range(repetitions):
        chosen = rng.integers(0, len(packages), len(packages))
        sampled_totals = totals[chosen].sum(axis=0)
        sampled_lagged = lagged[chosen].sum(axis=0)
        sampled_rates = np.divide(sampled_lagged, sampled_totals,
                                  out=np.full(len(PRIMARY), np.nan), where=sampled_totals>0)
        for category, index in category_index.items():
            if category != "caret" and np.isfinite(sampled_rates[index]) and np.isfinite(sampled_rates[reference]):
                samples[category].append(sampled_rates[index] - sampled_rates[reference])
    result = {}
    for category, index in category_index.items():
        if category == "caret":
            continue
        interval = np.quantile(samples[category], [0.025, 0.975]) if samples[category] else (np.nan, np.nan)
        result[category] = {"risk_difference_vs_caret": float(rates[index] - rates[reference]),
                            "ci_low": float(interval[0]), "ci_high": float(interval[1]),
                            "bootstrap_repetitions": len(samples[category])}
    return result


def _magnitude_tests(db) -> dict[str, Any]:
    import numpy as np
    from scipy.stats import kruskal, mannwhitneyu

    values = {}
    for category in PRIMARY:
        rows = db.execute("""SELECT version_lag FROM classified
            WHERE status='ok' AND version_lag>0 AND constraint_category=?""", [category]).fetchall()
        values[category] = np.asarray([row[0] for row in rows], dtype=float)
    nonempty = [value for value in values.values() if value.size]
    omnibus = kruskal(*nonempty) if len(nonempty) > 1 else None
    result: dict[str, Any] = {"kruskal_h": float(omnibus.statistic) if omnibus else None,
                              "p_value": float(omnibus.pvalue) if omnibus else None, "pairwise_vs_caret": {}}
    caret = values["caret"]
    for category in PRIMARY:
        if category == "caret" or not values[category].size or not caret.size:
            continue
        test = mannwhitneyu(values[category], caret, alternative="two-sided")
        delta = 2 * float(test.statistic) / (values[category].size * caret.size) - 1
        result["pairwise_vs_caret"][category] = {
            "n": int(values[category].size), "n_caret": int(caret.size),
            "median_difference": float(np.median(values[category]) - np.median(caret)),
            "cliffs_delta": delta, "p_value": float(test.pvalue),
        }
    return result


def _age_gradient(db) -> list[tuple[Any, ...]]:
    """Weighted OLS on package-version/category cells with package-clustered covariance."""
    import numpy as np
    from scipy.stats import norm

    rows = db.execute("""SELECT source_package, constraint_category,
        avg(ln(1+version_lag)) AS outcome, max(package_age_years) AS age,
        max(releases_per_year) AS cadence, max(production_dependency_count) AS dependencies,
        count(*) AS weight
        FROM classified WHERE status='ok' AND constraint_category IN ('exact','caret','tilde','wildcard','range')
        AND package_age_years IS NOT NULL AND releases_per_year IS NOT NULL
        GROUP BY source_package, source_version, constraint_category""").fetchall()
    if not rows:
        return []
    categories = [category for category in PRIMARY if category != "caret"]
    columns = ["intercept", "age", "log_cadence", "log_dependencies"]
    columns += [f"category_{category}" for category in categories]
    columns += [f"age_x_{category}" for category in categories]
    matrix, outcomes, weights, clusters = [], [], [], []
    for package, category, outcome, age, cadence, dependencies, weight in rows:
        base = [1.0, age, math.log1p(cadence), math.log1p(dependencies)]
        base += [float(category == item) for item in categories]
        base += [age * float(category == item) for item in categories]
        matrix.append(base); outcomes.append(outcome); weights.append(weight); clusters.append(package)
    x, y, w = np.asarray(matrix), np.asarray(outcomes), np.asarray(weights, dtype=float)
    root_w = np.sqrt(w)
    bread = np.linalg.pinv((x * root_w[:, None]).T @ (x * root_w[:, None]))
    beta = bread @ ((x * w[:, None]).T @ y)
    residual = y - x @ beta
    clusters_array = np.asarray(clusters)
    unique, inverse = np.unique(clusters_array, return_inverse=True)
    # Accumulate package-level score vectors in one pass. Re-scanning every
    # package-version cell once per package is quadratic on full-v1.
    scores = np.zeros((unique.size, x.shape[1]))
    np.add.at(scores, inverse, x * (w * residual)[:, None])
    meat = scores.T @ scores
    covariance = bread @ meat @ bread
    if unique.size > 1 and x.shape[0] > x.shape[1]:
        covariance *= (unique.size / (unique.size - 1)) * ((x.shape[0] - 1) / (x.shape[0] - x.shape[1]))
    standard_errors = np.sqrt(np.maximum(0, np.diag(covariance)))
    output = []
    for name, estimate, error in zip(columns, beta, standard_errors):
        z = estimate / error if error else float("nan")
        p_value = 2 * norm.sf(abs(z)) if math.isfinite(z) else float("nan")
        output.append((name, estimate, error, estimate - 1.96 * error, estimate + 1.96 * error, z, p_value,
                       x.shape[0], unique.size))
    return output


def _plots(distribution, metrics, db, destination: Path) -> None:
    plt = configure_plotting()
    destination.mkdir(parents=True, exist_ok=True)
    main_dist = [row for row in distribution if row[0] in PRIMARY]
    categories = [row[0] for row in main_dist]
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.bar(categories, [row[1] for row in main_dist], color="#376996")
    ax.set(title="Version constraint distribution", ylabel="Requirements")
    fig.tight_layout(); fig.savefig(destination / "constraint-distribution.png"); plt.close(fig)

    main_metrics = [row for row in metrics if row[0] in PRIMARY]
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.bar([row[0] for row in main_metrics], [100 * row[4] for row in main_metrics], color="#8b1e3f")
    ax.set(title="Technical lag prevalence by constraint", ylabel="Lagged requirements, %")
    fig.tight_layout(); fig.savefig(destination / "lag-prevalence.png"); plt.close(fig)

    fig, axes = plt.subplots(1, 2, figsize=(10.4, 4.2))
    axes[0].bar([row[0] for row in main_metrics], [row[5] or 0 for row in main_metrics], color="#d17a22")
    axes[0].set(title="Conditional version lag", ylabel="Median version lag")
    axes[1].bar([row[0] for row in main_metrics], [row[6] or 0 for row in main_metrics], color="#5b8c5a")
    axes[1].set(title="Conditional time lag", ylabel="Median days")
    fig.tight_layout(); fig.savefig(destination / "conditional-lag-magnitude.png"); plt.close(fig)

    gradients = db.execute("""WITH bins AS (SELECT constraint_category, version_lag,
        ntile(10) OVER (PARTITION BY constraint_category ORDER BY package_age_years) AS age_decile
        FROM classified WHERE status='ok' AND constraint_category IN ('exact','caret','tilde','wildcard','range')
        AND package_age_years IS NOT NULL)
        SELECT constraint_category, age_decile, avg(version_lag) FROM bins GROUP BY 1,2 ORDER BY 1,2""").fetchall()
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    for category in PRIMARY:
        values = [row for row in gradients if row[0] == category]
        ax.plot([row[1] for row in values], [row[2] for row in values], marker="o", label=category)
    ax.set(title="Observed age gradient of version lag", xlabel="Package-age decile", ylabel="Mean version lag")
    ax.legend(); fig.tight_layout(); fig.savefig(destination / "age-gradient.png"); plt.close(fig)


def _findings(dataset_id, coverage, metrics, omnibus, magnitude, gradients) -> str:
    by_category = {row[0]: row for row in metrics}
    lines = [f"# A7 version constraints: {dataset_id}", "",
             f"All {coverage['total']:,} dependency requirements were assigned one mutually exclusive constraint category.",
             f"Technical lag was calculable for {coverage['successful']:,} requirements; excluded rows were not treated as zero lag.", ""]
    for category in PRIMARY:
        row = by_category.get(category)
        if row:
            lines.append(f"{category}: {row[1]:,} calculable requirements, {row[4]:.2%} with lag, median positive version lag {row[5] or 0:.3g}.")
    lines += ["", f"The lag/no-lag omnibus comparison had Cramer's V={omnibus['cramers_v']:.3f} (p={omnibus['p_value']:.3g})."]
    if magnitude.get("p_value") is not None:
        lines.append(f"Conditional positive version lag differed across groups (Kruskal-Wallis p={magnitude['p_value']:.3g}); pairwise Cliff's delta values are in the statistical results.")
    interaction_rows = [row for row in gradients if str(row[0]).startswith("age_x_")]
    if interaction_rows:
        lines.append("Age-gradient interactions relative to caret are reported with package-clustered standard errors in age-gradient.csv.")
    lines += ["", "These are observational associations among historically reconstructable surviving releases, not causal effects of constraint syntax.",
              "Coverage can differ by constraint category, package age, ecosystem cohort, and survival in the current npm registry snapshot.", ""]
    return "\n".join(lines)


def run(dataset_id: str, normalized_dir: Path, lag_dir: Path, output_dir: Path,
        bootstrap_repetitions: int = 2000, seed: int = 20261027) -> dict[str, Any]:
    import duckdb
    import matplotlib
    import numpy as np
    import pyarrow as pa
    import pyarrow.parquet as pq
    import scipy

    if bootstrap_repetitions < 0:
        raise ValueError("bootstrap repetitions must be non-negative")
    normalized_dir, lag_dir = normalized_dir.resolve(), lag_dir.resolve()
    normalized_manifest = verify_manifest(normalized_dir, "normalization-manifest.json")
    lag_manifest = verify_manifest(lag_dir, "technical-lag-manifest.json")
    staging = atomic_directory(output_dir)
    db = duckdb.connect()
    db.execute("SET memory_limit='2GB'"); db.execute("SET threads=4")
    db.execute(f"SET temp_directory={sql_string(staging / '.spill')}")
    try:
        tables = staging / "tables"; tables.mkdir(parents=True)
        observations = tables / "analysis-observations.parquet"
        materialize_enriched(db, normalized_dir, lag_dir, observations)
        mapping = tables / "constraint-classification.parquet"
        unique_requirements = _classify(db, observations, mapping, staging, pa, pq)
        classified = tables / "classified-observations.parquet"
        db.execute(f"""COPY (SELECT o.*, c.constraint_category, c.constraint_subtype
            FROM read_parquet({sql_string(observations)}) o
            JOIN read_parquet({sql_string(mapping)}) c USING(requirement))
            TO {sql_string(classified)} (FORMAT PARQUET, COMPRESSION ZSTD)""")
        observations.unlink()
        db.execute(f"CREATE VIEW classified AS SELECT * FROM read_parquet({sql_string(classified)})")
        total, successful, unknown = db.execute("""SELECT count(*), count(*) FILTER (WHERE status='ok'),
            count(*) FILTER (WHERE constraint_category IS NULL OR constraint_category NOT IN
            ('exact','caret','tilde','wildcard','range','prerelease','unsupported','invalid')) FROM classified""").fetchone()
        if unknown:
            raise ValueError(f"unclassified requirements: {unknown}")
        coverage = {"total": total, "successful": successful, "excluded": total-successful,
                    "success_rate": successful/total if total else 0.0, "unique_requirements": unique_requirements}
        distribution = db.execute("""SELECT constraint_category, count(*), count(*)*1.0/sum(count(*)) OVER (),
            count(*) FILTER (WHERE status='ok'), avg((status='ok')::INTEGER)
            FROM classified GROUP BY 1 ORDER BY 2 DESC""").fetchall()
        write_csv(tables / "constraint-distribution.csv",
                  ("constraint_category", "n", "fraction", "successful", "success_fraction"), distribution)
        metrics = db.execute("""SELECT constraint_category, count(*),
            count(*) FILTER (WHERE version_lag>0), count(*) FILTER (WHERE version_lag=0),
            avg((version_lag>0)::INTEGER), median(version_lag) FILTER (WHERE version_lag>0),
            median(time_lag_days) FILTER (WHERE version_lag>0), avg(version_lag), avg(time_lag_days)
            FROM classified WHERE status='ok' GROUP BY 1 ORDER BY 1""").fetchall()
        write_csv(tables / "lag-by-constraint.csv", ("constraint_category", "n", "lagged", "no_lag",
                  "lag_fraction", "median_positive_version_lag", "median_positive_time_lag_days",
                  "mean_version_lag", "mean_time_lag_days"), metrics)
        composition = db.execute("""SELECT constraint_category, update_type, count(*),
            count(*)*1.0/sum(count(*)) OVER (PARTITION BY constraint_category)
            FROM classified WHERE status='ok' GROUP BY 1,2 ORDER BY 1,2""").fetchall()
        write_csv(tables / "update-types-by-constraint.csv",
                  ("constraint_category", "update_type", "n", "fraction"), composition)

        primary_counts = db.execute("""SELECT constraint_category,
            count(*) FILTER (WHERE version_lag=0), count(*) FILTER (WHERE version_lag>0)
            FROM classified WHERE status='ok' AND constraint_category IN ('exact','caret','tilde','wildcard','range')
            GROUP BY 1 ORDER BY 1""").fetchall()
        chi2, p_value, cramers_v = _cramers_v([[row[1], row[2]] for row in primary_counts])
        omnibus = {"chi_square": chi2, "p_value": p_value, "cramers_v": cramers_v,
                   "groups": [row[0] for row in primary_counts], "counts": [[row[1], row[2]] for row in primary_counts]}
        write_json(staging / "statistical-tests/lag-prevalence-omnibus.json", omnibus)
        cluster_rows = db.execute("""SELECT source_package, constraint_category, count(*),
            count(*) FILTER (WHERE version_lag>0) FROM classified
            WHERE status='ok' AND constraint_category IN ('exact','caret','tilde','wildcard','range') GROUP BY 1,2""").fetchall()
        risk = _cluster_risk_differences(cluster_rows, bootstrap_repetitions, seed)
        write_json(staging / "statistical-tests/risk-differences.json", risk)
        magnitude = _magnitude_tests(db)
        write_json(staging / "statistical-tests/version-lag-magnitude.json", magnitude)
        gradients = _age_gradient(db)
        write_csv(staging / "statistical-tests/age-gradient.csv",
                  ("term", "estimate", "cluster_se", "ci_low", "ci_high", "z", "p_value", "cells", "packages"), gradients)
        _plots(distribution, metrics, db, staging / "figures")
        (staging / "findings.md").write_text(_findings(dataset_id, coverage, metrics, omnibus, magnitude, gradients), encoding="utf-8")
        spill = staging / ".spill"
        if spill.exists():
            import shutil
            shutil.rmtree(spill)
        semver_version = subprocess.run(
            ["node", "-p", "require('./processing/technical_lag/node_modules/semver/package.json').version"],
            capture_output=True, text=True, check=True).stdout.strip()
        result = {
            "analysis": "version-constraints", "analysis_version": ANALYSIS_VERSION, "dataset_id": dataset_id,
            "inputs": {"normalization_manifest_sha256": digest(normalized_dir / "normalization-manifest.json"),
                       "technical_lag_manifest_sha256": digest(lag_dir / "technical-lag-manifest.json"),
                       "normalized_counts": normalized_manifest["report"]["counts"],
                       "technical_lag_rows": lag_manifest["report"]["rows"]},
            "parameters": {"bootstrap_repetitions": bootstrap_repetitions, "seed": seed,
                           "reference_category": "caret"},
            "runtime": {"python": platform.python_version(), "duckdb": duckdb.__version__,
                        "pyarrow": pa.__version__, "numpy": np.__version__, "scipy": scipy.__version__,
                        "matplotlib": matplotlib.__version__, "semver": semver_version},
            "coverage": coverage, "omnibus": omnibus, "risk_differences": risk,
        }
        result["outputs"] = output_records(staging)
        write_json(staging / "analysis-manifest.json", result)
        db.close(); finalize_directory(staging, output_dir)
        return result
    except BaseException:
        db.close(); cleanup_directory(staging); raise
