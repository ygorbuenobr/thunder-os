// Isolated review rendering and response validation; no network or live data.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, 'painel_modelo.html'), 'utf8');
const start = html.indexOf('function journeyReviewFieldsValid(');
const end = html.indexOf('function journeyInvalidateCadence(', start);
assert.ok(start >= 0 && end > start);
const target = {innerHTML: '', replaceChildren() { this.innerHTML = ''; }};
const edit = {binding: {cadence_id: 'cad-1', step_id: 'step-1', campaign_id: 'BR-TEST'}};
const container = {dataset: {journeyEditor: 'one'}, querySelector: () => target};
const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const context = {journeyEdits: new Map([['one', edit]]), journeySyncEditor() {}, esc,
  journeyRows: value => Array.isArray(value) ? value : [], journeyDescribe: String,
  journeyBR: value => typeof value === 'string' && value.startsWith('BR-')};
vm.runInNewContext(html.slice(start, end), context);
const review = {cadence_id:'cad-1',step_id:'step-1',expected_version:1,review_hash:'a'.repeat(64),context_hash:'b'.repeat(64),
  content:'Mensagem sintética',subject:'Assunto',recipient:'pessoa@example.invalid',channel:'email',order:1,interval_hours:24,
  can_approve:true,blockers:[],execution_confirmation:{confirmed:true,batch_id:'cad-1',batch_version:1,
    context:{platform:'thunder_os',campaign_id:'BR-TEST',campaign_version:'1',rules_version:'1'}}};
assert.equal(context.journeyValidateReview(review, edit), review);
edit.review = review; context.journeyRenderReview(container);
for (const text of ['Destinatário: pessoa@example.invalid', 'Canal: email', 'Ordem: 1', 'Intervalo: 24 horas', 'data-journey-approve']) assert.ok(target.innerHTML.includes(text), text);
for (const [field,value] of [['recipient',''],['recipient',null],['channel',''],['order',0],['order',1.5],['interval_hours',-1],['interval_hours','24'],['interval_hours',87601]]) {
  const invalid = {...review,[field]:value};
  assert.throws(() => context.journeyValidateReview(invalid, edit), /Revisão inválida/);
  edit.review = invalid; context.journeyRenderReview(container);
  assert.ok(!target.innerHTML.includes('data-journey-approve'), field);
  assert.ok(target.innerHTML.includes('Aprovação bloqueada'), field);
}
for (const field of ['recipient','channel','order','interval_hours']) {
  const invalid = {...review}; delete invalid[field];
  assert.throws(() => context.journeyValidateReview(invalid, edit), /Revisão inválida/);
}
edit.review = {...review, recipient:'<img src=x onerror=alert(1)>'};
context.journeyRenderReview(container);
assert.ok(target.innerHTML.includes('&lt;img src=x onerror=alert(1)&gt;'));
assert.ok(!target.innerHTML.includes('<img'));
console.log('Journey review synthetic validation and rendering: OK');
