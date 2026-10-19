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
 assert.ok(!ctx.selectors.includes('[data-a="descartar"]'));console.log('PASS blocked actions');
 const scripts=[...html.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map(m=>m[1]);scripts.forEach(s=>new vm.Script(s));console.log('PASS all inline scripts parse');
})().catch(e=>{console.error(e);process.exitCode=1});
