'use strict';
const fs=require('node:fs'),crypto=require('node:crypto'),http=require('node:http');
const EMAIL='ygor.bueno@thundermustard.com';
const ID='452c6677-5ead-41d8-b036-6ef63945752d';
const WORKSPACE='80d9245b-bc72-420e-b48b-f72e63b8660b';
const CALLBACK='/api/mailboxes/auth/callback';
const SCOPE='https://www.googleapis.com/auth/gmail.readonly';
function assert(v,code){if(!v)throw Error(code);}
function checkGrant(tokens,email){
 assert(tokens.refresh_token,'refresh_missing');
 const scopes=(tokens.scope||'').split(' ');
 assert(scopes.includes(SCOPE)&&scopes.every(s=>[SCOPE,'openid','email','https://www.googleapis.com/auth/userinfo.email'].includes(s)),'scope_mismatch');
 if(email!==undefined)assert(email===EMAIL,'mailbox_identity_mismatch');
}
async function saveGrant(sql,encrypted,previous){
 await sql.begin(async tx=>{
  const changed=await tx`UPDATE mailboxes SET refresh_token=${encrypted} WHERE id=${ID} AND workspace_id=${WORKSPACE} AND email=${EMAIL} AND is_active=true AND refresh_token=${previous} RETURNING id`;
  assert(changed.length===1,'mailbox_changed_concurrently');
  const saved=await tx`SELECT id FROM mailboxes WHERE id=${ID} AND workspace_id=${WORKSPACE} AND refresh_token=${encrypted}`;
  assert(saved.length===1,'readback_failed');
 });
}
function validate(url,state,expires,used){
 assert(!used && Date.now()<expires,'expired_or_used');
 assert(url.pathname===CALLBACK,'wrong_path');
 assert(url.searchParams.getAll('state').length===1 && url.searchParams.get('state')===state,'state_mismatch');
 assert(!url.searchParams.has('error'),'google_denied');
 assert(url.searchParams.getAll('code').length===1 && url.searchParams.get('code'),'code_missing');
}
function guard(auth){
 const t=auth.transporter,original=t.request;
 t.request=function(o,...rest){
  const u=new URL(String(o.url)),m=String(o.method||'GET').toUpperCase();
  assert(u.protocol==='https:'&&!u.username&&!u.password&&!u.port &&
   ((u.hostname==='oauth2.googleapis.com'&&u.pathname==='/token'&&m==='POST')||
    (u.hostname==='gmail.googleapis.com'&&u.pathname==='/gmail/v1/users/me/profile'&&m==='GET')),'route_blocked');
  return original.call(this,{...o,retry:false,maxRedirects:0,timeout:10000},...rest);
 };
}
async function main(){
 const root='/app/.next/server/',path='/app/thunder-bridge/native.cjs';
 assert(crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex')==='5dabd8eb34d1d1c0fbd787803f60539581c6684f0bed94fc6610b822cda9e31b','native_mismatch');
 const {PINS,registerBuiltins}=require(path);
 for(const [p,hash] of Object.entries(PINS))assert(crypto.createHash('sha256').update(fs.readFileSync(root+p)).digest('hex')===hash,'pin_mismatch');
 const runtime=require(root+'webpack-runtime.js');
 registerBuiltins(runtime,fs.readFileSync(root+'app/api/cron/poll-replies/route.js','utf8'));
 await Promise.all([4741,1813,7881,9994,7582].map(id=>runtime.e(id)));
 const native=await runtime(61069),enc=runtime(87416);
 const sql=require('node:module').createRequire('/app/package.json')('postgres')(process.env.DATABASE_URL,{max:1,prepare:false,connect_timeout:5});
 const rows=await sql.begin('READ ONLY',tx=>tx`SELECT refresh_token FROM mailboxes WHERE id=${ID} AND workspace_id=${WORKSPACE} AND email=${EMAIL} AND is_active=true`);
 assert(rows.length===1,'mailbox_missing');
 const previous=rows[0].refresh_token; rows[0].refresh_token=undefined;
 const auth=native.Zn();guard(auth);
 assert(process.env.GOOGLE_OAUTH_REDIRECT_URI==='https://seqd.r3xdigital.tech'+CALLBACK,'redirect_mismatch');
 const state=crypto.randomBytes(32).toString('base64url'),verifier=crypto.randomBytes(48).toString('base64url');
 const challenge=crypto.createHash('sha256').update(verifier).digest('base64url');
 const expires=Date.now()+600000;let used=false;
 const url=auth.generateAuthUrl({access_type:'offline',prompt:'consent',login_hint:EMAIL,scope:[SCOPE],
  include_granted_scopes:false,state,code_challenge:challenge,code_challenge_method:'S256'});
 return await new Promise((resolve,reject)=>{
  let timer;
  const server=http.createServer(async(req,res)=>{
   res.setHeader('Cache-Control','no-store');res.setHeader('Referrer-Policy','no-referrer');
   res.setHeader('Content-Type','text/plain; charset=utf-8');res.setHeader('Content-Security-Policy',"default-src 'none'");
   let u;try{u=new URL(req.url,'https://seqd.r3xdigital.tech');}catch{res.writeHead(400);res.end('Invalid request');return;}
   if(req.method!=='GET'||u.pathname!==CALLBACK){res.writeHead(404);res.end('Not found');return;}
   try{validate(u,state,expires,used);}catch{res.writeHead(400);res.end('Autorizacao invalida ou expirada.');return;}
   used=true;
   try{
    const {tokens}=await auth.getToken({code:u.searchParams.get('code'),codeVerifier:verifier});
    checkGrant(tokens);
    const encrypted=enc.w(tokens.refresh_token);tokens.refresh_token=undefined;tokens.access_token=undefined;
    const gmail=await native.qf(encrypted);guard(gmail.context._options.auth);
    const profile=await gmail.users.getProfile({userId:'me',fields:'emailAddress'},{timeout:10000,retry:false});
    assert(profile.data.emailAddress===EMAIL,'mailbox_identity_mismatch');
    assert(Date.now()<expires,'expired_or_used');
    await saveGrant(sql,encrypted,previous);
    console.log(JSON.stringify({event:'reconnected',mailbox:EMAIL,identity_verified:true,updated_rows:1,send_authorized:false}));
    res.end('Caixa reconectada para leitura. Pode voltar ao Codex. Nenhuma campanha ou envio foi ligado.');
   }catch(e){
    const safe=['refresh_missing','scope_mismatch','mailbox_identity_mismatch','mailbox_changed_concurrently','readback_failed','route_blocked'];
    console.log(JSON.stringify({event:'failed',code:safe.includes(e.message)?e.message:'oauth_exchange_failed'}));
    res.writeHead(400);res.end('Nao foi possivel concluir. Nenhuma campanha ou envio foi ligado. Volte ao Codex.');
   }finally{clearTimeout(timer);server.close(async()=>{await sql.end({timeout:2});resolve();});}
  });
  server.requestTimeout=15000;server.headersTimeout=10000;server.maxConnections=20;
  server.on('error',reject);
  server.listen(3000,'0.0.0.0',()=>console.log(JSON.stringify({event:'ready',auth_url:url,expires_minutes:10})));
  timer=setTimeout(()=>{server.closeAllConnections();server.close(async()=>{await sql.end({timeout:2});resolve();});},600000);
 });
}
module.exports={main,validate,guard,checkGrant,saveGrant};
