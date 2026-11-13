"""Maintainer-only deterministic projection of published data, never synthetic records."""
import json,shutil,subprocess
from pathlib import Path
import duckdb,pyarrow as pa,pyarrow.parquet as pq
from analysis.common import digest,output_records,write_json,verify_manifest,sql_string

def export(output):
    if output.exists():raise ValueError('refusing to overwrite frozen distribution inputs')
    output.mkdir(parents=True)
    # Obtain tiny OSV files from the already published pilot cache, not live APIs.
    cache=Path('.dvc/cache/files/md5')
    index=json.loads((cache/'47/32d00dbe9aaa96a016c65c9e28eab8.dir').read_text())
    lookup={x['relpath']:cache/x['md5'][:2]/x['md5'][2:] for x in index}
    pilot_manifest=lookup['manifest.json'];original=json.loads(pilot_manifest.read_text())
    osv=output/'pilot-osv';osv.mkdir()
    for s in original['shards']['normalized']['vulnerability']:
        source=lookup[s['path']]
        if digest(source)!=s['sha256']:raise ValueError('pilot OSV hash mismatch')
        target=osv/s['path'];target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(source,target)
    write_json(osv/'manifest.json',{'shards':{'normalized':{'vulnerability':original['shards']['normalized']['vulnerability']}},
        'distribution_projection':{'original_pilot_manifest_sha256':digest(pilot_manifest),'original_dvc_md5':'4732d00dbe9aaa96a016c65c9e28eab8.dir','note':'unchanged complete OSV shards; npm/deps.dev archives omitted'}})
    n=Path('data/datasets/pilot-v1-normalized/normalized');verify_manifest(n,'normalization-manifest.json')
    db=duckdb.connect();db.execute("SET memory_limit='1GB'");db.execute('SET threads=2')
    for name,path in [('inputs','technical_lag_inputs.parquet'),('packages','packages/*.parquet'),('versions','versions/*.parquet'),('quality','version_quality.parquet'),('requirements','dependency_requirements/*.parquet')]:
        db.execute(f'CREATE VIEW {name} AS SELECT * FROM read_parquet({sql_string(n/path)})')
    import gzip
    records=[json.loads(line) for s in original['shards']['normalized']['vulnerability'] for line in gzip.open(osv/s['path'],'rt')]
    db.register('osv_names',pa.table({'dependency_name':sorted({r['package_name'] for r in records})}))
    db.execute("""CREATE TEMP TABLE consumers AS SELECT source_package FROM inputs GROUP BY 1
        HAVING count(*) BETWEEN 50 AND 700 AND count(*) FILTER(WHERE dependency_type='dependencies' AND preparation_exclusion_reason IS NULL AND dependency_name IN (SELECT dependency_name FROM osv_names))>=5
        ORDER BY md5(source_package),source_package LIMIT 25""")
    db.execute('CREATE TEMP TABLE selected_inputs AS SELECT * FROM inputs WHERE source_package IN (SELECT source_package FROM consumers)')
    db.execute('CREATE TEMP TABLE names AS SELECT source_package package_name FROM consumers UNION SELECT dependency_name FROM selected_inputs WHERE dependency_package_matched')
    dest=output/'demo-v1'/'normalized';dest.mkdir(parents=True)
    queries={
        'packages/part-00000.parquet':'SELECT * FROM packages WHERE package_name IN (SELECT package_name FROM names)',
        'versions/part-00000.parquet':'SELECT * FROM versions WHERE package_name IN (SELECT package_name FROM names)',
        'version_quality.parquet':'SELECT * FROM quality WHERE package_name IN (SELECT package_name FROM names)',
        'dependency_requirements/part-00000.parquet':'SELECT * FROM requirements WHERE source_package IN (SELECT source_package FROM consumers)',
        'technical_lag_inputs.parquet':'SELECT * FROM selected_inputs'}
    for path,q in queries.items():
        target=dest/path;target.parent.mkdir(parents=True,exist_ok=True)
        pq.write_table(db.execute(q).fetch_arrow_table(),target,compression='zstd')
    counts={name:db.execute(q.replace('SELECT *','SELECT count(*)',1)).fetchone()[0] for name,q in [('packages',queries['packages/part-00000.parquet']),('versions',queries['versions/part-00000.parquet']),('dependency_requirements',queries['technical_lag_inputs.parquet'])]}
    counts['dependency_relations']=0
    write_json(dest/'normalization-manifest.json',{'build':{'distribution':'demo-v1','source_manifest_sha256':digest(n/'normalization-manifest.json'),
        'selection':'25 consumers ordered by MD5(name), 50..700 requirements, >=5 eligible production requirements targeting OSV packages; retain all consumer requirements and complete matched dependency histories'},'report':{'counts':counts},'outputs':output_records(dest)})
    ml=output/'demo-v1'/'ml';ml.mkdir()
    source=Path('artifacts/ml/full-v1-corrected-v2');verify_manifest(source/'evaluation','run-manifest.json');verify_manifest(source/'dataset','dataset-manifest.json')
    db.execute(f"CREATE VIEW ml AS SELECT * FROM read_parquet({sql_string(source/'dataset/ml_dataset.parquet')})")
    pq.write_table(db.execute("SELECT * FROM ml WHERE split='test' ORDER BY md5(source_package||'|'||dependency_name),source_package,dependency_name LIMIT 500").fetch_arrow_table(),ml/'ml_dataset.parquet',compression='zstd')
    for name in ('logistic.joblib','tree.joblib','run-manifest.json','metrics.json'):
        shutil.copyfile(source/'evaluation'/name,ml/name)
    shutil.copyfile(source/'dataset/class-distribution.csv',ml/'source-class-distribution.csv')
    write_json(ml/'demo-ml-manifest.json',{'source_run_manifest_sha256':digest(source/'evaluation/run-manifest.json'),
        'source_dataset_manifest_sha256':digest(source/'dataset/dataset-manifest.json'),'rows':500,'purpose':'inference demonstration on fixed holdout projection, not a replacement final evaluation',
        'outputs':output_records(ml)})
    write_json(output/'demo-v1/distribution-manifest.json',{'id':'demo-v1','counts':counts,'consumers':[r[0] for r in db.execute('SELECT source_package FROM consumers ORDER BY 1').fetchall()],
        'osv_projection_manifest_sha256':digest(osv/'manifest.json'),'exporter_sha256':digest(Path(__file__)),
        'outputs':output_records(output/'demo-v1')})
    db.close()
    print(json.dumps(counts))
if __name__=='__main__':export(Path('data/distribution'))
