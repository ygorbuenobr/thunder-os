'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const {runAssisted,hash,durableJournal,panelPort,reconcileAssisted}=require('./codex_assisted_runner.cjs');
function fixture(){
 const packet={schema:'thunder_codex_assisted_v1',dispatch_authorized:false,cadence_id:'synthetic',step_id:'s1',job_id:'fixture-job',payload:{to:'fixture@example.invalid'},limits:{max_attempts:1}};
 packet.review_hash=hash(packet);let sends=0,claimed=0;const records=[];
 const ports={now:()=>100,withLock:async f=>f(),preflight:async()=>({complete:true,eligible:true,packet_hash:packet.review_hash,checked_at:100}),
 journal:{createExclusive:async()=>{if(records.length)throw Error('EXISTS');records.push('created');},append:async r=>records.push(r)},
 gmail:{send_email:async()=>{sends++;return {id:'abc123'};}},panel:async(a,b)=>{
  if(a==='claim'){claimed++;const t={...packet,dispatch_authorized:true,expires_at:160};t.ticket_hash=hash(t);return t;}
  if(a==='dispatch')return {ticket_hash:b.ticket_hash,dispatch_authorized:true,expires_at:105};
  if(a==='receipt-preview')return {review_hash:'fixture-review'};
  return {status:'confirmed',receipt:b.receipt};}};
 ports.withLock.check=async()=>true;
 return {packet,ports,records,counts:()=>({sends,claimed})};
}
test('synthetic complete flow and no replay',async()=>{const f=fixture();assert.equal((await runAssisted(f.packet,f.ports)).status,'confirmed');await assert.rejects(runAssisted(f.packet,f.ports),/EXISTS/);assert.deepEqual(f.counts(),{sends:1,claimed:1});});
test('timeout remains unknown, never resend',async()=>{const f=fixture();f.ports.gmail.send_email=async()=>{throw Error('lost');};await assert.rejects(runAssisted(f.packet,f.ports),/lost/);assert.equal(f.records.at(-1).state,'UNCERTAIN_NO_RETRY');await assert.rejects(runAssisted(f.packet,f.ports),/EXISTS/);});
test('partial history never claims or sends',async()=>{const f=fixture();f.ports.preflight=async()=>({complete:false});await assert.rejects(runAssisted(f.packet,f.ports),/INCOMPLETE/);assert.deepEqual(f.counts(),{sends:0,claimed:0});});
test('changed packet rejected',async()=>{const f=fixture();f.packet.payload.to='other@example.invalid';await assert.rejects(runAssisted(f.packet,f.ports),/CHANGED/);assert.deepEqual(f.counts(),{sends:0,claimed:0});});
test('expired ticket never sends',async()=>{const f=fixture(),old=f.ports.panel;f.ports.panel=async(...args)=>{const t=await old(...args);t.expires_at=99;delete t.ticket_hash;t.ticket_hash=hash(t);return t;};await assert.rejects(runAssisted(f.packet,f.ports),/TICKET_INVALID/);assert.equal(f.counts().sends,0);});
test('durable journal exclusive across instances',async()=>{const fs=require('node:fs/promises'),os=require('node:os'),path=require('node:path');const dir=await fs.mkdtemp(path.join(os.tmpdir(),'assisted-fixture-'));const a=await durableJournal(dir),b=await durableJournal(dir);try{await a.createExclusive('a'.repeat(64),{synthetic:true});await a.append({state:'UNCERTAIN'});await assert.rejects(b.createExclusive('a'.repeat(64),{}),/EEXIST/);assert.equal((await fs.readFile(path.join(dir,'a'.repeat(64)+'.jsonl'),'utf8')).trim().split('\n').length,2);}finally{await a.close();await b.close();await fs.rm(dir,{recursive:true});}});
test('panel port obtains nonce for each return, no send',async()=>{const calls=[];const port=panelPort(async(p,o)=>{calls.push([p,o]);return p.endsWith('/session')?{actor:'ygor',nonce:'synthetic'}:{review_hash:'proof',status:'confirmed'};});await reconcileAssisted({job_id:'fixture',receipt:'gmail:abc123'},port);assert.equal(calls.length,4);assert.equal(calls[1][1].headers['X-Thunder-Nonce'],'synthetic');});

test('lost lock after preflight blocks claim',async()=>{const f=fixture();f.ports.withLock.check=async()=>false;await assert.rejects(runAssisted(f.packet,f.ports),/LOCK_NOT_HELD/);assert.equal(f.counts().sends,0);assert.equal(f.counts().claimed,0);});

test('E2 forwards exact reply_message_id and only approved new text',async()=>{const f=fixture();f.packet.payload.reply_message_id='def456';f.packet.payload.payload={mime_type:'text/plain',charset:'UTF-8',body:{content:'Synthetic approved E2'}};f.packet.reply_contract={version:1,message_id:'def456'};delete f.packet.review_hash;f.packet.review_hash=hash(f.packet);f.ports.preflight=async()=>({complete:true,eligible:true,packet_hash:f.packet.review_hash,checked_at:100});let payload;f.ports.gmail.send_email=async p=>{payload=p;return {id:'abc123'};};await runAssisted(f.packet,f.ports);assert.equal(payload.reply_message_id,'def456');assert.equal(payload.payload.body.content,'Synthetic approved E2');});
