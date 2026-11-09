'use strict';
const fs=require('node:fs');
const {calculateLag,prepareDependencyHistory}=require('../../processing/technical_lag/core.cjs');
const input=JSON.parse(fs.readFileSync(0,'utf8'));
const histories=new Map(input.histories.map(h=>[h.package_name,prepareDependencyHistory(h)]));
const out=input.rows.map(r=>{
 if(r.censored)return {...r,label:null,reason:'right_censored'};
 if(r.tied)return {...r,label:null,reason:'consumer_release_time_tie'};
 if(r.requirement===null)return {...r,label:0,reason:'dependency_removed'};
 if(r.dependency_type!=='dependencies'||r.shadowed_by_optional)return {...r,label:null,reason:'future_out_of_scope'};
 const result=calculateLag({consumer:{package_name:r.source_package,version:r.future_version,published_at:r.future_published_at,provenance:{retrieved_at:r.consumer_retrieved_at}},dependency:histories.get(r.dependency_name)||null,requirement:r.requirement,dependency_type:r.dependency_type,shadowed_by_optional:r.shadowed_by_optional,observation_time:r.endpoint});
 return {...r,label:result.status==='ok'?Number(result.update_type==='major'):null,reason:result.exclusion_reason||'ok',future_version_lag:result.version_lag,future_update_type:result.update_type};
});
process.stdout.write(JSON.stringify(out));
