'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {gmailHistory,operation,sessionFetch}=require('./codex_assisted_operation.cjs');
const mailbox='ygor.bueno@thundermustard.com';
const toolsFor=pages=>({mcp__codex_apps__gmail_get_profile:async()=>({email:mailbox}),mcp__codex_apps__gmail_search_email_ids:async()=>pages.shift()});
test('real adapter contract paginates and deduplicates IDs, no global completeness',async()=>{const r=await gmailHistory(toolsFor([{message_ids:['abc'],next_page_token:'p2'},{message_ids:['abc','def'],next_page_token:null}]),'synthetic@example.invalid');assert.equal(r.pages,2);assert.equal(r.complete,true);assert.equal(r.person_complete,false);assert.deepEqual(r.message_ids,['abc','def']);});
test('malformed cursors cannot manufacture complete empty coverage',async()=>{for(const token of [false,0,'',{},[]])await assert.rejects(gmailHistory(toolsFor([{message_ids:[],next_page_token:token}]),'synthetic@example.invalid'),/PAGINATION/);});
test('page budget preserves continuation and holds',async()=>{const r=await gmailHistory(toolsFor([{message_ids:['abc'],next_page_token:'next'}]),'synthetic@example.invalid',{maxPages:1});assert.equal(r.complete,false);assert.equal(r.continuation,'next');});
test('wrong mailbox blocks history search',async()=>{const t=toolsFor([]);t.mcp__codex_apps__gmail_get_profile=async()=>({email:'wrong@example.invalid'});await assert.rejects(gmailHistory(t,'synthetic@example.invalid'),/MAILBOX/);});
test('panel session does not fabricate auth headers or follow redirects',async()=>{let options;const p=sessionFetch('https://painel.r3xdigital.tech/',async(u,o)=>{options=o;return{status:401};});await assert.rejects(p('/api/os/v1/session'),/SESSION_UNAVAILABLE/);assert.equal(options.credentials,'include');assert.equal(options.redirect,'error');await assert.rejects(p('/api/os/v1/session',{headers:{'X-Authenticated-User':'ygor'}}),/AUTH_HEADER_FORBIDDEN/);});
test('dry-run calls actual port implementations but never claim, send, lock or journal create',async()=>{let sends=0;const t=toolsFor([{message_ids:[],next_page_token:null}]);t.mcp__codex_apps__gmail_send_email=async()=>{sends++;};const r=await operation({mode:'dry-run',panel_url:'https://painel.r3xdigital.tech/',scope:{kind:'internal_canary',max_attempts:1,no_followup:true,from:mailbox,to:'synthetic@example.invalid',subject:'Fixture',content:'Synthetic'},lock_holder:'/missing/synthetic',journal_directory:'/missing/synthetic'},{tools:t,fetchImpl:async()=>({status:401})});assert.equal(sends,0);assert.equal(r.send_attempted,false);assert.equal(r.read_only,true);assert.ok(r.blockers.includes('CANONICAL_PACKET_REQUIRED'));assert.ok(r.blockers.includes('PANEL_EXISTING_SESSION_UNAVAILABLE'));assert.equal(r.observed.gmail.complete,true);});
const {withResources}=require('./codex_assisted_operation.cjs');
test('lock released when journal initialization fails',async()=>{let closed=0;await assert.rejects(withResources({close:async()=>closed++},async()=>{throw Error('journal unavailable');},async()=>{}),/unavailable/);assert.equal(closed,1);});
test('lock released even when journal close fails',async()=>{let closed=0;await assert.rejects(withResources({close:async()=>closed++},async()=>({close:async()=>{throw Error('close failed');}}),async()=>{}),e=>e.message==='CLEANUP_FAILED'&&e.cleanup_errors.includes('JOURNAL_CLOSE_FAILED'));assert.equal(closed,1);});
const {invokeConnectedRequest,browserPanelRequest}=require('./codex_assisted_operation.cjs');
const {hash}=require('./codex_assisted_runner.cjs');
function hostFixture(){
 const p={job_id:'job-a',payload:{to:'synthetic@example.invalid'}};p.review_hash=hash(p);
 const grant={dispatch_authorized:true,ticket_hash:'a'.repeat(64),expires_at:104,job_id:p.job_id,packet_hash:p.review_hash};
 const r={tool:'mcp__codex_apps__gmail_send_email',args:p.payload,authority:{packet_hash:p.review_hash,ticket_hash:grant.ticket_hash,not_after:grant.expires_at,job_id:p.job_id}};
 const ctx={allowSend:true,packet:p,grant,used:new Set(),now:()=>100,checkLock:async()=>true,consumeAttempt:async()=>true};
 return {p,r,ctx};
}
test('operator tool-host rejects expired send and duplicate attempt, never retries unknown',async()=>{
 const {r,ctx}=hostFixture();let sends=0;const t={mcp__codex_apps__gmail_send_email:async()=>{sends++;throw Error('uncertain');}};
 ctx.grant.expires_at=r.authority.not_after=99;await assert.rejects(invokeConnectedRequest(r,t,ctx),/EXPIRED/);assert.equal(sends,0);
 r.authority.not_after=104;await assert.rejects(invokeConnectedRequest(r,t,ctx),/GRANT_MISMATCH/);ctx.grant.expires_at=104;
 await assert.rejects(invokeConnectedRequest(r,t,ctx),/uncertain/);await assert.rejects(invokeConnectedRequest(r,t,ctx),/ALREADY_ATTEMPTED/);assert.equal(sends,1);
});
test('host rejects grant belonging to another packet or job',async()=>{
 for(const field of ['job_id','packet_hash']){const {r,ctx}=hostFixture();ctx.grant[field]='other';let sends=0;
 await assert.rejects(invokeConnectedRequest(r,{mcp__codex_apps__gmail_send_email:async()=>sends++},ctx),/GRANT_MISMATCH/);assert.equal(sends,0);}
});
test('host requires durable consumption and checks lock and deadline again after it',async()=>{
 for(const failure of ['missing','lock','deadline']){
  const {r,ctx}=hostFixture();let sends=0;
  if(failure==='missing')delete ctx.consumeAttempt;
  else {let consumed=false;ctx.checkLock=async()=>!(consumed&&failure==='lock');ctx.now=()=>consumed&&failure==='deadline'?105:100;ctx.consumeAttempt=async()=>{consumed=true;return true;};}
  await assert.rejects(invokeConnectedRequest(r,{mcp__codex_apps__gmail_send_email:async()=>sends++},ctx),/GUARDS|LOCK_LOST|EXPIRED/);assert.equal(sends,0);
 }
});
test('real durable host markers survive new host context, timeout, and parallel invocations',async()=>{
 const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os');
 const {consumeHostAttempt}=require('./codex_assisted_operation.cjs');const dir=await fs.mkdtemp(path.join(os.tmpdir(),'host-guards-'));
 try{
  const {r,ctx}=hostFixture();ctx.consumeAttempt=a=>consumeHostAttempt(dir,a);let sends=0;
  const t={mcp__codex_apps__gmail_send_email:async()=>{sends++;throw Error('uncertain');}};
  const results=await Promise.allSettled([invokeConnectedRequest(r,t,ctx),invokeConnectedRequest(r,t,{...ctx,used:new Set()})]);
  assert.equal(results.filter(x=>x.status==='rejected').length,2);assert.equal(sends,1);
  await assert.rejects(invokeConnectedRequest(r,t,{...ctx,used:new Set()}),/ALREADY_ATTEMPTED/);assert.equal(sends,1);
 }finally{await fs.rm(dir,{recursive:true,force:true});}
});
test('cleanup preserves primary failure and always attempts both closes',async()=>{
 const calls=[];const error=Error('PRIMARY');error.assisted={phase:'PROVIDER_CALL',send_attempted:true};
 await assert.rejects(withResources({close:async()=>{calls.push('lock');throw Error('lock');}},async()=>({close:async()=>{calls.push('journal');throw Error('journal');}}),async()=>{throw error;}),e=>e===error&&e.cleanup_errors.length===2&&e.assisted.send_attempted);
 assert.deepEqual(calls,['journal','lock']);
});
test('browser transport rejects cross-origin routes and spoofed identities',async()=>{const c={send:async()=>{throw Error('unexpected');}};await assert.rejects(browserPanelRequest(c,{route:'https://other.invalid'}),/ROUTE/);await assert.rejects(browserPanelRequest(c,{route:'/api/os/v1/session',options:{headers:{Cookie:'synthetic'}}}),/HEADER/);});

test('integrated runner and V8-compatible host use actual local guard CLI and retain receipt',async()=>{
 const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),{spawn}=require('node:child_process');
 const {runAssisted,durableJournal,connectedGmail}=require('./codex_assisted_runner.cjs');
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),'integrated-host-'));
 const context={lock_pid:process.pid,journal_directory:dir};
 const guard=request=>new Promise((resolve,reject)=>{const p=spawn(process.execPath,[path.join(__dirname,'codex_assisted_operation.cjs'),'--host-guard'],{stdio:['pipe','pipe','pipe']});let out='';p.stdout.on('data',b=>out+=b);p.on('error',reject);p.on('close',()=>{const r=JSON.parse(out);r.error?reject(Error(r.error)):resolve(r);});p.stdin.end(JSON.stringify({...request,context}));});
 let journal;try{
  const packet={schema:'thunder_codex_assisted_v1',dispatch_authorized:false,cadence_id:'fixture',step_id:'e1',job_id:'job-a',payload:{to:'synthetic@example.invalid'},limits:{max_attempts:1}};packet.review_hash=hash(packet);
  const used=new Set();let grant,sends=0;const realTools={mcp__codex_apps__gmail_send_email:async()=>{sends++;return{id:'abc123'};}};
  const brokerTools={mcp__codex_apps__gmail_send_email:(args,authority)=>invokeConnectedRequest({tool:'mcp__codex_apps__gmail_send_email',args,authority},realTools,{allowSend:true,packet,grant,used,now:()=>100,checkLock:async()=>(await guard({action:'check-lock'})).held,consumeAttempt:async attempt=>(await guard({action:'consume-attempt',attempt})).consumed})};
  const panel=async(action,body)=>{
   if(action==='claim'){const t={...packet,dispatch_authorized:true,expires_at:160};t.ticket_hash=hash(t);return t;}
   if(action==='dispatch'){grant={dispatch_authorized:true,ticket_hash:body.ticket_hash,packet_hash:packet.review_hash,job_id:packet.job_id,expires_at:105};return grant;}
   return{review_hash:'b'.repeat(64),status:'confirmed',receipt:body.receipt,send_attempted:false,retry_write:false,verification:'provider_sent_copy',attempt_attribution:'operator_reported'};
  };
  const lock=async f=>f();lock.check=async()=>true;journal=await durableJournal(dir);
  const result=await runAssisted(packet,{panel,gmail:connectedGmail(brokerTools),journal,withLock:lock,now:()=>100,preflight:async()=>({complete:true,eligible:true,packet_hash:packet.review_hash,checked_at:100})});
  assert.equal(result.receipt,'gmail:abc123');assert.equal(sends,1);
  assert.equal((await fs.readFile(path.join(dir,packet.review_hash+'.jsonl'),'utf8')).trim().split('\n').map(JSON.parse).at(-1).state,'RECEIPT_RETURNED');
  await assert.rejects(guard({action:'consume-attempt',attempt:{packet_hash:packet.review_hash,ticket_hash:grant.ticket_hash,job_id:packet.job_id}}),/ALREADY_ATTEMPTED/);
 }finally{if(journal)await journal.close();await fs.rm(dir,{recursive:true,force:true});}
});

test('cleanup readback is local only and never claims production flag removal',async()=>{
 const r=await withResources({close:async()=>{}},async()=>({close:async()=>{}}),async()=>({send_attempted:false}));
 assert.equal(r.cleanup.journal_closed,true);assert.equal(r.cleanup.lock_released,true);
 assert.equal(r.cleanup.production_policy_cleanup,'operator_required_not_performed');
});

test('host-V8 executor factory uses real CLI with robust quoting and persistent reservations',async()=>{
 const fs=require('node:fs/promises'),path=require('node:path'),os=require('node:os'),{spawn}=require('node:child_process');
 const {executorHostGuards}=require('./codex_assisted_operation.cjs');
 const dir=await fs.mkdtemp(path.join(os.tmpdir(),"host-quote'-"));
 const tools={exec_command:({cmd})=>new Promise((resolve,reject)=>{const p=spawn('/bin/sh',['-c',cmd],{stdio:['ignore','pipe','pipe']});let out='';p.stdout.on('data',b=>out+=b);p.on('error',reject);p.on('close',exit_code=>resolve({exit_code,output:out}));})};
 try{
  const context={lock_pid:process.pid,journal_directory:dir};
  const factory=()=>executorHostGuards(tools,context,{scriptPath:path.join(__dirname,'codex_assisted_operation.cjs'),nodePath:process.execPath});
  const guards=factory();assert.equal(await guards.checkLock(),true);
  const attempt={packet_hash:'c'.repeat(64),ticket_hash:'d'.repeat(64),job_id:"fixture'$(false)",not_after:Date.now()/1000+5};
  assert.equal(await guards.consumeAttempt(attempt),true);
  const saved=JSON.parse(await fs.readFile(path.join(dir,'host-packet-'+attempt.packet_hash+'.json'),'utf8'));assert.equal(saved.job_id,attempt.job_id);
  await assert.rejects(factory().consumeAttempt(attempt),/EXECUTION_FAILED/);
 }finally{await fs.rm(dir,{recursive:true,force:true});}
});
