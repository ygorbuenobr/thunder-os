'use strict';
const fs=require('node:fs'),crypto=require('node:crypto');
const {EXPECTED,BridgeError,check,identifier,hash,business}=require('./contract.cjs');
const ROOT='/app/.next/server/';
const PINS=Object.freeze({
 'app/api/internal/thunder/send-approved/route.js':'ca9ec913ab25793eda54a5de9ad22de15623f12c0df4872785d096b22e56cf3a',
 'chunks/3317.js':'d63f208341bdb2cbce20edfbfea9c2ef9dca32977ded98bd9af5818c949e1267',
 'webpack-runtime.js':'20fb3acf64b82b17345c161ff7e824fdbef7acf91c900c53ea0615de9a56a141',
 'chunks/4741.js':'faa1e32fc82a3f26d3fc3a83836c8f3aa971c8be8170fc5a83487ca838c3df51',
 'chunks/1813.js':'2bbb98872d041527b0e4703555c32c0e7bf8bbaa52822e4990ebc69df3118775',
 'chunks/7881.js':'7b0f38ebae5954245eb0e193bc8e0dd06b9035da301cf02bfb4c938b911c0816',
 'chunks/9994.js':'0809e8485adb9834e958114714fc5dfc22a77024f80080d69f7decaceaa559f8',
 'chunks/7582.js':'0200896d56f88518e088c7183c26e5c9c0da9170725de6247bf969e9cdb98d12',
 'app/api/cron/poll-replies/route.js':'6066f9168bc3edc52fd15d5483fff976889199ca9d0f70ef844373b204f68f33'
});
function header(payload,name){return (payload?.headers??[]).filter(h=>h.name?.toLowerCase()===name.toLowerCase()).map(h=>h.value).join(', ');}
function decodeSubject(value){return value.replace(/=\?utf-8\?B\?([A-Za-z0-9+/=]+)\?=/gi,(_,data)=>Buffer.from(data,'base64').toString('utf8'));}
function textPart(payload){
 let parts=[],truncated=false,bytes=0,nodes=0,found=false;
 function walk(p,depth){
  if(!p)return;
  if(++nodes>64||depth>4){truncated=true;return;}
  if(p.mimeType==='text/plain'){
   if(typeof p.body?.data!=='string'){truncated=true;return;}
   found=true;
   if(p.body.data.length>90000){truncated=true;return;}
   const decoded=Buffer.from(p.body.data,'base64url');
   if(bytes+decoded.length>65536){truncated=true;parts.push(decoded.subarray(0,65536-bytes));bytes=65536;return;}
   bytes+=decoded.length;parts.push(decoded);
  }else if(p.mimeType?.startsWith('multipart/')){
   if(!Array.isArray(p.parts)||!p.parts.length)truncated=true;
  }else if(p.mimeType!=='text/html'){truncated=true;}
  for(const child of p.parts??[]){if(nodes>=64){truncated=true;break;}walk(child,depth+1);}
 }
 walk(payload,0);let body=Buffer.concat(parts).toString('utf8');while(Buffer.byteLength(body)>65536)body=body.slice(0,-1);return {body,truncated:truncated||!found};
}
function normalized(message){const payload=message.payload;return {id:message.id,thread_id:message.threadId,from:header(payload,'From').slice(0,1024),to:header(payload,'To').slice(0,1024),subject:decodeSubject(header(payload,'Subject')).slice(0,2048),date:header(payload,'Date').slice(0,128),message_id:header(payload,'Message-ID'),sent:(message.labelIds??[]).includes('SENT'),...textPart(payload)};}
function readbackHeaders(payload){
 const headers=payload?.headers;
 check(Array.isArray(headers)&&headers.length<=500,'read_headers_incomplete',503);
 const critical=new Set(['from','to','subject','cc','bcc','date','message-id','content-type','content-disposition','content-transfer-encoding']),values={};let bytes=0;
 for(const item of headers){
  check(item&&typeof item.name==='string'&&/^[A-Za-z0-9-]{1,100}$/.test(item.name)&&typeof item.value==='string','read_headers_invalid',503);
  bytes+=Buffer.byteLength(item.name)+Buffer.byteLength(item.value);
  check(bytes<=65536&&Buffer.byteLength(item.value)<=8192&&!/[\x00-\x1f\x7f]/.test(item.value)&&Buffer.from(item.value).toString('utf8')===item.value,'read_headers_incomplete',503);
  const name=item.name.toLowerCase();
  if(critical.has(name)){check(!Object.hasOwn(values,name),'read_header_duplicate',503);values[name]=item.value;}
 }
 for(const name of ['from','to','subject'])check(typeof values[name]==='string'&&values[name].trim().length>0,'read_headers_incomplete',503);
 const subject=decodeSubject(values.subject);
 check(!subject.includes('=?')&&Buffer.from(subject).toString('utf8')===subject&&!/[\x00-\x1f\x7f\ufffd]/.test(subject),'read_subject_encoding',503);
 for(const value of [values.from,values.to,subject,values.cc??'',values.bcc??''])check(Buffer.byteLength(value)<=2048,'read_headers_incomplete',503);
 return {from:values.from,to:values.to,subject,cc:values.cc??'',bcc:values.bcc??'',date:values.date??'',message_id:values['message-id']??'',headers:headers.map(item=>({name:item.name,value:item.value})),headers_complete:true};
}
function readbackText(payload){
 check(payload&&payload.mimeType==='text/plain'&&(!Object.hasOwn(payload,'parts')||(Array.isArray(payload.parts)&&payload.parts.length===0)),'read_body_unsupported',503);
 check(!payload.filename&&!payload.body?.attachmentId,'read_body_incomplete',503);
 const headers=payload.headers;check(Array.isArray(headers),'read_body_incomplete',503);
 let ascii=false;
 for(const item of headers){
   check(item&&typeof item.name==='string'&&typeof item.value==='string','read_body_incomplete',503);
   if(item.name.toLowerCase()==='content-disposition')check(!/^attachment\b/i.test(item.value)&&!/(?:^|;)\s*filename\s*=/i.test(item.value),'read_body_incomplete',503);
   if(item.name.toLowerCase()==='content-type'){
    check(/^text\/plain(?:\s*;|$)/i.test(item.value)&&[...item.value.matchAll(/(?:^|;)\s*charset\s*=/gi)].length<=1,'read_body_charset',503);
    const charset=/charset\s*=\s*["']?([^;\s"']+)/i.exec(item.value)?.[1];
    check(!charset||/^(utf-8|us-ascii)$/i.test(charset),'read_body_charset',503);ascii=charset?.toLowerCase()==='us-ascii';
   }
 }
 const data=payload.body?.data;
 check(typeof data==='string'&&data.length<=90000&&/^[A-Za-z0-9_-]*={0,2}$/.test(data)&&data.length%4!==1,'read_body_incomplete',503);
 const decoded=Buffer.from(data,'base64url');
 check(decoded.toString('base64url')===data.replace(/=+$/,'')&&Number.isSafeInteger(payload.body.size)&&payload.body.size===decoded.length&&decoded.length<=65536,'read_body_incomplete',503);
 check(!ascii||decoded.every(byte=>byte<128),'read_body_charset',503);
 let body;try{body=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(decoded);}catch{throw new BridgeError('read_body_encoding',503);}
 check(body.trim().length>0&&!body.includes('\ufffd'),'read_body_incomplete',503);
 return {body,truncated:false};
}
function normalizedReadOnly(message){
 check(message&&identifier(message.id)&&identifier(message.threadId)&&message.truncated!==true,'read_message_invalid',503);
 check(Array.isArray(message.labelIds)&&message.labelIds.every(value=>typeof value==='string')&&message.labelIds.length<=500,'read_labels_incomplete',503);
 return {id:message.id,thread_id:message.threadId,...readbackHeaders(message.payload),
  labelIds:[...message.labelIds],sent:message.labelIds.includes('SENT'),...readbackText(message.payload)};
}
const CONTEXT_LIMITS=Object.freeze({http_bytes:1000000,text_bytes:65536,messages:100,parts_per_message:64,parts_per_thread:512,depth:8});
const LIST_FIELDS='messages(id,threadId),nextPageToken';
const READONLY_LIST_QUERY='in:inbox newer_than:7d';
// Exact canonical address grammar excludes every Gmail query operator delimiter.
const CONTACT_EMAIL=/^[a-z0-9][a-z0-9._+-]{0,63}@[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+$/;
function contactQuery(email){check(typeof email==='string'&&email.length<=254&&CONTACT_EMAIL.test(email),'invalid_contact_email');return `in:anywhere {from:${email} to:${email} cc:${email} bcc:${email}} -in:drafts`;}
function isContactQuery(query){if(typeof query!=='string')return false;const match=/^in:anywhere \{from:([^ ]+) to:([^ ]+) cc:([^ ]+) bcc:([^ ]+)\} -in:drafts$/.exec(query);return !!match&&CONTACT_EMAIL.test(match[1])&&match[1].length<=254&&match.slice(2).every(value=>value===match[1]);}
const THREAD_FIELDS='id,messages(id,threadId,labelIds,internalDate,payload)';
const hexId=value=>typeof value==='string'&&/^[a-f0-9]{1,64}$/.test(value);
const opaqueCursor=value=>typeof value==='string'&&/^[\x21-\x7e]{1,1024}$/.test(value);
function contextSize(value){const serialized=JSON.stringify(value);check(typeof serialized==='string'&&Buffer.byteLength(serialized)<=CONTEXT_LIMITS.http_bytes,'context_response_too_large',503);}
function normalizedThread(thread){
 contextSize(thread);
 check(thread&&hexId(thread.id)&&Array.isArray(thread.messages),'context_thread_invalid',503);
 const reasons=new Set(),participants=new Set(),ids=new Set();let totalParts=0,truncated=false;
 const mark=(set,reason,cut=false)=>{set.add(reason);reasons.add(reason);if(cut)truncated=true;};
 if(thread.truncated===true)mark(reasons,'provider_partial',true);
 if(!thread.messages.length)reasons.add('messages_missing');
 if(thread.messages.length>CONTEXT_LIMITS.messages)mark(reasons,'message_limit',true);
 const messages=thread.messages.slice(0,CONTEXT_LIMITS.messages).map(message=>{
  check(message&&hexId(message.id)&&message.threadId===thread.id&&!ids.has(message.id),'context_message_identity',503);ids.add(message.id);
  const missing=new Set();let nodes=0,textBytes=0;
  if(message.truncated===true)mark(missing,'provider_partial',true);
  const field=(value,max,code)=>{
   if(typeof value!=='string'||Buffer.byteLength(value)>max||Buffer.from(value).toString('utf8')!==value||value.includes('\0')){mark(missing,code);return null;}
   return value;
  };
  function headersFor(payload,root){
   if(!Array.isArray(payload?.headers)||payload.headers.length>500){mark(missing,'headers_missing');return [];}
   const headers=[];let bytes=0;
   for(const item of payload.headers){
    if(!item||typeof item.name!=='string'||!/^[A-Za-z0-9-]{1,100}$/.test(item.name)||typeof item.value!=='string'||/[\x00-\x1f\x7f]/.test(item.value)||Buffer.from(item.value).toString('utf8')!==item.value||Buffer.byteLength(item.value)>8192){mark(missing,'headers_invalid');continue;}
    bytes+=Buffer.byteLength(item.name)+Buffer.byteLength(item.value);
    if(bytes>65536){mark(missing,'header_limit',true);break;}
    headers.push({name:item.name,value:item.value});
   }
   if(root){
    for(const key of ['from','to','subject','date','message-id']){
     const matches=headers.filter(item=>item.name.toLowerCase()===key);
     if(matches.length!==1||!matches[0].value.trim())mark(missing,'headers_incomplete');
    }
    for(const key of ['cc','bcc','content-type','content-disposition'])if(headers.filter(item=>item.name.toLowerCase()===key).length>1)mark(missing,'headers_ambiguous');
   }
   return headers;
  }
  const headers=headersFor(message.payload,true),values={};
  for(const key of ['from','to','subject','cc','bcc','date','message-id']){
   const matches=headers.filter(item=>item.name.toLowerCase()===key);values[key]=matches.length?matches.map(item=>item.value).join(', '):null;
  }
  for(const key of ['from','to','cc','bcc'])for(const item of headers.filter(item=>item.name.toLowerCase()===key)){
   // Observed addresses aid navigation only; these are not verified identities.
   const addresses=item.value.toLowerCase().match(/[a-z0-9.!#$%&'*+\-/=?^_`{|}~]+@[a-z0-9.-]+\.[a-z]{2,63}/g)??[];
   for(const address of addresses)participants.add(address);
  }
  let labelIds=null;
  if(Array.isArray(message.labelIds)&&message.labelIds.length<=500&&message.labelIds.every(value=>typeof value==='string'&&Buffer.byteLength(value)<=1024))labelIds=[...message.labelIds];
  else mark(missing,'labels_missing');
  const internalDate=typeof message.internalDate==='string'&&/^\d{1,20}$/.test(message.internalDate)?message.internalDate:null;
  if(internalDate===null)mark(missing,'internal_date_missing');
  const texts=[],htmls=[],attachments=[];
  function walk(part,depth,root=false){
   if(nodes>=CONTEXT_LIMITS.parts_per_message||totalParts>=CONTEXT_LIMITS.parts_per_thread||depth>CONTEXT_LIMITS.depth){mark(missing,'part_limit',true);return null;}
   nodes++;totalParts++;
   if(!part||typeof part!=='object'||Array.isArray(part)){mark(missing,'part_missing');return null;}
   const mime=field(part.mimeType,200,'mime_missing'),partHeaders=root?headers:headersFor(part,false);
   const filename=part.filename===undefined?'':field(part.filename,2048,'attachment_metadata_missing');
   const partId=part.partId===undefined?null:field(part.partId,200,'part_id_invalid');
   const body=part.body&&typeof part.body==='object'&&!Array.isArray(part.body)?part.body:{};
   const size=Number.isSafeInteger(body.size)&&body.size>=0?body.size:null;
   const attachmentId=body.attachmentId===undefined?null:field(body.attachmentId,2048,'attachment_metadata_missing');
   const visible={part_id:partId,mime_type:mime,filename,headers:partHeaders,size,attachment_id:attachmentId,text:null,html:null,parts:[]};
   const contentTypes=partHeaders.filter(item=>item.name.toLowerCase()==='content-type');
   if(contentTypes.length>1||contentTypes.some(item=>item.value.split(';',1)[0].trim().toLowerCase()!==mime))mark(missing,'mime_header_mismatch');
   const disposition=partHeaders.filter(item=>item.name.toLowerCase()==='content-disposition').map(item=>item.value).join(';');
   const attachment=Boolean(filename||attachmentId||/^attachment\b/i.test(disposition));
   if(attachment){attachments.push({part_id:partId,mime_type:mime,filename,size,attachment_id:attachmentId,content_fetched:false});mark(missing,'attachment_body_not_fetched');}
   if(mime?.startsWith('multipart/')){
    if(!Array.isArray(part.parts)||!part.parts.length)mark(missing,'parts_missing');
    if((body.data!==undefined&&body.data!=='')||(size!==null&&size!==0))mark(missing,'container_body_unread');
   }else if(mime==='text/plain'||mime==='text/html'){
    if(!attachment){
     const data=body.data;
     const charsets=contentTypes.flatMap(item=>[...item.value.matchAll(/(?:^|;)\s*charset\s*=\s*["']?([^;\s"']+)/gi)].map(match=>match[1].toLowerCase()));
     if(charsets.length>1||charsets.some(value=>!['utf-8','us-ascii'].includes(value)))mark(missing,'text_charset_unsupported');
     else if(typeof data!=='string'||data.length>90000||!/^[A-Za-z0-9_-]*={0,2}$/.test(data)||data.length%4===1)mark(missing,'text_data_missing',typeof data==='string'&&data.length>90000);
     else{
      const decoded=Buffer.from(data,'base64url');
      if(decoded.toString('base64url')!==data.replace(/=+$/,'')||size===null||size!==decoded.length)mark(missing,'text_size_mismatch');
      else if(textBytes+decoded.length+((mime==='text/plain'?texts:htmls).length?1:0)>CONTEXT_LIMITS.text_bytes)mark(missing,'text_limit',true);
      else{
       let text=null;
       try{if(charsets.includes('us-ascii')&&decoded.some(byte=>byte>127))throw Error('ascii');text=new TextDecoder('utf-8',{fatal:true,ignoreBOM:true}).decode(decoded);}catch{mark(missing,'text_encoding_invalid');}
       if(text!==null){const bucket=mime==='text/plain'?texts:htmls;textBytes+=decoded.length+(bucket.length?1:0);visible[mime==='text/plain'?'text':'html']=text;bucket.push(text);}
      }
     }
    }
   }else if(!attachment)mark(missing,'mime_body_unsupported');
   if(part.parts!==undefined){
    if(!Array.isArray(part.parts))mark(missing,'parts_missing');
    else{
     if(!mime?.startsWith('multipart/')&&part.parts.length)mark(missing,'mime_structure_ambiguous');
     for(const child of part.parts){
      if(nodes>=CONTEXT_LIMITS.parts_per_message||totalParts>=CONTEXT_LIMITS.parts_per_thread){mark(missing,'part_limit',true);break;}
      const nested=walk(child,depth+1);if(nested)visible.parts.push(nested);
     }
    }
   }
   return visible;
  }
  const parts=walk(message.payload,0,true);
  const join=items=>{if(!items.length)return null;const value=items.join('\n');if(Buffer.byteLength(value)>CONTEXT_LIMITS.text_bytes){mark(missing,'text_limit',true);return null;}return value;};
  const bodies={text:join(texts),html:join(htmls)};
  return {id:message.id,thread_id:message.threadId,context_only:true,untrusted_context:true,receipt_eligible:false,
   headers,labelIds,internal_date:internalDate,from:values.from,to:values.to,cc:values.cc,bcc:values.bcc,subject:values.subject,date:values.date,message_id:values['message-id'],
   bodies,parts,attachments,complete:missing.size===0,coverage:{reasons:[...missing],parts_returned:nodes,text_bytes:textBytes,attachment_bodies_fetched:false}};
 });
 const result={id:thread.id,context_only:true,untrusted_context:true,receipt_eligible:false,messages,participants:[...participants],complete:reasons.size===0,truncated,
  coverage:{scope:'thread',messages_total:thread.messages.length,messages_returned:messages.length,parts_returned:totalParts,attachment_bodies_fetched:false,reasons:[...reasons],limits:CONTEXT_LIMITS}};
 contextSize(result);return result;
}
function mime(mail,reference){
 const headers=[`From: ${EXPECTED.email}`,`To: ${mail.to}`,`Subject: =?UTF-8?B?${Buffer.from(mail.subject).toString('base64')}?=`,`Message-ID: ${mail.message_id}`,'MIME-Version: 1.0','Content-Type: text/plain; charset=UTF-8','Content-Transfer-Encoding: base64'];
 if(reference){check(/^<[^<>\s\r\n]{1,500}>$/.test(reference),'invalid_thread_reference');headers.push('In-Reply-To: '+reference,'References: '+reference);}
 return Buffer.from(headers.join('\r\n')+'\r\n\r\n'+Buffer.from(mail.body).toString('base64').match(/.{1,76}/g).join('\r\n')).toString('base64url');
}
function guardSendWire(gmail){
 const auth=gmail?.context?._options?.auth,transporter=auth?.transporter;
 check(transporter&&typeof transporter.request==='function','wire_guard_unavailable',503);
 const original=transporter.request;let sends=0;
 transporter.request=function(options,...rest){
  const url=new URL(String(options.url));
  if(url.hostname==='gmail.googleapis.com'&&url.pathname==='/gmail/v1/users/me/messages/send'){
   check(url.protocol==='https:'&&String(options.method).toUpperCase()==='POST'&&++sends===1,'send_retry_blocked',503);
   options={...options,maxRedirects:0,retry:false,retryConfig:{...options.retryConfig,retry:0}};
  }else{check(url.protocol==='https:'&&url.hostname==='oauth2.googleapis.com'&&url.pathname==='/token','unexpected_native_request',503);}
  return original.call(this,options,...rest);
 };
 return ()=>{transporter.request=original;};
}
function guardReadWire(gmail){
 const transporter=gmail?.context?._options?.auth?.transporter;
 check(transporter&&typeof transporter.request==='function','read_wire_guard_unavailable',503);
 const original=transporter.request;
 transporter.request=function(options,...rest){
  let url;try{url=new URL(String(options.url));}catch{throw new BridgeError('read_wire_blocked',503);}
  const method=String(options.method??'GET').toUpperCase();
  check(url.protocol==='https:'&&!url.username&&!url.password&&!url.port&&!url.hash,'read_wire_blocked',503);
  if(url.hostname==='oauth2.googleapis.com'&&url.pathname==='/token'){
   check(method==='POST'&&!url.search&&options.body===undefined,'read_wire_blocked',503);
   const data=options.data;
   check(typeof data==='string'||data instanceof URLSearchParams||(data&&Object.getPrototypeOf(data)===Object.prototype),'read_wire_blocked',503);
   const params=new URLSearchParams(data),keys=[...params.keys()];
   check(params.get('grant_type')==='refresh_token'&&params.getAll('grant_type').length===1&&
    keys.every(key=>['grant_type','client_id','client_secret','refresh_token'].includes(key))&&new Set(keys).size===keys.length&&
    typeof params.get('refresh_token')==='string'&&params.get('refresh_token').length>0,'read_wire_blocked',503);
  }else{
   check(method==='GET'&&url.hostname==='gmail.googleapis.com'&&options.body===undefined&&options.data===undefined&&
    /^\/gmail\/v1\/users\/me\/(profile|messages|(?:messages|threads)\/[a-f0-9]{1,64})$/.test(url.pathname),'read_wire_blocked',503);
   const params=new URLSearchParams(options.params??{});
   for(const [key,value]of url.searchParams)params.append(key,value);
   const collection=url.pathname.endsWith('/messages'),allowed=collection?['fields','maxResults','pageToken','q','alt','includeSpamTrash']:['fields','format','alt'];
   check([...params.keys()].every(key=>allowed.includes(key))&&
    allowed.every(key=>params.getAll(key).length<=1)&&
    (!params.has('alt')||params.get('alt')==='json'),'read_wire_blocked',503);
   if(url.pathname.endsWith('/profile'))check(params.get('fields')==='emailAddress'&&!params.has('format'),'read_wire_blocked',503);
   else if(collection)check(params.get('fields')===LIST_FIELDS&&((params.get('q')===READONLY_LIST_QUERY&&!params.has('includeSpamTrash'))||(isContactQuery(params.get('q'))&&params.get('includeSpamTrash')==='true'))&&/^(?:[1-9]|10)$/.test(params.get('maxResults')??'')&&(!params.has('pageToken')||opaqueCursor(params.get('pageToken'))),'read_wire_blocked',503);
   else if(url.pathname.includes('/threads/'))check(params.get('format')==='full'&&params.get('fields')===THREAD_FIELDS,'read_wire_blocked',503);
   else check(params.get('format')==='full'&&params.get('fields')==='id,threadId,labelIds,payload','read_wire_blocked',503);
  }
  return Promise.resolve(original.call(this,{...options,timeout:Math.min(options.timeout??3000,3000),retry:false,retryConfig:{retry:0},maxRedirects:0,redirect:'error',maxContentLength:1000000,size:1000000},...rest)).catch(error=>{
   if(error?.response?.status===429||error?.status===429)throw new BridgeError('provider_rate_limited',429);
   throw error;
  });
 };
}
function nativeSession(gmail,quota,{readOnly=false}={}){
 if(readOnly)guardReadWire(gmail);
 let quotaProof=null;
 const deadline=Date.now()+35000;
 const options=()=>{const remaining=deadline-Date.now();check(remaining>0,'operation_deadline',503);return {timeout:Math.min(15000,remaining),retry:false,maxRedirects:0};};
 async function get(id){check(identifier(id),'invalid_message_id');const result=await gmail.users.messages.get({userId:'me',id,format:'full',fields:readOnly?'id,threadId,labelIds,payload':'id,threadId,labelIds,payload(mimeType,headers(name,value),body(data,size),parts(mimeType,body(data,size),parts(mimeType,body(data,size))))'},options());return readOnly?normalizedReadOnly(result.data):normalized(result.data);}
 async function search(q,maxResults){const result=await gmail.users.messages.list({userId:'me',q,maxResults,fields:'messages(id,threadId),nextPageToken'},options());return result.data;}
 const verifyProfile=async()=>{const result=await gmail.users.getProfile({userId:'me',fields:'emailAddress'},options());return result?.data?.emailAddress?.toLowerCase();};
 if(readOnly)return {verifyProfile,get,
  async contactSearch({email,cursor}){
   const q=contactQuery(email);check(cursor==null||opaqueCursor(cursor),'invalid_cursor');
   const result=await gmail.users.messages.list({userId:'me',maxResults:10,includeSpamTrash:true,
    ...(cursor!=null?{pageToken:cursor}:{}),fields:LIST_FIELDS,q},options());
   const data=result?.data;contextSize(data);
   check(data&&typeof data==='object'&&(!Object.hasOwn(data,'messages')||Array.isArray(data.messages)),'invalid_provider_response',503);
   const messages=data.messages??[];
   check(messages.length<=10&&messages.every(row=>row&&hexId(row.id)&&hexId(row.threadId))&&
    (data.nextPageToken===undefined||opaqueCursor(data.nextPageToken)),'invalid_provider_response',503);
   return {messages:messages.map(row=>({id:row.id,thread_id:row.threadId})),next_cursor:data.nextPageToken??null};
  },
  async list({limit,cursor}){
   check(Number.isInteger(limit)&&limit>=1&&limit<=10,'invalid_limit');check(cursor===null||cursor===undefined||opaqueCursor(cursor),'invalid_cursor');
   const result=await gmail.users.messages.list({userId:'me',maxResults:limit,...(cursor!=null?{pageToken:cursor}:{}),fields:LIST_FIELDS,q:READONLY_LIST_QUERY},options());
   const data=result?.data;contextSize(data);
   check(data&&typeof data==='object'&&(!Object.hasOwn(data,'messages')||Array.isArray(data.messages)),'invalid_provider_response',503);
   const messages=data.messages??[];
   check(messages.length<=limit&&messages.every(row=>row&&hexId(row.id)&&hexId(row.threadId))&&
    (data.nextPageToken===undefined||opaqueCursor(data.nextPageToken)),'invalid_provider_response',503);
   return {messages:messages.map(row=>({id:row.id,thread_id:row.threadId})),next_cursor:data.nextPageToken??null};
  },
  async readThread(id){
   check(hexId(id),'invalid_thread_id');
   const result=await gmail.users.threads.get({userId:'me',id,format:'full',fields:THREAD_FIELDS},options());
   check(result?.data?.id===id,'provider_thread_mismatch',503);return normalizedThread(result.data);
  }
 };
 return {
  async quotaStatus(){check(quota,'quota_unavailable',503);return quota.status();},
  async reserveQuota(info){check(quota,'quota_unavailable',503);await quota.reserve(info);quotaProof={...info};},
  async finishQuota(info){check(quota,'quota_unavailable',503);return quota.finish(info);},
  verifyProfile,
  async list({limit,cursor}){const result=await gmail.users.messages.list({userId:'me',maxResults:limit,...(cursor?{pageToken:cursor}:{}),fields:'messages(id,threadId),nextPageToken'},options());return {messages:(result.data.messages??[]).map(row=>({id:row.id,thread_id:row.threadId})),next_cursor:result.data.nextPageToken??null};},
  get,
  async findSent(messageId){check(/^<thunderos\.[a-f0-9]{64}@thundermustard\.com>$/.test(messageId),'invalid_message_id');const found=await search('in:sent rfc822msgid:'+messageId,2);if(found.nextPageToken)return [{},{}];return Promise.all((found.messages??[]).map(row=>get(row.id)));},
  async findDuplicates(mail){
   // Recipient-only search is deliberately conservative: no escaping/user search grammar.
   const found=await search('in:sent to:'+mail.to+' subject:'+JSON.stringify(mail.subject),10);
   if(found.nextPageToken)return {messages:[],complete:false};
   const rows=await Promise.all((found.messages??[]).map(row=>get(row.id)));
   if(rows.some(row=>row.truncated))return {messages:[],complete:false};
   const addresses=value=>(value.toLowerCase().match(/[a-z0-9.!#$%&'*+\-/=?^_`{|}~]+@[a-z0-9.-]+\.[a-z]{2,63}/g)??[]);
   const normalizedBody=value=>value.replace(/\r\n/g,'\n').normalize('NFC');
   return {complete:true,messages:rows.filter(row=>row.sent&&addresses(row.to).includes(mail.to)&&row.subject.normalize('NFC')===mail.subject.normalize('NFC')&&normalizedBody(row.body)===normalizedBody(mail.body))};
  },
  async send(mail){
   let reference;
   if(mail.thread_id){
    const result=await gmail.users.threads.get({userId:'me',id:mail.thread_id,format:'metadata',metadataHeaders:['Message-ID','From','To','Subject'],fields:'id,messages(id,payload(headers(name,value)))'},options());
    check(result.data.id===mail.thread_id&&result.data.messages?.length,'thread_not_found');
    const messages=result.data.messages;
    const participants=messages.flatMap(row=>[header(row.payload,'From'),header(row.payload,'To')]).join(' ').toLowerCase();
    const emails=participants.match(/[a-z0-9.!#$%&'*+\-/=?^_`{|}~]+@[a-z0-9.-]+\.[a-z]{2,63}/g)??[];
    check(emails.includes(mail.to)&&emails.includes(EXPECTED.email),'thread_participant_mismatch');
    const last=messages[messages.length-1];reference=header(last.payload,'Message-ID');
    const base=value=>value.replace(/^(?:re:\s*)+/i,'').trim();
    check(base(decodeSubject(header(last.payload,'Subject')))===base(mail.subject),'thread_subject_mismatch');
   }
   check(quotaProof&&quotaProof.to===mail.to&&quotaProof.messageId===mail.message_id&&quotaProof.effect===hash(business(mail)),'quota_proof_missing',503);
   await quota.assertCurrent(quotaProof);quotaProof=null;
   const restore=guardSendWire(gmail);
   try{const result=await gmail.users.messages.send({userId:'me',requestBody:{raw:mime(mail,reference),...(mail.thread_id?{threadId:mail.thread_id}:{})}},options());return {id:result?.data?.id,thread_id:result?.data?.threadId};}finally{restore();}
  }
 };
}
function registerBuiltins(runtime,source,load=require){
 const builtins=new Set(require('node:module').builtinModules);let count=0;
 for(const match of source.matchAll(/(\d+):([a-z])=>\{\2.exports=require\("([^"\n]+)"\)\}/g)){
  if(builtins.has(match[3])||match[3].startsWith('node:')){runtime.m[match[1]]=module=>{module.exports=load(match[3]);};count++;}
 }
 return count;
}
async function createNativeProvider({readOnly=false}={}){
 for(const [file,pin] of Object.entries(PINS))check(crypto.createHash('sha256').update(fs.readFileSync(ROOT+file)).digest('hex')===pin,'runtime_identity_mismatch',503);
 const runtime=require(ROOT+'webpack-runtime.js'),source=fs.readFileSync(ROOT+'app/api/cron/poll-replies/route.js','utf8');
 registerBuiltins(runtime,source);
 await Promise.all([4741,1813,7881,9994,7582].map(id=>runtime.e(id)));const native=await runtime(61069);check(typeof native.qf==='function','native_helper_unavailable',503);
 const appRequire=require('node:module').createRequire('/app/package.json');
 return {async session(){
  const sql=appRequire('postgres')(process.env.POSTGRES_URL||process.env.DATABASE_URL,{max:1,prepare:false,connect_timeout:5,idle_timeout:5});
  try{const rows=await sql.begin('READ ONLY',tx=>tx`SELECT id,workspace_id,email,is_active,refresh_token FROM mailboxes WHERE id=${EXPECTED.mailbox} AND workspace_id=${EXPECTED.workspace} AND email=${EXPECTED.email} AND is_active=true`);
   check(rows.length===1,'mailbox_selection_failed',503);const row=rows[0];
   check(row.id===EXPECTED.mailbox&&row.workspace_id===EXPECTED.workspace&&row.email===EXPECTED.email&&row.is_active===true&&typeof row.refresh_token==='string'&&row.refresh_token,'mailbox_selection_failed',503);
   const gmail=await native.qf(row.refresh_token);row.refresh_token=undefined;
   if(readOnly)return nativeSession(gmail,null,{readOnly:true});
   return nativeSession(gmail, Object.fromEntries(['reserve','assertCurrent','finish','status'].map(method=>[method,async info=>{
    const connection=appRequire('postgres')(process.env.POSTGRES_URL||process.env.DATABASE_URL,{max:1,prepare:false,connect_timeout:5,idle_timeout:5});
    try{return await require('./quota.cjs').createQuota(connection)[method](info);}finally{await connection.end({timeout:2});}
   }])));
  }finally{await sql.end({timeout:2});}
 }};
}
module.exports={contactQuery,isContactQuery,PINS,READONLY_LIST_QUERY,createNativeProvider,nativeSession,guardSendWire,guardReadWire,mime,normalized,normalizedReadOnly,normalizedThread,readbackHeaders,registerBuiltins};
