'use strict';
// Synthetic test helper; never loads native adapter.
const fs=require('node:fs/promises'),path=require('node:path');
const {createBridge}=require('./bridge.cjs'),{EXPECTED}=require('./contract.cjs');
let raw='';process.stdin.on('data',d=>raw+=d);process.stdin.on('end',async()=>{try{const data=JSON.parse(raw),provider={async quotaStatus(){return {ready:true};},async reserveQuota(){},async finishQuota(){},async verifyProfile(){return EXPECTED.email;},async findDuplicates(){return {messages:[],complete:true};},async send(){await fs.appendFile(path.join(process.argv[2],'fixture-send-count'),'send\n');return {id:'fixture1',thread_id:'t1'};}};const bridge=createBridge({provider,credential:data.credential,journalDir:process.argv[2],sendEnabled:true,now:()=>data.now});process.stdout.write(JSON.stringify(await bridge.handle({method:'POST',url:'/v1/send',authorization:'Bearer '+data.credential,body:data.envelope})));}catch{process.exitCode=1;}});
