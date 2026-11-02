from __future__ import annotations

import gzip, json, math, platform, shutil
from pathlib import Path
from analysis.common import (ANALYSIS_VERSION, atomic_directory, cleanup_directory, configure_plotting,
    digest, finalize_directory, output_records, sql_string, verify_manifest, write_csv, write_json)


def _fixed_map(pilot: Path, security: Path, versions: Path, pa, pq, target: Path) -> dict:
    manifest = json.loads((pilot / "manifest.json").read_text())
    raw = {}
    for shard in manifest["shards"]["normalized"]["vulnerability"]:
        path = pilot / shard["path"]
        if digest(path) != shard["sha256"]: raise ValueError(f"OSV checksum mismatch: {shard['path']}")
        with gzip.open(path, "rt") as stream:
            for line in stream:
                item = json.loads(line); key = (item.get("package_name"), item.get("osv_id"))
                if all(isinstance(v, str) and v for v in key): raw.setdefault(key, set()).update(v for v in item.get("fixed_versions", []) if isinstance(v, str))
    dates = {(r["package_name"], r["version"]): r["published_at"] for r in pq.read_table(versions, columns=["package_name","version","published_at"]).to_pylist()}
    rows = []
    for row in pq.read_table(security, columns=["package_name","version","is_vulnerable","osv_ids","severity"]).to_pylist():
        if not row["is_vulnerable"]: continue
        candidates = [(dates[(row["package_name"], version)], version, osv) for osv in json.loads(row["osv_ids"])
                      for version in raw.get((row["package_name"], osv), set()) if (row["package_name"], version) in dates]
        fixed = min(candidates) if candidates else (None, None, None)
        rows.append({"package_name": row["package_name"], "version": row["version"], "fixed_published_at": fixed[0], "fixed_version": fixed[1], "fixed_osv_id": fixed[2], "severity_known": bool(json.loads(row["severity"]))})
    schema = pa.schema([(n, pa.string()) for n in ("package_name","version","fixed_published_at","fixed_version","fixed_osv_id")] + [("severity_known",pa.bool_())])
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), target, compression="zstd")
    return {"vulnerable_versions": len(rows), "dated_fixes": sum(r["fixed_published_at"] is not None for r in rows), "severity_known": sum(r["severity_known"] for r in rows)}


def cliffs_delta(left, right):
    import numpy as np
    x, y = np.asarray(left), np.asarray(right)
    return float(sum((a > y).sum() - (a < y).sum() for a in x) / (len(x) * len(y)))


def _plots(db, out: Path):
    import numpy as np
    plt = configure_plotting(); out.mkdir(parents=True, exist_ok=True)
    yearly = db.execute("SELECT year(try_cast(observation_time AS TIMESTAMPTZ)), is_vulnerable, count(*) FROM observations GROUP BY 1,2 ORDER BY 1,2").fetchall()
    years = sorted({r[0] for r in yearly}); rates=[]
    for year in years:
        rows=[r for r in yearly if r[0]==year]; rates.append(next((r[2] for r in rows if r[1]),0)/sum(r[2] for r in rows))
    fig,ax=plt.subplots(figsize=(7.2,4.2)); ax.plot(years,[100*r for r in rates],marker="o",color="#8b1e3f"); ax.set(title="Known-vulnerable permitted versions",xlabel="Observation year",ylabel="Observations, %"); fig.tight_layout(); fig.savefig(out/"vulnerability-prevalence-by-year.png"); plt.close(fig)
    groups=[np.asarray([r[0] for r in db.execute("SELECT version_lag FROM terminal WHERE is_vulnerable=?",[v]).fetchall()]) for v in (False,True)]
    fig,ax=plt.subplots(figsize=(6.5,4.2)); ax.boxplot(groups,tick_labels=["not vulnerable","vulnerable"],showfliers=False); ax.set(title="Terminal version lag by vulnerability",ylabel="Version lag"); fig.tight_layout(); fig.savefig(out/"terminal-lag-by-vulnerability.png"); plt.close(fig)
    values=np.asarray([r[0] for r in db.execute("SELECT days_after_fix FROM fixed_available").fetchall()],dtype=float)
    fig,ax=plt.subplots(figsize=(7.2,4.2)); ax.hist(np.log1p(values),bins=35,color="#d17a22"); ax.set(title="Vulnerable observations after fixed version",xlabel="log(1 + days after fix)",ylabel="Observations"); fig.tight_layout(); fig.savefig(out/"delay-after-fix.png"); plt.close(fig)


def run(dataset_id: str, temporal_dir: Path, security_dir: Path, normalized_dir: Path, pilot_dir: Path, output_dir: Path) -> dict:
    import duckdb, matplotlib, numpy as np, pyarrow as pa, pyarrow.parquet as pq, scipy
    from scipy.stats import mannwhitneyu
    temporal_dir,security_dir,normalized_dir,pilot_dir=(p.resolve() for p in (temporal_dir,security_dir,normalized_dir,pilot_dir))
    temporal=verify_manifest(temporal_dir,"temporal-manifest.json"); security=verify_manifest(security_dir,"security-manifest.json"); verify_manifest(normalized_dir,"normalization-manifest.json")
    staging=atomic_directory(output_dir); db=duckdb.connect(); db.execute("SET memory_limit='2GB'")
    try:
        fmap=staging/"tables"/"osv-fixed-map.parquet"; fmap.parent.mkdir(parents=True)
        fmap_stats=_fixed_map(pilot_dir,security_dir/"security_features.parquet",normalized_dir/"versions/part-00000.parquet",pa,pq,fmap)
        db.execute(f"""CREATE TABLE observations AS SELECT t.*,coalesce(s.is_vulnerable,false) AS is_vulnerable,s.osv_ids,s.severity FROM read_parquet({sql_string(temporal_dir/'temporal_technical_lag.parquet')}) t LEFT JOIN read_parquet({sql_string(security_dir/'security_features.parquet')}) s ON s.package_name=t.dependency_name AND s.version=t.v_req WHERE t.status='ok'""")
        db.execute("""CREATE TABLE terminal AS SELECT * EXCLUDE(rn) FROM (SELECT *,row_number() OVER(PARTITION BY source_package,source_version,dependency_type,dependency_name ORDER BY observation_time DESC) rn FROM observations) WHERE rn=1""")
        db.execute(f"""CREATE TABLE fixed_available AS SELECT o.*,date_diff('second',try_cast(f.fixed_published_at AS TIMESTAMPTZ),try_cast(o.observation_time AS TIMESTAMPTZ))/86400.0 AS days_after_fix FROM observations o JOIN read_parquet({sql_string(fmap)}) f ON f.package_name=o.dependency_name AND f.version=o.v_req WHERE f.fixed_published_at IS NOT NULL AND try_cast(f.fixed_published_at AS TIMESTAMPTZ)<=try_cast(o.observation_time AS TIMESTAMPTZ)""")
        total,vulnerable=db.execute("SELECT count(*),count(*) FILTER(WHERE is_vulnerable) FROM observations").fetchone()
        x=[r[0] for r in db.execute("SELECT version_lag FROM terminal WHERE is_vulnerable").fetchall()]; y=[r[0] for r in db.execute("SELECT version_lag FROM terminal WHERE NOT is_vulnerable").fetchall()]
        mw=mannwhitneyu(x,y,alternative="two-sided"); test={"test":"Mann-Whitney U (terminal relation snapshots)","u_statistic":float(mw.statistic),"p_value":float(mw.pvalue),"cliffs_delta":cliffs_delta(x,y),"vulnerable_n":len(x),"not_vulnerable_n":len(y)}
        count,median=db.execute("SELECT count(*),median(days_after_fix) FROM fixed_available").fetchone(); fixed={"available_fix_observations":count,"median_days_after_fix":float(median) if median is not None else None,**fmap_stats}
        coverage={"valid_observations":total,"vulnerable_observations":vulnerable,"vulnerable_fraction":vulnerable/total if total else 0,"terminal_relations":len(x)+len(y)}
        write_csv(staging/"tables"/"coverage.csv",tuple(coverage),[tuple(coverage.values())]); write_csv(staging/"tables"/"lag-by-vulnerability.csv",("group","n","median_version_lag","mean_version_lag"),db.execute("SELECT CASE WHEN is_vulnerable THEN 'vulnerable' ELSE 'not_vulnerable' END,count(*),median(version_lag),avg(version_lag) FROM terminal GROUP BY 1 ORDER BY 1").fetchall()); write_csv(staging/"tables"/"fixed-availability.csv",tuple(fixed),[tuple(fixed.values())]); write_json(staging/"statistical-tests"/"lag-distribution-test.json",test); _plots(db,staging/"figures")
        effect=abs(test["cliffs_delta"]); label="negligible" if effect<.147 else "small" if effect<.33 else "medium" if effect<.474 else "large"
        (staging/"conclusions.md").write_text(f"# Vulnerability and technical lag: {dataset_id}\n\nOf {total:,} valid observations, {vulnerable:,} ({coverage['vulnerable_fraction']:.2%}) used a version known vulnerable in OSV. Mann–Whitney U on terminal relations: p={test['p_value']:.3g}, Cliff's delta={test['cliffs_delta']:.3f} ({label} effect).\n\nA fixed npm version was already published for {count:,} vulnerable observations; median time retaining a vulnerable permitted version after fixed-version publication was {fixed['median_days_after_fix']:.1f} days. This is an observational association: `v_req` is reconstructed, not a lock-file resolution.\n\n## Limitations\n\nOSV is limited to pilot packages and exact npm name matching; Git ranges are excluded. Severity is heterogeneous (often a CVSS vector without a numerical score), so it is reported as coverage rather than compared by level. Fixed versions are counted only when explicitly present in OSV and found in saved npm history. No causal claim is made.\n",encoding="utf-8")
        result={"analysis":"vulnerabilities","analysis_version":ANALYSIS_VERSION,"dataset_id":dataset_id,"inputs":{"temporal_manifest_sha256":digest(temporal_dir/'temporal-manifest.json'),"security_manifest_sha256":digest(security_dir/'security-manifest.json')},"coverage":coverage,"fixed_version_analysis":fixed,"statistical_test":test,"runtime":{"python":platform.python_version(),"duckdb":duckdb.__version__,"numpy":np.__version__,"scipy":scipy.__version__,"matplotlib":matplotlib.__version__,"pyarrow":pa.__version__}}
        result["outputs"]=output_records(staging); write_json(staging/"analysis-manifest.json",result); db.close(); finalize_directory(staging,output_dir); return result
    except BaseException: db.close(); cleanup_directory(staging); raise
