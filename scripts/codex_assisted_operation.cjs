'use strict';
// One operator entry over the existing runner. No scheduler, cookies, auth headers or credentials.
const fs=require('node:fs/promises'),path=require('node:path'),{spawn}=require('node:child_process');
const {runAssisted,hash,panelPort,durableJournal,connectedGmail,heldLockPort}=require('./codex_assisted_runner.cjs');
const {createHash}=require('node:crypto');
const check=(ok,code)=>{if(!ok)throw Error(code);};
const MAILBOX='ygor.bueno@thundermustard.com';
const LOCK_PIN='8d85ed98215a8564b9f01c065f5687b4e61f48574eb6337ecb4de55d2d2049ea';
function unpack(r){check(!r?.isError,'PROVIDER_ERROR');if(r?.structuredContent)return r.structuredContent;if(r?.content){const t=r.content.find(x=>x.type==='text');check(t,'PROVIDER_SCHEMA');return JSON.parse(t.text);}return r;}
function sessionFetch(base,fetchImpl=fetch){
 const u=new URL(base);check(u.origin==='https://painel.r3xdigital.tech'&&u.pathname==='/'&&!u.search&&!u.hash,'PANEL_ORIGIN_INVALID');
 return async(route,options={})=>{
  check(/^\/api\/os\/v1\/(session|integrations\/codex-assisted\/(preview|claim|dispatch|receipt-preview|receipt-confirm|recover-preclaim|recover-pre-dispatch|reconcile-reservation|prepare-canary|approve-canary|queue-canary))$/.test(route),'PANEL_ROUTE_INVALID');
  const method=options.method||'GET';check(['GET','POST'].includes(method),'PANEL_METHOD_INVALID');
  const h=options.headers||{};check(Object.keys(h).every(k=>['Content-Type','X-Thunder-Nonce'].includes(k)),'PANEL_AUTH_HEADER_FORBIDDEN');
  const r=await fetchImpl(u.origin+route,{...options,method,credentials:'include',redirect:'error',signal:AbortSignal.timeout(15000)});
  check(r.status!==401&&r.status!==403,'PANEL_EXISTING_SESSION_UNAVAILABLE');check(r.ok,'PANEL_HTTP_ERROR');
  check((r.headers.get('content-type')||'').includes('application/json'),'PANEL_SESSION_OR_JSON_UNAVAILABLE');return r.json();
 };
}
async function gmailHistory(tools,recipient,{maxPages=10}={}){
 check(/^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$/.test(recipient),'RECIPIENT_INVALID');
 const profile=unpack(await tools.mcp__codex_apps__gmail_get_profile({}));
 check((profile.email_address||profile.emailAddress||profile.email)?.toLowerCase()===MAILBOX,'MAILBOX_MISMATCH');
 const ids=new Set(),tokens=new Set();let token,complete=false,pages=0;
 for(;pages<maxPages;pages++){
  const r=unpack(await tools.mcp__codex_apps__gmail_search_email_ids({query:`in:anywhere {from:${recipient} to:${recipient} cc:${recipient} bcc:${recipient}} -in:drafts`,max_results:100,...(token?{next_page_token:token}:{})}));
  check(!r.error&&Array.isArray(r.message_ids)&&r.message_ids.every(id=>typeof id==='string'&&/^[a-f0-9]{1,64}$/.test(id)),'GMAIL_SEARCH_INVALID');
  r.message_ids.forEach(id=>ids.add(id));token=r.next_page_token;
  if(token===undefined||token===null){complete=true;pages++;break;}check(typeof token==='string'&&token.length>0&&!tokens.has(token)&&r.message_ids.length>0,'GMAIL_PAGINATION_LOOP');tokens.add(token);
 }
 // IDs alone never classify response/opt-out or prove absence beyond this mailbox/address.
 return {mailbox:MAILBOX,aliases:[recipient],query_scope:'exact_address_this_mailbox',complete,pages,message_ids:[...ids].sort(),continuation:token||null,person_complete:false};
}
async function acquireExistingLock(holder,python='python3'){
 check(path.isAbsolute(holder),'LOCK_PATH_REQUIRED');const real=await fs.realpath(holder);
 check(createHash('sha256').update(await fs.readFile(real)).digest('hex')===LOCK_PIN,'LOCK_HOLDER_PIN_MISMATCH');
 const child=spawn(python,['-B',real],{stdio:['ignore','pipe','pipe']});child.stderr.resume();
 try{await new Promise((resolve,reject)=>{let out='';const t=setTimeout(()=>reject(Error('LOCK_TIMEOUT')),5000);child.once('error',()=>{clearTimeout(t);reject(Error('LOCK_UNAVAILABLE'));});child.once('exit',()=>{clearTimeout(t);reject(Error('LOCK_BUSY_OR_EXITED'));});child.stdout.on('data',b=>{out+=b;if(out.includes('\n')){clearTimeout(t);out.trim()==='LOCK_HELD'?resolve():reject(Error('LOCK_NOT_HELD'));}});});}
 catch(e){child.kill();throw e;}
 const verify=async()=>{if(child.exitCode!==null||child.signalCode!==null)return false;try{process.kill(child.pid,0);return true;}catch{return false;}};
 return {pid:child.pid,port:heldLockPort(child,verify),close:async()=>{child.kill('SIGTERM');await new Promise(r=>child.exitCode!==null||child.signalCode!==null?r():child.once('exit',r));}};
}
async function withResources(lock,makeJournal,work){
 let journal,result,failure;const cleanup=[];
 try{journal=await makeJournal();result=await work(journal);}catch(error){failure=error;}
 try{if(journal)await journal.close();}catch{cleanup.push('JOURNAL_CLOSE_FAILED');}
 try{await lock.close();}catch{cleanup.push('LOCK_CLOSE_FAILED');}
 const readback={journal_closed:!cleanup.includes('JOURNAL_CLOSE_FAILED'),lock_released:!cleanup.includes('LOCK_CLOSE_FAILED'),attempt_reservations:'retained_no_release',production_policy_cleanup:'operator_required_not_performed'};
 if(failure){failure.cleanup_readback=readback;if(cleanup.length)failure.cleanup_errors=cleanup;throw failure;}
 if(cleanup.length){const error=Error('CLEANUP_FAILED');error.cleanup_errors=cleanup;error.cleanup_readback=readback;
  error.assisted={phase:'CLEANUP',send_attempted:result?.send_attempted===true,receipt:result?.receipt?.receipt||null,retry_write:false,outcome:result?.receipt?.status==='confirmed'?'receipt_confirmed':'unknown'};throw error;}
 return result&&typeof result==='object'?{...result,cleanup:readback}:result;
}
// Uses the same private journal directory. Partial/failed writes retain markers: never release an attempt.
async function consumeHostAttempt(directory,attempt){
 const keys=[['packet',attempt.packet_hash],['ticket',attempt.ticket_hash]];
 check(path.isAbsolute(directory||'')&&keys.every(([,v])=>/^[a-f0-9]{64}$/.test(v))&&typeof attempt.job_id==='string','HOST_ATTEMPT_INVALID');
 const stat=await fs.lstat(directory);check(stat.isDirectory()&&!stat.isSymbolicLink(),'JOURNAL_DIRECTORY_INVALID');directory=await fs.realpath(directory);
 for(const [kind,key] of keys){
  let h;try{
   h=await fs.open(path.join(directory,'host-'+kind+'-'+key+'.json'),require('node:fs').constants.O_WRONLY|require('node:fs').constants.O_CREAT|require('node:fs').constants.O_EXCL|require('node:fs').constants.O_NOFOLLOW,0o600);
   await h.writeFile(JSON.stringify(attempt)+'\n');await h.sync();
   const dir=await fs.open(directory,'r');try{await dir.sync();}finally{await dir.close();}
  }catch(e){if(e.code==='EEXIST')throw Error('RPC_ALREADY_ATTEMPTED');throw e;}finally{if(h)await h.close();}
 }
 const dir=await fs.open(directory,'r');try{await dir.sync();}finally{await dir.close();}return true;
}
async function hostGuard(request){
 // Context must come from this operation's host_context event, never from a Gmail RPC.
 const context=request.context;
 check(context&&Number.isSafeInteger(context.lock_pid)&&context.lock_pid>1&&typeof context.journal_directory==='string','HOST_CONTEXT_INVALID');
 const alive=()=>{try{process.kill(context.lock_pid,0);return true;}catch{return false;}};
 if(request.action==='check-lock')return {held:alive()};
 check(request.action==='consume-attempt','HOST_GUARD_ACTION_INVALID');check(alive(),'LOCK_LOST_BEFORE_SEND');
 return {consumed:await consumeHostAttempt(context.journal_directory,request.attempt)};
}
// Self-contained host-V8 factory: uses only the supported executor tool and local guard CLI.
// Paths/context are operator-held; never accept them from a Gmail tool request.
function executorHostGuards(tools,context,{scriptPath,nodePath}={}){
 const check=(ok,code)=>{if(!ok)throw Error(code);};
 check(typeof tools?.exec_command==='function'&&typeof scriptPath==='string'&&scriptPath.startsWith('/')&&typeof nodePath==='string'&&nodePath.startsWith('/'),'HOST_EXECUTOR_REQUIRED');
 const pinned=JSON.parse(JSON.stringify(context));
 const quote=value=>"'"+value.replaceAll("'","'\\''")+"'";
 const invoke=async request=>{
  const input=JSON.stringify({...request,context:pinned});
  const cmd="printf '%s' "+quote(input)+' | '+quote(nodePath)+' '+quote(scriptPath)+' --host-guard';
  const result=await tools.exec_command({cmd,max_output_tokens:1000,yield_time_ms:1000});
  check(result.exit_code===0&&typeof result.output==='string','HOST_GUARD_EXECUTION_FAILED');
  let value;try{value=JSON.parse(result.output);}catch{throw Error('HOST_GUARD_RESULT_INVALID');}
  check(value&&!value.error,'HOST_GUARD_REJECTED');return value;
 };
 return {checkLock:async()=>(await invoke({action:'check-lock'})).held===true,
         consumeAttempt:async attempt=>(await invoke({action:'consume-attempt',attempt})).consumed===true};
}
async function operation(input,{tools,fetchImpl=fetch,onHostContext=()=>{}}={}){
 check(input&&['dry-run','execute'].includes(input.mode),'MODE_REQUIRED');
 check(['resume_preclaim','resume_pre_dispatch','resume_reservation'].filter(k=>input[k]===true).length<=1,'RECOVERY_MODE_CONFLICT');
 check(input.scope?.kind==='internal_canary'&&input.scope.max_attempts===1&&input.scope.no_followup===true,'INTERNAL_SINGLE_CANARY_SCOPE_REQUIRED');
 check(input.scope.from===MAILBOX&&typeof input.scope.subject==='string'&&typeof input.scope.content==='string','SCOPE_INVALID');
 const calls=[],observed={},blockers=[];
 const traced={};for(const n of ['gmail_get_profile','gmail_search_email_ids','gmail_read_email','gmail_send_email']){
  const key='mcp__codex_apps__'+n;traced[key]=async (a,authority)=>{check(typeof tools?.[key]==='function','CONNECTED_TOOL_UNAVAILABLE');check(input.mode==='execute'||n!=='gmail_send_email','DRY_RUN_SEND_FORBIDDEN');calls.push(key);return tools[key](a,authority);};
 }
 const panelFetch=input.panel_transport==='browser-session'
  ? async(route,options={})=>{check(typeof tools?.thunder_panel_request==='function','BROWSER_SESSION_TRANSPORT_REQUIRED');return unpack(await tools.thunder_panel_request({route,options}));}
  : sessionFetch(input.panel_url,fetchImpl);
 let session;try{calls.push('panel:GET/session');session=await panelFetch('/api/os/v1/session');check(session.actor==='ygor','PANEL_SESSION_ACTOR');observed.panel={actor:session.actor};}catch(e){blockers.push(e.message);}
 try{observed.gmail=await gmailHistory(traced,input.scope.to);}catch(e){blockers.push(e.message);}
 let packet=input.packet;
 if(packet){try{
  const {review_hash,...unsigned}=packet;check(hash(unsigned)===review_hash,'PACKET_CHANGED');
  check(packet.payload.from_address===input.scope.from&&packet.payload.to===input.scope.to&&packet.payload.subject===input.scope.subject&&packet.payload.payload.body.content===input.scope.content&&!packet.payload.reply_message_id,'CANARY_PACKET_SCOPE_MISMATCH');
  check(Object.keys(packet.payload).every(k=>['from_address','to','subject','payload','response_fields'].includes(k)),'CANARY_EXTRA_RECIPIENT_OR_ATTACHMENT');
  if(session){calls.push('panel:POST/preview');const fresh=await panelFetch('/api/os/v1/integrations/codex-assisted/preview',{method:'POST',headers:{'Content-Type':'application/json','X-Thunder-Nonce':session.nonce},body:JSON.stringify({cadence_id:packet.cadence_id,step_id:packet.step_id})});check(fresh.review_hash===review_hash,'CANONICAL_PACKET_CHANGED');}
 }catch(e){blockers.push(e.message);}}else blockers.push('CANONICAL_PACKET_REQUIRED');
 // The pre-existing shared lock implementation is pinned, not replaced by another lock domain.
 try{check(createHash('sha256').update(await fs.readFile(input.lock_holder)).digest('hex')===LOCK_PIN,'LOCK_HOLDER_PIN_MISMATCH');observed.lock={implementation_verified:true,acquired:false};}catch(e){blockers.push('LOCK_IMPLEMENTATION_UNAVAILABLE');}
 try{const stat=await fs.lstat(input.journal_directory);check(stat.isDirectory()&&!stat.isSymbolicLink(),'JOURNAL_DIRECTORY_INVALID');observed.journal={directory_verified:true,created:false};}catch(e){blockers.push('JOURNAL_DIRECTORY_UNAVAILABLE');}
 if(!observed.gmail?.complete)blockers.push('GMAIL_COVERAGE_INCOMPLETE');
 // Existing conversation requires human review; never infer no reply from labels or IDs.
 if(observed.gmail?.message_ids.length){
  observed.gmail.headers=[];
  for(const id of observed.gmail.message_ids.slice(0,100)){
   try{
    const m=unpack(await traced.mcp__codex_apps__gmail_read_email({message_id:id,format:'metadata'}));
    check(m.id===id&&Array.isArray(m.payload?.headers),'GMAIL_METADATA_INCOMPLETE');
    const get=n=>{const rows=m.payload.headers.filter(h=>h.name?.toLowerCase()===n);check(rows.length===1&&typeof rows[0].value==='string','GMAIL_HEADER_AMBIGUOUS');return rows[0].value;};
    observed.gmail.headers.push({id,subject:get('subject'),from:get('from'),to:get('to'),date:get('date')});
   }catch(e){blockers.push(e.message);break;}
  }
  if(observed.gmail.headers.length!==observed.gmail.message_ids.length)blockers.push('GMAIL_METADATA_COVERAGE_INCOMPLETE');
  if(observed.gmail.headers.some(m=>m.subject===input.scope.subject))blockers.push('CANARY_SUBJECT_ALREADY_PRESENT');
  observed.gmail.review_hash=hash({packet_hash:packet?.review_hash||null,headers:observed.gmail.headers});
  if(input.history_review_hash!==observed.gmail.review_hash)blockers.push('EXISTING_CONVERSATION_REQUIRES_REVIEW');
 }
 const result={mode:input.mode,read_only:input.mode==='dry-run',packet_hash:packet?.review_hash||null,calls,observed,blockers:[...new Set(blockers)],send_attempted:false};
 if(input.mode==='dry-run'||blockers.length)return result;
 const lock=await acquireExistingLock(input.lock_holder,input.python);
 const recoveryPanel=panelPort(panelFetch);
 return withResources(lock,()=>durableJournal(input.journal_directory,{recoverPreclaim:input.resume_preclaim===true?p=>recoveryPanel('recover-preclaim',{cadence_id:p.cadence_id,step_id:p.step_id,review_hash:p.review_hash}):undefined,
  recoverPreDispatch:input.resume_pre_dispatch===true?p=>recoveryPanel('recover-pre-dispatch',{cadence_id:p.cadence_id,step_id:p.step_id,review_hash:p.review_hash}):undefined,
  reconcileReservation:input.resume_reservation===true?p=>recoveryPanel('reconcile-reservation',{cadence_id:p.cadence_id,step_id:p.step_id,review_hash:p.review_hash}):undefined}),async journal=>{
  await onHostContext({lock_pid:lock.pid,journal_directory:input.journal_directory});
  const preflight=async p=>{
   const fresh=await gmailHistory(traced,p.payload.to);
   check(fresh.complete&&JSON.stringify(fresh.message_ids)===JSON.stringify(observed.gmail.message_ids),'HISTORY_CHANGED_OR_INCOMPLETE');
   check(!fresh.message_ids.length||input.history_review_hash===observed.gmail.review_hash,'HISTORY_REVIEW_REQUIRED');
   const s=await panelFetch('/api/os/v1/session');const current=await panelFetch('/api/os/v1/integrations/codex-assisted/preview',{method:'POST',headers:{'Content-Type':'application/json','X-Thunder-Nonce':s.nonce},body:JSON.stringify({cadence_id:p.cadence_id,step_id:p.step_id})});
   check(current.review_hash===p.review_hash,'CANONICAL_PACKET_CHANGED');
   return {complete:true,eligible:true,packet_hash:p.review_hash,checked_at:Date.now()/1000,coverage:fresh};
  };
  const gmail=connectedGmail(traced),guarded={send_email:async (payload,authority)=>{check(await lock.port.check(),'LOCK_LOST_BEFORE_SEND');check(Number.isFinite(authority?.not_after)&&Date.now()/1000<authority.not_after,"DISPATCH_EXPIRED");return gmail.send_email(payload,authority);}};
  const receipt=await runAssisted(packet,{panel:panelPort(panelFetch),gmail:guarded,journal,withLock:lock.port,preflight});
  return {...result,calls,send_attempted:true,receipt};
 });
}
// Called inside the supported CUA CDP capability of an already-authenticated panel tab.
// Session/nonce remain in that origin; no cookies, browser storage or auth headers are read.
async function browserPanelRequest(cdp,request){
 const {route,options={}}=request;check(/^\/api\/os\/v1\/(session|integrations\/codex-assisted\/(preview|claim|dispatch|receipt-preview|receipt-confirm|recover-preclaim|recover-pre-dispatch|reconcile-reservation|prepare-canary|approve-canary|queue-canary))$/.test(route),'PANEL_ROUTE_INVALID');
 check(['GET','POST'].includes(options.method||'GET'),'PANEL_METHOD_INVALID');
 check(Object.keys(options.headers||{}).every(k=>['Content-Type','X-Thunder-Nonce'].includes(k)),'PANEL_AUTH_HEADER_FORBIDDEN');
 const expression='(async()=>{if(location.origin!=="https://painel.r3xdigital.tech")throw Error("PANEL_ORIGIN_INVALID");const q='+JSON.stringify({route,options})+';const r=await fetch(q.route,{...q.options,credentials:"same-origin",redirect:"error",signal:AbortSignal.timeout(15000)});if(!r.ok)throw Error("PANEL_HTTP_"+r.status);return r.json();})()';
 const value=await cdp.send('Runtime.evaluate',{expression,awaitPromise:true,returnByValue:true},{timeoutMs:20000});
 check(!value.exceptionDetails&&value.result?.type==='object'&&value.result.value,'BROWSER_PANEL_REQUEST_FAILED');return value.result.value;
}
// Run this handler in the Codex tool host, not in the child process. Check at effect time.
async function invokeConnectedRequest(request,tools,{allowSend=false,packet,grant,used,consumeAttempt,checkLock,now=()=>Date.now()/1000}={}){
 // packet is the operator-held authenticated preview, NEVER a field from the RPC request.
 const check=(ok,code)=>{if(!ok)throw Error(code);};
 const canonical=v=>JSON.stringify(v&&typeof v==='object'?(Array.isArray(v)?v.map(x=>JSON.parse(canonical(x))):Object.fromEntries(Object.keys(v).sort().map(k=>[k,JSON.parse(canonical(v[k]))]))):v);
 const unpack=r=>{check(!r?.isError,'PROVIDER_ERROR');if(r?.structuredContent)return r.structuredContent;if(r?.content)return JSON.parse(r.content.find(x=>x.type==='text').text);return r;};
 const reads=['mcp__codex_apps__gmail_get_profile','mcp__codex_apps__gmail_search_email_ids','mcp__codex_apps__gmail_read_email'];
 if(request.tool==='mcp__codex_apps__gmail_send_email'){
  const a=request.authority;check(allowSend===true&&used instanceof Set&&packet&&grant?.dispatch_authorized===true,'SEND_NOT_AUTHORIZED');
  check(a?.ticket_hash===grant.ticket_hash&&a?.not_after===grant.expires_at&&a?.job_id===packet.job_id&&
   grant.job_id===packet.job_id&&grant.packet_hash===packet.review_hash,'RPC_GRANT_MISMATCH');
  const review_hash=packet.review_hash;
  check(/^[a-f0-9]{64}$/.test(review_hash||'')&&a?.packet_hash===review_hash&&canonical(request.args)===canonical(packet.payload),'RPC_SEND_SCOPE_MISMATCH');
  check(/^[a-f0-9]{64}$/.test(a.ticket_hash||'')&&Number.isFinite(a.not_after)&&a.not_after>now()&&a.not_after-now()<=5,'RPC_DISPATCH_EXPIRED');
  check(typeof consumeAttempt==='function'&&typeof checkLock==='function','HOST_DURABLE_GUARDS_REQUIRED');
  check(!used.has(review_hash)&&!used.has(a.ticket_hash),'RPC_ALREADY_ATTEMPTED');
  check(await checkLock()===true,'LOCK_LOST_BEFORE_SEND');
  used.add(review_hash);used.add(a.ticket_hash);
  check(await consumeAttempt({packet_hash:review_hash,ticket_hash:a.ticket_hash,job_id:packet.job_id,not_after:a.not_after})===true,'HOST_ATTEMPT_NOT_DURABLE');
  check(await checkLock()===true,'LOCK_LOST_BEFORE_SEND');
  check(a.not_after>now()&&a.not_after-now()<=5,'RPC_DISPATCH_EXPIRED');
  // Never retry after this invocation, including a timeout or lost result.
  return tools.mcp__codex_apps__gmail_send_email(request.args);
 }
 check(reads.includes(request.tool),'RPC_TOOL_NOT_ALLOWED');
 if(request.tool==='mcp__codex_apps__gmail_read_email')check(request.args.format==='metadata','RPC_READ_FORMAT');
 const raw=await tools[request.tool](request.args);
 if(request.tool==='mcp__codex_apps__gmail_read_email'){
  const d=unpack(raw);
  return {id:d.id,payload:{headers:d.payload?.headers?.filter(h=>['subject','from','to','date'].includes(h.name?.toLowerCase()))}};
 }
 return raw;
}
module.exports={operation,sessionFetch,gmailHistory,acquireExistingLock,withResources,browserPanelRequest,invokeConnectedRequest,consumeHostAttempt,hostGuard,executorHostGuards};
// JSON-lines broker: Codex supplies only named connected-tool results; no eval or shell commands.
if(require.main===module&&process.argv.includes('--host-guard')){
 let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',s=>{input+=s;if(input.length>16384){process.stderr.write('HOST_GUARD_INPUT_TOO_LARGE\n');process.exit(1);}});
 process.stdin.on('end',()=>Promise.resolve().then(()=>hostGuard(JSON.parse(input))).then(result=>console.log(JSON.stringify(result))).catch(e=>{console.log(JSON.stringify({error:/^[A-Z0-9_]+$/.test(e.message)?e.message:'HOST_GUARD_FAILED'}));process.exitCode=1;}));
}else if(require.main===module){
 const readline=require('node:readline');const lines=readline.createInterface({input:process.stdin});let seq=0,pending=null,started=false;
 const tools=new Proxy({}, {get:(_,tool)=>(args,authority)=>new Promise((resolve,reject)=>{check(!pending,'RPC_CONCURRENCY');const id=++seq;const timer=setTimeout(()=>{pending=null;reject(Error('CONNECTED_TOOL_TIMEOUT'));},60000);pending={id,resolve,reject,timer};process.stdout.write(JSON.stringify({type:'tool_call',id,tool,args,...(authority?{authority}:{})})+'\n');})});
 lines.on('line',line=>{try{const data=JSON.parse(line);if(!started){started=true;operation(data,{tools,onHostContext:async context=>{process.stdout.write(JSON.stringify({type:'host_context',context})+'\n');}}).then(result=>{console.log(JSON.stringify({type:'result',result}));lines.close();}).catch(e=>{console.log(JSON.stringify({type:'error',code:/^[A-Z0-9_]+$/.test(e.message)?e.message:'OPERATION_FAILED',...(e.assisted?{outcome:e.assisted}:{}),...(e.cleanup_errors?{cleanup_errors:e.cleanup_errors}:{}),...(e.cleanup_readback?{cleanup:e.cleanup_readback}:{})}));lines.close();process.exitCode=1;});}else{check(pending&&data.id===pending.id,'RPC_REPLY_MISMATCH');const p=pending;pending=null;clearTimeout(p.timer);data.error?p.reject(Error('CONNECTED_TOOL_ERROR')):p.resolve(data.result);}}catch{console.log(JSON.stringify({type:'error',code:'RPC_INPUT_INVALID'}));process.exitCode=1;lines.close();}});
}
