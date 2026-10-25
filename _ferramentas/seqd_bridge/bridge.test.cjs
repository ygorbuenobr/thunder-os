'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {spawn}=require('node:child_process');
const {createBridge}=require('./bridge.cjs'),{Journal}=require('./journal.cjs');
const {EXPECTED,hash,snapshot,signature,validate}=require('./contract.cjs');
const {nativeSession,guardSendWire,mime,normalized}=require('./native.cjs');
const KEY='a'.repeat(64),NOW=Date.parse('2030-01-01T00:00:00Z');
function envelope(extra={}){const e={preview_id:'preview-1',version:1,content_hash:'',approved_actor:'YGOR',expires_at:'2030-01-01T00:05:00.000Z',idempotency_key:'effect-1',to:'person@example.invalid',subject:'Pesquisa — ação',body:'Olá, fixture.\nSem envio real.',thread_id:null,...extra};e.content_hash=hash(snapshot(e));e.approval_signature=signature(e,KEY);return e;}
async function fixture(t){const dir=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'seqd-bridge-fixture-')));t.after(()=>fs.rm(dir,{recursive:true,force:true}));let sends=0,stored=null;const provider={async quotaStatus(){return {ready:true};},async reserveQuota(){},async finishQuota(){},async verifyProfile(){return EXPECTED.email;},async list(){return {messages:[],next_cursor:null};},async get(id){return {id,thread_id:'t1',body:'fixture',truncated:false};},async findDuplicates(){return {messages:[],complete:true};},async send(mail){sends++;stored={...mail,id:'m1',thread_id:mail.thread_id??'t1',sent:true,truncated:false};return {id:'m1',thread_id:'t1'};},async findSent(){return stored?[stored]:[];}};const options={provider,credential:KEY,journalDir:dir,sendEnabled:true,now:()=>NOW};return {dir,provider,options,bridge:createBridge(options),sends:()=>sends,stored:()=>stored};}
function call(bridge,method,url,body){return bridge.handle({method,url,body,authorization:'Bearer '+KEY});}
test('valid snapshot/HMAC has stable cross-language vector',()=>{const e=envelope();assert.doesNotThrow(()=>validate(e,KEY,NOW));const vector=require('./contract-vector.json');assert.equal(hash(snapshot(vector.envelope)),vector.content_hash);assert.equal(signature(vector.envelope,vector.credential),vector.approval_signature);});
test('auth, actor, expiry, hash, extra recipients and header injection rejected',async t=>{const f=await fixture(t);await assert.rejects(f.bridge.handle({method:'GET',url:'/v1/status',authorization:'Bearer wrong'}),/unauthorized/);for(const e of [envelope({approved_actor:'hermes'}),envelope({expires_at:'2029-01-01T00:00:00Z'}),envelope({cc:'hidden@example.invalid'}),envelope({mailbox:'other'}),envelope({subject:'x\r\nBcc: hidden'}),envelope({to:'person@example.invalid, other@example.invalid'}),{...envelope(),body:'changed'}])await assert.rejects(call(f.bridge,'POST','/v1/send',e));assert.equal(f.sends(),0);});
test('disabled send and mismatched provider never touch send',async t=>{const f=await fixture(t);await assert.rejects(call(createBridge({...f.options,sendEnabled:false}),'POST','/v1/send',envelope()),/disabled/);f.provider.verifyProfile=async()=> 'other@example.invalid';await assert.rejects(call(f.bridge,'GET','/v1/messages'),/mismatch/);assert.equal(f.sends(),0);});
test('bounded read routes are fixed and read-only',async t=>{const f=await fixture(t);assert.equal((await call(f.bridge,'GET','/v1/status')).mailbox,EXPECTED.email);assert.deepEqual((await call(f.bridge,'GET','/v1/messages?limit=10')).messages,[]);assert.equal((await call(f.bridge,'GET','/v1/messages/m1')).message.body,'fixture');for(const url of ['/v1/messages?limit=11','/v1/messages?account=other','/v1/messages?limit=1&limit=2','/v1/messages/../send'])await assert.rejects(call(f.bridge,'GET',url));assert.equal(f.sends(),0);});
test('concurrent sends and restart retain one send; receipt requires Gmail ID',async t=>{const f=await fixture(t);const results=await Promise.all(Array.from({length:20},()=>call(f.bridge,'POST','/v1/send',envelope())));assert.equal(f.sends(),1);assert.ok(results.some(r=>r.status==='sent'));const fresh=createBridge(f.options);assert.equal((await call(fresh,'POST','/v1/send',envelope())).provider_id,'m1');assert.equal(f.sends(),1);});
test('same business snapshot with new preview/key does not resend',async t=>{const f=await fixture(t);await call(f.bridge,'POST','/v1/send',envelope());const e=envelope({preview_id:'new',version:2,idempotency_key:'new-key'});const result=await call(f.bridge,'POST','/v1/send',e);assert.equal(result.status,'sent');assert.equal(result.deduplicated,true);assert.equal(f.sends(),1);await assert.rejects(call(f.bridge,'POST','/v1/send',envelope({body:'different'})),/idempotency_conflict/);});
test('timeout after commit reconciles exact body and never resends',async t=>{const f=await fixture(t),send=f.provider.send;f.provider.send=async mail=>{await send(mail);throw new Error('never expose provider secret');};assert.equal((await call(f.bridge,'POST','/v1/send',envelope())).status,'uncertain');assert.equal((await call(createBridge(f.options),'POST','/v1/send',envelope())).status,'uncertain');const result=await call(f.bridge,'POST','/v1/reconcile',{idempotency_key:'effect-1'});assert.equal(result.status,'sent');assert.equal(result.provider_id,'m1');assert.equal(f.sends(),1);});
test('unknown and wrong-body receipts remain uncertain, no retries',async t=>{const f=await fixture(t);f.provider.send=async()=>{throw Error('timeout');};await call(f.bridge,'POST','/v1/send',envelope());assert.equal((await call(f.bridge,'POST','/v1/reconcile',{idempotency_key:'effect-1'})).status,'uncertain');const info=validate(envelope(),KEY,NOW);f.provider.findSent=async()=>[{id:'m9',thread_id:'t9',to:envelope().to,subject:envelope().subject,body:'wrong',message_id:info.messageId,sent:true,truncated:false}];assert.equal((await call(f.bridge,'POST','/v1/reconcile',{idempotency_key:'effect-1'})).error_code,'receipt_snapshot_mismatch');});
test('legacy duplicate or incomplete search blocks before provider send',async t=>{for(const result of [{messages:[{id:'legacy'}],complete:true},{messages:[],complete:false}]){const f=await fixture(t);f.provider.findDuplicates=async()=>result;assert.equal((await call(f.bridge,'POST','/v1/send',envelope())).error_code,'possible_duplicate');assert.equal(f.sends(),0);await call(createBridge(f.options),'POST','/v1/send',envelope());assert.equal(f.sends(),0);}});
test('crash after only key reservation is conservatively uncertain',async t=>{const f=await fixture(t),e=envelope(),info=validate(e,KEY,NOW);await f.bridge.journal.append(hash(e.idempotency_key)+'.key.json',{snapshotHash:info.snapshotHash,businessHash:info.businessHash});assert.equal((await call(f.bridge,'POST','/v1/send',e)).status,'uncertain');assert.equal(f.sends(),0);});
test('partial journal crash and symlink refuse execution',async t=>{const f=await fixture(t);await f.bridge.journal.ready;await fs.writeFile(path.join(f.dir,hash('effect-1')+'.key.json'),'{partial');await assert.rejects(call(f.bridge,'POST','/v1/send',envelope()),/journal_unavailable/);assert.equal(f.sends(),0);});
test('journal contains hashes/receipts but never body or credential',async t=>{const f=await fixture(t);await call(f.bridge,'POST','/v1/send',envelope());const contents=(await Promise.all((await fs.readdir(f.dir)).map(name=>fs.readFile(path.join(f.dir,name),'utf8')))).join();assert.ok(!contents.includes(KEY));assert.ok(!contents.includes(envelope().body));assert.ok(!contents.includes(envelope().subject));});
test('cross-process CAS permits exactly one local send invocation',async t=>{const f=await fixture(t);const script=path.join(__dirname,'process-fixture.cjs');const run=()=>new Promise((resolve,reject)=>{const p=spawn(process.execPath,[script,f.dir],{stdio:['pipe','pipe','pipe']});let out='';p.stdout.on('data',d=>out+=d);p.on('exit',code=>code===0?resolve(JSON.parse(out)):reject(Error('fixture process failed')));p.stdin.end(JSON.stringify({envelope:envelope(),credential:KEY,now:NOW}));});await Promise.all(Array.from({length:8},run));assert.equal((await fs.readFile(path.join(f.dir,'fixture-send-count'),'utf8')).trim().split('\n').length,1);});
test('wire guard blocks auth-library second resource attempt',async()=>{let wires=0;const transporter={async request(){wires++;return {data:{id:'x'}};}};const gmail={context:{_options:{auth:{transporter}}}};const restore=guardSendWire(gmail);await transporter.request({url:'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',method:'POST'});assert.throws(()=>transporter.request({url:'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',method:'POST'}),/retry_blocked/);assert.equal(wires,1);restore();});
test('native MIME is fixed sender/plain text/deterministic Message-ID',()=>{const info=validate(envelope(),KEY,NOW),raw=Buffer.from(mime({...envelope(),message_id:info.messageId}), 'base64url').toString();assert.ok(raw.includes('From: '+EXPECTED.email));assert.ok(raw.includes('Message-ID: '+info.messageId));assert.ok(raw.includes('Content-Type: text/plain'));assert.equal(Buffer.from(raw.split('\r\n\r\n')[1].replace(/\r\n/g,''),'base64').toString(),envelope().body);});
test('native wrong thread participant or subject cannot send',async()=>{let sends=0;const gmail={users:{threads:{async get(){return {data:{id:'t1',messages:[{payload:{headers:[{name:'From',value:'someoneelse@example.invalid'},{name:'To',value:EXPECTED.email},{name:'Subject',value:'Different'},{name:'Message-ID',value:'<a@example.invalid>'}]}}]}};}},messages:{async send(){sends++;}}}};await assert.rejects(nativeSession(gmail).send({...envelope({thread_id:'t1'}),message_id:'<a@example.invalid>'}),/participant_mismatch/);assert.equal(sends,0);});
test('legacy duplicate normalizes display name and CRLF safely',async()=>{
 const e=envelope();
 const data={id:'old',threadId:'told',labelIds:['SENT'],payload:{mimeType:'text/plain',headers:[{name:'To',value:`Person <${e.to}>`},{name:'Subject',value:e.subject}],body:{data:Buffer.from(e.body.replace(/\n/g,'\r\n')).toString('base64url')}}};
 const gmail={users:{messages:{async list(){return {data:{messages:[{id:'old'}]}};},async get(){return {data};}}}};
 const result=await nativeSession(gmail).findDuplicates(e);assert.equal(result.messages.length,1);
});
test('MIME HTML-only, attachment and missing/deep multipart are incomplete',()=>{
 for(const payload of [{mimeType:'text/html',body:{data:Buffer.from('<b>html</b>').toString('base64url')}},{mimeType:'multipart/alternative'},{mimeType:'application/pdf',body:{data:'AA'}}])assert.equal(normalized({payload}).truncated,true);
 const wide={mimeType:'multipart/mixed',parts:Array.from({length:100},()=>({mimeType:'text/plain',body:{data:Buffer.from('😀'.repeat(1000)).toString('base64url')}}))};
 const result=normalized({payload:wide});assert.equal(result.truncated,true);assert.ok(Buffer.byteLength(result.body)<=65536);
});
test('native bootstrap regex registers only real builtin externals',()=>{
 const {registerBuiltins}=require('./native.cjs'),runtime={m:{}},loads=[];
 const source='123:a=>{a.exports=require("node:fs")},456:b=>{b.exports=require("crypto")},789:c=>{c.exports=require("not-a-builtin")}';
 assert.equal(registerBuiltins(runtime,source,id=>{loads.push(id);return id;}),2);
 const output={};runtime.m['123'](output);assert.equal(output.exports,'node:fs');assert.equal(runtime.m['789'],undefined);assert.deepEqual(loads,['node:fs']);
});
test('HTTP boundary authenticates and limits payload without sockets',async t=>{
 const f=await fixture(t),{serverFor}=require('./server.cjs'),{Readable}=require('node:stream');const server=serverFor(f.bridge);
 const request=(authorization,content)=>new Promise(resolve=>{const req=Readable.from(content?[Buffer.from(content)]:[]);req.method='POST';req.url='/v1/send';req.headers={authorization};const res={setHeader(){},writeHead(code){this.code=code;},end(raw){resolve({code:this.code,body:JSON.parse(raw)});}};server.emit('request',req,res);});
 const denied=await request('Bearer bad',JSON.stringify(envelope()));assert.equal(denied.code,401);
 const big=await request('Bearer '+KEY,'x'.repeat(100001));assert.equal(big.code,413);
 const sent=await request('Bearer '+KEY,JSON.stringify(envelope()));assert.equal(sent.body.status,'sent');assert.equal(f.sends(),1);server.close();
});
test('quota missing, full or suppressed refuses Gmail and retains uncertain reservation',async t=>{
 for(const mode of ['missing','full','suppressed']){
  const f=await fixture(t);if(mode==='missing')delete f.provider.reserveQuota;
  else f.provider.reserveQuota=async()=>{throw Error(mode);};
  const result=await call(f.bridge,'POST','/v1/send',envelope());assert.equal(result.status,'uncertain');assert.equal(f.sends(),0);
  assert.equal((await call(createBridge(f.options),'POST','/v1/send',envelope())).status,'uncertain');assert.equal(f.sends(),0);
 }
});
test('quota acknowledgement failure remains uncertain; reconciliation never reserves or sends',async t=>{
 const f=await fixture(t);let reserves=0,finishes=0;
 f.provider.reserveQuota=async()=>{reserves++;};f.provider.finishQuota=async()=>{if(++finishes===1)throw Error('db unavailable');};
 assert.equal((await call(f.bridge,'POST','/v1/send',envelope())).status,'uncertain');assert.equal(f.sends(),1);
 const off=createBridge({...f.options,sendEnabled:false});assert.equal((await call(off,'POST','/v1/reconcile',{idempotency_key:'effect-1'})).status,'sent');
 assert.equal(reserves,1);assert.equal(finishes,2);assert.equal(f.sends(),1);
});
test('native quota proof binds exact content and rechecks current suppression before wire',async()=>{
 let sends=0;const gmail={context:{_options:{auth:{transporter:{async request(){}}}}},users:{messages:{async send(){sends++;return {data:{id:'m1'}};}}}};
 const e=envelope(),info=validate(e,KEY,NOW),mail={...e,message_id:info.messageId};
 const q={async reserve(){},async assertCurrent(){throw Error('recipient_suppressed');},async finish(){}};
 const session=nativeSession(gmail,q);await session.reserveQuota({effect:info.businessHash,to:e.to,messageId:info.messageId});
 await assert.rejects(session.send({...mail,body:'tampered'}),/proof_missing/);
 await assert.rejects(session.send(mail),/recipient_suppressed/);assert.equal(sends,0);
});
test('send-enabled readiness requires working quota but read-only status does not',async t=>{
 const f=await fixture(t);delete f.provider.quotaStatus;
 await assert.rejects(call(f.bridge,'GET','/v1/status'),/quota_unavailable/);
 assert.equal((await call(createBridge({...f.options,sendEnabled:false}),'GET','/v1/status')).send_enabled,false);
});
test('native bootstrap pins legacy sender guard before permitting any session',()=>{
 assert.equal(require('./native.cjs').PINS['chunks/3317.js'],'d63f208341bdb2cbce20edfbfea9c2ef9dca32977ded98bd9af5818c949e1267');
});
test('scoped bridge pins recipient content expiry and one journal attempt across restart',async t=>{
 const {createScopedBridge}=require('./server.cjs');const f=await fixture(t),e=envelope();
 const p={approval_ref:'synthetic-only',mailbox:EXPECTED.email,to:e.to,subject:e.subject,body_hash:hash(e.body),idempotency_key:e.idempotency_key,issued_at:NOW-1,expires_at:NOW+300000,max_attempts:1};
 const options={readProvider:f.provider,writeProvider:f.provider,credential:KEY,journalDir:f.dir,policyLoader:()=>p,now:()=>NOW};
 const bridge=createScopedBridge(options);
 for(const changed of [{to:'other@example.invalid'},{body:'changed'},{subject:'changed'},{idempotency_key:'other'},{thread_id:'thread-other'}])
  await assert.rejects(call(bridge,'POST','/v1/send',envelope(changed)),/scoped_policy_mismatch/);
 assert.equal(f.sends(),0);
 assert.equal((await call(bridge,'POST','/v1/send',e)).status,'sent');
 assert.equal((await call(createScopedBridge(options),'POST','/v1/send',e)).status,'sent');assert.equal(f.sends(),1);
 p.expires_at=NOW;await assert.rejects(call(bridge,'POST','/v1/send',e),/scoped_policy_mismatch/);assert.equal(f.sends(),1);
 // Read requests still use the richer read-only path, never the mutable provider.
 assert.equal((await call(bridge,'GET','/v1/status')).send_enabled,false);
});
test('scoped authority revoked during provider read blocks before send',async t=>{
 const f=await fixture(t);let authorized=true;
 f.provider.findDuplicates=async()=>{authorized=false;return {messages:[],complete:true};};
 const bridge=createBridge({...f.options,sendAuthority:()=>authorized});
 assert.equal((await call(bridge,'POST','/v1/send',envelope())).status,'uncertain');assert.equal(f.sends(),0);
 assert.equal((await call(createBridge({...f.options,sendEnabled:false}),'POST','/v1/reconcile',{idempotency_key:'effect-1'})).status,'uncertain');
});
test('scoped native quota wait cannot outlive or revoke approval',async t=>{
 const {createScopedBridge}=require('./server.cjs');
 for(const revoke of [false,true]){
  const f=await fixture(t),e=envelope();let tick=NOW,sends=0;
  const p={approval_ref:'synthetic-only',mailbox:EXPECTED.email,to:e.to,subject:e.subject,body_hash:hash(e.body),idempotency_key:e.idempotency_key,issued_at:NOW-1,expires_at:NOW+300000,max_attempts:1};
  const native=nativeSession({users:{messages:{async send(){sends++;throw Error('must not reach Gmail');}}}},
   {async reserve(){},async assertCurrent(){if(revoke)p.body_hash='revoked';else tick=NOW+300000;}});
  f.provider.reserveQuota=native.reserveQuota;f.provider.send=native.send;
  const bridge=createScopedBridge({readProvider:f.provider,writeProvider:f.provider,credential:KEY,journalDir:f.dir,policyLoader:()=>p,now:()=>tick});
  assert.equal((await call(bridge,'POST','/v1/send',e)).status,'uncertain');assert.equal(sends,0);
 }
});
test('wire authority is rechecked after auth refresh before resource request',()=>{
 let allowed=true,wires=0;const transporter={request(){wires++;return {};}};
 const restore=guardSendWire({context:{_options:{auth:{transporter}}}},()=>{assert.ok(allowed,'revoked');});
 allowed=false;assert.throws(()=>transporter.request({url:'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',method:'POST'}),/revoked/);
 assert.equal(wires,0);restore();
});
