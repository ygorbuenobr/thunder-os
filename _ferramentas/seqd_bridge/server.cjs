'use strict';
// Prepared-only patch over seqd_bridge source b6c3dd6aefe0247b50156b6dd9aeb818cd332ea3.
// This entrypoint forces read-only; importing it never installs or starts a service.
const http=require('node:http'),fs=require('node:fs');
const {createBridge}=require('./bridge.cjs');
const {EXPECTED,BridgeError}=require('./contract.cjs');
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
async function main(){
 // Logs from native dependencies must not expose tokens, bodies or request errors.
 for(const method of ['log','error','warn','info','debug'])console[method]=()=>{};
 const credential=fs.readFileSync('/run/secrets/thunder_bridge','utf8').trim();
 const {createNativeProvider}=require('./native.cjs');
 const provider=await createNativeProvider({readOnly:true});
 const bridge=createReadOnlyBridge(provider,credential);
 if(bridge.journal)await bridge.journal.ready;
 const server=serverFor(bridge);server.requestTimeout=35000;server.headersTimeout=10000;server.timeout=35000;
 // Internal container bind; deployment publishes only host loopback 127.0.0.1:8792.
 server.listen(3001,'0.0.0.0');
 const stop=()=>server.close(()=>process.exit(0));process.on('SIGTERM',stop);process.on('SIGINT',stop);
}
if(require.main===module)main().catch(()=>{process.stderr.write('seqd_bridge_bootstrap_failed\n');process.exitCode=1;});
module.exports={serverFor,createReadOnlyBridge};
