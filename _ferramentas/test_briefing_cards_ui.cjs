const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const html=fs.readFileSync(process.argv[2]||path.join(__dirname,'painel_modelo.html'),'utf8');
const source=html.slice(html.indexOf('function briefingCardHTML('),html.indexOf('function cardHTML('));
const context={esc:s=>String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))};
vm.createContext(context);vm.runInContext(source,context);
const item={id:'briefing-fixture',origem:'briefing',status:'pendente',titulo:'Contato · Empresa',porque:'Pediu uma conversa.',contexto:[{quem:'Próximo passo',texto:'Revisar o rascunho.'}],rascunho:'Podemos conversar?',fatos:['Fonte sintética']};
test('summary and full context are separate and collapsed',()=>{
 const result=context.briefingCardHTML(item);
 assert.match(result,/Pediu uma conversa/);assert.match(result,/<details><summary>Contexto e próximo passo/);
 assert.match(result,/<details><summary>Revisar rascunho/);assert.match(result,/<details><summary>Fontes e evidências/);
 assert.doesNotMatch(result,/<details\s+open/);
});
test('untrusted content is escaped in every field',()=>{
 const x='<img src=x onerror=alert(1)>';
 const result=context.briefingCardHTML({...item,titulo:x,porque:x,rascunho:x,contexto:[{quem:x,quando:x,texto:x}],fatos:[x]});
 assert.doesNotMatch(result,/<img/);assert.equal((result.match(/&lt;img/g)||[]).length,7);
});
test('briefing cannot render send or approval controls even with injected action fields',()=>{
 const result=context.briefingCardHTML({...item,tipo:'dm',acao_aprovada:'enviar',cadencia:{status:'approved'},opcoes:[{id:'aprovar'}]});
 assert.doesNotMatch(result,/data-os-approve|data-a="aprovar"|data-today-a="aprovar"/);
 assert.equal((result.match(/<button/g)||[]).length,1);
 assert.match(result,/data-a="descartar"/);
 assert.match(context.briefingCardHTML(item,true),/data-today-a="descartar"/);
 assert.doesNotMatch(context.briefingCardHTML({...item,status:'descartado'}),/<button/);
});
test('briefings appear above operational work and count as pending',()=>{
 assert.ok(html.indexOf('id="hoje-briefing"')<html.indexOf('id="hoje-pedido"'));
 assert.match(html,/hojeContagens.aprovacoes=aprovFechadas.length\+briefings.length/);
 assert.match(html,/briefings.slice\(0,3\).map\(todayCardHTML\)/);
});
test('today and full views share record but unique anchors',()=>{
 assert.match(context.briefingCardHTML(item),/id="briefing-fixture" data-id="briefing-fixture"/);
 assert.match(context.briefingCardHTML(item,true),/id="hoje-briefing-fixture" data-today-id="briefing-fixture"/);
 assert.match(html,/function cardHTML\(it\)\{\s*if\(it.origem==='briefing'\) return briefingCardHTML\(it\);/);
 assert.match(html,/function todayCardHTML\(it\)\{\s*if\(it.origem==='briefing'\) return briefingCardHTML\(it,true\);/);
});
