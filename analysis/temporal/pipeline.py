from __future__ import annotations

import json
import math
import platform
import shutil
from collections import Counter
from pathlib import Path
from typing import Iterable

from analysis.common import (ANALYSIS_VERSION, atomic_directory, cleanup_directory,
                             configure_plotting, digest, finalize_directory,
                             output_records, sql_string, verify_manifest,
                             write_csv, write_json)


def classify_scenario(lags: Iterable[int]) -> str:
    """Classify a relation from its valid snapshots, ordered in time."""
    values = list(lags)
    if not values:
        return "other"
    if max(values) == 0:
        return "always_current"
    if values[0] == 0 and values[-1] > 0:
        return "accumulation"
    if max(values) > 0 and values[-1] == 0:
        return "catch_up"
    if values[0] > 0 and values[-1] > 0:
        return "persistent_lag"
    return "other"


def _spearman(rows, x_index: int, y_index: int, label: str) -> dict:
    from scipy.stats import spearmanr

    values = [(float(row[x_index]), float(row[y_index])) for row in rows
              if row[x_index] is not None and row[y_index] is not None
              and math.isfinite(float(row[x_index])) and math.isfinite(float(row[y_index]))]
    statistic = spearmanr([v[0] for v in values], [v[1] for v in values])
    return {"hypothesis": label, "test": "Spearman rank correlation (package-level)",
            "n": len(values), "effect": float(statistic.statistic), "effect_name": "rho",
            "p_value": float(statistic.pvalue)}


def _plots(db, figures: Path) -> None:
    import numpy as np

    plt = configure_plotting()
    figures.mkdir(parents=True, exist_ok=True)
    yearly = db.execute("""SELECT year(try_cast(observation_time AS TIMESTAMPTZ)) AS year,
        median(version_lag), avg((version_lag > 0)::INTEGER), count(*)
        FROM terminal GROUP BY 1 ORDER BY 1""").fetchall()
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.1))
    axes[0].plot([r[0] for r in yearly], [r[1] for r in yearly], marker="o", color="#8b1e3f")
    axes[0].set(title="Median terminal version lag", xlabel="Terminal-observation year", ylabel="Versions")
    axes[1].plot([r[0] for r in yearly], [100*r[2] for r in yearly], marker="o", color="#2667a8")
    axes[1].set(title="Lag prevalence over time", xlabel="Terminal-observation year", ylabel="Relations with lag, %")
    fig.tight_layout(); fig.savefig(figures / "terminal-lag-by-year.png"); plt.close(fig)

    scenarios = db.execute("SELECT scenario, count(*) FROM scenarios GROUP BY 1 ORDER BY 2 DESC").fetchall()
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.bar([r[0].replace('_', '\n') for r in scenarios], [r[1] for r in scenarios], color="#5b8c5a")
    ax.set(title="Temporal scenarios of dependency relations", ylabel="Relations")
    fig.tight_layout(); fig.savefig(figures / "scenario-distribution.png"); plt.close(fig)

    progression = db.execute("""WITH ranked AS (
        SELECT version_lag, row_number() OVER relation AS position
        FROM valid WINDOW relation AS (PARTITION BY relation_id ORDER BY observation_time))
        SELECT position, median(version_lag), avg((version_lag>0)::INTEGER), count(*) FROM ranked GROUP BY 1 ORDER BY 1""").fetchall()
    fig, ax = plt.subplots(figsize=(7.2, 4.2))
    ax.plot([r[0] for r in progression], [r[1] for r in progression], marker="o", color="#d17a22")
    ax.set(title="Lag over successive snapshots", xlabel="Snapshot position within relation", ylabel="Median version lag")
    fig.tight_layout(); fig.savefig(figures / "lag-by-snapshot-position.png"); plt.close(fig)

    deciles = db.execute("SELECT feature, decile, avg(mean_terminal_lag) FROM feature_deciles GROUP BY 1,2 ORDER BY 1,2").fetchall()
    labels = {"dependency_release_rate": "Dependency releases/year", "consumer_age_years": "Consumer age, years", "dependency_count": "Dependencies"}
    fig, axes = plt.subplots(1, 3, figsize=(13, 4.2), sharey=True)
    for ax, feature in zip(axes, labels):
        values = [r for r in deciles if r[0] == feature]
        ax.plot([r[1] for r in values], [r[2] for r in values], marker="o", color="#376996")
        ax.set(title=labels[feature], xlabel="Feature decile", ylabel="Mean terminal lag")
    fig.suptitle("Package-level terminal lag by factor decile")
    fig.tight_layout(); fig.savefig(figures / "factor-gradients.png"); plt.close(fig)


def _conclusions(dataset_id: str, coverage: dict, tests: list[dict], scenarios: list[tuple]) -> str:
    lookup = {item["hypothesis"]: item for item in tests}
    scenario_text = "; ".join(f"{name}: {count:,}" for name, count in scenarios)
    lines = [f"# Temporal analysis: {dataset_id}", "",
             f"В анализ вошли {coverage['relations']:,} dependency relations и {coverage['valid_snapshots']:,} пригодных временных снимков. "
             f"Terminal lag рассчитан для {coverage['terminal_relations']:,} relations.", "",
             "## Типовые сценарии", "",
             f"Распределение: {scenario_text}.",
             "`always_current` означает отсутствие lag во всех пригодных снимках; `accumulation` — переход от нуля к положительному terminal lag; "
             "`catch_up` — положительный lag с последующим возвратом к нулю; `persistent_lag` — lag как в первом, так и в последнем пригодном состоянии.", "",
             "## Проверка гипотез", ""]
    text = {
        "H1_release_frequency": "H1: у dependency packages с более высокой исторической частотой релизов terminal version lag выше.",
        "H2_package_age": "H2: у более старых consumer packages средний terminal version lag выше.",
        "H3_dependency_count": "H3: у consumer packages с большим числом зависимостей средний terminal version lag выше.",
    }
    for key in ("H1_release_frequency", "H2_package_age", "H3_dependency_count"):
        item = lookup[key]
        practical = "малый" if abs(item['effect']) < .1 else "умеренный" if abs(item['effect']) < .3 else "заметный"
        decision = "есть статистические основания отвергнуть нулевую гипотезу" if item['p_value'] < .05 else "статистических оснований отвергнуть нулевую гипотезу нет"
        lines += [f"### {key}", "", text[key], "",
                  f"Тест: {item['test']}; n={item['n']:,}; rho={item['effect']:.3f}; p={item['p_value']:.3g}. ",
                  f"При α=0,05 {decision}. Размер эффекта {practical}; статистическая значимость при большом n не равна практической значимости.", ""]
    lines += ["## Ограничения", "",
              "Это наблюдательный анализ pilot sample, а не причинный эксперимент. Временная история восстановлена из сохранённого npm Registry snapshot; удалённые версии и исправленные позже метаданные могут отсутствовать. "
              "Связи тестируются на package-level средних, поэтому не описывают фактически установленные версии, lock-файлы или полный dependency graph.", ""]
    return "\n".join(lines)


def run(dataset_id: str, temporal_dir: Path, normalized_dir: Path, output_dir: Path) -> dict:
    import duckdb
    import matplotlib
    import numpy as np
    import pyarrow
    import scipy

    temporal_dir, normalized_dir = temporal_dir.resolve(), normalized_dir.resolve()
    temporal_manifest = verify_manifest(temporal_dir, "temporal-manifest.json")
    normalized_manifest = verify_manifest(normalized_dir, "normalization-manifest.json")
    staging = atomic_directory(output_dir)
    db = duckdb.connect(); db.execute("SET memory_limit='2GB'"); db.execute("SET threads=4")
    db.execute(f"SET temp_directory={sql_string(staging / '.spill')}")
    try:
        temporal = sql_string(temporal_dir / "temporal_technical_lag.parquet")
        versions = sql_string(normalized_dir / "versions/*.parquet")
        packages = sql_string(normalized_dir / "packages/*.parquet")
        db.execute(f"""CREATE VIEW source AS SELECT *, concat_ws('|', source_package, source_version, dependency_type, dependency_name) AS relation_id
            FROM read_parquet({temporal})""")
        db.execute("CREATE TABLE valid AS SELECT * FROM source WHERE status='ok' AND version_lag IS NOT NULL")
        db.execute("""CREATE TABLE terminal AS SELECT * EXCLUDE(rank) FROM (
            SELECT *, row_number() OVER (PARTITION BY relation_id ORDER BY observation_time DESC) AS rank FROM valid) WHERE rank=1""")
        rows = db.execute("SELECT relation_id, list(version_lag ORDER BY observation_time) FROM valid GROUP BY 1").fetchall()
        scenario_rows = [(key, classify_scenario(values)) for key, values in rows]
        db.execute("CREATE TABLE scenarios(relation_id VARCHAR, scenario VARCHAR)")
        db.executemany("INSERT INTO scenarios VALUES (?, ?)", scenario_rows)
        db.execute("""CREATE TABLE relation_features AS
            WITH dependency_cadence AS (
              SELECT t.relation_id, count(v.version) AS release_count,
                date_diff('second', min(try_cast(v.published_at AS TIMESTAMPTZ)), try_cast(t.observation_time AS TIMESTAMPTZ)) / (365.2425*86400.0) AS release_span_years
              FROM terminal t LEFT JOIN read_parquet(""" + versions + """) v ON v.package_name=t.dependency_name
                AND try_cast(v.published_at AS TIMESTAMPTZ) <= try_cast(t.observation_time AS TIMESTAMPTZ)
              GROUP BY 1, t.observation_time),
            dependency_counts AS (SELECT source_package, source_version, count(DISTINCT dependency_name) AS dependency_count FROM terminal GROUP BY 1,2)
            SELECT t.*, s.scenario, greatest(0.0, date_diff('second', try_cast(p.created_at AS TIMESTAMPTZ), try_cast(t.observation_time AS TIMESTAMPTZ))/(365.2425*86400.0)) AS consumer_age_years,
              CASE WHEN c.release_span_years>0 THEN (c.release_count-1)/c.release_span_years END AS dependency_release_rate,
              d.dependency_count
            FROM terminal t JOIN scenarios s USING(relation_id)
            LEFT JOIN read_parquet(""" + packages + """) p ON p.package_name=t.source_package
            LEFT JOIN dependency_cadence c USING(relation_id) LEFT JOIN dependency_counts d USING(source_package,source_version)""")
        db.execute("""CREATE TABLE package_features AS SELECT source_package,
            avg(version_lag)::DOUBLE AS mean_terminal_lag, avg((version_lag>0)::INTEGER)::DOUBLE AS lag_fraction,
            avg(consumer_age_years)::DOUBLE AS consumer_age_years, avg(dependency_count)::DOUBLE AS dependency_count,
            avg(dependency_release_rate)::DOUBLE AS dependency_release_rate
            FROM relation_features GROUP BY 1""")
        features = ("dependency_release_rate", "consumer_age_years", "dependency_count")
        db.execute("CREATE TABLE feature_deciles(feature VARCHAR, decile INTEGER, mean_terminal_lag DOUBLE)")
        test_rows = []
        labels = {"dependency_release_rate": "H1_release_frequency", "consumer_age_years": "H2_package_age", "dependency_count": "H3_dependency_count"}
        for feature in features:
            values = db.execute(f"SELECT {feature}, mean_terminal_lag FROM package_features WHERE {feature} IS NOT NULL").fetchall()
            test_rows.append(_spearman(values, 0, 1, labels[feature]))
            bins = db.execute(f"""WITH ranked AS (SELECT {feature}, mean_terminal_lag, ntile(10) OVER (ORDER BY {feature}) AS decile
                FROM package_features WHERE {feature} IS NOT NULL) SELECT decile, avg(mean_terminal_lag) FROM ranked GROUP BY 1 ORDER BY 1""").fetchall()
            db.executemany("INSERT INTO feature_deciles VALUES (?, ?, ?)", [(feature, *row) for row in bins])
        scenario_counts = db.execute("SELECT scenario, count(*) FROM scenarios GROUP BY 1 ORDER BY 2 DESC").fetchall()
        coverage = {"relations": db.execute("SELECT count(DISTINCT relation_id) FROM source").fetchone()[0],
                    "valid_snapshots": db.execute("SELECT count(*) FROM valid").fetchone()[0],
                    "terminal_relations": db.execute("SELECT count(*) FROM terminal").fetchone()[0]}
        tables = staging / "tables"; tables.mkdir(parents=True)
        write_csv(tables / "temporal-scenarios.csv", ("scenario", "relations"), scenario_counts)
        write_csv(tables / "hypothesis-tests.csv", ("hypothesis", "test", "n", "effect", "effect_name", "p_value"),
                  [(r["hypothesis"], r["test"], r["n"], r["effect"], r["effect_name"], r["p_value"]) for r in test_rows])
        deciles = db.execute("SELECT feature, decile, mean_terminal_lag FROM feature_deciles ORDER BY 1,2").fetchall()
        write_csv(tables / "factor-deciles.csv", ("feature", "decile", "mean_terminal_lag"), deciles)
        annual = db.execute("SELECT year(try_cast(observation_time AS TIMESTAMPTZ)) AS year, median(version_lag), avg((version_lag>0)::INTEGER), count(*) FROM terminal GROUP BY 1 ORDER BY 1").fetchall()
        write_csv(tables / "terminal-lag-by-year.csv", ("year", "median_version_lag", "lag_fraction", "relations"), annual)
        write_json(staging / "statistical-tests" / "hypotheses.json", test_rows)
        _plots(db, staging / "figures")
        (staging / "conclusions.md").write_text(_conclusions(dataset_id, coverage, test_rows, scenario_counts), encoding="utf-8")
        spill = staging / ".spill"
        if spill.exists(): shutil.rmtree(spill)
        result = {"analysis": "temporal", "analysis_version": ANALYSIS_VERSION, "dataset_id": dataset_id,
                  "inputs": {"temporal_manifest_sha256": digest(temporal_dir / "temporal-manifest.json"),
                             "normalization_manifest_sha256": digest(normalized_dir / "normalization-manifest.json"),
                             "temporal_observations": temporal_manifest["report"]["temporal_observations"],
                             "normalized_counts": normalized_manifest["report"]["counts"]},
                  "coverage": coverage, "hypotheses": test_rows,
                  "runtime": {"python": platform.python_version(), "duckdb": duckdb.__version__, "numpy": np.__version__, "scipy": scipy.__version__, "matplotlib": matplotlib.__version__, "pyarrow": pyarrow.__version__}}
        result["outputs"] = output_records(staging); write_json(staging / "analysis-manifest.json", result)
        db.close(); finalize_directory(staging, output_dir); return result
    except BaseException:
        db.close(); cleanup_directory(staging); raise
