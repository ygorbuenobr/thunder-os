'use strict';
const fixtureMode=process.argv.includes('--fixture-json');
const test=fixtureMode?null:require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm');
const net=require('node:net'),tls=require('node:tls');
const {createBridge}=require('./bridge.cjs');
const {createReadOnlyBridge}=require('./server.cjs');
const {EXPECTED}=require('./contract.cjs');
const {nativeSession,normalizedReadOnly,guardReadWire,PINS}=require('./native.cjs');
const KEY='a'.repeat(64),ID='abc123',THREAD='def456';

// Importing the entrypoint is inert; every test forbids listeners/connections.
if(!fixtureMode)test.beforeEach(t=>{
 for(const [object,key]of [[net.Server.prototype,'listen'],[net.Socket.prototype,'connect'],[tls,'connect']])
  t.mock.method(object,key,()=>{throw Error('test_network_forbidden');});
});
function raw(){
 const body=Buffer.from('Synthetic observed body.\nNo external delivery.');
 return {id:ID,threadId:THREAD,labelIds:['SENT'],payload:{mimeType:'text/plain',headers:[
  {name:'From',value:EXPECTED.email},{name:'To',value:'Person <person@example.invalid>'},
  {name:'Subject',value:'Synthetic subject'},{name:'Date',value:'Thu, 01 Jan 2030 00:00:00 +0000'},
  {name:'Message-ID',value:'<synthetic@example.invalid>'},
  {name:'Content-Type',value:'text/plain; charset=UTF-8'},
  {name:'X-Synthetic-Evidence',value:'complete headers preserved'}],body:{size:body.length,data:body.toString('base64url')}}};
}
function fixture(){
 const calls=[],wires=[];let data=raw(),profile=EXPECTED.email;
 const transporter={async request(options){wires.push(options);return {data:options.url.endsWith('/profile')?{emailAddress:profile}:data};}};
 const gmail={context:{_options:{auth:{transporter}}},users:{
  async getProfile(params,options){calls.push(['profile',params]);return transporter.request({...options,url:'https://gmail.googleapis.com/gmail/v1/users/me/profile',method:'GET',params:{fields:params.fields}});},
  messages:{async get(params,options){calls.push(['get',params]);return transporter.request({...options,url:'https://gmail.googleapis.com/gmail/v1/users/me/messages/'+params.id,method:'GET',params:{format:params.format,fields:params.fields}});}}
 }};
 const quota=new Proxy({}, {get(){throw Error('quota_forbidden');}});
 const provider={async session(){calls.push(['session']);return nativeSession(gmail,quota,{readOnly:true});}};
 return {provider,calls,wires,setData(value){data=value;},setProfile(value){profile=value;},bridge:createBridge({provider,credential:KEY,readOnly:true,sendEnabled:true})};
}
const call=(bridge,method,url,body)=>bridge.handle({method,url,body,authorization:'Bearer '+KEY});

if(!fixtureMode){
test('read-only GET status checks pinned profile without Journal or quota',async()=>{
 const f=fixture(),result=await call(f.bridge,'GET','/v1/status');
 assert.deepEqual(result,{mailbox:EXPECTED.email,status:'ready',send_enabled:false,capabilities:{exact_reply_parent:false}});
 assert.equal(f.bridge.journal,null);assert.deepEqual(f.calls.map(row=>row[0]),['session','profile']);
 assert.equal(f.wires.length,1);assert.equal(f.wires[0].timeout,3000);
});
test('exact ID reads full observed body, all headers, labels and explicit Cc/Bcc',async()=>{
 const f=fixture(),result=await call(f.bridge,'GET','/v1/messages/'+ID),m=result.message;
 assert.equal(result.mailbox,EXPECTED.email);assert.equal(result.status,'ok');
 assert.equal(m.id,ID);assert.equal(m.thread_id,THREAD);assert.equal(m.from,EXPECTED.email);
 assert.equal(m.to,'Person <person@example.invalid>');assert.equal(m.subject,'Synthetic subject');
 assert.equal(m.body,Buffer.from(raw().payload.body.data,'base64url').toString());
 assert.equal(m.sent,true);assert.equal(m.truncated,false);assert.equal(m.cc,'');assert.equal(m.bcc,'');
 assert.equal(m.headers_complete,true);assert.deepEqual(m.headers,raw().payload.headers);assert.deepEqual(m.labelIds,['SENT']);
 assert.deepEqual(f.calls.map(row=>row[0]),['session','profile','get']);
 assert.equal(f.calls[2][1].format,'full');assert.equal(f.calls[2][1].fields,'id,threadId,labelIds,payload');
 for(const wire of f.wires){assert.equal(wire.method,'GET');assert.equal(wire.retry,false);assert.equal(wire.retryConfig.retry,0);assert.equal(wire.maxRedirects,0);}
});
test('profile mismatch blocks exact read before message transport',async()=>{
 const f=fixture();f.setProfile('other@example.invalid');
 await assert.rejects(call(f.bridge,'GET','/v1/messages/'+ID),/provider_account_mismatch/);
 assert.deepEqual(f.calls.map(row=>row[0]),['session','profile']);
});
test('wrong exact message ID never returns a receipt observation',async()=>{
 const f=fixture(),data=raw();data.id='bad456';f.setData(data);
 await assert.rejects(call(f.bridge,'GET','/v1/messages/'+ID),/provider_message_mismatch/);
});
test('every non-GET verb is rejected before provider, Journal or filesystem',async()=>{
 const loaded={exports:{}},source=fs.readFileSync(require.resolve('./bridge.cjs'),'utf8');let journals=0;
 const provider=new Proxy({}, {get(){throw Error('provider_forbidden');}});
 vm.runInNewContext(source,{module:loaded,require(name){
  if(name==='./journal.cjs')return {Journal:class{constructor(){journals++;throw Error('journal_forbidden');}}};
  if(name==='./contract.cjs')return require(name);throw Error('unexpected_module');
 },URL,Buffer});
 const bridge=loaded.exports.createBridge({provider,credential:KEY,readOnly:true,sendEnabled:true});
 for(const method of ['POST','PUT','PATCH','DELETE','HEAD','OPTIONS','TRACE','CONNECT','get',''])
  for(const url of ['/v1/send','/v1/reconcile','/v1/status','/v1/messages/'+ID])
   await assert.rejects(call(bridge,method,url,{}),error=>error.code==='read_only_method'&&error.status===405);
 assert.equal(journals,0);assert.equal(bridge.journal,null);
});
test('readonly route rejects unbounded collection queries encoded IDs and GET bodies before provider',async()=>{
 const f=fixture();
 for(const url of ['/v1/messages?limit=11','/v1/messages?limit=1&format=raw','/v1/messages/abc123?format=raw',
  '/v1/messages/ABC123','/v1/messages/%61bc123','/v1/messages/../status','/v1/status?x=1','https://bridge.invalid/v1/status'])
  await assert.rejects(call(f.bridge,'GET',url));
 await assert.rejects(call(f.bridge,'GET','/v1/status',{}),/read_only_body/);
 assert.deepEqual(f.calls,[]);
});
test('prepared entrypoint ignores SEND env and requires explicit bearer authentication',async()=>{
 const before=process.env.SEQD_BRIDGE_SEND_ENABLED;process.env.SEQD_BRIDGE_SEND_ENABLED='true';
 try{
  const f=fixture(),bridge=createReadOnlyBridge(f.provider,KEY);
  assert.equal((await call(bridge,'GET','/v1/status')).send_enabled,false);
  await assert.rejects(call(bridge,'POST','/v1/send',{}),/read_only_method/);
  await assert.rejects(bridge.handle({method:'GET',url:'/v1/status',authorization:'Bearer wrong'}),/unauthorized/);
  assert.equal(bridge.journal,null);
 }finally{if(before===undefined)delete process.env.SEQD_BRIDGE_SEND_ENABLED;else process.env.SEQD_BRIDGE_SEND_ENABLED=before;}
});
test('missing or duplicated critical headers and missing label evidence fail closed',()=>{
 const cases=[];
 for(const name of ['From','To','Subject']){
  const data=raw();data.payload.headers=data.payload.headers.filter(h=>h.name!==name);cases.push(data);
 }
 for(const name of ['From','To','Subject','Cc','Bcc','Date','Message-ID','Content-Type','Content-Disposition','Content-Transfer-Encoding']){
  const data=raw();if(!data.payload.headers.some(h=>h.name===name))data.payload.headers.push({name,value:''});
  data.payload.headers.push({name:name.toLowerCase(),value:'duplicated@example.invalid'});cases.push(data);
 }
 const noHeaders=raw();delete noHeaders.payload.headers;cases.push(noHeaders);
 const noLabels=raw();delete noLabels.labelIds;cases.push(noLabels);
 const badLabels=raw();badLabels.labelIds=['SENT',null];cases.push(badLabels);
 for(const data of cases)assert.throws(()=>normalizedReadOnly(data),/read_(headers|header|labels)/);
});
test('long From To Subject are rejected without silent truncation',()=>{
 for(const name of ['From','To','Subject','Cc','Bcc']){
  const data=raw(),value='x'.repeat(2049),existing=data.payload.headers.find(h=>h.name===name);
  if(existing)existing.value=value;else data.payload.headers.push({name,value});
  assert.throws(()=>normalizedReadOnly(data),/read_headers_incomplete/);
 }
 const data=raw();data.payload.headers.push({name:'X-Large',value:'x'.repeat(8193)});
 assert.throws(()=>normalizedReadOnly(data),/read_headers_incomplete/);
});
test('Cc Bcc SENT and DRAFT remain observed evidence, never silently erased',async()=>{
 const f=fixture(),data=raw();data.labelIds=['SENT','DRAFT'];
 data.payload.headers.push({name:'Cc',value:'copy@example.invalid'},{name:'Bcc',value:'hidden@example.invalid'});
 f.setData(data);const result=(await call(f.bridge,'GET','/v1/messages/'+ID)).message;
 assert.deepEqual(result.labelIds,['SENT','DRAFT']);assert.equal(result.sent,true);
 assert.equal(result.cc,'copy@example.invalid');assert.equal(result.bcc,'hidden@example.invalid');
 const unsent=raw();unsent.labelIds=['DRAFT'];assert.equal(normalizedReadOnly(unsent).sent,false);
});
test('partial oversized malformed HTML-only or ambiguous MIME is never complete',()=>{
 const cases=[];
 for(const modify of [data=>{data.truncated=true;},data=>{data.payload.body.size++;},
  data=>{delete data.payload.body.data;},data=>{data.payload.body.attachmentId='attachment';},
  data=>{data.payload.filename='attachment.txt';},
  data=>{data.payload.body.data='!';},data=>{data.payload.body={data:'_w',size:1};},
  data=>{data.payload.mimeType='text/html';},
  data=>{data.payload.headers.find(h=>h.name==='Content-Type').value='text/plain; charset=iso-8859-1';},
  data=>{data.payload.headers.find(h=>h.name==='Content-Type').value='text/html; charset=UTF-8';},
  data=>{data.payload.headers.find(h=>h.name==='Content-Type').value='text/plain; charset=us-ascii; charset=utf-8';},
  data=>{data.payload.headers.push({name:'Content-Disposition',value:'inline; filename=hidden.txt'});},
  data=>{data.payload.body={data:Buffer.from('\ufffd').toString('base64url'),size:3};},
  data=>{const body=Buffer.alloc(65537,65);data.payload.body={size:body.length,data:body.toString('base64url')};}]){
  const data=raw();modify(data);cases.push(data);
 }
 const multiple=raw(),part=structuredClone(multiple.payload);multiple.payload={...multiple.payload,mimeType:'multipart/mixed',parts:[part,part]};cases.push(multiple);
 const deep=raw();for(let i=0;i<10;i++)deep.payload={mimeType:'multipart/mixed',headers:deep.payload.headers,parts:[deep.payload]};cases.push(deep);
 for(const data of cases)assert.throws(()=>normalizedReadOnly(data),/read_(message|body)/);
});
test('approved plain plus additional or unavailable HTML never becomes complete body',()=>{
 for(const mimeType of ['multipart/mixed','multipart/alternative'])for(const html of [
  {mimeType:'text/html',body:{data:Buffer.from('additional unapproved content').toString('base64url')}},
  {mimeType:'text/html',body:{size:100}}]){
  const data=raw(),plain=structuredClone(data.payload);
  data.payload={mimeType,headers:data.payload.headers,parts:[plain,html]};
  assert.throws(()=>normalizedReadOnly(data),/read_body_unsupported/);
 }
 const data=raw();data.payload.parts=[{mimeType:'text/html'}];
 assert.throws(()=>normalizedReadOnly(data),/read_body_unsupported/);
});
test('normalized provider omissions and mismatched header copies fail at bridge boundary',async()=>{
 for(const key of ['sent','truncated','cc','bcc','headers','headers_complete','labelIds']){
  const f=fixture(),message=normalizedReadOnly(raw());delete message[key];
  const bridge=createBridge({credential:KEY,readOnly:true,provider:{async verifyProfile(){return EXPECTED.email;},async get(){return message;}}});
  await assert.rejects(call(bridge,'GET','/v1/messages/'+ID),/read_/);assert.equal(f.calls.length,0);
 }
 const message=normalizedReadOnly(raw());message.to='other@example.invalid';
 const bridge=createBridge({credential:KEY,readOnly:true,provider:{async verifyProfile(){return EXPECTED.email;},async get(){return message;}}});
 await assert.rejects(call(bridge,'GET','/v1/messages/'+ID),/read_headers_mismatch/);
});
test('native readonly session exposes only profile and read ports, no quota or writer',()=>{
 const transporter={request(){throw Error('transport_not_called');}},gmail={context:{_options:{auth:{transporter}}}};
 assert.deepEqual(Object.keys(nativeSession(gmail,new Proxy({}, {get(){throw Error('quota_forbidden');}}),{readOnly:true})).sort(),['contactSearch','get','list','readThread','verifyProfile']);
});
test('native wire permits only pinned GETs and the existing refresh-token exchange',async()=>{
 const seen=[],transporter={async request(options){seen.push(options);return {data:{}};}};
 guardReadWire({context:{_options:{auth:{transporter}}}});
 for(const options of [
  {url:'https://gmail.googleapis.com/gmail/v1/users/me/profile',method:'GET',params:{fields:'emailAddress'}},
  {url:'https://gmail.googleapis.com/gmail/v1/users/me/messages/'+ID,method:'GET',params:{format:'full',fields:'id,threadId,labelIds,payload'}},
  {url:'https://oauth2.googleapis.com/token',method:'POST',data:new URLSearchParams({grant_type:'refresh_token',refresh_token:'synthetic-only'})}])await transporter.request(options);
 assert.equal(seen.length,3);for(const options of seen){assert.equal(options.retry,false);assert.equal(options.timeout,3000);assert.equal(options.maxRedirects,0);}
});
test('native wire denies mutations unbounded listing threads redirects and other OAuth grants before original',()=>{
 let calls=0;const transporter={request(){calls++;}};guardReadWire({context:{_options:{auth:{transporter}}}});
 const root='https://gmail.googleapis.com/gmail/v1/users/me/';
 const denied=[...['messages/send','drafts','labels','messages','threads/abc123'].flatMap(path=>['POST','GET','DELETE'].map(method=>({url:root+path,method}))),
  {url:root+'profile',method:'GET',params:{fields:'emailAddress'},data:{}},
  {url:root+'messages/'+ID,method:'GET',params:{format:'metadata',fields:'id'}},
  {url:root+'profile?fields=emailAddress&fields=emailAddress',method:'GET'},
  {url:'http://gmail.googleapis.com/gmail/v1/users/me/profile',method:'GET'},
  {url:'https://other.invalid/gmail/v1/users/me/profile',method:'GET'},
  {url:root+'profile#fragment',method:'GET'},
  {url:'https://oauth2.googleapis.com/token',method:'POST',data:{grant_type:'authorization_code',code:'synthetic'}},
  {url:'https://oauth2.googleapis.com/token',method:'POST',data:'grant_type=refresh_token&refresh_token=synthetic&grant_type=authorization_code'}];
 for(const options of denied)assert.throws(()=>transporter.request(options),/read_wire_blocked/);
 assert.equal(calls,0);
});
test('read failure is not retried and legacy app pins are retained',async()=>{
 const f=fixture();f.provider.session=async()=>{f.calls.push(['session']);throw Error('synthetic timeout');};
 await assert.rejects(call(f.bridge,'GET','/v1/status'),/synthetic timeout/);assert.deepEqual(f.calls,[['session']]);
 assert.equal(PINS['chunks/3317.js'],'d63f208341bdb2cbce20edfbfea9c2ef9dca32977ded98bd9af5818c949e1267');
 assert.equal(PINS['webpack-runtime.js'],'20fb3acf64b82b17345c161ff7e824fdbef7acf91c900c53ea0615de9a56a141');
});
}else{
 // Synthetic literal for an independent bridge-JSON -> Python parser check.
 const f=fixture();
 call(f.bridge,'GET','/v1/messages/'+ID).then(value=>process.stdout.write(JSON.stringify(value)+'\n'))
  .catch(()=>{process.stderr.write('synthetic_fixture_failed\n');process.exitCode=1;});
}

test('multipart preserves full signed text and HTML as non-receipt evidence',async()=>{
 const data=raw(),plain=structuredClone(data.payload),body='Approved text.\r\nSignature https://example.invalid\r\n';
 plain.body={size:Buffer.byteLength(body),data:Buffer.from(body).toString('base64url')};
 const html={mimeType:'text/html',headers:[{name:'Content-Type',value:'text/html; charset=UTF-8'}],body:{size:12,data:Buffer.from('<p>Other</p>').toString('base64url')}};
 data.payload={mimeType:'multipart/alternative',headers:data.payload.headers.map(h=>h.name==='Content-Type'?{...h,value:'multipart/alternative; boundary=synthetic'}:h),body:{size:0},parts:[plain,html]};
 const f=fixture();f.setData(data);const result=(await call(f.bridge,'GET','/v1/messages/'+ID)).message;
 assert.equal(result.body,body);assert.equal(result.receipt_eligible,false);assert.equal(result.context_only,true);
 assert.equal(result.mime_evidence.alternatives[1].body,'<p>Other</p>');
 for(const mutate of [d=>d.payload.parts.push(plain),d=>d.payload.parts=[plain,plain],d=>d.payload.parts=[html],
  d=>d.payload.parts[0].filename='file.txt',d=>d.payload.parts[1].body.attachmentId='file',
  d=>d.payload.parts[0].parts=[plain],d=>d.payload.parts[1].body.size++,
  d=>d.payload.parts[1].headers.push({name:'Content-Type',value:'text/html'}),
  d=>d.payload.parts[0].headers.push({name:'Content-Disposition',value:'attachment'}),
  d=>d.payload.parts[0].headers.push({name:'Content-Disposition',value:' attachment'}),
  d=>d.payload.parts[0].headers.push({name:'Content-Disposition',value:"inline; filename*=UTF-8''file.txt"}),
  d=>d.payload.parts[0].headers.push({name:'Content-Disposition',value:'inline; filename*0*=file'}),
  d=>d.payload.parts[0].headers.find(h=>h.name==='Content-Type').value+="; name*=UTF-8''file.txt"]){
  const bad=structuredClone(data);mutate(bad);assert.throws(()=>normalizedReadOnly(bad),/read_body/);
 }
});
