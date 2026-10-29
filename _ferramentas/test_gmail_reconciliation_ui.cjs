const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const html = fs.readFileSync(require('node:path').join(__dirname, 'painel_modelo.html'), 'utf8');
const source = html.split('/* THUNDER_GMAIL_RECONCILIATION_BEGIN */')[1].split('/* THUNDER_GMAIL_RECONCILIATION_END */')[0];
const context = {}; vm.runInNewContext(source, context);
const ui = context.ThunderGmailReconciliation;
const binding = {state:'available',cadence_id:'cad-1',step_id:'step-1',effect_id:'job-1'};
const preview = {...binding,recipient:'a@example.test',subject:'Teste',content:'Texto',receipt_ref:'gmail:abcd',review_hash:'a'.repeat(64)};
assert.equal(ui.validBinding(binding,{id:'cad-1'},{id:'step-1'}),true);
assert.equal(ui.validBinding(binding,{id:'outro'},{id:'step-1'}),false);
assert.equal(ui.validPreview(preview,binding),true);
for (const change of [{effect_id:'outro'}, {cadence_id:'outro'}, {step_id:'outro'}, {review_hash:'x'}, {receipt_ref:'gmail:INVALID'}]) {
  assert.equal(ui.validPreview({...preview,...change},binding),false);
}
const escape = x => String(x).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;');
const output = ui.render([{id:'cad-1',steps:[{id:'step-1',gmail_reconciliation:binding}]}],escape);
assert.match(output,/data-gmail-preview/);
assert.doesNotMatch(output,/data-gmail-confirm/);
assert.match(output,/nenhum envio será repetido/);
const blocked = ui.render([{id:'cad-1',steps:[{id:'step-1',gmail_reconciliation:{state:'blocked',reason:'ambiguous_effects'}}]}],escape);
assert.doesNotMatch(blocked,/data-gmail-preview/);
assert.match(blocked,/Não reenvie/);
async function exercise() {
  const calls=[]; let click;
  const doc={createTextNode:text=>({text}),createElement(tag){return {tag,children:[],disabled:false,attrs:{},appendChild(n){this.children.push(n)},setAttribute(k,v){this.attrs[k]=v},addEventListener(k,f){this[k]=f}}}};
  const review={children:[],replaceChildren(){this.children=[]},appendChild(n){this.children.push(n)}};
  const status={textContent:''};
  const card={dataset:{cadence:'cad-1',step:'step-1',effect:'job-1'},ownerDocument:doc,querySelector(s){return s.includes('review')?review:status}};
  const container={contains:n=>n===card,addEventListener:(name,fn)=>{click=fn}};
  ui.mount(container,async (url,method,body)=>{calls.push({url,body});return url.endsWith('preview')?preview:{status:'confirmed',receipt_ref:'gmail:abcd',send_attempted:false};});
  const button=(isPreview)=>({disabled:false,closest:s=>s.includes('data-gmail-reconcile')?card:null,hasAttribute:s=>isPreview&&s==='data-gmail-preview'});
  const first=button(true);await click({target:{closest:()=>first}});
  assert.equal(calls.length,1);assert.match(calls[0].url,/reconcile-preview$/);
  const checkbox=review.children.find(x=>x.tag==='label').children[0];
  const confirmation=review.children.find(x=>x.tag==='button');assert.equal(confirmation.disabled,true);
  checkbox.checked=true;checkbox.change();assert.equal(confirmation.disabled,false);
  confirmation.closest=s=>s.includes('data-gmail-reconcile')?card:null;confirmation.hasAttribute=()=>false;
  await click({target:{closest:()=>confirmation}});
  assert.equal(calls.length,2);assert.match(calls[1].url,/reconcile-confirm$/);
  assert.equal(calls[1].body.review_hash,preview.review_hash);
  assert.ok(calls.every(x=>!x.url.includes('send')));
  assert.match(status.textContent,/Recibo confirmado/);
}
exercise().then(()=>console.log('Gmail reconciliation UI contract: OK')).catch(e=>{console.error(e);process.exitCode=1});
