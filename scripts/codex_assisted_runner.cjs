'use strict';
// Called by an explicitly authorized Codex operator with existing connected tools.
// No CLI autorun, credential discovery, scheduling, CRM or alternate provider.
const {createHash}=require('node:crypto');
const canonical=v=>JSON.stringify(v&&typeof v==='object'?(Array.isArray(v)?v.map(x=>JSON.parse(canonical(x))):Object.fromEntries(Object.keys(v).sort().map(k=>[k,JSON.parse(canonical(v[k]))]))):v);
const hash=v=>createHash('sha256').update(canonical(v)).digest('hex');
const requireValue=(ok,code)=>{if(!ok)throw Error(code);};
async function runAssisted(packet,{panel,gmail,journal,withLock,preflight,now=()=>Date.now()/1000}){
  requireValue(packet?.schema==='thunder_codex_assisted_v1'&&packet.dispatch_authorized===false,'PACKET_INVALID');
  const {review_hash,...unsigned}=packet;requireValue(hash(unsigned)===review_hash,'PACKET_CHANGED');
  requireValue(panel&&gmail&&journal&&typeof withLock==='function'&&typeof withLock.check==='function'&&typeof preflight==='function','PORTS_REQUIRED');
  return withLock(async()=>{
    // Journal creation must be atomic/durable; an existing attempt never retries a send.
    await journal.createExclusive(review_hash,{state:'STARTED',packet});
    const evidence=await preflight(packet);
    requireValue(evidence?.complete===true&&evidence?.eligible===true&&evidence?.packet_hash===review_hash&&
      Number.isFinite(evidence.checked_at)&&now()-evidence.checked_at>=0&&now()-evidence.checked_at<=30,'PREFLIGHT_INCOMPLETE');
    requireValue(await withLock.check()===true,'LOCK_NOT_HELD');
    const ticket=await panel('claim',{cadence_id:packet.cadence_id,step_id:packet.step_id,review_hash});
    const {ticket_hash,...unsignedTicket}=ticket;
    requireValue(ticket.dispatch_authorized===true&&ticket.review_hash===review_hash&&hash(unsignedTicket)===ticket_hash&&
      canonical(ticket.payload)===canonical(packet.payload)&&ticket.job_id===packet.job_id&&
      ticket.limits.max_attempts===1&&ticket.expires_at>now(),'TICKET_INVALID');
    await journal.append({state:'SEND_INTENT',ticket});
    requireValue(ticket.expires_at>now(),'TICKET_EXPIRED');
    requireValue(await withLock.check()===true,'LOCK_NOT_HELD');
    const grant=await panel('dispatch',{job_id:ticket.job_id,ticket_hash});
    requireValue(grant?.dispatch_authorized===true&&grant.ticket_hash===ticket_hash&&grant.expires_at>now(),'DISPATCH_REVOKED');
    let sent;
    try{
      sent=await gmail.send_email(ticket.payload,{not_after:grant.expires_at,ticket_hash,packet_hash:review_hash});
      requireValue(typeof sent?.id==='string'&&/^[a-f0-9]{1,64}$/.test(sent.id),'SEND_RESULT_UNKNOWN');
      await journal.append({state:'PROVIDER_RESULT',message_id:sent.id,thread_id:sent.thread_id||null});
    }catch(error){await journal.append({state:'UNCERTAIN_NO_RETRY'});throw error;}
    // Server reads Gmail independently; caller flags cannot manufacture confirmation.
    const receipt='gmail:'+sent.id,preview=await panel('receipt-preview',{job_id:ticket.job_id,receipt});
    const result=await panel('receipt-confirm',{job_id:ticket.job_id,receipt,review_hash:preview.review_hash});
    await journal.append({state:'RECEIPT_RETURNED',result});
    return result;
  });
}
module.exports={runAssisted,hash};

// Existing authenticated panel session supplied by Codex; no token extraction/login.
function panelPort(fetchPanel){
  return async(action,body)=>{
    requireValue(['claim','dispatch','receipt-preview','receipt-confirm','recover-preclaim'].includes(action),'PANEL_ACTION_INVALID');
    const session=await fetchPanel('/api/os/v1/session',{method:'GET'});
    requireValue(session?.actor==='ygor'&&typeof session.nonce==='string','PANEL_SESSION_REQUIRED');
    return fetchPanel('/api/os/v1/integrations/codex-assisted/'+action,
      {method:'POST',headers:{'Content-Type':'application/json','X-Thunder-Nonce':session.nonce},body:JSON.stringify(body)});
  };
}
async function durableJournal(directory,{recoverPreclaim}={}){
  const fs=require('node:fs/promises'),path=require('node:path');
  requireValue(path.isAbsolute(directory||''),'JOURNAL_DIRECTORY_REQUIRED');
  const stat=await fs.lstat(directory);requireValue(stat.isDirectory()&&!stat.isSymbolicLink(),'JOURNAL_DIRECTORY_INVALID');
  let handle;
  return {
    async createExclusive(key,value){
      requireValue(/^[a-f0-9]{64}$/.test(key),'JOURNAL_KEY_INVALID');
      const name=path.join(directory,key+'.jsonl');
      try{handle=await fs.open(name,'wx',0o600);}
      catch(error){
        if(error.code!=='EEXIST'||typeof recoverPreclaim!=='function')throw error;
        const flags=require('node:fs').constants;
        handle=await fs.open(name,flags.O_RDWR|flags.O_NOFOLLOW);
        const raw=await handle.readFile('utf8');
        requireValue(raw.endsWith('\n')&&raw.trim().split('\n').length===1,'JOURNAL_NOT_PRECLAIM');
        const prior=JSON.parse(raw);
        requireValue(prior.state==='STARTED'&&canonical(prior)===canonical(value),'JOURNAL_NOT_PRECLAIM');
        const proof=await recoverPreclaim(value.packet);
        requireValue(proof?.recovered_preclaim===true&&proof.dispatch_authorized===false&&
          proof.review_hash===key&&proof.job_id===value.packet.job_id&&Number.isFinite(proof.checked_at)&&
          Date.now()/1000-proof.checked_at>=0&&Date.now()/1000-proof.checked_at<30,'PRECLAIM_RECOVERY_INVALID');
        await handle.write(JSON.stringify({state:'RECOVERED_PRECLAIM',proof})+'\n');await handle.sync();
        return;
      }
      await handle.write(JSON.stringify(value)+'\n');await handle.sync();
      const dir=await fs.open(directory,'r');try{await dir.sync();}finally{await dir.close();}
    },
    async append(value){requireValue(handle,'JOURNAL_NOT_OPEN');await handle.write(JSON.stringify(value)+'\n');await handle.sync();},
    async close(){if(handle)await handle.close();}
  };
}
async function reconcileAssisted({job_id,receipt},panel){
  requireValue(typeof job_id==='string'&&/^gmail:[a-f0-9]{1,64}$/.test(receipt),'RECEIPT_INVALID');
  const preview=await panel('receipt-preview',{job_id,receipt});
  return panel('receipt-confirm',{job_id,receipt,review_hash:preview.review_hash});
}
module.exports.panelPort=panelPort;
module.exports.durableJournal=durableJournal;
module.exports.reconcileAssisted=reconcileAssisted;

function connectedGmail(tools){
  requireValue(typeof tools?.mcp__codex_apps__gmail_send_email==='function','CONNECTED_GMAIL_REQUIRED');
  return {send_email:async (payload,authority)=>{
    const raw=await tools.mcp__codex_apps__gmail_send_email(payload,authority);
    requireValue(!raw?.isError,'GMAIL_TOOL_ERROR');
    if(raw?.structuredContent)return raw.structuredContent;
    if(raw?.content){const text=raw.content.find(x=>x.type==='text')?.text;requireValue(typeof text==='string','GMAIL_RESULT_UNKNOWN');return JSON.parse(text);}
    return raw;
  }};
}
// Reuse the reviewed operator's already-held lock; never acquire a second engine.
function heldLockPort(lock,verifyHeld){
  requireValue(typeof verifyHeld==='function','LOCK_VERIFIER_REQUIRED');
  const port=async work=>{requireValue(await verifyHeld(lock)===true,'LOCK_NOT_HELD');return work();};
  port.check=()=>verifyHeld(lock);return port;
}
module.exports.connectedGmail=connectedGmail;
module.exports.heldLockPort=heldLockPort;
