"""Point-in-time features from immutable npm histories; A1 core is shared."""
import json,subprocess,shutil
from pathlib import Path
from analysis.common import sql_string,write_csv
from processing.technical_lag.pipeline import _write_histories

def classify_requirements(requirements):
    values=sorted(set(requirements))
    result=subprocess.run(['node','analysis/version_constraints/classifier.cjs'],
        input=''.join(json.dumps(x)+'\n' for x in values),capture_output=True,text=True,check=True)
    return {r['requirement']:r['category'] for r in map(json.loads,result.stdout.splitlines())}

def add_history(db, staging):
    """anchors + packages/quality/inputs views must refer to verified full-v1."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    from datetime import datetime
    db.execute('CREATE VIEW versions AS SELECT * FROM quality')
    db.execute('CREATE TEMP TABLE needed_dependencies AS SELECT DISTINCT dependency_name package_name FROM anchors')
    history=staging/'.histories.jsonl'
    _write_histories(db,history)
    db.execute("""CREATE TEMP TABLE prior_times AS SELECT source_package,dependency_name,consumer_retrieved_at,days,
        try_cast(observation_time AS TIMESTAMPTZ)-days*INTERVAL '1 day' cutoff_time,
        strftime(try_cast(observation_time AS TIMESTAMPTZ)-days*INTERVAL '1 day','%Y-%m-%dT%H:%M:%S.%fZ') cutoff
        FROM anchors CROSS JOIN (VALUES(30),(90)) h(days)""")
    db.execute("""CREATE VIEW stable_history AS SELECT package_name,version,published_at,try_cast(published_at AS TIMESTAMPTZ) t,
        count(*) OVER(PARTITION BY package_name,published_at)>1 tied FROM quality WHERE version_valid AND NOT is_prerelease""")
    db.execute("""CREATE TEMP TABLE prior_versions AS SELECT a.*,s.version prior_version,s.published_at prior_published_at,s.tied
        FROM prior_times a ASOF LEFT JOIN stable_history s ON a.source_package=s.package_name AND a.cutoff_time>=s.t""")
    query="""SELECT a.* EXCLUDE(cutoff_time),r.requirement,r.dependency_type,coalesce(r.shadowed_by_optional,false) shadowed_by_optional
        FROM prior_versions a LEFT JOIN inputs r ON a.source_package=r.source_package AND a.prior_version=r.source_version
        AND a.dependency_name=r.dependency_name
        QUALIFY row_number() OVER(PARTITION BY a.source_package,a.dependency_name,a.days
          ORDER BY CASE r.dependency_type WHEN 'dependencies' THEN 0 WHEN 'optionalDependencies' THEN 1 WHEN 'peerDependencies' THEN 2 ELSE 3 END)=1"""
    candidate=staging/'.prior-input.jsonl';output=staging/'.prior-output.jsonl'
    cur=db.execute(query);columns=[d[0] for d in cur.description]
    with candidate.open('w') as stream:
        for row in cur.fetchall():
            record=dict(zip(columns,row))
            if record['prior_published_at'] is not None:
                assert datetime.fromisoformat(record['prior_published_at'].replace('Z','+00:00'))<=datetime.fromisoformat(record['cutoff'].replace('Z','+00:00'))
            stream.write(json.dumps(record)+'\n')
    with candidate.open('rb') as source,output.open('wb') as target:
        subprocess.run(['node','ml/history.cjs',str(history)],stdin=source,stdout=target,check=True)
    records=[json.loads(line) for line in output.read_text().splitlines()]
    assert len(records)==2*db.execute('SELECT count(*) FROM anchors').fetchone()[0]
    db.register('prior_results',pa.Table.from_pylist(records,schema=pa.schema([
        ('source_package',pa.string()),('dependency_name',pa.string()),('days',pa.int64()),
        ('version_lag',pa.int64()),('major',pa.int64()),('reason',pa.string())])))
    db.execute("""CREATE TEMP TABLE priors AS SELECT source_package,dependency_name,
        max(version_lag) FILTER(WHERE days=30) prior_version_lag_30d,
        max(version_lag) FILTER(WHERE days=90) prior_version_lag_90d,
        max(major) FILTER(WHERE days=30) prior_major_30d FROM prior_results GROUP BY 1,2""")
    db.execute('CREATE VIEW prior_input_audit AS SELECT * FROM read_json_auto('+sql_string(candidate)+')')
    audit=db.execute('SELECT i.source_package,i.dependency_name,i.days,i.cutoff,i.prior_version,i.prior_published_at,i.requirement,r.version_lag,r.major,r.reason FROM prior_input_audit i JOIN prior_results r USING(source_package,dependency_name,days)').fetch_arrow_table()
    pq.write_table(audit,staging/'prior-feature-audit.parquet',compression='zstd')
    cur=db.execute('SELECT days,reason,count(*) observations FROM prior_results GROUP BY 1,2 ORDER BY 1,2')
    write_csv(staging/'prior-feature-coverage.csv',[d[0] for d in cur.description],cur.fetchall())
    for p in (candidate,output,history):p.unlink()
