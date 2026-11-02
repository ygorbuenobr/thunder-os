'use strict';
// Synthetic bridge producer for the offline Python consumer integration test.
const {createBridge}=require('./bridge.cjs');
const {EXPECTED}=require('./contract.cjs');
require('node:net').Socket.prototype.connect=function(){throw Error('network forbidden');};
const KEY='a'.repeat(64);
const provider={async session(){return {
 async verifyProfile(){return EXPECTED.email;},
 async snapshotList({cursor}){return {messages:[{id:cursor?'bb22':'aa11',thread_id:cursor?'def456':'abc123'}],next_cursor:cursor?null:'next'};},
 async snapshotThread(id){const text=Buffer.from('Original synthetic message');return {id,historyId:'42',messages:[{
  id:'a'+id,threadId:id,internalDate:'1792746000000',labelIds:['INBOX'],payload:{mimeType:'text/plain',
   headers:[{name:'From',value:'buyer@example.invalid'},{name:'Subject',value:'Synthetic'}],body:{data:text.toString('base64url')}}}]};}
};}};
const bridge=createBridge({provider,credential:KEY,readOnly:true,snapshotEnabled:true});
bridge.handle({method:'GET',url:process.argv[2],authorization:'Bearer '+KEY}).then(value=>process.stdout.write(JSON.stringify(value))).catch(()=>process.exitCode=1);
