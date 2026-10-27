'use strict';
const crypto=require('node:crypto');
const EXPECTED=Object.freeze({mailbox:'452c6677-5ead-41d8-b036-6ef63945752d',workspace:'80d9245b-bc72-420e-b48b-f72e63b8660b',email:'ygor.bueno@thundermustard.com'});
class BridgeError extends Error {constructor(code,status=400){super(code);this.code=code;this.status=status;}}
function check(ok,code,status=400){if(!ok)throw new BridgeError(code,status);}
function canonical(value){
 if(value===null||typeof value==='string'||typeof value==='boolean')return JSON.stringify(value);
 if(typeof value==='number'){check(Number.isSafeInteger(value),'invalid_number');return String(value);}
 if(Array.isArray(value))return '['+value.map(canonical).join(',')+']';
 check(value&&typeof value==='object'&&Object.getPrototypeOf(value)===Object.prototype,'invalid_object');
 return '{'+Object.keys(value).sort().map(k=>JSON.stringify(k)+':'+canonical(value[k])).join(',')+'}';
}
const hash=value=>crypto.createHash('sha256').update(typeof value==='string'?value:canonical(value),'utf8').digest('hex');
function snapshot(e){return {mailbox:EXPECTED.mailbox,workspace:EXPECTED.workspace,preview_id:e.preview_id,version:e.version,to:e.to,subject:e.subject,body:e.body,thread_id:e.thread_id??null,...(Object.hasOwn(e,'reply_message_id')?{reply_message_id:e.reply_message_id}:{})};}
function business(e){return {mailbox:EXPECTED.mailbox,workspace:EXPECTED.workspace,to:e.to,subject:e.subject,body:e.body,thread_id:e.thread_id??null,...(Object.hasOwn(e,'reply_message_id')?{reply_message_id:e.reply_message_id}:{})};}
function signature(e,key){const value={...e};delete value.approval_signature;return crypto.createHmac('sha256',key).update('seqd-approval-v1\n'+canonical(value),'utf8').digest('hex');}
function safeEqual(a,b){if(typeof a!=='string'||typeof b!=='string')return false;const x=Buffer.from(a),y=Buffer.from(b);return x.length===y.length&&crypto.timingSafeEqual(x,y);}
const identifier=value=>typeof value==='string'&&/^[A-Za-z0-9_-]{1,160}$/.test(value);
function validText(value,max){return typeof value==='string'&&Buffer.byteLength(value)<=max&&Buffer.from(value).toString('utf8')===value&&!value.includes('\0');}
function validate(e,key,now=Date.now()){
 const required=['preview_id','version','content_hash','approved_actor','expires_at','idempotency_key','to','subject','body','approval_signature'];
 check(e&&typeof e==='object'&&!Array.isArray(e),'invalid_envelope');
 check(required.every(k=>Object.hasOwn(e,k))&&Object.keys(e).every(k=>required.includes(k)||k==='thread_id'||k==='reply_message_id'),'invalid_envelope');
 check(identifier(e.preview_id)&&Number.isSafeInteger(e.version)&&e.version>0&&e.version<=2147483647,'invalid_preview');
 check(typeof e.idempotency_key==='string'&&/^[A-Za-z0-9_.:-]{1,128}$/.test(e.idempotency_key),'invalid_idempotency_key');
 check(e.approved_actor==='YGOR','human_approval_required',403);
 check(typeof e.to==='string'&&e.to===e.to.trim().toLowerCase()&&/^[a-z0-9.!#$%&'*+\-/=?^_`{|}~]+@[a-z0-9.-]+\.[a-z]{2,63}$/.test(e.to)&&e.to.length<=254,'invalid_recipient');
 check(validText(e.subject,512)&&e.subject.trim().length>0&&!/[\x00-\x1f\x7f]/.test(e.subject),'invalid_subject');
 check(validText(e.body,65536)&&e.body.trim().length>0,'invalid_body');
 check(e.thread_id===undefined||e.thread_id===null||identifier(e.thread_id),'invalid_thread');
 if(Object.hasOwn(e,'reply_message_id'))check(typeof e.reply_message_id==='string'&&/^[a-f0-9]{1,64}$/.test(e.reply_message_id)&&typeof e.thread_id==='string'&&/^[a-f0-9]{1,64}$/.test(e.thread_id),'invalid_reply_message_id');
 const expires=Date.parse(e.expires_at);
 check(typeof e.expires_at==='string'&&/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,3})?Z$/.test(e.expires_at)&&Number.isFinite(expires)&&expires>now&&expires<=now+300000,'approval_expired',403);
 check(safeEqual(e.content_hash,hash(snapshot(e))),'snapshot_hash_mismatch',409);
 check(safeEqual(e.approval_signature,signature(e,key)),'approval_signature_invalid',403);
 const businessHash=hash(business(e));
 return {snapshotHash:e.content_hash,businessHash,messageId:`<thunderos.${businessHash}@thundermustard.com>`};
}
module.exports={EXPECTED,BridgeError,check,canonical,hash,snapshot,business,signature,safeEqual,identifier,validate};
