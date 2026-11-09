"""Reproduce a bounded, pre-training ML scope audit. No models are fitted."""
import csv,json,subprocess,logging
from pathlib import Path
import duckdb,pyarrow as pa
from analysis.common import verify_manifest,digest,write_csv,write_json,output_records,atomic_directory,finalize_directory
logging.basicConfig(level=logging.INFO)
def run(output,limit=60000):
 n=Path('data/datasets/full-v1-normalized/normalized');l=Path('data/datasets/full-v1-technical-lag/technical_lag')
 verify_manifest(n,'normalization-manifest.json');verify_manifest(l,'technical-lag-manifest.json')
 out=atomic_directory(output);db=duckdb.connect();db.execute("SET memory_limit='2GB'");db.execute('SET threads=2');db.execute("SET TimeZone='UTC'")
 db.execute(f"SET temp_directory='{out}/spill'")
 db.execute(f"CREATE VIEW lag AS SELECT * FROM read_parquet('{l}/technical_lag.parquet')")
 db.execute(f"CREATE VIEW quality AS SELECT * FROM read_parquet('{n}/version_quality.parquet')")
 db.execute(f"CREATE VIEW req AS SELECT * FROM read_parquet('{n}/technical_lag_inputs.parquet')")
 db.execute(f"CREATE VIEW packages AS SELECT * FROM read_parquet('{n}/packages/*.parquet')")
 funnel=[]
 for name,condition in [('all','true'),('stable_consumer','consumer_version_valid AND NOT consumer_is_prerelease AND observation_time IS NOT NULL'),('production','consumer_version_valid AND NOT consumer_is_prerelease AND observation_time IS NOT NULL AND dependency_type=\'dependencies\' AND NOT shadowed_by_optional'),('calculable',"status='ok'"),('current_major',"status='ok' AND update_type='major'")]:
  funnel.append((name,db.execute(f'SELECT count(*) FROM lag WHERE {condition}').fetchone()[0]))
 # Earliest observed major-lag incident per consumer/dependency; selection uses no future outcome.
 db.execute("""CREATE TEMP TABLE incidents AS SELECT source_package,dependency_name,
 arg_min(struct_pack(source_version,observation_time,current_version_lag:=version_lag,current_time_lag_days:=time_lag_days,current_requirement:=requirement,consumer_retrieved_at:=consumer_provenance.retrieved_at,dependency_retrieved_at:=dependency_provenance.retrieved_at),struct_pack(t:=try_cast(observation_time AS TIMESTAMP_NS),source_version)) AS a
 FROM lag WHERE status='ok' AND update_type='major' GROUP BY 1,2""")
 funnel.append(('unique_first_major_incidents',db.execute('SELECT count(*) FROM incidents').fetchone()[0]))
 db.execute(f"CREATE TEMP TABLE anchors AS SELECT source_package,dependency_name,a.* FROM incidents ORDER BY md5(source_package||'|'||dependency_name),source_package,dependency_name LIMIT {int(limit)}")
 funnel.append(('deterministic_scope_sample',db.execute('SELECT count(*) FROM anchors').fetchone()[0]))
 db.execute("""CREATE TEMP TABLE endpoints AS SELECT *,horizon,try_cast(observation_time AS TIMESTAMPTZ)+horizon*INTERVAL '1 day' AS endpoint_time,
 strftime(try_cast(observation_time AS TIMESTAMPTZ)+horizon*INTERVAL '1 day','%Y-%m-%dT%H:%M:%S.%fZ') endpoint,
 try_cast(observation_time AS TIMESTAMPTZ)+horizon*INTERVAL '1 day'>least(try_cast(consumer_retrieved_at AS TIMESTAMPTZ),try_cast(dependency_retrieved_at AS TIMESTAMPTZ)) censored
 FROM anchors CROSS JOIN (VALUES(90),(180)) h(horizon)""")
 db.execute("""CREATE TEMP TABLE stable AS SELECT package_name,version,published_at,try_cast(published_at AS TIMESTAMPTZ) t,
 count(*) OVER(PARTITION BY package_name,published_at)>1 tied FROM quality WHERE version_valid AND NOT is_prerelease""")
 db.execute("""CREATE TEMP TABLE future AS SELECT e.*,s.version future_version,s.published_at future_published_at,s.tied
 FROM endpoints e ASOF LEFT JOIN stable s ON e.source_package=s.package_name AND e.endpoint_time>=s.t""")
 cur=db.execute("""SELECT f.* EXCLUDE(endpoint_time),r.requirement,r.dependency_type,coalesce(r.shadowed_by_optional,false) shadowed_by_optional FROM future f LEFT JOIN req r
 ON f.source_package=r.source_package AND f.future_version=r.source_version AND f.dependency_name=r.dependency_name
 QUALIFY row_number() OVER(PARTITION BY f.source_package,f.dependency_name,f.horizon ORDER BY CASE r.dependency_type WHEN 'dependencies' THEN 0 WHEN 'optionalDependencies' THEN 1 WHEN 'peerDependencies' THEN 2 ELSE 3 END)=1""")
 rows=[dict(zip([x[0] for x in cur.description],r)) for r in cur.fetchall()]
 db.register('needed',pa.table({'package_name':sorted({r['dependency_name'] for r in rows})}))
 hs=db.execute("""SELECT p.package_name,first(p.provenance),list(struct_pack(version,published_at)) FROM packages p JOIN needed USING(package_name) JOIN quality USING(package_name) GROUP BY 1""").fetchall()
 histories=[dict(package_name=k,provenance=p,versions=v) for k,p,v in hs]
 logging.info('Evaluating %d endpoints',len(rows))
 result=json.loads(subprocess.run(['node','ml/scope/evaluate.cjs'],input=json.dumps(dict(rows=rows,histories=histories)),capture_output=True,text=True,check=True).stdout)
 # Row-level audit is local only; final models must build a separately versioned dataset.
 write_json(out/'endpoint-audit.json',result)
 db.register('outcomes',pa.Table.from_pylist(result))
 def export(name,q):
  c=db.execute(q);write_csv(out/(name+'.csv'),[x[0] for x in c.description],c.fetchall())
 write_csv(out/'population.csv',('filter','observations'),funnel)
 export('horizons',"SELECT horizon,reason,label,count(*) observations FROM outcomes GROUP BY 1,2,3 ORDER BY 1,2,3")
 export('dates',"SELECT min(observation_time) first_anchor,max(observation_time) last_anchor,min(endpoint) first_endpoint,max(endpoint) last_endpoint FROM outcomes")
 export('yearly',"SELECT horizon,left(observation_time,4) AS anchor_year,label,count(*) observations FROM outcomes GROUP BY 1,2,3 ORDER BY 1,2,3")
 # Fixed calendar eras; embargo ensures training outcomes precede the next prediction era.
 export('split',"""SELECT horizon,CASE WHEN observation_time<'2019-07-05T00:00:00Z' THEN 'train'
 WHEN observation_time<'2020-01-01T00:00:00Z' THEN 'purged'
 WHEN observation_time<'2022-07-05T00:00:00Z' THEN 'validation'
 WHEN observation_time<'2023-01-01T00:00:00Z' THEN 'purged' ELSE 'test' END split,
 label,count(*) observations,min(observation_time) min_t,max(observation_time) max_t
 FROM outcomes GROUP BY 1,2,3 ORDER BY 1,2,3""")
 db.close()
 import shutil
 shutil.rmtree(out/'spill',ignore_errors=True)
 write_json(out/'analysis-manifest.json',{'analysis':'ml-scope','limit':limit,'input_manifests':{str(p):digest(p) for p in [n/'normalization-manifest.json',l/'technical-lag-manifest.json']},'sources':{str(p):digest(p) for p in [Path(__file__),Path('ml/scope/evaluate.cjs'),Path('processing/technical_lag/core.cjs')]},'outputs':output_records(out)})
 finalize_directory(out,output)
if __name__=='__main__':
 import argparse
 p=argparse.ArgumentParser();p.add_argument('--output-dir',type=Path,required=True);p.add_argument('--limit',type=int,default=60000);a=p.parse_args();run(a.output_dir,a.limit)
