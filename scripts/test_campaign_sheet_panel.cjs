// Offline source/DOM harness. No network, browser, backend or provider execution.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const html = fs.readFileSync(path.join(__dirname, '../_ferramentas/painel_modelo.html'), 'utf8');
function sourceBetween(start, end) {
  const from = html.indexOf(start), to = html.indexOf(end, from + start.length);
  assert(from >= 0 && to > from, `Missing source boundary: ${start}`);
  return html.slice(from, to);
}
const keys = ['offer', 'icp', 'proofs', 'objective', 'restrictions', 'cta', 'language', 'country'];
const labels = ['Oferta', 'ICP', 'Provas', 'Objetivo', 'Restrições', 'CTA', 'Idioma', 'País'];
const expectedHash = 'a'.repeat(64), observedHash = 'b'.repeat(64);
const record = {
  campaign_id: 'SYNTHETIC', status: 'complete', version: 'v1', fingerprint: 'c'.repeat(64), missing_fields: [],
  fields: keys.map((key, index) => ({key, label: labels[index], status: 'ok',
    path: `synthetic/${key}.md`, expected_sha256: expectedHash, observed_sha256: expectedHash,
    source_updated_at: '2026-10-23T10:00:00Z', content: `${labels[index]} sintética.\n\nSegundo parágrafo.`}))
};
const envelope = campaigns => ({schema_version: 1, read_only: true, campaigns});
const nodes = new Map();
const getNode = id => {
  if (!nodes.has(id)) nodes.set(id, {innerHTML: '', textContent: '', dataset: {}, attributes: {}, disabled: false,
    setAttribute(key, value) { this.attributes[key] = value; },
    querySelectorAll() { return []; }, querySelector() { return null; }});
  return nodes.get(id);
};
const target = getNode('sheet-target');
target.dataset.cadSheet = 'SYNTHETIC';
getNode('cad-read-campaigns').querySelectorAll = selector => {
  assert.equal(selector, '[data-cad-sheet]');
  return [target];
};
const calls = [], timers = [];
let response = envelope([record]);
const cadenceSnapshot = {campaigns: [
  {id: 'SYNTHETIC', tasks: [{title: 'Tarefa preservada', outcome: 'sent_with_receipt', receipt: {id: 'receipt:synthetic'}}]},
  {id: 'UNREGISTERED', tasks: []}
], coverage: {complete: false}, today: {}};
const ctx = {
  $: getNode,
  document: {hidden: false, body: {dataset: {aba: 'cadencias'}}},
  apiServidor: async (...args) => {
    calls.push(args);
    if (args[0] === '/api/os/v1/campaign-sheets') {
      if (response instanceof Error) throw response;
      return response;
    }
    if (args[0] === '/api/os/v1/cadencias') return cadenceSnapshot;
    throw new Error(`Unexpected offline API: ${args[0]}`);
  },
  setInterval: (callback, delay) => { timers.push({callback, delay}); return timers.length; },
  clearInterval: () => {}, localStorage: {setItem() {}}, location: {hash: '#cadencias'},
  history: {pushState() {}}, atualizarAgenttrail() {}, carregarContatosHermes() {}
};
for (const name of ['carregarIntegracoesEmail', 'carregarInstantlyReader', 'carregarControleRespostas',
  'carregarPreparacaoLinkedin', 'carregarFontesCampanhas', 'carregarPlacarAprendizados']) ctx[name] = async () => {};
vm.createContext(ctx);
vm.runInContext(sourceBetween('const esc = s =>', '\n'), ctx);
vm.runInContext(sourceBetween('let cadReadLoading=false', 'async function carregarIntegracoesEmail()'), ctx);
vm.runInContext(sourceBetween('async function atualizarCadencias()', 'async function carregarPlacarAprendizados()'), ctx);
vm.runInContext(sourceBetween('function mostrarAba(aba,', 'function iniciarAbas()'), ctx);
vm.runInContext(sourceBetween('function estadoEfeitoHumano(status)', 'function variantesHTML(it)'), ctx);
const render = id => ctx.cadCampaignSheetHTML(id);
const noWrites = markup => assert.doesNotMatch(markup, /<(?:button|input|textarea|form|select|a)\b|data-os-approve=/i);

(async () => {
  assert.match(render('SYNTHETIC'), /Carregando ficha/);
  assert.equal((render('SYNTHETIC').match(/<summary>/g) || []).length, 16);
  noWrites(render('SYNTHETIC'));

  await ctx.carregarFichasCampanhas();
  const complete = target.innerHTML;
  assert.match(complete, /Ficha da campanha/);
  assert.match(complete, /Ficha completa/);
  assert.doesNotMatch(complete, /ficha incompleta|não verificada/);
  assert.equal(target.attributes['aria-busy'], 'false');
  for (let i = 0; i < keys.length; i++) {
    assert(complete.includes(`<summary>${labels[i]}<span`));
    assert(complete.includes(`synthetic/${keys[i]}.md`));
    assert(complete.includes(`${labels[i]} sintética.\n\nSegundo parágrafo.`));
  }
  assert.match(complete, /23\/10\/2026/);
  assert(complete.includes(expectedHash));
  assert(complete.includes('Identificador da ficha'));
  assert.match(html, /\.cad-sheet pre\{white-space:pre-wrap;overflow-wrap:anywhere/);
  noWrites(complete);
  console.log('PASS complete: 8 fields, paragraphs, paths, dates, full hashes, read-only DOM');

  const failures = ['missing', 'missing_file', 'hash_mismatch', 'section_missing', 'empty', 'unavailable', 'unknown'];
  const failureLabels = ['Referência ausente', 'Arquivo ausente', 'Hash divergente', 'Seção ausente', 'Conteúdo vazio', 'Fonte indisponível', 'Fonte não verificada'];
  for (let i = 0; i < failures.length; i++) {
    response = envelope([{...record, status: 'incomplete', missing_fields: ['offer'],
      fields: record.fields.map(field => field.key === 'offer' ? {...field, status: failures[i], observed_sha256: observedHash} : field)}]);
    await ctx.carregarFichasCampanhas();
    assert.match(target.innerHTML, /ficha incompleta/);
    assert.match(target.innerHTML, /Itens pendentes: Oferta/);
    assert(target.innerHTML.includes(failureLabels[i]));
    assert.match(target.innerHTML, /Referência atual não verificada/);
    assert(target.innerHTML.includes(observedHash));
    assert.doesNotMatch(target.innerHTML, /Ficha completa/);
    noWrites(target.innerHTML);
  }
  response = envelope([{...record, fields: record.fields.slice(1)}]);
  await ctx.carregarFichasCampanhas();
  assert.match(target.innerHTML, /ficha incompleta/);
  assert.match(target.innerHTML, /Itens pendentes: Oferta/);
  console.log('PASS incomplete: every reference failure, omitted field, missing items; never verified');

  response = envelope([]);
  await ctx.carregarFichasCampanhas();
  assert.match(target.innerHTML, /ficha incompleta/);
  assert.match(target.innerHTML, /Fonte da ficha ausente: campanha sem registro/);
  assert.doesNotMatch(target.innerHTML, /synthetic\/offer|Ficha completa/);
  assert.equal((target.innerHTML.match(/Referência não informada/g) || []).length, 8);
  noWrites(target.innerHTML);

  const attack = '"><img src=x onerror=alert(1)><script>alert(2)</script>&';
  response = envelope([{...record, version: attack, fingerprint: attack, missing_fields: [attack],
    fields: record.fields.map(field => ({...field, path: attack, content: attack + '\n\nPlain text',
      expected_sha256: attack, observed_sha256: attack, source_updated_at: attack}))}]);
  await ctx.carregarFichasCampanhas();
  const malicious = target.innerHTML;
  assert.doesNotMatch(malicious, /<img|<script|<iframe|<svg/i);
  assert.match(malicious, /&quot;&gt;&lt;img/);
  assert.match(malicious, /&amp;\n\nPlain text/);
  assert.match(malicious, /Data não disponível/);
  noWrites(malicious);
  console.log('PASS missing record/empty source and malicious content, metadata and missing labels escaped');

  response = envelope([record]);
  await ctx.carregarFichasCampanhas();
  let release;
  response = new Promise(resolve => { release = resolve; });
  const pending = ctx.carregarFichasCampanhas();
  assert.equal(target.attributes['aria-busy'], 'true');
  assert.match(target.innerHTML, /Carregando ficha/);
  assert.doesNotMatch(target.innerHTML, /Ficha completa|synthetic\/offer/);
  release(envelope([record]));
  await pending;
  assert.match(target.innerHTML, /Ficha completa/);
  response = new Error('synthetic offline failure');
  await ctx.carregarFichasCampanhas();
  assert.equal(target.attributes['aria-busy'], 'false');
  assert.match(target.innerHTML, /Não foi possível carregar a ficha.*Use Atualizar/);
  assert.doesNotMatch(target.innerHTML, /Ficha completa|synthetic\/offer/);
  for (const invalid of [{schema_version: 2, read_only: true, campaigns: [record]},
    {schema_version: 1, read_only: false, campaigns: [record]}, {schema_version: 1, read_only: true}]) {
    response = invalid;
    await ctx.carregarFichasCampanhas();
    assert.match(target.innerHTML, /Não foi possível carregar a ficha/);
    assert.doesNotMatch(target.innerHTML, /Ficha completa/);
  }
  response = envelope([record]);
  await ctx.atualizarCadencias();
  assert.match(getNode('cad-read-campaigns').innerHTML, /Ficha completa/);
  assert.match(getNode('cad-read-campaigns').innerHTML, /Tarefa preservada/);
  assert.match(getNode('cad-read-campaigns').innerHTML, /receipt:synthetic/);
  assert.match(getNode('cad-read-campaigns').innerHTML, /Fonte da ficha ausente/);
  assert.equal((getNode('cad-read-campaigns').innerHTML.match(/data-cad-sheet=/g) || []).length, 2);
  console.log('PASS loading/error revoke complete snapshot; malformed envelope fails visibly; refresh recovers and preserves tasks/receipts');

  let releaseRefresh;
  response = new Promise(resolve => { releaseRefresh = resolve; });
  const refreshing = ctx.atualizarCadencias();
  const before = calls.length;
  await ctx.atualizarCadencias();
  assert.equal(calls.length, before, 'overlapping refresh must not duplicate requests');
  releaseRefresh(envelope([record]));
  await refreshing;
  response = envelope([record]);
  ctx.mostrarAba('cadencias', {registrar: false});
  await new Promise(resolve => setImmediate(resolve));
  const poll = timers.find(timer => timer.delay === 60000);
  assert(poll, 'existing Cadências timer must poll at 60s');
  const sheetCalls = () => calls.filter(call => call[0] === '/api/os/v1/campaign-sheets').length;
  const count = sheetCalls();
  poll.callback();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sheetCalls(), count + 1);
  ctx.document.hidden = true;
  poll.callback();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sheetCalls(), count + 1);
  ctx.document.hidden = false;
  ctx.document.body.dataset.aba = 'hoje';
  poll.callback();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(sheetCalls(), count + 1);
  assert(calls.every(call => call.length === 1), 'sheet refresh uses only default GET through apiServidor');
  console.log('PASS manual refresh + 60s active-tab GET polling; hidden/off-tab/overlap do not request again');

  const cadence = {id: 'synthetic', version: 1, review_hash: 'h', status: 'approved',
    execution_confirmation: {confirmed: true}, execution_question: 'Synthetic confirmation?',
    steps: [{channel: 'email', content: 'Preserved step', order: 1, interval_hours: 0, receipt_ref: 'receipt:kept'}]};
  assert.match(ctx.cadenciaHTML(cadence), /data-os-approve=/, 'legacy optional fields retain existing behavior');
  assert.match(ctx.cadenciaHTML({...cadence, campaign_sheet_status: 'complete', campaign_sheet_label: 'Ficha completa'}), /data-os-approve=/);
  for (const [status, label] of [['incomplete', 'ficha incompleta'], ['stale', 'versão anterior']]) {
    const output = ctx.cadenciaHTML({...cadence, campaign_sheet_status: status,
      campaign_sheet_version: attack, campaign_sheet_label: label + ' ' + attack});
    assert(output.includes(label));
    assert.doesNotMatch(output, /data-os-approve=|<img|<script/);
    assert.match(output, /Revisar a ficha da campanha antes de confirmar/);
    assert.match(output, /Preserved step/);
    assert.match(output, /receipt:kept/);
    assert(ctx.cadenciaHTML({...cadence, campaign_sheet_status: status}).includes(label));
  }
  for (const script of html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)) new vm.Script(script[1]);
  console.log('PASS approval: incomplete/stale hide confirmation, backend labels escaped, steps/receipts retained; inline scripts parse');
})().catch(error => { console.error(error); process.exitCode = 1; });
