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
