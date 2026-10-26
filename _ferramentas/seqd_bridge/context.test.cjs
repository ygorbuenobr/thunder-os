'use strict';
const test=require('node:test'),assert=require('node:assert/strict');
const net=require('node:net'),tls=require('node:tls');
const {createBridge}=require('./bridge.cjs');
const {EXPECTED}=require('./contract.cjs');
const {nativeSession,normalizedThread,guardReadWire}=require('./native.cjs');
const KEY='a'.repeat(64),ID='abc123',THREAD='def456';
const THREAD_FIELDS='id,messages(id,threadId,labelIds,internalDate,payload)';
const LIST_FIELDS='messages(id,threadId),nextPageToken';
const LIST_QUERY='in:inbox newer_than:7d';
test.beforeEach(t=>{
 for(const [object,key]of [[net.Server.prototype,'listen'],[net.Socket.prototype,'connect'],[tls,'connect']])
  t.mock.method(object,key,()=>{throw Error('test_network_forbidden');});
});
function part(mimeType,text){const body=Buffer.from(text);return {partId:'0',mimeType,filename:'',headers:[{name:'Content-Type',value:mimeType+'; charset=UTF-8'}],body:{size:body.length,data:body.toString('base64url')}};}
function message(id=ID,text='Observed outbound context.',sender=EXPECTED.email){
 const payload=part('text/plain',text);payload.headers.push(
  {name:'From',value:sender},{name:'To',value:'Person <person@example.invalid>'},
  {name:'Subject',value:'Observed subject'},{name:'Date',value:'Thu, 01 Jan 2030 00:00:00 +0000'},
  {name:'Message-ID',value:'<'+id+'@example.invalid>'});
 return {id,threadId:THREAD,labelIds:[sender===EXPECTED.email?'SENT':'INBOX'],internalDate:'1893456000000',payload};
}
function conversation(){return {id:THREAD,messages:[message(),message('abc124','Observed inbound response.','person@example.invalid')]};}
function mimeHeaders(headers,mimeType){return headers.map(row=>row.name==='Content-Type'?{name:'Content-Type',value:mimeType}:row);}
function fixture(){
 const calls=[],wires=[];let data=conversation(),profile=EXPECTED.email,page={messages:[{id:ID,threadId:THREAD}],nextPageToken:'opaque+page/=2'};
 const transporter={async request(options){wires.push(options);if(options.url.endsWith('/profile'))return {data:{emailAddress:profile}};if(options.url.endsWith('/messages'))return {data:page};return {data:options.url.includes('/threads/')?data:data.messages[0]};}};
 const wire=(path,params,options)=>transporter.request({...options,url:'https://gmail.googleapis.com/gmail/v1/users/me/'+path,method:'GET',params});
 const gmail={context:{_options:{auth:{transporter}}},users:{
  async getProfile(params,options){calls.push(['profile',params]);return wire('profile',{fields:params.fields},options);},
  messages:{async list(params,options){calls.push(['list',params]);const {userId,...query}=params;return wire('messages',query,options);},
   async get(params,options){calls.push(['get',params]);return wire('messages/'+params.id,{fields:params.fields,format:params.format},options);}},
  threads:{async get(params,options){calls.push(['thread',params]);return wire('threads/'+params.id,{fields:params.fields,format:params.format},options);}}
 }};
 const quota=new Proxy({}, {get(){throw Error('quota_forbidden');}});
 const provider={async session(){calls.push(['session']);return nativeSession(gmail,quota,{readOnly:true});}};
 return {calls,wires,transporter,gmail,provider,bridge:createBridge({provider,credential:KEY,readOnly:true,sendEnabled:true}),setData(value){data=value;},setPage(value){page=value;},setProfile(value){profile=value;}};
}
const call=(bridge,url)=>bridge.handle({method:'GET',url,authorization:'Bearer '+KEY});

test('readonly list preserves opaque cursor and shows page coverage without quota or Journal',async()=>{
 const f=fixture(),first=await call(f.bridge,'/v1/messages?limit=1');
 assert.equal(first.mailbox,EXPECTED.email);assert.deepEqual(first.messages,[{id:ID,thread_id:THREAD}]);
 assert.equal(first.next_cursor,'opaque+page/=2');assert.equal(first.complete,false);assert.equal(first.coverage.scope,'page');assert.equal(first.coverage.has_more,true);
 assert.equal(first.untrusted_context,true);assert.equal(first.receipt_eligible,false);
 assert.equal(first.scope,'inbox_last_7_days');assert.equal(first.query,LIST_QUERY);assert.equal(first.listing_complete,false);
 assert.equal(f.calls.find(row=>row[0]==='list')[1].q,LIST_QUERY);
 f.setPage({messages:[]});const last=await call(f.bridge,'/v1/messages?limit=1&cursor='+encodeURIComponent(first.next_cursor));
 assert.equal(last.next_cursor,null);assert.equal(last.coverage.has_more,false);assert.equal(last.complete,false);
 assert.equal(last.listing_complete,true);assert.equal(last.query,LIST_QUERY);assert.equal(last.coverage.window_days,7);
 assert.ok(f.calls.filter(row=>row[0]==='list').every(row=>row[1].q===LIST_QUERY));
 assert.equal(f.calls.filter(row=>row[0]==='list')[1][1].pageToken,first.next_cursor);
 assert.equal(f.bridge.journal,null);assert.ok(f.wires.every(row=>row.method==='GET'));
});
test('legacy native list keeps its existing queryless contract without enabling a writer',async()=>{
 let observed;const gmail={users:{messages:{async list(params){observed=params;return {data:{messages:[{id:ID,threadId:THREAD}]}};}}}};
 const session=nativeSession(gmail,null),page=await session.list({limit:2,cursor:'legacy-page'});
 assert.deepEqual(observed,{userId:'me',maxResults:2,pageToken:'legacy-page',fields:LIST_FIELDS});
 assert.deepEqual(page,{messages:[{id:ID,thread_id:THREAD}],next_cursor:null});
});
test('list rejects invalid limits cursors extra duplicate queries and malformed provider pages',async()=>{
 const f=fixture();
 for(const url of ['/v1/messages?limit=0','/v1/messages?limit=11','/v1/messages?limit=1.0','/v1/messages?limit=1e0','/v1/messages?limit=',
  '/v1/messages?limit=1&limit=2','/v1/messages?cursor=','/v1/messages?cursor=%00','/v1/messages?cursor='+('x'.repeat(1025)),
  '/v1/messages?cursor=a&cursor=b','/v1/messages?q=in:sent','/v1/messages?q='+encodeURIComponent(LIST_QUERY),'/v1/messages?format=full'])await assert.rejects(call(f.bridge,url));
 assert.deepEqual(f.calls,[]);
 for(const page of [{messages:[{id:ID,threadId:THREAD},{id:'abc124',threadId:THREAD}]},
  {messages:[{id:'not-hex',threadId:THREAD}]},{messages:[{id:ID}]},{messages:{}},{nextPageToken:''},{nextPageToken:4}]){
  f.setPage(page);await assert.rejects(call(f.bridge,'/v1/messages?limit=1'),/invalid_provider_response/);
 }
});
test('thread context preserves outbound inbound headers dates labels plain and HTML as untrusted data',async()=>{
 const f=fixture(),data=conversation(),html='<p>Observed <b>response</b></p><script>throw new Error("never execute")</script>';
 data.messages[1].payload={mimeType:'multipart/alternative',headers:mimeHeaders(data.messages[1].payload.headers,'multipart/alternative'),body:{size:0},parts:[part('text/plain','Observed inbound response.'),part('text/html',html)]};
 f.setData(data);const result=await call(f.bridge,'/v1/threads/'+THREAD),thread=result.thread;
 assert.equal(result.mailbox,EXPECTED.email);assert.equal(thread.id,THREAD);assert.equal(thread.complete,true);assert.equal(thread.truncated,false);
 assert.equal(thread.context_only,true);assert.equal(thread.receipt_eligible,false);assert.equal(thread.untrusted_context,true);
 assert.equal(thread.messages.length,2);assert.equal(thread.messages[0].bodies.text,'Observed outbound context.');
 assert.equal(thread.messages[1].bodies.text,'Observed inbound response.');assert.equal(thread.messages[1].bodies.html,html);
 assert.deepEqual(thread.messages[1].headers,data.messages[1].payload.headers);
 assert.deepEqual(thread.messages[0].labelIds,['SENT']);assert.deepEqual(thread.messages[1].labelIds,['INBOX']);
 assert.equal(thread.messages[1].internal_date,'1893456000000');assert.equal(thread.messages[1].date,'Thu, 01 Jan 2030 00:00:00 +0000');
 assert.ok(thread.participants.includes('person@example.invalid'));assert.ok(thread.participants.includes(EXPECTED.email));
 assert.equal(thread.messages[1].parts.parts.length,2);assert.equal(thread.messages[1].receipt_eligible,false);
 assert.equal(Object.hasOwn(thread.messages[1],'body'),false);assert.equal(Object.hasOwn(thread.messages[1],'sent'),false);
 assert.deepEqual(f.calls.map(row=>row[0]),['session','profile','thread']);
 assert.deepEqual(f.calls[2][1],{userId:'me',id:THREAD,format:'full',fields:THREAD_FIELDS});
});
test('HTML-only context retains HTML literally and never invents plain text',()=>{
 const data=conversation(),payload=part('text/html','<p>Only HTML &amp; a link</p>');payload.headers.push(...data.messages[0].payload.headers.filter(row=>row.name!=='Content-Type'));
 data.messages[0].payload=payload;const normalized=normalizedThread(data);
 assert.equal(normalized.complete,true);assert.equal(normalized.messages[0].bodies.text,null);
 assert.equal(normalized.messages[0].bodies.html,'<p>Only HTML &amp; a link</p>');
});
test('attachments stay metadata only and missing textual data remains visibly partial',()=>{
 for(const missing of [false,true]){
  const data=conversation(),m=data.messages[0],plain=m.payload,html=part('text/html','<p>Extra context</p>');
  if(missing)delete html.body.data;
  const attachment={partId:'2',filename:'sample.pdf',mimeType:'application/pdf',headers:[],body:{attachmentId:'opaque-binary-id',size:1234}};
  m.payload={mimeType:'multipart/mixed',headers:mimeHeaders(plain.headers,'multipart/mixed'),body:{size:0},parts:[plain,html,attachment]};
  const thread=normalizedThread(data),observed=thread.messages[0];
  assert.equal(thread.complete,false);assert.equal(observed.complete,false);assert.equal(observed.attachments.length,1);
  assert.equal(observed.attachments[0].filename,'sample.pdf');assert.equal(observed.attachments[0].size,1234);
  assert.equal(observed.attachments[0].attachment_id,'opaque-binary-id');assert.equal(observed.attachments[0].content_fetched,false);
  assert.equal(thread.coverage.attachment_bodies_fetched,false);assert.ok(thread.coverage.reasons.length);
  assert.equal(observed.bodies.text,'Observed outbound context.');
  if(missing)assert.equal(observed.bodies.html,null);
 }
});
test('missing headers labels dates payloads malformed encoding and size mismatches cannot be complete',()=>{
 const edits=[m=>{delete m.labelIds;},m=>{delete m.internalDate;},m=>{delete m.payload;},m=>{delete m.payload.headers;},
  m=>{m.payload.headers=m.payload.headers.filter(row=>row.name!=='From');},m=>{m.payload.headers.push({name:'From',value:'second@example.invalid'});},
  m=>{m.payload.body.size++;},m=>{m.payload.body.data='!';},m=>{m.payload.body={data:'_w',size:1};},
  m=>{m.payload.body.attachmentId='text-not-fetched';delete m.payload.body.data;},
  m=>{m.payload.headers[0].value='text/plain; charset=unknown';},m=>{m.payload.parts=[part('text/html','hidden child')];},
  m=>{m.payload.headers[0].value='text/html; charset=UTF-8';},m=>{m.payload.headers.find(row=>row.name==='From').value='';}];
 for(const edit of edits){const data=conversation();edit(data.messages[0]);const thread=normalizedThread(data);assert.equal(thread.complete,false);assert.ok(thread.coverage.reasons.length);}
});
test('message part depth count and total text bounds never report a complete conversation',()=>{
 const inputs=[];
 const oversized=conversation();oversized.messages[0].payload={...part('text/plain','x'.repeat(65537)),headers:oversized.messages[0].payload.headers};inputs.push(oversized);
 const combined=conversation();combined.messages[0].payload={mimeType:'multipart/alternative',headers:combined.messages[0].payload.headers,body:{size:0},parts:[part('text/plain','x'.repeat(40000)),part('text/html','y'.repeat(40000))]};inputs.push(combined);
 const deep=conversation();for(let i=0;i<10;i++)deep.messages[0].payload={mimeType:'multipart/mixed',headers:deep.messages[0].payload.headers,parts:[deep.messages[0].payload],body:{size:0}};inputs.push(deep);
 const manyParts=conversation();manyParts.messages[0].payload={mimeType:'multipart/mixed',headers:manyParts.messages[0].payload.headers,body:{size:0},parts:Array.from({length:65},()=>part('text/plain','x'))};inputs.push(manyParts);
 const manyMessages={id:THREAD,messages:Array.from({length:101},(_,i)=>message((1000+i).toString(16)))};inputs.push(manyMessages);
 for(const data of inputs){const thread=normalizedThread(data);assert.equal(thread.complete,false);assert.equal(thread.truncated,true);assert.ok(thread.coverage.reasons.length);assert.ok(thread.messages.length<=100);}
 const huge=conversation();huge.messages[0].payload.headers.push({name:'X-Huge',value:'x'.repeat(1000001)});
 assert.throws(()=>normalizedThread(huge),/context_response_too_large/);
});
test('UTF8 text at 64KiB is preserved exactly and the global 512 part bound is visible',()=>{
 const text='\u00e1'.repeat(32768),data={id:THREAD,messages:[message(ID,text)]},thread=normalizedThread(data);
 assert.equal(thread.complete,true);assert.equal(thread.messages[0].bodies.text,text);assert.equal(thread.messages[0].coverage.text_bytes,65536);
 const many={id:THREAD,messages:Array.from({length:9},(_,i)=>{
  const m=message((1000+i).toString(16));m.payload={mimeType:'multipart/mixed',headers:mimeHeaders(m.payload.headers,'multipart/mixed'),body:{size:0},parts:Array.from({length:63},()=>part('text/plain','x'))};return m;
 })};
 const limited=normalizedThread(many);assert.equal(limited.complete,false);assert.equal(limited.truncated,true);assert.equal(limited.coverage.parts_returned,512);assert.ok(limited.coverage.reasons.includes('part_limit'));
});
test('provider truncated flags and unhandled multipart container data stay partial',()=>{
 const data=conversation();data.truncated=true;let thread=normalizedThread(data);
 assert.equal(thread.complete,false);assert.equal(thread.truncated,true);assert.ok(thread.coverage.reasons.includes('provider_partial'));
 const m=message();m.payload={mimeType:'multipart/mixed',headers:mimeHeaders(m.payload.headers,'multipart/mixed'),body:{size:5,data:Buffer.from('extra').toString('base64url')},parts:[m.payload]};
 thread=normalizedThread({id:THREAD,messages:[m]});assert.equal(thread.complete,false);assert.ok(thread.coverage.reasons.includes('container_body_unread'));
});
test('thread identity mismatch duplicate message IDs and oversized serialized output fail closed',async()=>{
 for(const modify of [data=>{data.id='bad123';},data=>{data.messages[0].threadId='bad123';},data=>{data.messages[1].id=data.messages[0].id;}]){
  const f=fixture(),data=conversation();modify(data);f.setData(data);await assert.rejects(call(f.bridge,'/v1/threads/'+THREAD));
 }
 const data={id:THREAD,messages:Array.from({length:9},(_,i)=>message((1000+i).toString(16),'x'.repeat(60000)))};
 const f=fixture();f.setData(data);await assert.rejects(call(f.bridge,'/v1/threads/'+THREAD),/context_response_too_large/);
});
test('context routes enforce authentication pinned profile exact hex IDs and no GET body before content access',async()=>{
 const f=fixture();for(const url of ['/v1/threads/ABC123','/v1/threads/%64ef456','/v1/threads/'+THREAD+'?format=full','/v1/threads/../status','/v1/threads/abc/send'])await assert.rejects(call(f.bridge,url));
 assert.deepEqual(f.calls,[]);
 for(const url of ['/v1/messages?limit=1','/v1/threads/'+THREAD]){
  await assert.rejects(f.bridge.handle({method:'GET',url,authorization:'Bearer wrong'}),/unauthorized/);
  await assert.rejects(f.bridge.handle({method:'GET',url,authorization:'Bearer '+KEY,body:{}}),/read_only_body/);
 }
 assert.deepEqual(f.calls,[]);f.setProfile('other@example.invalid');
 for(const url of ['/v1/messages?limit=1','/v1/threads/'+THREAD])await assert.rejects(call(f.bridge,url),/provider_account_mismatch/);
 assert.ok(f.calls.every(row=>['session','profile'].includes(row[0])));
});
test('HTML multipart context never bypasses the strict exact message receipt reader',async()=>{
 for(const mimeType of ['text/html','multipart/alternative','multipart/mixed']){
  const f=fixture(),data=conversation(),plain=data.messages[0].payload;
  data.messages[0].payload=mimeType==='text/html'?{...part('text/html','<p>extra content</p>'),headers:mimeHeaders(plain.headers,mimeType)}:{mimeType,headers:mimeHeaders(plain.headers,mimeType),body:{size:0},parts:[plain,part('text/html','<p>extra content</p>')]};
  f.setData(data);assert.equal((await call(f.bridge,'/v1/threads/'+THREAD)).thread.context_only,true);
  if(mimeType==='multipart/alternative'){
   const observed=(await call(f.bridge,'/v1/messages/'+ID)).message;
   assert.equal(observed.context_only,true);assert.equal(observed.receipt_eligible,false);
   assert.equal(observed.mime_evidence.alternatives[1].body,'<p>extra content</p>');
  }else await assert.rejects(call(f.bridge,'/v1/messages/'+ID),/read_body_unsupported/);
 }
});
test('new context routes reject write verbs before provider and native wire blocks all mutations',async()=>{
 const f=fixture();for(const method of ['POST','PUT','PATCH','DELETE','HEAD','OPTIONS'])for(const url of ['/v1/messages?limit=1','/v1/threads/'+THREAD,'/v1/send','/v1/reconcile'])
  await assert.rejects(f.bridge.handle({method,url,authorization:'Bearer '+KEY}),/read_only_method/);
 assert.deepEqual(f.calls,[]);
 let calls=0;const transporter={request(){calls++;}};guardReadWire({context:{_options:{auth:{transporter}}}});
 for(const method of ['POST','PUT','PATCH','DELETE'])for(const path of ['messages','threads/'+THREAD,'messages/'+ID,'messages/'+ID+'/modify','messages/'+ID+'/attachments/abc'])
  assert.throws(()=>transporter.request({url:'https://gmail.googleapis.com/gmail/v1/users/me/'+path,method,params:{format:'full',fields:THREAD_FIELDS}}),/read_wire_blocked/);
 assert.equal(calls,0);
});
test('wire permits fixed list thread queries only with bounded values and applies HTTP bounds',async()=>{
 const seen=[],transporter={async request(options){seen.push(options);return {data:{}};}};guardReadWire({context:{_options:{auth:{transporter}}}});
 const root='https://gmail.googleapis.com/gmail/v1/users/me/';
 await transporter.request({url:root+'messages',method:'GET',params:{fields:LIST_FIELDS,maxResults:10,pageToken:'opaque+page/=2',q:LIST_QUERY}});
 await transporter.request({url:root+'threads/'+THREAD,method:'GET',params:{fields:THREAD_FIELDS,format:'full'}});
 assert.equal(seen.length,2);for(const options of seen){assert.equal(options.timeout,3000);assert.equal(options.retry,false);assert.equal(options.retryConfig.retry,0);assert.equal(options.maxContentLength,1000000);assert.equal(options.size,1000000);assert.equal(options.maxRedirects,0);}
 for(const params of [{fields:LIST_FIELDS},{fields:LIST_FIELDS,maxResults:11},{fields:LIST_FIELDS,maxResults:1,q:'in:sent'},
  {fields:LIST_FIELDS,maxResults:1,pageToken:''},{fields:LIST_FIELDS,maxResults:1,pageToken:'x'.repeat(1025)},
  {fields:'*',maxResults:1},{fields:LIST_FIELDS,maxResults:1,format:'full'}])assert.throws(()=>transporter.request({url:root+'messages',method:'GET',params:{q:LIST_QUERY,...params}}),/read_wire_blocked/);
 for(const q of [undefined,'','in:inbox','newer_than:7d','in:inbox newer_than:30d',LIST_QUERY+' OR in:sent'])
  assert.throws(()=>transporter.request({url:root+'messages',method:'GET',params:{fields:LIST_FIELDS,maxResults:1,...(q===undefined?{}:{q})}}),/read_wire_blocked/);
 for(const params of [{fields:THREAD_FIELDS,format:'metadata'},{fields:'*',format:'full'},{fields:THREAD_FIELDS,format:'full',metadataHeaders:'To'}])
  assert.throws(()=>transporter.request({url:root+'threads/'+THREAD,method:'GET',params}),/read_wire_blocked/);
 assert.throws(()=>transporter.request({url:root+'messages?maxResults=1',method:'GET',params:{fields:LIST_FIELDS,maxResults:1,q:LIST_QUERY}}),/read_wire_blocked/);
 assert.throws(()=>transporter.request({url:root+'messages?q='+encodeURIComponent(LIST_QUERY),method:'GET',params:{fields:LIST_FIELDS,maxResults:1,q:LIST_QUERY}}),/read_wire_blocked/);
 assert.throws(()=>transporter.request({url:'https://drive.googleapis.com/drive/v3/files',method:'GET'}),/read_wire_blocked/);
 assert.equal(seen.length,2);
});
test('list and thread provider failures are surfaced without retries',async()=>{
 for(const port of ['list','readThread']){
  let calls=0;const provider={async verifyProfile(){return EXPECTED.email;},async [port](){calls++;throw Error('synthetic timeout');}};
  const bridge=createBridge({credential:KEY,readOnly:true,provider});
  await assert.rejects(call(bridge,port==='list'?'/v1/messages?limit=1':'/v1/threads/'+THREAD),/synthetic timeout/);assert.equal(calls,1);
 }
});
test('existing HTTP handler exposes context safely without listening and sanitizes read failures',async()=>{
 const {serverFor}=require('./server.cjs'),{Readable}=require('node:stream'),f=fixture(),server=serverFor(f.bridge);
 const request=(method,url,authorization='Bearer '+KEY)=>new Promise(resolve=>{
  const req=Readable.from([]);Object.assign(req,{method,url,headers:{authorization}});
  const res={setHeader(){},writeHead(code){this.code=code;},end(raw){resolve({code:this.code,body:JSON.parse(raw)});}};
  server.emit('request',req,res);
 });
 try{
  const result=await request('GET','/v1/threads/'+THREAD);assert.equal(result.code,200);assert.equal(result.body.thread.id,THREAD);assert.equal(result.body.thread.context_only,true);
  assert.equal((await request('GET','/v1/messages?limit=1')).code,200);
  assert.equal((await request('GET','/v1/threads/'+THREAD,'Bearer wrong')).code,401);
  assert.equal((await request('POST','/v1/threads/'+THREAD)).code,405);
  f.provider.session=async()=>{throw Error('synthetic-private-detail');};
  const failed=await request('GET','/v1/threads/'+THREAD);assert.equal(failed.code,503);assert.equal(failed.body.error_code,'bridge_unavailable');assert.equal(JSON.stringify(failed).includes('synthetic-private-detail'),false);
 }finally{server.close();}
});


test('contact search exact query and signed cursor bind alias mailbox expiry and reject tampering',async()=>{
 const {contactQuery}=require('./native.cjs');
 const key='s'.repeat(64),email='person+tag@example.invalid',calls=[];let tick=1000;
 const provider={async verifyProfile(){return EXPECTED.email;},async contactSearch(value){calls.push(value);return {messages:[{id:'aaa',thread_id:'bbb'}],next_cursor:value.cursor===null?'opaque-token':null};}};
 const bridge=createBridge({provider,credential:key,readOnly:true,now:()=>tick});
 const call=url=>bridge.handle({method:'GET',url,authorization:'Bearer '+key});
 const url='/v1/contact-search?email='+encodeURIComponent(email),first=await call(url);
 assert.equal(first.query,contactQuery(email));assert.equal(first.receipt_eligible,false);
 assert.equal(first.coverage.include_spam_trash,true);assert.equal(first.coverage.window_days,null);
 assert.equal(first.coverage.listing_complete,false);
 const second=await call(url+'&cursor='+encodeURIComponent(first.next_cursor));
 assert.equal(second.coverage.listing_complete,true);assert.equal(calls[1].cursor,'opaque-token');
 const count=calls.length;
 for(const value of [first.next_cursor.slice(0,-1)+'z','opaque-token'])await assert.rejects(call(url+'&cursor='+encodeURIComponent(value)),/invalid_search_cursor/);
 await assert.rejects(call('/v1/contact-search?email=other@example.invalid&cursor='+encodeURIComponent(first.next_cursor)),/invalid_search_cursor/);
 tick+=300001;await assert.rejects(call(url+'&cursor='+encodeURIComponent(first.next_cursor)),/invalid_search_cursor/);
 assert.equal(calls.length,count);assert.equal(bridge.journal,null);
});

test('contact search rejects query operators duplicated parameters and all mutation verbs',async()=>{
 const key='s'.repeat(64);let calls=0;
 const provider={async verifyProfile(){calls++;return EXPECTED.email;},async contactSearch(){calls++;throw Error('not reached');}};
 const bridge=createBridge({provider,credential:key,readOnly:true});
 for(const email of ['x@example.invalid OR from:anyone','x} to:y@example.invalid','x"@example.invalid','x\n@example.invalid'])
  await assert.rejects(bridge.handle({method:'GET',url:'/v1/contact-search?email='+encodeURIComponent(email),authorization:'Bearer '+key}),/invalid_contact_email/);
 for(const suffix of ['&email=other@example.invalid','&q=in:anywhere','&limit=100'])
  await assert.rejects(bridge.handle({method:'GET',url:'/v1/contact-search?email=x@example.invalid'+suffix,authorization:'Bearer '+key}),/invalid_query/);
 for(const method of ['POST','PUT','PATCH','DELETE'])
  await assert.rejects(bridge.handle({method,url:'/v1/contact-search?email=x@example.invalid',authorization:'Bearer '+key}),/read_only_method/);
 assert.equal(calls,0);
});

test('native contact search includes spam trash through bounded existing GET transport and preserves429',async()=>{
 const {contactQuery}=require('./native.cjs');const wires=[];let fail=false;
 const transporter={async request(options){wires.push(options);if(fail)throw {response:{status:429}};return {data:{messages:[{id:'aaa',threadId:'bbb'}],nextPageToken:'next'}};}};
 const gmail={context:{_options:{auth:{transporter}}},users:{messages:{async list(params,options){return transporter.request({...options,url:'https://gmail.googleapis.com/gmail/v1/users/me/messages',method:'GET',params:{fields:params.fields,q:params.q,maxResults:params.maxResults,includeSpamTrash:params.includeSpamTrash,...(params.pageToken?{pageToken:params.pageToken}:{})}});}}}};
 const session=nativeSession(gmail,null,{readOnly:true});
 const result=await session.contactSearch({email:'x@example.invalid',cursor:null});
 assert.deepEqual(result,{messages:[{id:'aaa',thread_id:'bbb'}],next_cursor:'next'});
 assert.equal(wires[0].params.q,contactQuery('x@example.invalid'));assert.equal(wires[0].params.includeSpamTrash,true);assert.equal(wires[0].params.maxResults,10);
 assert.equal(wires[0].retry,false);assert.equal(wires[0].maxRedirects,0);
 fail=true;await assert.rejects(session.contactSearch({email:'x@example.invalid',cursor:'next'}),error=>error.status===429&&error.code==='provider_rate_limited');
 assert.equal(wires.length,2);
});

test('wire only accepts canonical equal-participant query with explicit spam trash and no mutations',async()=>{
 const {contactQuery}=require('./native.cjs');let calls=0;const transporter={async request(){calls++;return {};}};
 guardReadWire({context:{_options:{auth:{transporter}}}});
 const params={fields:'messages(id,threadId),nextPageToken,resultSizeEstimate',q:contactQuery('x@example.invalid'),maxResults:10,includeSpamTrash:true};
 const url='https://gmail.googleapis.com/gmail/v1/users/me/messages';
 await transporter.request({url,method:'GET',params});
 for(const extra of [{fields:LIST_FIELDS},{q:'in:anywhere'},{q:params.q.replace('to:x@','to:y@')},{includeSpamTrash:false},{maxResults:11}])
  assert.throws(()=>transporter.request({url,method:'GET',params:{...params,...extra}}),/read_wire_blocked/);
 assert.throws(()=>transporter.request({url,method:'POST',params}),/read_wire_blocked/);
 assert.equal(calls,1);
});


test('largest escaped provider cursor roundtrips at max email length without weakening signature',async()=>{
 const key='s'.repeat(64),email='x'.repeat(64)+'@'+'a'.repeat(63)+'.'+'b'.repeat(63)+'.'+'c'.repeat(61);
 assert.equal(email.length,254);
 for(const token of ['\\'.repeat(1024),'"'.repeat(1024)]){
  const calls=[];
  const provider={async verifyProfile(){return EXPECTED.email;},async contactSearch(value){calls.push(value);return {messages:[{id:'aaa',thread_id:'bbb'}],next_cursor:value.cursor===null?token:null};}};
  const bridge=createBridge({provider,credential:key,readOnly:true,now:()=>1792929600000});
  const url='/v1/contact-search?email='+encodeURIComponent(email);
  const request=url=>bridge.handle({method:'GET',url,authorization:'Bearer '+key});
  const first=await request(url);
  assert.ok(first.next_cursor.length>1900);assert.ok(first.next_cursor.split('.')[0].length<=4096);
  const second=await request(url+'&cursor='+encodeURIComponent(first.next_cursor));
  assert.equal(second.next_cursor,null);assert.equal(second.coverage.listing_complete,true);assert.equal(calls[1].cursor,token);
  const altered=first.next_cursor.slice(0,-1)+(first.next_cursor.endsWith('a')?'b':'a');
  await assert.rejects(request(url+'&cursor='+encodeURIComponent(altered)),/invalid_search_cursor/);
  assert.equal(calls.length,2);
 }
});


test('contact search requests zero-result metadata instead of accepting a blank masked response',async()=>{
 const wires=[];let forceBlank=false;
 const transporter={async request(options){wires.push(options);return {data:forceBlank||!options.params.fields.includes('resultSizeEstimate')?'':{resultSizeEstimate:0}};}};
 const gmail={context:{_options:{auth:{transporter}}},users:{messages:{async list(params,options){const {userId,...query}=params;return transporter.request({...options,url:'https://gmail.googleapis.com/gmail/v1/users/me/messages',method:'GET',params:query});}}}};
 const session=nativeSession(gmail,null,{readOnly:true});
 assert.deepEqual(await session.contactSearch({email:'x@example.invalid',cursor:null}),{messages:[],next_cursor:null});
 assert.equal(wires[0].params.fields,'messages(id,threadId),nextPageToken,resultSizeEstimate');
 forceBlank=true;await assert.rejects(session.contactSearch({email:'x@example.invalid',cursor:null}),/invalid_provider_response/);
 assert.equal(wires.length,2);
});

test('contact search empty result requires explicit zero object and legacy inbox mask remains fixed',async()=>{
 const f=fixture();
 for(const value of ['',{},[],null,{resultSizeEstimate:1},{resultSizeEstimate:'0'}]){
  f.setPage(value);await assert.rejects(call(f.bridge,'/v1/contact-search?email=x@example.invalid'),/invalid_provider_response/);
 }
 f.setPage({resultSizeEstimate:0});const result=await call(f.bridge,'/v1/contact-search?email=x@example.invalid');
 assert.deepEqual(result.messages,[]);assert.equal(result.coverage.listing_complete,true);
 f.setPage({});await call(f.bridge,'/v1/messages?limit=1');
 assert.equal(f.calls.filter(row=>row[0]==='list').at(-1)[1].fields,LIST_FIELDS);
});
