// Offline JS contract tests. Browser QA is reported separately.
const fs=require('fs'),vm=require('vm'),assert=require('assert/strict');
const html=fs.readFileSync('_ferramentas/painel_modelo.html','utf8');
const source=html.split('/* PANEL_DISCARD_BEGIN */')[1].split('/* PANEL_DISCARD_END */')[0];
const ctx={};vm.createContext(ctx);vm.runInContext(source+';this.discard=discardAndRead;this.selectors=legacyBlockedSelectors;',ctx);
(async()=>{
 const calls=[];let rows=[{id:'a',status:'pendente'}];
 const api=async(path,method,body)=>{calls.push([path,method,body]);return method==='POST'?{}:[{id:'a',status:'descartado'}]};
 rows=await ctx.discard(api,'a');assert.equal(rows.filter(x=>x.status==='pendente').length,0);assert.equal(calls[0][0],'/api/os/v1/items/a/discard');assert.equal(calls[1][0],'/api/os/v1/items');console.log('PASS readback success');
 rows=[{id:'a',status:'pendente'}];await assert.rejects(async()=>{rows=await ctx.discard(async(p,m)=>{if(m!=='POST')throw Error('offline');return{}},'a')},/leitura de confirmação falhou/);assert.equal(rows[0].status,'pendente');console.log('PASS readback failure');
 await assert.rejects(()=>ctx.discard(async()=>[], 'a'),/ainda não confirmado/);
 for(const action of ['aprovar','opcao','variante','falar','refazer','perguntar','whatsapp','whatsapp-feito','feito','reuniao'])assert.ok(ctx.selectors.includes(`[data-a="${action}"]`));
 for(const selector of ['[data-classificacao]','[data-sugestao]','#pedido button[type="submit"]'])assert.ok(ctx.selectors.includes(selector));
 assert.ok(!ctx.selectors.includes('[data-a="descartar"]'));
 const buttons=['aprovar','opcao','variante','falar','refazer','perguntar','whatsapp','whatsapp-feito','feito','reuniao'].map(a=>({dataset:{a},textContent:a}));
 buttons.push({dataset:{classificacao:'positiva'},textContent:'Classificar'},{dataset:{sugestao:'testando'},textContent:'Testar'},{dataset:{},textContent:'Enviar pedido',pedido:true});
 const discard={dataset:{a:'descartar'},textContent:'Descartar',card:{dataset:{id:'unsafe'}}};
 for(const b of [...buttons,discard]){b.disabled=false;b.attrs={};b.setAttribute=(k,v)=>b.attrs[k]=v;b.after=n=>b.nextElementSibling=n;b.closest=selector=>selector==='#pedido'?(b.pedido?{}:null):b.card;}
 ctx.MODO='servidor';ctx.item=id=>id==='unsafe'?{descarte_bloqueio:'Item já encaminhado; confira a fila.'}:null;
 ctx.document={querySelectorAll:selector=>selector==='[data-a="descartar"],[data-today-a="descartar"]'?[discard]:buttons,createElement:()=>{const n={textContent:''};n.classList={contains:c=>n.className===c};return n}};
 const capabilityCode=html.slice(html.indexOf('let legacyCapabilities='),html.indexOf('async function descartar(it,b)'));
 vm.runInContext(capabilityCode+';legacyCapabilities={legacy_mutations:false,reason:"Bloqueado pelo modo"};applyPanelCapabilities();',ctx);
 for(const b of [...buttons,discard]){assert.equal(b.disabled,true,b.textContent);assert.ok(b.nextElementSibling.textContent.length>10,b.textContent);assert.equal(b.nextElementSibling.className,'action-block-reason');assert.equal(b.title,b.nextElementSibling.textContent);assert.ok(b.attrs['aria-label'].includes(b.nextElementSibling.textContent));}
 assert.match(buttons[0].nextElementSibling.textContent,/Revise a campanha/);assert.match(discard.nextElementSibling.textContent,/encaminhado/);
 console.log('PASS blocked actions: disabled and visible reason before interaction');
 const scripts=[...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]);scripts.forEach(s=>new vm.Script(s));console.log('PASS all inline scripts parse');
})().catch(e=>{console.error(e);process.exitCode=1});
