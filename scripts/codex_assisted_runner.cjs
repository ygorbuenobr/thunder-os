'use strict';
// Called by an explicitly authorized Codex operator with existing connected tools.
// No CLI autorun, credential discovery, scheduling, CRM or alternate provider.
const {createHash}=require('node:crypto');
const canonical=v=>JSON.stringify(v&&typeof v==='object'?(Array.isArray(v)?v.map(x=>JSON.parse(canonical(x))):Object.fromEntries(Object.keys(v).sort().map(k=>[k,JSON.parse(canonical(v[k]))]))):v);
const hash=v=>createHash('sha256').update(canonical(v)).digest('hex');
const requireValue=(ok,code)=>{if(!ok)throw Error(code);};
async function confirmReceipt(panel,{job_id,receipt},onPhase=()=>{}){
  onPhase('RECEIPT_PREVIEW');
  const preview=await panel('receipt-preview',{job_id,receipt});
  requireValue(preview?.receipt===receipt&&/^[a-f0-9]{64}$/.test(preview.review_hash||'')&&
    preview.verification==='provider_sent_copy'&&preview.attempt_attribution==='operator_reported','RECEIPT_PREVIEW_INVALID');
  onPhase('RECEIPT_CONFIRM');
  const result=await panel('receipt-confirm',{job_id,receipt,review_hash:preview.review_hash});
  requireValue(result?.status==='confirmed'&&result.review_hash===preview.review_hash&&result.receipt===receipt&&result.send_attempted===false&&
    result.retry_write===false&&result.verification==='provider_sent_copy'&&result.attempt_attribution==='operator_reported','RECEIPT_CONFIRMATION_INVALID');
  return result;
}
async function runAssisted(packet,{panel,gmail,journal,withLock,preflight,now=()=>Date.now()/1000}){
  requireValue(packet?.schema==='thunder_codex_assisted_v1'&&packet.dispatch_authorized===false,'PACKET_INVALID');
  const {review_hash,...unsigned}=packet;requireValue(hash(unsigned)===review_hash,'PACKET_CHANGED');
  requireValue(panel&&gmail&&journal&&typeof withLock==='function'&&typeof withLock.check==='function'&&typeof preflight==='function','PORTS_REQUIRED');
  return withLock(async()=>{
    let phase='STARTED',sendAttempted=false,receipt=null;
    try{
      // Existing journals never retry a send; recovery is restricted to untouched STARTED.
      const recovered=await journal.createExclusive(review_hash,{state:'STARTED',packet});
      phase='PREFLIGHT';const evidence=await preflight(packet);
      requireValue(evidence?.complete===true&&evidence?.eligible===true&&evidence?.packet_hash===review_hash&&
        Number.isFinite(evidence.checked_at)&&now()-evidence.checked_at>=0&&now()-evidence.checked_at<=30,'PREFLIGHT_INCOMPLETE');
      requireValue(await withLock.check()===true,'LOCK_NOT_HELD');
      phase='CLAIM';const ticket=recovered?.ticket||await panel('claim',{cadence_id:packet.cadence_id,step_id:packet.step_id,review_hash});
      const {ticket_hash,...unsignedTicket}=ticket;
      requireValue(ticket.dispatch_authorized===true&&ticket.review_hash===review_hash&&hash(unsignedTicket)===ticket_hash&&
        canonical(ticket.payload)===canonical(packet.payload)&&ticket.job_id===packet.job_id&&
        ticket.limits.max_attempts===1&&ticket.expires_at>now(),'TICKET_INVALID');
      phase='SEND_INTENT';await journal.append({state:'SEND_INTENT',ticket});
      requireValue(ticket.expires_at>now(),'TICKET_EXPIRED');
      requireValue(await withLock.check()===true,'LOCK_NOT_HELD');
      phase='DISPATCH';const grant=await panel('dispatch',{job_id:ticket.job_id,ticket_hash});
      requireValue(grant?.dispatch_authorized===true&&grant.ticket_hash===ticket_hash&&grant.packet_hash===review_hash&&
        grant.job_id===packet.job_id&&grant.expires_at>now()&&grant.expires_at-now()<=5,'DISPATCH_REVOKED');
      try{
        phase='PROVIDER_CALL';sendAttempted=true;
        const sent=await gmail.send_email(ticket.payload,{not_after:grant.expires_at,ticket_hash,packet_hash:review_hash,job_id:packet.job_id});
        requireValue(typeof sent?.id==='string'&&/^[a-f0-9]{1,64}$/.test(sent.id),'SEND_RESULT_UNKNOWN');
        receipt='gmail:'+sent.id;phase='PROVIDER_RESULT';
        await journal.append({state:'PROVIDER_RESULT',message_id:sent.id,thread_id:sent.thread_id||null});
      }catch(error){
        try{await journal.append({state:'UNCERTAIN_NO_RETRY',receipt});}catch{error.journal_error='JOURNAL_WRITE_FAILED';}
        throw error;
      }
      const result=await confirmReceipt(panel,{job_id:ticket.job_id,receipt},p=>{phase=p;});
      phase='RECEIPT_RETURNED';await journal.append({state:'RECEIPT_RETURNED',result});return result;
    }catch(error){
      error.assisted={phase,send_attempted:sendAttempted,receipt,retry_write:false,
        outcome:phase==='RECEIPT_RETURNED'?'receipt_confirmed_journal_pending':receipt?'provider_receipt_known_confirmation_pending':sendAttempted?'send_outcome_uncertain':'not_sent'};
      throw error;
    }
  });
}
module.exports={runAssisted,hash};

// Existing authenticated panel session supplied by Codex; no token extraction/login.
function panelPort(fetchPanel){
  return async(action,body)=>{
    requireValue(['claim','dispatch','receipt-preview','receipt-confirm','recover-preclaim','recover-pre-dispatch','reconcile-reservation'].includes(action),'PANEL_ACTION_INVALID');
    const session=await fetchPanel('/api/os/v1/session',{method:'GET'});
    requireValue(session?.actor==='ygor'&&typeof session.nonce==='string','PANEL_SESSION_REQUIRED');
    return fetchPanel('/api/os/v1/integrations/codex-assisted/'+action,
      {method:'POST',headers:{'Content-Type':'application/json','X-Thunder-Nonce':session.nonce},body:JSON.stringify(body)});
  };
}
async function durableJournal(directory,{recoverPreclaim,recoverPreDispatch,reconcileReservation}={}){
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
        if(error.code!=='EEXIST'||![recoverPreclaim,recoverPreDispatch,reconcileReservation].some(f=>typeof f==='function'))throw error;
        requireValue([recoverPreclaim,recoverPreDispatch,reconcileReservation].filter(f=>typeof f==='function').length===1,'RECOVERY_MODE_CONFLICT');
        const flags=require('node:fs').constants;
        handle=await fs.open(name,flags.O_RDWR|flags.O_NOFOLLOW);
        const stat=await handle.stat();requireValue(stat.isFile()&&stat.size<=1000000,'JOURNAL_NOT_PRECLAIM');
        const raw=await handle.readFile('utf8');
        requireValue(raw.endsWith('\n'),'JOURNAL_NOT_PRECLAIM');
        const rows=raw.trim().split('\n').map(JSON.parse);
        requireValue(rows.length>=1&&rows.length<=32&&rows[0].state==='STARTED'&&canonical(rows[0])===canonical(value),'JOURNAL_NOT_PRECLAIM');
        for(const row of rows.slice(1))requireValue(['RECOVERED_PRECLAIM','RECOVERED_PRE_DISPATCH'].includes(row.state)&&row.proof?.recovered_preclaim===true&&row.proof.dispatch_authorized===false&&row.proof.review_hash===key&&row.proof.job_id===value.packet.job_id&&Number.isFinite(row.proof.checked_at),'JOURNAL_NOT_PRECLAIM');
        if(typeof reconcileReservation==='function'){
          const ticket=await reconcileReservation(value.packet);
          const {ticket_hash,...unsigned}=ticket||{};
          requireValue(ticket?.dispatch_authorized===true&&ticket.review_hash===key&&ticket.job_id===value.packet.job_id&&
            canonical(ticket.payload)===canonical(value.packet.payload)&&hash(unsigned)===ticket_hash&&ticket.expires_at>Date.now()/1000,'RESERVATION_RECOVERY_INVALID');
          await handle.write(JSON.stringify({state:'RESERVATION_RECONCILED',ticket})+'\n');await handle.sync();return {ticket};
        }
        requireValue(typeof recoverPreDispatch==='function'?rows.length>=2:rows.length===1,'JOURNAL_NOT_PRECLAIM');
        const proof=await (recoverPreDispatch||recoverPreclaim)(value.packet);
        requireValue(proof?.recovered_preclaim===true&&proof.dispatch_authorized===false&&
          proof.review_hash===key&&proof.job_id===value.packet.job_id&&Number.isFinite(proof.checked_at)&&
          Date.now()/1000-proof.checked_at>=0&&Date.now()/1000-proof.checked_at<30,'PRECLAIM_RECOVERY_INVALID');
        await handle.write(JSON.stringify({state:recoverPreDispatch?'RECOVERED_PRE_DISPATCH':'RECOVERED_PRECLAIM',proof})+'\n');await handle.sync();
        return;
      }
      await handle.write(JSON.stringify(value)+'\n');await handle.sync();
      const dir=await fs.open(directory,'r');try{await dir.sync();}finally{await dir.close();}
    },
    async append(value){requireValue(handle,'JOURNAL_NOT_OPEN');await handle.write(JSON.stringify(value)+'\n');await handle.sync();},
    async close(){if(handle){await handle.close();handle=null;}return {closed:true};}
  };
}
async function reconcileAssisted({job_id,receipt},panel){
  requireValue(typeof job_id==='string'&&/^gmail:[a-f0-9]{1,64}$/.test(receipt),'RECEIPT_INVALID');
  return confirmReceipt(panel,{job_id,receipt});
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
