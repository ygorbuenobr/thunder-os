'use strict';
const test=require('node:test'),assert=require('node:assert/strict'),fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');
const {EXPECTED,hash,canonical,snapshot,business,signature,validate}=require('./contract.cjs');
const {createBridge}=require('./bridge.cjs'),{nativeSession,mime,guardSendWire}=require('./native.cjs');
// This process has no credential/provider bootstrap and denies all real transports.
const deny=()=>{throw Error('network_denied');};
require('node:net').Socket.prototype.connect=deny;
require('node:http').request=deny;require('node:https').request=deny;globalThis.fetch=deny;
const KEY='a'.repeat(64),NOW=Date.parse('2030-01-01T00:00:00Z'),THREAD='abc123',PARENT='def456';
function envelope(extra={}){
 const e={preview_id:'synthetic-preview',version:1,content_hash:'',approved_actor:'YGOR',expires_at:'2030-01-01T00:05:00.000Z',idempotency_key:'synthetic-effect',to:'person@example.invalid',subject:'Pesquisa — ação',body:'Literal.\n  Texto sem strip.  ',thread_id:THREAD,reply_message_id:PARENT,...extra};
 e.content_hash=hash(snapshot(e));e.approval_signature=signature(e,KEY);return e;
}
function parent(){return {id:PARENT,threadId:THREAD,labelIds:['SENT'],payload:{headers:[
 {name:'From',value:EXPECTED.email},{name:'To',value:'Person <person@example.invalid>'},{name:'Subject',value:'Pesquisa — ação'},
 {name:'Message-ID',value:'<parent@example.invalid>'},{name:'References',value:'<root@example.invalid> <middle@example.invalid>'}
]}};}
function thread(){return {id:THREAD,messages:[parent(),{id:'eeee',threadId:THREAD,labelIds:['INBOX'],payload:{headers:[
 {name:'From',value:'person@example.invalid'},{name:'To',value:EXPECTED.email},{name:'Subject',value:'Re: Pesquisa — ação'},
 {name:'Message-ID',value:'<latest-inbound@example.invalid>'}
]}}]};}
function wireFixture(data=thread()){
 let wires=0,threadReads=0,reserves=0,stored=null;const calls=[];
 const transporter={async request(options){calls.push(options);wires++;assert.equal(options.retry,false);assert.equal(options.maxRedirects,0);return {data:{id:'fed987',threadId:THREAD}};}};
 const gmail={context:{_options:{auth:{transporter}}},users:{
 async getProfile(){return {data:{emailAddress:EXPECTED.email}};},
 threads:{async get(args,options){threadReads++;calls.push(args);assert.equal(args.format,'metadata');assert.equal(options.retry,false);assert.ok(options.timeout<=15000);return {data};}},
 messages:{async list(){return {data:{messages:stored?[{id:stored.id}]:[]}};},async get(){return {data:stored};},
 async send(args,options){assert.equal(options.retry,false);const response=await transporter.request({url:'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',method:'POST'});
  const raw=Buffer.from(args.requestBody.raw,'base64url').toString(),[headers,body]=raw.split('\r\n\r\n');
  stored={id:'fed987',threadId:THREAD,labelIds:['SENT'],payload:{mimeType:'text/plain',headers:headers.split('\r\n').map(line=>{const i=line.indexOf(':');return {name:line.slice(0,i),value:line.slice(i+2)};}),body:{data:Buffer.from(body.replace(/\r\n/g,''),'base64').toString('base64url')}}};
  return response;
 }}}};
 const quota={async status(){return {ready:true};},async reserve(){reserves++;},async assertCurrent(){},async finish(){}};
 return {gmail,quota,session:nativeSession(gmail,quota),calls,wires:()=>wires,reads:()=>threadReads,reserves:()=>reserves,stored:()=>stored};
}
async function bridgeFixture(t,provider){
 const dir=await fs.realpath(await fs.mkdtemp(path.join(os.tmpdir(),'s51C-threading-')));t.after(()=>fs.rm(dir,{recursive:true,force:true}));
 const options={provider,credential:KEY,journalDir:dir,sendEnabled:true,now:()=>NOW};return {dir,options,bridge:createBridge(options)};
}
const call=(bridge,method='POST',url='/v1/send',body)=>bridge.handle({method,url,body:body===undefined&&method==='POST'&&url==='/v1/send'?envelope():body,authorization:'Bearer '+KEY});
async function sendNative(f,e=envelope()){
 const info=validate(e,KEY,NOW);await f.session.reserveQuota({effect:info.businessHash,to:e.to,messageId:info.messageId,reply_message_id:e.reply_message_id});
 return f.session.send({...e,message_id:info.messageId});
}
function headerValue(row,name,value){const h=row.payload.headers.find(h=>h.name===name);if(value===undefined)row.payload.headers.splice(row.payload.headers.indexOf(h),1);else h.value=value;}

test('legacy envelope snapshot business HMAC and MIME preserve bytes and static vector',()=>{
 const vector=require('./contract-vector.json');assert.equal(validate(vector.envelope,vector.credential,NOW).snapshotHash,vector.content_hash);assert.equal(signature(vector.envelope,vector.credential),vector.approval_signature);
 const e=envelope();delete e.reply_message_id;
 const legacy={mailbox:EXPECTED.mailbox,workspace:EXPECTED.workspace,preview_id:e.preview_id,version:e.version,to:e.to,subject:e.subject,body:e.body,thread_id:e.thread_id};
 assert.equal(canonical(snapshot(e)),canonical(legacy));delete legacy.preview_id;delete legacy.version;
 assert.equal(canonical(business(e)),canonical(legacy));
 const oldHeaders=[`From: ${EXPECTED.email}`,`To: ${e.to}`,`Subject: =?UTF-8?B?${Buffer.from(e.subject).toString('base64')}?=`,`Message-ID: <legacy@example.invalid>`,'MIME-Version: 1.0','Content-Type: text/plain; charset=UTF-8','Content-Transfer-Encoding: base64','In-Reply-To: <old@example.invalid>','References: <old@example.invalid>'];
 const old=Buffer.from(oldHeaders.join('\r\n')+'\r\n\r\n'+Buffer.from(e.body).toString('base64').match(/.{1,76}/g).join('\r\n')).toString('base64url');
 assert.equal(mime({...e,message_id:'<legacy@example.invalid>'},'<old@example.invalid>'),old);
});
test('parent changes snapshot business deterministic message ID and HMAC; malformed anchor rejected',()=>{
 const a=envelope(),b=envelope({reply_message_id:'def457'}),i=validate(a,KEY,NOW),j=validate(b,KEY,NOW);
 for(const field of ['snapshotHash','businessHash','messageId'])assert.notEqual(i[field],j[field]);assert.notEqual(a.approval_signature,b.approval_signature);
 assert.throws(()=>validate({...a,reply_message_id:b.reply_message_id},KEY,NOW),/snapshot_hash_mismatch/);
 assert.throws(()=>validate({...b,approval_signature:a.approval_signature},KEY,NOW),/approval_signature_invalid/);
 for(const reply_message_id of [null,'','<rfc@example.invalid>','xyz','a\r\nBcc:x','a'.repeat(65)])assert.throws(()=>validate(envelope({reply_message_id}),KEY,NOW),/invalid_reply_message_id/);
 for(const thread_id of [undefined,null,'','not-hex'])assert.throws(()=>validate({...a,thread_id},KEY,NOW),thread_id===''?/invalid_thread/:/invalid_reply_message_id/);
});
test('capability requires strict true; unsupported parent rejected before all journal reservations and quota',async t=>{
 for(const capability of [undefined,false,1,'true']){
  let sends=0,reserves=0;const p={supportsExactReplyParent:capability,async verifyProfile(){return EXPECTED.email;},async quotaStatus(){return {ready:true};},async send(){sends++;},async reserveQuota(){reserves++;}};
  const f=await bridgeFixture(t,p);const status=await call(f.bridge,'GET','/v1/status',undefined);
  assert.deepEqual(status.capabilities,{exact_reply_parent:false});
  await assert.rejects(call(f.bridge),/exact_parent_unsupported/);await f.bridge.journal.ready;
  assert.deepEqual(await fs.readdir(f.dir),[]);assert.equal(sends,0);assert.equal(reserves,0);
 }
});
test('exact parent routes through bridge quota and fake Gmail wire; latest inbound is never selected',async t=>{
 const f=wireFixture(),b=await bridgeFixture(t,f.session),e=envelope();
 let quotaInput;const reserve=f.quota.reserve;f.quota.reserve=async info=>{quotaInput=info;return reserve(info);};
 const status=await call(b.bridge,'GET','/v1/status',undefined);assert.deepEqual(status.capabilities,{exact_reply_parent:true});
 assert.equal((await call(b.bridge)).status,'sent');assert.equal(f.wires(),1);assert.equal(f.reserves(),1);
 const fields=f.calls.find(c=>c.format==='metadata');assert.ok(fields.fields.includes('threadId,labelIds'));assert.ok(fields.metadataHeaders.includes('References'));
 const h=f.stored().payload.headers;assert.equal(h.find(h=>h.name==='In-Reply-To').value,'<parent@example.invalid>');
 assert.equal(h.find(h=>h.name==='References').value,'<root@example.invalid> <middle@example.invalid> <parent@example.invalid>');
 assert.equal(Buffer.from(f.stored().payload.body.data,'base64url').toString(),e.body);
 assert.equal(quotaInput.effect,validate(e,KEY,NOW).businessHash);assert.equal(quotaInput.reply_message_id,PARENT);
 assert.equal((await call(createBridge(b.options))).deduplicated,true);assert.equal(f.wires(),1);
});
test('parent thread sender recipient SENT subject RFC and injection matrix blocks all send wires',async()=>{
 const cases=[
  ['missing',d=>{d.messages.shift();}],['duplicate',d=>d.messages.push(parent())],['thread',d=>{d.id='aaaa';}],
  ['parent thread',d=>{d.messages[0].threadId='aaaa';}],['missing parent thread',d=>{delete d.messages[0].threadId;}],
  ['not sent',d=>{d.messages[0].labelIds=['INBOX'];}],['from',d=>headerValue(d.messages[0],'From','other@example.invalid')],
  ['to',d=>headerValue(d.messages[0],'To','other@example.invalid')],['multiple recipients',d=>headerValue(d.messages[0],'To','person@example.invalid, other@example.invalid')],
  ['subject',d=>headerValue(d.messages[0],'Subject','different')],['RFC missing',d=>headerValue(d.messages[0],'Message-ID',undefined)],
  ['RFC malformed',d=>headerValue(d.messages[0],'Message-ID','<no-domain>')],
  ['injection RFC',d=>headerValue(d.messages[0],'Message-ID','<parent@example.invalid>\r\nBcc: bad@example.invalid')],
  ['injection refs',d=>headerValue(d.messages[0],'References','<root@example.invalid>\nBcc: bad@example.invalid')],
  ['bad refs',d=>headerValue(d.messages[0],'References','not-an-rfc-id')],
  ['duplicate RFC',d=>d.messages[0].payload.headers.push({name:'message-id',value:'<other@example.invalid>'})]
 ];
 for(const [name,mutate] of cases){const data=thread();mutate(data);const f=wireFixture(data);await assert.rejects(sendNative(f),undefined,name);assert.equal(f.wires(),0,name);}
});
test('parent no references is valid and legacy thread still chooses last message',async()=>{
 const d=thread();headerValue(d.messages[0],'References',undefined);headerValue(d.messages[0],'Subject','Re: Pesquisa — ação');
 const exact=wireFixture(d);await sendNative(exact);assert.equal(exact.stored().payload.headers.find(h=>h.name==='References').value,'<parent@example.invalid>');
 const old=wireFixture(),e=envelope();delete e.reply_message_id;e.content_hash=hash(snapshot(e));e.approval_signature=signature(e,KEY);await sendNative(old,e);
 assert.equal(old.stored().payload.headers.find(h=>h.name==='In-Reply-To').value,'<latest-inbound@example.invalid>');
});
test('parent uncertainty survives restart new approval and new parent without another quota or send',async t=>{
 const f=wireFixture(),original=f.gmail.users.messages.send;f.gmail.users.messages.send=async(...args)=>{await original(...args);throw Error('timeout after effect');};
 const b=await bridgeFixture(t,f.session);assert.equal((await call(b.bridge)).status,'uncertain');assert.equal(f.wires(),1);
 const restarted=createBridge(b.options);assert.equal((await call(restarted)).status,'uncertain');
 assert.equal((await call(restarted,'POST','/v1/send',envelope({preview_id:'new-preview',idempotency_key:'new-key'}))).status,'uncertain');
 await assert.rejects(call(restarted,'POST','/v1/send',envelope({reply_message_id:'bbbb'})),/reply_anchor_conflict|idempotency_conflict/);
 await assert.rejects(call(restarted,'POST','/v1/send',envelope({reply_message_id:'bbbb',preview_id:'other-preview',idempotency_key:'other-key'})),/reply_anchor_conflict/);
 assert.equal(f.wires(),1);assert.equal(f.reserves(),1);
 const reconciled=await call(restarted,'POST','/v1/reconcile',{idempotency_key:'synthetic-effect'});assert.equal(reconciled.status,'sent');assert.equal(f.wires(),1);assert.equal(f.reserves(),1);
});
test('reconciliation rejects wrong reply header chain thread or body without retry',async t=>{
 for(const mode of ['in-reply-to','references','thread','body']){
  const f=wireFixture(),original=f.gmail.users.messages.send;f.gmail.users.messages.send=async(...args)=>{await original(...args);throw Error('ambiguous');};
  const b=await bridgeFixture(t,f.session);assert.equal((await call(b.bridge)).status,'uncertain');
  if(mode==='thread')f.stored().threadId='aaaa';else if(mode==='body')f.stored().payload.body.data=Buffer.from('wrong').toString('base64url');
  else headerValue(f.stored(),mode==='references'?'References':'In-Reply-To','<other@example.invalid>');
  if(mode==='body')assert.equal((await call(b.bridge,'POST','/v1/reconcile',{idempotency_key:'synthetic-effect'})).error_code,'receipt_snapshot_mismatch');
  else await assert.rejects(call(b.bridge,'POST','/v1/reconcile',{idempotency_key:'synthetic-effect'}),/reply_receipt_parent_mismatch/);
  assert.equal((await call(createBridge(b.options))).status,'uncertain');assert.equal(f.wires(),1);assert.equal(f.reserves(),1);
 }
});
test('capability never enables static disabled or readonly bridge and never accesses provider send',async t=>{
 const f=wireFixture(),b=await bridgeFixture(t,f.session);
 for(const readOnly of [false,true]){
  const bridge=createBridge({...b.options,sendEnabled:false,readOnly});
  const status=await call(bridge,'GET','/v1/status',undefined);assert.equal(status.send_enabled,false);assert.equal(status.capabilities.exact_reply_parent,true);
  await assert.rejects(call(bridge),readOnly?/read_only_method/:/send_disabled/);
 }
 assert.equal(f.wires(),0);assert.equal(f.reserves(),0);assert.equal(f.reads(),0);
});
test('quota proof binds parent and fake wire guard blocks second resource request and other destinations',async()=>{
 const f=wireFixture(),e=envelope(),info=validate(e,KEY,NOW);
 await f.session.reserveQuota({effect:info.businessHash,to:e.to,messageId:info.messageId,reply_message_id:PARENT});
 const d=thread();d.messages.push({...parent(),id:'bbbb'});const other=wireFixture(d);
 await other.session.reserveQuota({effect:info.businessHash,to:e.to,messageId:info.messageId,reply_message_id:PARENT});
 await assert.rejects(other.session.send({...e,reply_message_id:'bbbb',message_id:info.messageId}),/quota_proof_missing/);assert.equal(other.wires(),0);
 const restore=guardSendWire(f.gmail);await f.gmail.context._options.auth.transporter.request({url:'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',method:'POST'});
 assert.throws(()=>f.gmail.context._options.auth.transporter.request({url:'https://gmail.googleapis.com/gmail/v1/users/me/messages/send',method:'POST'}),/send_retry_blocked/);
 assert.throws(()=>f.gmail.context._options.auth.transporter.request({url:'https://other.invalid/send',method:'POST'}),/unexpected_native_request/);restore();
 assert.throws(()=>require('node:https').request('https://gmail.googleapis.com'),/network_denied/);
});

test('legacy bridge quota and send receive no new field and retain the same effect',async t=>{
 let reserved,mail;
 const provider={async verifyProfile(){return EXPECTED.email;},async reserveQuota(value){reserved=value;},async finishQuota(){},async findDuplicates(){return {complete:true,messages:[]};},async send(value){mail=value;return {id:'abcd',thread_id:THREAD};}};
 const b=await bridgeFixture(t,provider),e=envelope();delete e.reply_message_id;e.content_hash=hash(snapshot(e));e.approval_signature=signature(e,KEY);
 assert.equal((await call(b.bridge,'POST','/v1/send',e)).status,'sent');
 assert.equal(Object.hasOwn(reserved,'reply_message_id'),false);assert.equal(Object.hasOwn(mail,'reply_message_id'),false);
 assert.equal(reserved.effect,validate(e,KEY,NOW).businessHash);
});
test('provider response thread mismatch stays uncertain and cannot trigger another send',async t=>{
 const f=wireFixture(),original=f.gmail.users.messages.send;
 f.gmail.users.messages.send=async(...args)=>{const result=await original(...args);result.data.threadId='aaaa';return result;};
 const b=await bridgeFixture(t,f.session);assert.equal((await call(b.bridge)).status,'uncertain');
 assert.equal((await call(createBridge(b.options))).status,'uncertain');assert.equal(f.wires(),1);
});
test('native readonly session does not announce a send implementation',()=>{
 const f=wireFixture(),read=nativeSession(f.gmail,null,{readOnly:true});
 assert.equal(read.send,undefined);assert.notEqual(read.supportsExactReplyParent,true);
});

test('bounded reference chain reconciles its appended parent and oversized RFC header is refused',async t=>{
 const data=thread();headerValue(data.messages[0],'References',Array.from({length:50},(_,i)=>`<r${i}@x.io>`).join(' '));
 const f=wireFixture(data),original=f.gmail.users.messages.send;
 f.gmail.users.messages.send=async(...args)=>{await original(...args);throw Error('timeout after effect');};
 const b=await bridgeFixture(t,f.session);assert.equal((await call(b.bridge)).status,'uncertain');
 assert.equal((await call(b.bridge,'POST','/v1/reconcile',{idempotency_key:'synthetic-effect'})).status,'sent');assert.equal(f.wires(),1);
 const wide=thread();headerValue(wide.messages[0],'References',Array.from({length:50},(_,i)=>`<${i}${'x'.repeat(25)}@example.invalid>`).join(' '));
 const denied=wireFixture(wide);await assert.rejects(sendNative(denied),/invalid_thread_references/);assert.equal(denied.wires(),0);
});

function technicalPolicy(e=envelope()){
 return {approval_ref:'synthetic-approval',mailbox:EXPECTED.email,to:e.to,subject:e.subject,body_hash:hash(e.body),idempotency_key:e.idempotency_key,issued_at:NOW-1,expires_at:NOW+300000,max_attempts:1,thread_id:e.thread_id,reply_message_id:e.reply_message_id};
}
async function scopedFixture(t,{policy=technicalPolicy(),staticCapability=true,sessionCapability=true,tick=NOW}={}){
 const f=wireFixture();let writeSessions=0,readProfiles=0,policyReads=0;
 f.session.supportsExactReplyParent=sessionCapability;
 const writeProvider={supportsExactReplyParent:staticCapability,async session(){writeSessions++;return f.session;}};
 const readProvider={supportsExactReplyParent:false,async verifyProfile(){readProfiles++;return EXPECTED.email;},quotaStatus:deny,send:deny};
 const base=await bridgeFixture(t,writeProvider);
 const {createScopedBridge,serverFor}=require('./server.cjs');
 const bridge=createScopedBridge({readProvider,writeProvider,credential:KEY,journalDir:base.dir,policyLoader(){policyReads++;return policy;},now:()=>tick});
 const server=serverFor(bridge);t.after(()=>server.close());
 const http=(method,url,body,authorization='Bearer '+KEY)=>new Promise(resolve=>{
  const req=require('node:stream').Readable.from(body===undefined?[]:[Buffer.from(JSON.stringify(body))]);
  req.method=method;req.url=url;req.headers={authorization};
  const res={setHeader(){},writeHead(code){this.code=code;},end(raw){resolve({code:this.code,body:JSON.parse(raw)});}};
  server.emit('request',req,res);
 });
 return {...f,...base,bridge,http,policy,writeSessions:()=>writeSessions,readProfiles:()=>readProfiles,policyReads:()=>policyReads};
}
test('scoped HTTP status advertises only static write capability without write session policy or quota',async t=>{
 for(const staticCapability of [true,false,undefined,1,'true']){
  const f=await scopedFixture(t,{staticCapability});
  // Destructuring defaults turn undefined into true; explicitly remove the property case.
  if(staticCapability===undefined)delete f.options.provider.supportsExactReplyParent;
  const status=await f.http('GET','/v1/status');
  assert.equal(status.code,200);assert.equal(status.body.send_enabled,false);
  assert.equal(status.body.capabilities.exact_reply_parent,staticCapability===true);
  assert.equal(f.writeSessions(),0);assert.equal(f.policyReads(),0);assert.equal(f.reserves(),0);assert.equal(f.wires(),0);assert.equal(f.readProfiles(),1);
  assert.equal((await f.http('GET','/v1/status',undefined,'Bearer wrong')).code,401);assert.equal(f.writeSessions(),0);
 }
});
test('scoped HTTP legacy policy still refuses every followup before journal quota or write profile',async t=>{
 const policy=technicalPolicy();delete policy.thread_id;delete policy.reply_message_id;
 const f=await scopedFixture(t,{policy});
 for(const e of [envelope(),envelope({reply_message_id:'bbbb'})]){
  const result=await f.http('POST','/v1/send',e);assert.equal(result.code,400);assert.equal(result.body.error_code,'scoped_policy_mismatch');
 }
 await f.bridge.journal.ready;assert.deepEqual(await fs.readdir(f.dir),[]);assert.equal(f.writeSessions(),0);assert.equal(f.reserves(),0);assert.equal(f.wires(),0);
});
test('scoped HTTP exact technical policy dispatches once to exact fake Gmail parent and read status stays disabled',async t=>{
 const f=await scopedFixture(t);
 const result=await f.http('POST','/v1/send',envelope());assert.equal(result.code,200);assert.equal(result.body.status,'sent');
 const replay=await f.http('POST','/v1/send',envelope());assert.equal(replay.body.deduplicated,true);
 assert.equal(f.wires(),1);assert.equal(f.reserves(),1);
 assert.equal(f.stored().payload.headers.find(h=>h.name==='In-Reply-To').value,'<parent@example.invalid>');
 const before=f.writeSessions();const status=await f.http('GET','/v1/status');
 assert.equal(status.body.send_enabled,false);assert.equal(status.body.capabilities.exact_reply_parent,true);assert.equal(f.writeSessions(),before);
});
test('scoped HTTP policy tamper and literal envelope matrix fail before journal quota or send',async t=>{
 const cases=[
  ['thread mismatch',p=>{p.thread_id='aaaa';}],['parent mismatch',p=>{p.reply_message_id='bbbb';}],
  ['thread missing',p=>{delete p.thread_id;}],['parent missing',p=>{delete p.reply_message_id;}],
  ['thread null',p=>{p.thread_id=null;}],['parent null',p=>{p.reply_message_id=null;}],
  ['wildcard thread',p=>{p.thread_id='*';}],['wildcard parent',p=>{p.reply_message_id='*';}],
  ['nonhex parent',p=>{p.reply_message_id='<parent@example.invalid>';}],['long parent',p=>{p.reply_message_id='a'.repeat(65);}],
  ['mailbox',p=>{p.mailbox='other@example.invalid';}],['recipient',p=>{p.to='other@example.invalid';}],
  ['body hash',p=>{p.body_hash=hash('different');}],['idempotency',p=>{p.idempotency_key='other';}],
  ['subject',p=>{p.subject='other';}],['approval missing',p=>{p.approval_ref='';}],
  ['attempts2',p=>{p.max_attempts=2;}],['attempts0',p=>{p.max_attempts=0;}],['attempts string',p=>{p.max_attempts='1';}],
  ['future issued',p=>{p.issued_at=NOW+1;}],['expired',p=>{p.expires_at=NOW;}],
  ['ttl too long',p=>{p.expires_at=p.issued_at+900001;}],['issued noninteger',p=>{p.issued_at=NOW-0.5;}],
  ['unknown field',p=>{p.allow_any_parent=true;}],
  ['body literal',()=>{},()=>envelope({body:'Literal changed'})],
  ['parent envelope',()=>{},()=>envelope({reply_message_id:'bbbb'})],
  ['subject header injection',p=>{p.subject='subject\r\nBcc: evil@example.invalid';},p=>envelope({subject:p.subject})],
  ['recipient header injection',p=>{p.to='person@example.invalid\r\nBcc: evil@example.invalid';},p=>envelope({to:p.to})]
 ];
 for(const [name,mutate,makeEnvelope] of cases){
  const policy=technicalPolicy();mutate(policy);const f=await scopedFixture(t,{policy});
  const result=await f.http('POST','/v1/send',makeEnvelope?makeEnvelope(policy):envelope());
  assert.ok([400,403].includes(result.code),name);assert.equal(result.body.status,'rejected',name);
  await f.bridge.journal.ready;assert.deepEqual(await fs.readdir(f.dir),[],name);assert.equal(f.reserves(),0,name);assert.equal(f.wires(),0,name);
 }
});
test('scoped static capability cannot bypass the active session capability guard',async t=>{
 const f=await scopedFixture(t,{sessionCapability:false});
 assert.equal((await f.http('GET','/v1/status')).body.capabilities.exact_reply_parent,true);
 const result=await f.http('POST','/v1/send',envelope());assert.equal(result.code,503);assert.equal(result.body.error_code,'exact_parent_unsupported');
 await f.bridge.journal.ready;assert.deepEqual(await fs.readdir(f.dir),[]);assert.equal(f.reserves(),0);assert.equal(f.wires(),0);
});
test('scoped policy accepts inclusive 15 minute TTL without changing envelope approval expiry',async t=>{
 const policy=technicalPolicy();policy.issued_at=NOW-600000;policy.expires_at=NOW+300000;
 const f=await scopedFixture(t,{policy});assert.equal((await f.http('POST','/v1/send',envelope())).body.status,'sent');assert.equal(f.wires(),1);
});
test('native factory declares static write capability only without opening database or provider session',async()=>{
 const vm=require('node:vm'),source=require('node:fs').readFileSync(path.join(__dirname,'native.cjs'),'utf8'),pins=require('./native.cjs').PINS;
 const runtime=async()=>({qf:deny});runtime.e=async()=>{};runtime.m={};
 const sandbox={module:{exports:{}},require(id){
  if(id==='node:fs')return {readFileSync(file){return file;}};
  if(id==='node:crypto')return {createHash(){return {update(value){return {digest(){return pins[value.slice('/app/.next/server/'.length)];}};}};}};
  if(id==='node:module')return {builtinModules:[],createRequire(){return deny;}};
  if(id==='./contract.cjs')return require('./contract.cjs');
  if(id==='/app/.next/server/webpack-runtime.js')return runtime;
  throw Error('unexpected factory dependency');
 }};
 vm.runInNewContext(source,sandbox,{filename:'synthetic-native-factory.cjs'});
 const write=await sandbox.module.exports.createNativeProvider({readOnly:false});
 const read=await sandbox.module.exports.createNativeProvider({readOnly:true});
 assert.equal(write.supportsExactReplyParent,true);assert.equal(Object.hasOwn(read,'supportsExactReplyParent'),false);
 assert.equal(typeof write.session,'function');assert.equal(typeof read.session,'function');
});
