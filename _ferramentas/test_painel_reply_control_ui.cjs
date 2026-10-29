// Exercise the real alert renderer with synthetic responses and no network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, 'painel_modelo.html'), 'utf8');
const start = html.indexOf('async function carregarControleRespostas()');
const end = html.indexOf('async function abrirCadenciaContato(', start);
assert.ok(start >= 0 && end > start);
const scriptStart = html.indexOf("const MODO = /*MODO*/'artifact'");
const escapeStart = html.indexOf('const esc = ', scriptStart);
const escapeEnd = html.indexOf('\n', escapeStart);
assert.ok(scriptStart >= 0 && escapeStart > scriptStart && escapeEnd > escapeStart && escapeEnd < start);
const target = {innerHTML:'', textContent:''};
let response = {};
const context = {$: () => target, apiServidor: async () => response};
vm.runInNewContext(html.slice(escapeStart, escapeEnd) + '\n' + html.slice(start, end), context);
async function run() {
  response = {external_pause_enabled:false, alerts:[{title:'Resposta sintética',reason:'external_pause_off',
    hubspot_contact_id:'123',campaign_id:'BR-TEST',content:'Resposta de teste',
    next_action:'<img src=x onerror=alert(1)> revisar'}]};
  await context.carregarControleRespostas();
  assert.ok(target.innerHTML.includes('Contato 123'));
  assert.ok(target.innerHTML.includes('Próxima ação:'));
  assert.ok(target.innerHTML.includes('&lt;img src=x onerror=alert(1)&gt; revisar'));
  assert.ok(!target.innerHTML.includes('<img'));
  assert.ok(target.innerHTML.includes('Pausas externas: desligadas'));
  for (const value of [undefined, null, '', '  ', {text:'invalid'}]) {
    response.alerts[0].next_action = value;
    await context.carregarControleRespostas();
    assert.ok(target.innerHTML.includes('Revisar a resposta e decidir o próximo passo.'));
  }
  response = {external_pause_enabled:false, alerts:[]};
  await context.carregarControleRespostas();
  assert.ok(target.innerHTML.includes('Isso não comprova ausência de respostas.'));
  console.log('Reply control synthetic alert rendering: OK');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
