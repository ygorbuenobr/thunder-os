const {test}=require('node:test'),assert=require('node:assert/strict');
const {validate,guard,checkGrant,saveGrant}=require('./seqd_reconnect.cjs');
test('readonly grant and exact mailbox required',()=>{
 const token={refresh_token:'synthetic',scope:'https://www.googleapis.com/auth/gmail.readonly'};
 checkGrant(token,'ygor.bueno@thundermustard.com');
 assert.throws(()=>checkGrant(token,'other@example.com'));
 assert.throws(()=>checkGrant({...token,refresh_token:undefined}));
 assert.throws(()=>checkGrant({...token,scope:token.scope+' https://www.googleapis.com/auth/gmail.send'}));
});
test('update exact existing row only, readback, CAS failure',async()=>{
 const calls=[];const sql={begin:async fn=>fn(async(parts,...args)=>{calls.push({text:parts.join('?'),args});return [{id:'synthetic'}];})};
 await saveGrant(sql,'new-encrypted','old-encrypted');
 assert.equal(calls.length,2);assert.match(calls[0].text,/UPDATE mailboxes SET refresh_token=\?/);
 assert.match(calls[0].text,/workspace_id=\? AND email=\? AND is_active=true AND refresh_token=\?/);
 assert.equal(calls[0].args.at(-1),'old-encrypted');
 assert.equal(calls.some(c=>/INSERT|DELETE|is_active=\?/.test(c.text)),false);
 let count=0;
 await assert.rejects(saveGrant({begin:async fn=>fn(async()=>{count++;return [];})},'new','old'),/concurrently/);
 assert.equal(count,1);
});
test('callback rejects wrong state, replay, expired, missing code',()=>{
 const u=new URL('https://seqd.r3xdigital.tech/api/mailboxes/auth/callback?state=ok&code=synthetic');
 validate(u,'ok',Date.now()+10000,false);
 assert.throws(()=>validate(u,'wrong',Date.now()+10000,false));
 assert.throws(()=>validate(u,'ok',Date.now()+10000,true));
 assert.throws(()=>validate(u,'ok',Date.now()-1,false));
 u.searchParams.delete('code');assert.throws(()=>validate(u,'ok',Date.now()+10000,false));
});
test('reconnect transport blocks sends, inbox and wrong hosts',()=>{
 let calls=0;const auth={transporter:{request(){calls++}}};guard(auth);
 for(const url of ['https://gmail.googleapis.com/gmail/v1/users/me/messages/send','https://gmail.googleapis.com/gmail/v1/users/me/messages','https://evil.test/token'])
 assert.throws(()=>auth.transporter.request({url,method:'POST'}));
 assert.equal(calls,0);
 auth.transporter.request({url:'https://oauth2.googleapis.com/token',method:'POST'});
 auth.transporter.request({url:'https://gmail.googleapis.com/gmail/v1/users/me/profile',method:'GET'});
 assert.equal(calls,2);
});
