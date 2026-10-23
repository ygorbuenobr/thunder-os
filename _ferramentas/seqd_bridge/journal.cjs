'use strict';
const fs=require('node:fs/promises'),C=require('node:fs').constants,path=require('node:path');
const {check,hash,canonical,BridgeError}=require('./contract.cjs');
class Journal {
 constructor(root){this.root=path.resolve(root);this.ready=this.init();this.ready.catch(()=>{});}
 async init(){await fs.mkdir(this.root,{recursive:true,mode:0o700});check(await fs.realpath(this.root)===this.root,'journal_symlink',503);}
 file(name){check(/^[a-f0-9]{64}\.(key|reservation|sent|uncertain)\.json$/.test(name),'journal_invalid_key',503);return path.join(this.root,name);}
 async syncDir(){const h=await fs.open(this.root,C.O_RDONLY|C.O_DIRECTORY);try{await h.sync();}finally{await h.close();}}
 async read(name){await this.ready;let h;try{h=await fs.open(this.file(name),C.O_RDONLY|C.O_NOFOLLOW);const bytes=await h.readFile();check(bytes.length<=8192,'journal_corrupt',503);return JSON.parse(bytes.toString('utf8'));}catch(e){if(e.code==='ENOENT')return null;throw new BridgeError('journal_unavailable',503);}finally{if(h)await h.close();}}
 async append(name,value){
  await this.ready;let h;const temporary=path.join(this.root,require('node:crypto').randomUUID()+'.tmp');
  try{
   h=await fs.open(temporary,C.O_WRONLY|C.O_CREAT|C.O_EXCL|C.O_NOFOLLOW,0o600);
   await h.writeFile(canonical(value));await h.sync();await h.close();h=null;
   // Hard-link publication is atomic, no-clobber CAS; readers never see partial JSON.
   await fs.link(temporary,this.file(name));await this.syncDir();return true;
  }catch(e){if(e.code==='EEXIST')return false;throw new BridgeError('journal_unavailable',503);}
  finally{if(h)await h.close();await fs.unlink(temporary).catch(()=>{});}
 }
 async reserve(key,info){
  const keyName=hash(key)+'.key.json',binding={snapshotHash:info.snapshotHash,businessHash:info.businessHash};
  const freshKey=await this.append(keyName,binding),existing=await this.read(keyName);
  check(existing?.snapshotHash===info.snapshotHash&&existing?.businessHash===info.businessHash,'idempotency_conflict',409);
  if(!freshKey)return {execute:false,businessHash:info.businessHash};
  const freshBusiness=await this.append(info.businessHash+'.reservation.json',{version:1,businessHash:info.businessHash,messageId:info.messageId,requestedThreadId:info.requestedThreadId??null});
  return {execute:freshBusiness,businessHash:info.businessHash};
 }
 async key(key){const item=await this.read(hash(key)+'.key.json');check(item,'idempotency_unknown',404);check(/^[a-f0-9]{64}$/.test(item.businessHash),'journal_corrupt',503);return item.businessHash;}
 async state(businessHash){
  const sent=await this.read(businessHash+'.sent.json');if(sent)return sent;
  const reserved=await this.read(businessHash+'.reservation.json');
  return {status:'uncertain',message_id:reserved?.messageId??null};
 }
 async sent(businessHash,receipt){
  const fresh=await this.append(businessHash+'.sent.json',receipt),saved=await this.read(businessHash+'.sent.json');
  check(fresh||canonical(saved)===canonical(receipt),'receipt_conflict',409);return saved;
 }
 async uncertain(businessHash,code){await this.append(businessHash+'.uncertain.json',{status:'uncertain',error_code:code});return this.state(businessHash);}
}
module.exports={Journal};
