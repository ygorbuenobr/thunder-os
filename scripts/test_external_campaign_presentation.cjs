// Offline source-function regression; integration and provider behavior require separate tests.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync(path.join(__dirname, '../_ferramentas/painel_modelo.html'), 'utf8');
const start = html.indexOf('function cadenciaHTML(cadencia)');
const end = html.indexOf('\nfunction variantesHTML', start);
assert(start >= 0 && end > start);
const ctx = {esc: value => String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('"','&quot;'), estadoEfeitoHumano: value => value};
vm.createContext(ctx);
vm.runInContext(html.slice(start, end), ctx);
const cadence = {id:'fixture', version:1, status:'ready_for_review', review_hash:'a'.repeat(64),
  execution_confirmation:{confirmed:true}, execution_question:'Identifique a plataforma',
  steps:[{channel:'email',order:1,interval_hours:0,content:'Teste sintético'}]};
for (const platform of ['hubspot_seq','apollo','instantly','chattie','codex_local']) {
  const rendered = ctx.cadenciaHTML({...cadence, execution_context:{platform}});
  assert.match(rendered, /plataforma externa/);
  assert.match(rendered, /aprovação e a execução por este cartão não estão disponíveis/);
  assert.match(rendered, /Consultar o fluxo da plataforma antes de decidir/);
  assert.doesNotMatch(rendered, /data-os-approve=|Aprovar a sequência completa|Identifique a plataforma/);
}
const internal = ctx.cadenciaHTML({...cadence, execution_context:{platform:'thunder_os'}});
assert.match(internal, /data-os-approve=/);
assert.match(internal, /Identifique a plataforma/);
assert.doesNotMatch(internal, /plataforma externa/);
const missing = ctx.cadenciaHTML({...cadence, execution_confirmation:null});
assert.doesNotMatch(missing, /data-os-approve=/);
for (const platform of [undefined, 'unrecognized']) {
  const rendered = ctx.cadenciaHTML({...cadence, execution_context:{platform}});
  assert.match(rendered, /plataforma desta campanha não foi identificada/);
  assert.doesNotMatch(rendered, /data-os-approve=|Aprovar a sequência completa/);
}
Object.assign(ctx, {sujo:{}, variantesEscolhidas:{}, confirmar:{}, conversa:{}, MODO:'servidor', dono:true,
  EXECUTOR:false, TIPO:{}, ST:{}, tituloItem:()=> 'Teste', verConta:()=>'', pesquisaHTML:()=>'',
  anguloChipHTML:()=>'', variantesHTML:()=>'', quando:()=>''});
const cardsStart=html.indexOf('function briefingCardHTML(');
const cardsEnd=html.indexOf('\nfunction render()', cardsStart);
assert(cardsStart >= 0 && cardsEnd > cardsStart);
vm.runInContext(html.slice(cardsStart,cardsEnd),ctx);
for (const platform of ['hubspot_seq', undefined, 'unrecognized']) {
  for (const tipo of ['dm','comentario','whatsapp','decisao']) for (const status of ['pendente','erro','aguardando-confirmacao']) {
    const item={id:'fixture',tipo,status,texto:'Mensagem',opcoes:[{id:'sim',rotulo:'Aprovar'}],cadencia:{...cadence,execution_context:{platform}}};
    assert.doesNotMatch(ctx.cardHTML(item), /data-a="(?:aprovar|opcao|whatsapp(?:-feito)?)"|data-os-approve=/);
    assert.doesNotMatch(ctx.todayCardHTML(item), /data-today-a="(?:aprovar|opcao)"|data-os-approve=/);
  }
}
const internalItem={id:'fixture',tipo:'dm',status:'pendente',texto:'Mensagem',cadencia:{...cadence,execution_context:{platform:'thunder_os'}}};
assert.doesNotMatch(ctx.cardHTML(internalItem), /data-a="aprovar"/);
assert.doesNotMatch(ctx.todayCardHTML(internalItem), /data-today-a="aprovar"/);
assert.doesNotMatch(ctx.cardHTML(internalItem), /data-os-approve=/);
assert.doesNotMatch(ctx.todayCardHTML(internalItem), /data-os-approve=/);
assert.match(ctx.cardHTML(internalItem), /Esta visualização não aprova cadências/);
assert.match(ctx.todayCardHTML(internalItem), /Consulte o andamento no OS/);
for (const cadencia of [undefined,{...cadence,execution_context:{platform:'thunder_os'}}]) {
  const rendered=ctx.cardHTML({...internalItem,tipo:'whatsapp',cadencia});
  if(cadencia)assert.doesNotMatch(rendered, /data-a="whatsapp(?:-feito)?"/);
  else { assert.match(rendered, /data-a="whatsapp"/); assert.match(rendered, /data-a="whatsapp-feito"/); }
  const decision={...internalItem,tipo:'decisao',cadencia,opcoes:[{id:'sim',rotulo:'Aprovar'}]};
  if(cadencia) {
    assert.doesNotMatch(ctx.cardHTML(decision), /data-a="opcao"/);
    assert.doesNotMatch(ctx.todayCardHTML(decision), /data-today-a="opcao"/);
  } else {
    assert.match(ctx.cardHTML(decision), /data-a="opcao"/);
    assert.match(ctx.todayCardHTML(decision), /data-today-a="opcao"/);
  }
}
for (const cadencia of [undefined,null]) {
  const ordinary={...internalItem,cadencia};
  assert.match(ctx.cardHTML(ordinary), /data-a="aprovar"/);
  assert.match(ctx.todayCardHTML(ordinary), /data-today-a="aprovar"/);
}
for (const cadencia of [false,0,'']) for (const tipo of ['dm','whatsapp','decisao']) {
  const item={...internalItem,tipo,cadencia,opcoes:[{id:'sim',rotulo:'Aprovar'}]};
  assert.doesNotMatch(ctx.cardHTML(item), /data-a="(?:aprovar|opcao|whatsapp(?:-feito)?)"|data-os-approve=/);
  assert.doesNotMatch(ctx.todayCardHTML(item), /data-today-a="(?:aprovar|opcao)"|data-os-approve=/);
}
assert.doesNotMatch(ctx.briefingCardHTML({...internalItem,origem:'briefing'}), /data-a="aprovar"|data-os-approve=/);
console.log('PASS: external and unknown presentation across cards, internal compatibility');
