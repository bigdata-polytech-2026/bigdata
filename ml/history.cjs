'use strict';
const fs=require('node:fs');
const readline=require('node:readline');
const {calculateLag,prepareDependencyHistory}=require('../processing/technical_lag/core.cjs');
async function main(){
 const histories=new Map();
 for await(const line of readline.createInterface({input:fs.createReadStream(process.argv[2]),crlfDelay:Infinity})){
  const h=JSON.parse(line);histories.set(h.package_name,prepareDependencyHistory(h));
 }
 for await(const line of readline.createInterface({input:process.stdin,crlfDelay:Infinity})){
  const r=JSON.parse(line);let result={status:'excluded',exclusion_reason:'no_prior_production_manifest',version_lag:null,update_type:null};
  if(r.tied)result.exclusion_reason='consumer_release_time_tie';
  else if(r.prior_version && r.requirement!==null){
   result=calculateLag({consumer:{package_name:r.source_package,version:r.prior_version,published_at:r.prior_published_at,provenance:{retrieved_at:r.consumer_retrieved_at}},dependency:histories.get(r.dependency_name)||null,requirement:r.requirement,dependency_type:r.dependency_type,shadowed_by_optional:r.shadowed_by_optional,observation_time:r.cutoff});
  }
  process.stdout.write(JSON.stringify({source_package:r.source_package,dependency_name:r.dependency_name,days:r.days,version_lag:result.version_lag,major:result.status==='ok'?Number(result.update_type==='major'):null,reason:result.exclusion_reason})+'\n');
 }
}
main().catch(e=>{console.error(e);process.exitCode=1;});
