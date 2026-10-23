'use strict';
const {EXPECTED,check,identifier}=require('./contract.cjs');
// sql is a postgres.js connection. All inputs are bound parameters.
// No DDL, credential reads, retries, release of reservations, or provider calls here.
function createQuota(sql){
 const query=(tx,text,args=[])=>tx.unsafe(text,args);
 async function boundary(tx,to){
  const rows=await query(tx,`SELECT id,workspace_id,email,is_active,daily_limit,
   (clock_timestamp() AT TIME ZONE 'UTC')::date::text AS utc_day
   FROM mailboxes WHERE id=$1 AND workspace_id=$2 AND email=$3 FOR UPDATE`,[EXPECTED.mailbox,EXPECTED.workspace,EXPECTED.email]);
  const row=rows[0];check(rows.length===1&&row.is_active===true&&Number.isInteger(row.daily_limit)&&row.daily_limit>0,'quota_mailbox_unavailable',503);
  const suppressed=await query(tx,`SELECT 1 FROM contacts WHERE workspace_id=$1 AND lower(btrim(email))=$2 AND status IS DISTINCT FROM 'active' LIMIT 1`,[EXPECTED.workspace,to]);
  check(!suppressed.length,'recipient_suppressed',403);return row;
 }
 async function usage(tx,day){
   const counts=await query(tx,`SELECT (
    (SELECT count(*) FROM thunder_bridge_quota q WHERE q.mailbox_id=$1 AND q.utc_day=$2::date) +
    (SELECT count(*) FROM emails_sent e WHERE e.mailbox_id=$1 AND e.sent_at >= $2::date AND e.sent_at < $2::date + interval '1 day'
      AND NOT EXISTS (SELECT 1 FROM thunder_bridge_quota q WHERE q.mailbox_id=e.mailbox_id AND q.utc_day=$2::date AND q.provider_id=e.gmail_message_id))
   )::integer AS used`,[EXPECTED.mailbox,day]);
  check(Number.isInteger(counts[0]?.used),'quota_count_unavailable',503);return counts[0].used;
 }
 function input(info){check(info&&/^[a-f0-9]{64}$/.test(info.effect)&&typeof info.to==='string'&&info.to===info.to.trim().toLowerCase()&&/^[^\s@]+@[^\s@]+$/.test(info.to)&&info.messageId===`<thunderos.${info.effect}@thundermustard.com>`,'invalid_quota_request');}
 return {
  async status(){return sql.begin('READ ONLY',async tx=>{
   const rows=await query(tx,`SELECT daily_limit,is_active FROM mailboxes WHERE id=$1 AND workspace_id=$2 AND email=$3`,[EXPECTED.mailbox,EXPECTED.workspace,EXPECTED.email]);
   check(rows.length===1&&rows[0].is_active===true&&Number.isInteger(rows[0].daily_limit)&&rows[0].daily_limit>0,'quota_mailbox_unavailable',503);
   const access=await query(tx,`SELECT (has_table_privilege(current_user,'thunder_bridge_quota','SELECT') AND has_table_privilege(current_user,'thunder_bridge_quota','INSERT') AND has_table_privilege(current_user,'thunder_bridge_quota','UPDATE')) AS allowed`);
   check(access[0]?.allowed===true,'quota_unavailable',503);
   return {ready:true,daily_limit:rows[0].daily_limit};
  });},
  async reserve(info){input(info);return sql.begin(async tx=>{
   const row=await boundary(tx,info.to);
   await query(tx,'SELECT pg_advisory_xact_lock(hashtextextended($1,0))',[EXPECTED.mailbox+':'+row.utc_day]);
   const previous=await query(tx,'SELECT effect FROM thunder_bridge_quota WHERE mailbox_id=$1 AND effect=$2',[EXPECTED.mailbox,info.effect]);
   check(!previous.length,'quota_effect_already_reserved',409);
   const used=await usage(tx,row.utc_day);
   check(used<row.daily_limit,'daily_quota_exhausted',409);
   await query(tx,`INSERT INTO thunder_bridge_quota(mailbox_id,effect,utc_day,recipient,message_id) VALUES($1,$2,$3::date,$4,$5)`,[EXPECTED.mailbox,info.effect,row.utc_day,info.to,info.messageId]);
   return {effect:info.effect,utc_day:row.utc_day};
  });},
  async assertCurrent(info){input(info);return sql.begin(async tx=>{
   const row=await boundary(tx,info.to);
   const records=await query(tx,`SELECT effect FROM thunder_bridge_quota WHERE mailbox_id=$1 AND effect=$2 AND utc_day=$3::date AND recipient=$4 AND message_id=$5 AND state='reserved'`,[EXPECTED.mailbox,info.effect,row.utc_day,info.to,info.messageId]);
   check(records.length===1,'quota_reservation_unavailable',409);
   check(await usage(tx,row.utc_day)<=row.daily_limit,'daily_quota_exhausted',409);
  });},
  async finish({effect,providerId}){
   check(/^[a-f0-9]{64}$/.test(effect)&&identifier(providerId),'invalid_quota_receipt');
   return sql.begin(async tx=>{
    const rows=await query(tx,`UPDATE thunder_bridge_quota SET state='sent',provider_id=$3,confirmed_at=COALESCE(confirmed_at,clock_timestamp()) WHERE mailbox_id=$1 AND effect=$2 AND (provider_id IS NULL OR provider_id=$3) RETURNING effect`,[EXPECTED.mailbox,effect,providerId]);
    check(rows.length===1,'quota_receipt_conflict',409);
   });
  }
 };
}
module.exports={createQuota};
