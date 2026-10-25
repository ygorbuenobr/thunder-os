'use strict';
// Prepared-only patch over seqd_bridge source b6c3dd6aefe0247b50156b6dd9aeb818cd332ea3.
// Read-only by default; scoped send requires explicit pinned policy. Importing starts nothing.
const http=require('node:http'),fs=require('node:fs');
const {createBridge}=require('./bridge.cjs');
const {EXPECTED,BridgeError,check,hash}=require('./contract.cjs');
function serverFor(bridge){
 return http.createServer(async(req,res)=>{
  res.setHeader('Content-Type','application/json; charset=utf-8');res.setHeader('Cache-Control','no-store');
  let total=0,chunks=[];
  try{
   for await(const chunk of req){total+=chunk.length;if(total>100000)throw new BridgeError('request_too_large',413);chunks.push(chunk);}
   let body;if(total){try{body=JSON.parse(Buffer.concat(chunks).toString('utf8'));}catch{throw new BridgeError('invalid_json');}}
   const result=await bridge.handle({method:req.method,url:req.url,authorization:req.headers.authorization,body});
   res.writeHead(200);res.end(JSON.stringify(result));
  }catch(error){res.writeHead(error instanceof BridgeError?error.status:503);res.end(JSON.stringify({mailbox:EXPECTED.email,status:'rejected',error_code:error instanceof BridgeError?error.code:'bridge_unavailable'}));}
 });
}
function createReadOnlyBridge(provider,credential){
 return createBridge({provider,credential,readOnly:true});
}
function createScopedBridge({readProvider,writeProvider,credential,journalDir,policyLoader,now=()=>Date.now()}){
 const authority=(body,tick)=>{
  const p=policyLoader();
  check(p&&Object.keys(p).sort().join(',')===['approval_ref','mailbox','to','subject','body_hash','idempotency_key','issued_at','expires_at','max_attempts'].sort().join(','),'scoped_policy_invalid');
  check(typeof p.approval_ref==='string'&&p.approval_ref.length>0&&p.mailbox===EXPECTED.email&&p.max_attempts===1&&
   Number.isSafeInteger(p.issued_at)&&Number.isSafeInteger(p.expires_at)&&p.issued_at<=tick&&tick<p.expires_at&&p.expires_at-p.issued_at<=900000&&
   p.to===body.to&&p.subject===body.subject&&p.body_hash===hash(body.body)&&p.idempotency_key===body.idempotency_key&&body.thread_id==null,'scoped_policy_mismatch');
  return true;
 };
 const read=createReadOnlyBridge(readProvider,credential);
 const write=createBridge({provider:writeProvider,credential,journalDir,sendEnabled:true,sendAuthority:authority,now});
 return {journal:write.journal,handle:request=>request.method==='GET'?read.handle(request):write.handle(request)};
}
function pinnedSendPolicy(){
 const path=process.env.THUNDER_BRIDGE_SEND_POLICY_FILE;
 check(typeof path==='string'&&require('node:path').isAbsolute(path),'scoped_policy_unavailable',503);
 const stat=fs.lstatSync(path);check(stat.isFile()&&!stat.isSymbolicLink()&&stat.size<=16384,'scoped_policy_unavailable',503);
 const raw=fs.readFileSync(path);check(require('node:crypto').createHash('sha256').update(raw).digest('hex')===process.env.THUNDER_BRIDGE_SEND_POLICY_SHA256,'scoped_policy_changed',503);
 return JSON.parse(raw.toString('utf8'));
}
async function main(){
 // Logs from native dependencies must not expose tokens, bodies or request errors.
 for(const method of ['log','error','warn','info','debug'])console[method]=()=>{};
 const credential=fs.readFileSync('/run/secrets/thunder_bridge','utf8').trim();
 const {createNativeProvider}=require('./native.cjs');
 const provider=await createNativeProvider({readOnly:true});
 let bridge=createReadOnlyBridge(provider,credential);
 if(process.env.THUNDER_BRIDGE_SCOPED_SEND==='true'){
  const journalDir=process.env.THUNDER_BRIDGE_JOURNAL_DIR;
  check(typeof journalDir==='string'&&require('node:path').isAbsolute(journalDir)&&fs.lstatSync(journalDir).isDirectory(),'scoped_journal_required',503);
  pinnedSendPolicy();
  const writeProvider=await createNativeProvider({readOnly:false});
  bridge=createScopedBridge({readProvider:provider,writeProvider,credential,journalDir,policyLoader:pinnedSendPolicy});
 }
 if(bridge.journal)await bridge.journal.ready;
 const server=serverFor(bridge);server.requestTimeout=35000;server.headersTimeout=10000;server.timeout=35000;
 // Internal container bind; deployment publishes only host loopback 127.0.0.1:8792.
 server.listen(3001,'0.0.0.0');
 const stop=()=>server.close(()=>process.exit(0));process.on('SIGTERM',stop);process.on('SIGINT',stop);
}
if(require.main===module)main().catch(()=>{process.stderr.write('seqd_bridge_bootstrap_failed\n');process.exitCode=1;});
module.exports={serverFor,createReadOnlyBridge,createScopedBridge};
