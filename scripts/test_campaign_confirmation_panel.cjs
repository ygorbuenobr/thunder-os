// Offline source-function test, not a browser or production HTTP test.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const path = require('node:path');
const html = fs.readFileSync(path.join(__dirname, '../_ferramentas/painel_modelo.html'), 'utf8');
const start = html.indexOf("$('thunder-os-readback').addEventListener('click',async ev=>{\n  const botao=ev.target.closest('[data-os-approve]')");
assert(start >= 0);
const end = html.indexOf('\n});', start) + 4;
let handler, allow = false;
const calls = [], status = {};
const sandbox = {thunderOSApproving: false, window: {confirm: () => allow},
  $: id => id === 'thunder-os-readback' ? {addEventListener: (_, fn) => handler = fn} : status,
  apiServidor: async (...args) => calls.push(args),
  esc: value => String(value).replaceAll('&','&amp;').replaceAll('"','&quot;').replaceAll('<','&lt;'),
  estadoEfeitoHumano: value => value};
vm.createContext(sandbox);
vm.runInContext(html.slice(start, end), sandbox);
const renderStart = html.indexOf('function cadenciaHTML(cadencia)');
const renderEnd = html.indexOf('\nfunction variantesHTML', renderStart);
vm.runInContext(html.slice(renderStart,renderEnd), sandbox);
const confirmation = {confirmed:true,context:{platform:'thunder_os',campaign_id:'synthetic'},batch_id:'b'};
const button = {disabled:false,dataset:{osApprove:'b',osVersion:'1',osReviewHash:'h',osQuestion:'Confirmar?',osConfirmation:JSON.stringify(confirmation)}};
(async()=>{
  await handler({target:{closest:()=>button}});
  assert.equal(calls.length,0,'cancel must not POST');
  allow=true;
  await handler({target:{closest:()=>button}});
  assert.equal(calls.length,1);
  assert.equal(JSON.stringify(calls[0][2].execution_confirmation), JSON.stringify(confirmation));
  const draft = {id:'b',version:1,review_hash:'h',status:'approved',steps:[{channel:'email',content:'offline',order:1,interval_hours:0}]};
  assert(!sandbox.cadenciaHTML(draft).includes('data-os-approve='),'legacy must not render execution button');
  assert(sandbox.cadenciaHTML({...draft,execution_confirmation:confirmation,execution_question:'Confirmar?'}).includes('data-os-approve='),'approved batch must permit fresh resume confirmation');
  console.log('PASS: cancel, explicit payload, legacy blocked, resume confirmation UI source');
})().catch(error=>{console.error(error);process.exitCode=1});

(async()=>{
  const start=html.indexOf('async function confirmCampaignOperation(');
  const stop=html.indexOf('\n}',start)+2;
  const calls=[];
  const request={context:{platform:'thunder_os',campaign_id:'offline',campaign_version:'v1',rules_version:'r1',rules_hash:'a'.repeat(64),configuration_hash:'b'.repeat(64)},batch_id:'seqd:fixture',batch_version:1,scope_hash:'c'.repeat(64)};
  const sandbox={window:{prompt:()=>null,confirm:()=>true,alert:()=>{}},api:async(...args)=>{calls.push(args);return request;}};
  vm.createContext(sandbox);vm.runInContext(html.slice(start,stop),sandbox);
  assert.equal(await sandbox.confirmCampaignOperation(sandbox.api,'/integration',{id:'fixture',version:1}),null);
  assert.equal(calls.length,0);
  sandbox.window.prompt=()=> 'offline';sandbox.window.confirm=q=>{assert(q.includes('Thunder OS'));assert(q.includes('Regras: r1'));return false;};
  assert.equal(await sandbox.confirmCampaignOperation(sandbox.api,'/integration',{id:'fixture',version:1}),null);
  sandbox.window.confirm=()=>true;
  assert.equal(await sandbox.confirmCampaignOperation(sandbox.api,'/integration',{id:'fixture',version:1}),request);
  assert(calls.every(c=>c[0].endsWith('/campaign-review')));
  console.log('PASS: integration campaign review, cancel and exact confirmation (source harness)');
})().catch(error=>{console.error(error);process.exitCode=1;});
