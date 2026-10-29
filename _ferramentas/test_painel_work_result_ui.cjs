// Synthetic rendering only: no server, browser session, credentials or network.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, 'painel_modelo.html'), 'utf8');
const start = html.indexOf('/* THUNDER_WORK_RESULT_BEGIN */');
const end = html.indexOf('/* THUNDER_WORK_RESULT_END */');
assert.ok(start >= 0 && end > start);

class Element {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ''; this.style = {}; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren() { this.children = []; }
  getAttribute() { return 'synthetic'; }
}
const doc = {createElement: tag => new Element(tag)};
const slot = new Element('slot');
const container = {ownerDocument: doc, querySelectorAll: () => [slot]};
const context = {URL};
vm.runInNewContext(html.slice(start, end), context);
const attack = '<img src=x onerror="window.pwned=1">';
const artifact = {
  recommendation: 'pesquisar mais',
  facts: [
    {statement: attack, source: 'https://example.test/source', observed_at: '2026-10-20', published_at: '2026-09-01', freshness: 'stale'},
    {statement: 'Outro fato', source: 'javascript:alert(1)', observed_at: '2026-10-21', published_at: null},
  ],
  research_gaps: [{description: attack, severity: 'high', blocks_hypothesis: true},
    {description: 'Fonte incompleta', severity: 'medium', blocks_hypothesis: false},
    {description: 'Sem classificação', severity: null, blocks_hypothesis: null}],
  counter_signal_assessment: {status:'signals_found',details:attack,fact_ids:['F1'],severity:'high',disposition:'defer',qualification_question:'A situação mudou?'},
  limitations: ['Fonte sintética indisponível', attack],
};
const snapshot = {work: [{id: 'synthetic', status: 'succeeded', result: {artifact}}]};
function all(node) { return [node, ...node.children.flatMap(all)]; }
function check() {
  const nodes = all(slot), content = nodes.map(n => n.textContent).join('\n');
  assert.match(content, /Publicado em: 2026-09-01/);
  assert.match(content, /Publicado em: data não informada/);
  assert.match(content, /Atualidade registrada: desatualizado/);
  assert.match(content, /Atualidade registrada: não informada/);
  assert.match(content, /contrassinais encontrados · Severidade: alta · Encaminhamento: adiar/);
  assert.ok(content.includes('Fonte sintética indisponível'));
  assert.ok(content.includes('A situação mudou?'));
  assert.match(content, /Severidade: alta · bloqueia a hipótese/);
  assert.match(content, /Severidade: média · não bloqueia a hipótese/);
  assert.match(content, /Severidade: não informada · bloqueio não informado/);
  assert.ok(content.includes(attack));
  assert.equal(nodes.filter(n => n.tag === 'img' || n.tag === 'script').length, 0);
  assert.deepEqual(nodes.filter(n => n.tag === 'a').map(n => n.href), ['https://example.test/source']);
}
context.ThunderWorkResult.render(container, snapshot);
check();
// A fresh render from the same serialized response simulates a UI reload; it is
// not a persistence or backend readback test.
context.ThunderWorkResult.render(container, JSON.parse(JSON.stringify(snapshot)));
check();
assert.equal(context.pwned, undefined);
console.log('ThunderWorkResult synthetic render and reload: OK');
