'use strict';
const {EXPECTED,BridgeError,check,hash,business,validate,safeEqual,identifier}=require('./contract.cjs');
const {Journal}=require('./journal.cjs');
function createBridge({provider,credential,journalDir,sendEnabled=false,readOnly=false,sendAuthority=null,now=()=>Date.now()}){
 check(typeof credential==='string'&&/^[A-Za-z0-9_-]{32,4096}$/.test(credential),'credential_unavailable',503);
 check(typeof readOnly==='boolean','read_only_config_invalid',503);
 const journal=readOnly?null:new Journal(journalDir);
 if(readOnly)sendEnabled=false;
 const scoped=value=>({mailbox:EXPECTED.email,...value});
 async function receipt(info,result,active){
  check(result&&identifier(result.id)&&(!result.thread_id||identifier(result.thread_id)),'provider_receipt_missing',503);
  check(typeof active.finishQuota==='function','quota_unavailable',503);
  await active.finishQuota({effect:info.businessHash,providerId:result.id});
  return journal.sent(info.businessHash,{status:'sent',provider_id:result.id,thread_id:result.thread_id??null,message_id:info.messageId,receipt_ref:'gmail:'+result.id});
 }
 async function handle({method,url,authorization,body}){
  if(readOnly){check(method==='GET','read_only_method',405);check(body===undefined,'read_only_body');}
  check(safeEqual(authorization,'Bearer '+credential),'unauthorized',401);
  let active;
  async function profile(){active=provider.session?await provider.session():provider;const result=await active.verifyProfile();check(result===EXPECTED.email,'provider_account_mismatch',503);}
  const parsed=new URL(url,'http://bridge.invalid');check(parsed.origin==='http://bridge.invalid'&&!parsed.hash,'invalid_route');
  const route=parsed.pathname;
  if(readOnly)check(url===route+parsed.search&&(route==='/v1/status'||route==='/v1/messages'||route==='/v1/contact-search'||/^\/v1\/(messages|threads)\/[a-f0-9]{1,64}$/.test(route))&&
   (route==='/v1/messages'||route==='/v1/contact-search'||!parsed.search),'read_only_route',404);
  if(method==='GET'&&route==='/v1/status'){
   check(!parsed.search,'invalid_query');await profile();if(sendEnabled){check(typeof active.quotaStatus==='function','quota_unavailable',503);const status=await active.quotaStatus();check(status?.ready===true,'quota_unavailable',503);}
   return scoped({status:'ready',send_enabled:sendEnabled,capabilities:{exact_reply_parent:active.supportsExactReplyParent===true}});
  }
  if(readOnly&&method==='GET'&&route==='/v1/contact-search'){
   const {contactQuery}=require('./native.cjs'),crypto=require('node:crypto');
   const keys=[...parsed.searchParams.keys()];
   check(keys.every(key=>key==='email'||key==='cursor')&&new Set(keys).size===keys.length,'invalid_query');
   const email=parsed.searchParams.get('email'),query=contactQuery(email),cursor=parsed.searchParams.get('cursor');
   const sign=value=>crypto.createHmac('sha256',credential).update('contact-search-v1\n'+value).digest('hex');
   let token=null,expires=now()+300000;
   if(cursor!==null){
    // 1024 ASCII token bytes may double under JSON escaping; plus email254 and metadata.
    // 4096 base64url characters cover that envelope while retaining a fixed cap.
    check(/^[A-Za-z0-9_-]{1,4096}\.[a-f0-9]{64}$/.test(cursor),'invalid_search_cursor');
    const [encoded,signature]=cursor.split('.');check(safeEqual(signature,sign(encoded)),'invalid_search_cursor');
    let data;try{data=JSON.parse(Buffer.from(encoded,'base64url').toString('utf8'));}catch{throw new BridgeError('invalid_search_cursor');}
    check(data&&data.email===email&&data.mailbox===EXPECTED.email&&typeof data.token==='string'&&/^[\x21-\x7e]{1,1024}$/.test(data.token)&&
     Number.isSafeInteger(data.expires)&&data.expires>=now()&&data.expires<=now()+300000,'invalid_search_cursor');token=data.token;expires=data.expires;
   }
   await profile();check(typeof active.contactSearch==='function','contact_search_unavailable',404);
   const result=await active.contactSearch({email,cursor:token});
   check(result&&Array.isArray(result.messages)&&result.messages.length<=10&&result.messages.every(row=>row&&typeof row.id==='string'&&typeof row.thread_id==='string'&&/^[a-f0-9]{1,64}$/.test(row.id)&&/^[a-f0-9]{1,64}$/.test(row.thread_id)),'invalid_provider_response',503);
   let next=null;
   if(result.next_cursor!=null){
    check(typeof result.next_cursor==='string'&&/^[\x21-\x7e]{1,1024}$/.test(result.next_cursor)&&result.next_cursor!==token,'invalid_search_cursor',503);
    const encoded=Buffer.from(JSON.stringify({email,mailbox:EXPECTED.email,token:result.next_cursor,expires})).toString('base64url');next=encoded+'.'+sign(encoded);
   }
   return scoped({status:'ok',context_only:true,receipt_eligible:false,email,query,messages:result.messages,next_cursor:next,
    coverage:{scope:'mailbox_verified_alias',include_spam_trash:true,window_days:null,limit:10,listing_complete:next===null}});
  }
  if(method==='GET'&&route==='/v1/messages'){
   check([...parsed.searchParams.keys()].every(k=>k==='cursor'||k==='limit')&&new Set(parsed.searchParams.keys()).size===[...parsed.searchParams.keys()].length,'invalid_query');
   const limit=Number(parsed.searchParams.get('limit')??10),cursor=parsed.searchParams.get('cursor');
   if(readOnly)check(!parsed.searchParams.has('limit')||/^(?:[1-9]|10)$/.test(parsed.searchParams.get('limit')),'invalid_limit');
   check(Number.isInteger(limit)&&limit>=1&&limit<=10,'invalid_limit');
   check(cursor===null||(/^[\x21-\x7e]{1,1024}$/.test(cursor)),'invalid_cursor');
   await profile();const result=await active.list({limit,cursor});
   check(result&&Array.isArray(result.messages)&&result.messages.length<=limit,'invalid_provider_response',503);
   const messages=result.messages.map(item=>{check(item&&identifier(item.id)&&identifier(item.thread_id)&&(!readOnly||(/^[a-f0-9]{1,64}$/.test(item.id)&&/^[a-f0-9]{1,64}$/.test(item.thread_id))),'invalid_provider_response',503);return {id:item.id,thread_id:item.thread_id};});
   if(readOnly)check(result.next_cursor===null||result.next_cursor===undefined||(typeof result.next_cursor==='string'&&/^[\x21-\x7e]{1,1024}$/.test(result.next_cursor)),'invalid_provider_response',503);
   const query=readOnly?require('./native.cjs').READONLY_LIST_QUERY:null;
   return scoped({status:'ok',messages,next_cursor:result.next_cursor??null,...(readOnly?{complete:false,context_only:true,untrusted_context:true,receipt_eligible:false,
    scope:'inbox_last_7_days',query,listing_complete:result.next_cursor==null,
    coverage:{scope:'page',query,window_days:7,limit,returned:messages.length,has_more:result.next_cursor!=null}}:{})});
  }
  if(readOnly&&method==='GET'&&route.startsWith('/v1/threads/')){
   const id=route.slice('/v1/threads/'.length);
   check(/^[a-f0-9]{1,64}$/.test(id)&&!parsed.search,'invalid_thread_id');
   await profile();const thread=await active.readThread(id);
   check(thread?.id===id&&thread.context_only===true&&thread.receipt_eligible===false&&Array.isArray(thread.messages)&&
    typeof thread.complete==='boolean'&&thread.coverage&&Array.isArray(thread.coverage.reasons),'invalid_provider_response',503);
   const result=scoped({status:'ok',context_only:true,untrusted_context:true,thread});
   check(Buffer.byteLength(JSON.stringify(result))<=1000000,'context_response_too_large',503);
   return result;
  }
  if(method==='GET'&&route.startsWith('/v1/messages/')){
   const id=route.slice('/v1/messages/'.length);check(identifier(id)&&!parsed.search,'invalid_message_id');
   await profile();const message=await active.get(id);check(message?.id===id,'provider_message_mismatch',503);
   check(typeof message.body==='string'&&Buffer.byteLength(message.body)<=65536,'invalid_provider_response',503);
   check(identifier(message.thread_id),'invalid_provider_response',503);
   if(readOnly){
    check(typeof message.sent==='boolean'&&typeof message.truncated==='boolean'&&message.truncated===false&&message.headers_complete===true,'read_evidence_incomplete',503);
    check(Array.isArray(message.labelIds)&&message.labelIds.every(value=>typeof value==='string')&&message.sent===message.labelIds.includes('SENT'),'read_labels_incomplete',503);
    const observed=require('./native.cjs').readbackHeaders({headers:message.headers});
    for(const key of ['from','to','subject','cc','bcc','date','message_id'])check(typeof message[key]==='string'&&message[key]===observed[key],'read_headers_mismatch',503);
    return scoped({status:'ok',message:{id:message.id,thread_id:message.thread_id,body:message.body,truncated:false,
     sent:message.sent,labelIds:[...message.labelIds],...observed,
     ...(message.context_only===true?{context_only:true,receipt_eligible:false,mime_evidence:message.mime_evidence}:{})}});
   }
   const visible={id:message.id,thread_id:message.thread_id,body:message.body,truncated:message.truncated===true};
   for(const key of ['from','to','subject','date'])visible[key]=typeof message[key]==='string'?message[key].slice(0,2048):'';
   return scoped({status:'ok',message:visible});
  }
  if(method==='POST'&&route==='/v1/send'){
   check(!parsed.search,'invalid_query');check(sendEnabled,'send_disabled',503);
   const authorize=()=>{if(sendAuthority!==null)check(sendAuthority(body,now())===true,'send_scope_not_authorized',403);};
   authorize();
   const info=validate(body,credential,now());
   await profile();validate(body,credential,now());authorize();
   const exact=Object.hasOwn(body,'reply_message_id');
   if(exact){
    check(active.supportsExactReplyParent===true,'exact_parent_unsupported',503);
    // Existing journal CAS primitives, separate namespaces; no new journal schema.
    // Pin content/thread across approval keys and parent changes, even after timeout.
    const legacy={...business(body)};delete legacy.reply_message_id;
    const touchKey=hash('exact-reply-touch-v1:'+hash(legacy))+'.key.json';
    await journal.append(touchKey,{businessHash:info.businessHash});
    check((await journal.read(touchKey))?.businessHash===info.businessHash,'reply_anchor_conflict',409);
    const anchorKey=hash('exact-reply-anchor-v1:'+info.businessHash)+'.key.json';
    await journal.append(anchorKey,{reply_message_id:body.reply_message_id});
    check((await journal.read(anchorKey))?.reply_message_id===body.reply_message_id,'reply_anchor_conflict',409);
   }
   const reserved=await journal.reserve(body.idempotency_key,{...info,requestedThreadId:body.thread_id??null});
   if(!reserved.execute)return scoped({...await journal.state(info.businessHash),deduplicated:true});
   // Expiry rechecked after fsync. Reservation remains uncertain on any failure.
   try{validate(body,credential,now());authorize();
    check(typeof active.findDuplicates==='function','duplicate_check_unavailable',503);
    const duplicates=await active.findDuplicates({to:body.to,subject:body.subject,body:body.body});
    if(duplicates?.complete!==true||!Array.isArray(duplicates.messages)||duplicates.messages.length){
      return scoped({...await journal.uncertain(info.businessHash,'possible_duplicate'),'error_code':'possible_duplicate'});
    }
    validate(body,credential,now());authorize();
    check(typeof active.reserveQuota==='function'&&typeof active.finishQuota==='function','quota_unavailable',503);
    await active.reserveQuota({effect:info.businessHash,to:body.to,messageId:info.messageId,...(exact?{reply_message_id:body.reply_message_id}:{})});
    validate(body,credential,now());authorize();
    const result=await active.send({to:body.to,subject:body.subject,body:body.body,thread_id:body.thread_id??null,message_id:info.messageId,...(exact?{reply_message_id:body.reply_message_id}:{})},{authorize:()=>{validate(body,credential,now());authorize();}});
    return scoped(await receipt(info,result,active));
   }catch(error){return scoped(await journal.uncertain(info.businessHash,'send_outcome_uncertain'));}
  }
  if(method==='POST'&&route==='/v1/reconcile'){
   check(!parsed.search&&body&&Object.keys(body).length===1&&typeof body.idempotency_key==='string'&&/^[A-Za-z0-9_.:-]{1,128}$/.test(body.idempotency_key),'invalid_reconciliation');
   const businessHash=await journal.key(body.idempotency_key),state=await journal.state(businessHash);
   if(state.status==='sent')return scoped(state);
   const reservation=await journal.read(businessHash+'.reservation.json');
   if(!reservation)return scoped(state);
   const anchor=await journal.read(hash('exact-reply-anchor-v1:'+businessHash)+'.key.json');
   if(anchor)check(typeof anchor.reply_message_id==='string'&&/^[a-f0-9]{1,64}$/.test(anchor.reply_message_id),'journal_corrupt',503);
   await profile();
   if(anchor)check(active.supportsExactReplyParent===true,'exact_parent_unsupported',503);
   const found=await active.findSent(reservation.messageId,...(anchor?[{reply_message_id:anchor.reply_message_id,thread_id:reservation.requestedThreadId}]:[]));
   check(Array.isArray(found)&&found.length<=2,'invalid_provider_response',503);
   if(found.length!==1)return scoped({...state,error_code:found.length?'ambiguous_receipt':'receipt_not_found'});
   const candidate=found[0];
   const matches=candidate.sent===true&&!candidate.truncated&&candidate.message_id===reservation.messageId&&identifier(candidate.id)
       &&(reservation.requestedThreadId===null||candidate.thread_id===reservation.requestedThreadId)
       &&(anchor?candidate.reply_message_id===anchor.reply_message_id:!Object.hasOwn(candidate,'reply_message_id'))
       &&hash(business({...candidate,thread_id:reservation.requestedThreadId,...(anchor?{reply_message_id:anchor.reply_message_id}:{})}))===businessHash;
   if(!matches)return scoped({...state,error_code:'receipt_snapshot_mismatch'});
   return scoped(await receipt({businessHash,messageId:reservation.messageId},candidate,active));
  }
  throw new BridgeError('route_not_found',404);
 }
 return {handle,journal};
}
module.exports={createBridge};
