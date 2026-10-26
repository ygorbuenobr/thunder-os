// S46 browser fixtures: isolated Chromium, all requests intercepted; no providers.
// Author evidence only. The principal owns the TLC verifier and real browser.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, '_ferramentas/painel_modelo.html'), 'utf8');
const output = path.join(root, '.scratch/S46-frontend');
const between = (from,to) => {const a=source.indexOf(from),b=source.indexOf(to,a+from.length);assert(a>=0&&b>a);return source.slice(a,b);};
const attack = '<img src=x onerror="window.injected=true">';
const original = 'Original completo\n\n  Espaçamento.\n'+ 'Texto original. '.repeat(800)+'\nORIGINAL_END\n'+attack;
const fixture = () => ({enabled:true,status:'ok',campaign_id:'BR-APOLLO-01',account_id:'account-fixture',external_campaign_id:'sequence-fixture',last_success_at:'2031-03-12T12:30:00Z',last_attempt_at:'2031-03-12T12:30:00Z',coverage:{complete:true,contacts_complete:true,messages_complete:true},metrics:{contacts:205,sent:1,reply_signals:2,human_replies:null,automatic_replies:1,unclassified_replies:1},leads:Array.from({length:205},(_,i)=>({id:'lead-'+i,name:'Pessoa '+i,company:i===204?'Última Empresa':'Empresa '+i,email:`fixture${i}@example.invalid`,membership_status:i===0?'scheduled':'active',events:i===0?[{provider_id:'scheduled',event_type:'scheduled',content_complete:false},{provider_id:'sent-original',event_type:'email_sent',occurred_at:'2031-03-12T12:00:00Z',subject:'Assunto '+attack,text:original,content_complete:true},{provider_id:'signal',event_type:'reply_signal',content_complete:false},{provider_id:'reply-original',event_type:'reply_received',human_reply:true,subject:'Resposta',text:'Resposta original integral',content_complete:true},{event_type:'auto_reply_received',text:'Fora do escritório',content_complete:true},{event_type:'unclassified_reply',content_complete:false}]:[]})),crm_sync:{enabled:false,status:'blocked',permission_status:'EMAIL_REQUIRES_PERMISSION_MODIFICATION',contacts_existing:3,contacts_created:0,emails_logged:null,retained:2}});
const css=between('<style>','</style>')+'</style>';
const markup=between('  <section class="cad-read-section cad-instantly" id="cad-apollo-reader"','  <section class="cad-read-section"><h3>Respostas e paradas</h3>');
const instantly=between('let cadInstantlyLastGood=', '// S46: cache-only Apollo');
const apollo=between('// S46: cache-only Apollo','async function atualizarCadencias(){');
const update=between('async function atualizarCadencias(){','async function carregarPlacarAprendizados(){');
const cadenceDate=between('function cadReadDate(value){','function cadReadTasks(');
assert.match(source,/setInterval\([\s\S]*?atualizarCadencias\(\);\},60000\)/);
assert.match(update,/carregarApolloReader\(\)/);
assert(!apollo.includes("'POST'")&&!apollo.includes("'PATCH'")&&!apollo.includes('window.prompt'));
const harness=`<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">${css}</head><body data-aba="cadencias"><main style="padding:16px;max-width:1100px;margin:auto">${markup}<button id="cad-read-refresh">Atualizar</button><button id="system-read-refresh">Atualizar sistema</button><p id="cad-read-status"></p><div id="cad-instantly-reader"><p id="cad-instantly-status"></p><div id="cad-instantly-search-field"><input id="cad-instantly-search"></div><div id="cad-instantly-content"></div></div></main><script>
const $=id=>document.getElementById(id);
${between('const esc = s =>','\n')}
const cadReadNumber=v=>Number.isInteger(v)?String(v):'Não verificado';
${cadenceDate}
let cadReadLoading=false,cadReadSnapshot=null,cadReadContact=null;
const apiServidor=async route=>{const response=await fetch(route,{method:'GET',credentials:'same-origin'});if(!response.ok)throw Error('Cache indisponível');return response.json();};
for(const name of ['carregarIntegracoesEmail','carregarFichasCampanhas','carregarControleRespostas','carregarPreparacaoLinkedin','carregarFontesCampanhas','carregarPlacarAprendizados','renderCadencias'])window[name]=()=>Promise.resolve();
${instantly}
${apollo}
${update}
$('cad-read-refresh').addEventListener('click',atualizarCadencias);
setInterval(()=>{if(!document.hidden)atualizarCadencias();},60000);
atualizarCadencias();
</script></body></html>`;
(async()=>{
 fs.mkdirSync(output,{recursive:true});
 const browser=await chromium.launch({headless:true});
 const report=[];
 try{
  for(const width of [1440,390]){
   const context=await browser.newContext({viewport:{width,height:1000},serviceWorkers:'block'});
   const page=await context.newPage();
   let state=fixture(),fail=false,hold=false,release,reads=0;
   const requests=[],external=[],errors=[];
   page.on('pageerror',e=>errors.push(e.message));
   await context.route('**/*',async route=>{
    const req=route.request(),url=new URL(req.url());requests.push({method:req.method(),path:url.pathname});
    if(url.origin!=='https://panel.example.invalid'){external.push(req.url());return route.abort();}
    if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:harness});
    if(url.pathname==='/api/os/v1/integrations/apollo/reader'){
     reads++;if(hold)await new Promise(resolve=>release=resolve);
     return route.fulfill({status:fail?503:200,contentType:'application/json',body:JSON.stringify(state)});
    }
    return route.fulfill({status:404,contentType:'application/json',body:'{}'});
   });
   await page.clock.install();
   await page.goto('https://panel.example.invalid/');
   const refresh=async()=>{await page.locator('#cad-apollo-refresh').click();await page.waitForFunction(()=>document.getElementById('cad-apollo-reader').getAttribute('aria-busy')==='false');};
   await page.waitForFunction(()=>document.getElementById('cad-apollo-count').textContent.includes('205'));
   const leads=page.locator('#cad-apollo-leads > details');
   assert.equal(await leads.count(),100);
   assert.match(await page.locator('#cad-apollo-count').innerText(),/1–100 de 205/);
   assert.match(await page.locator('#cad-apollo-content').innerText(),/necessário ajustar a permissão.*EMAIL/);
   assert.equal(await page.evaluate(()=>cadApolloNumber(-1)),'Não verificado');
   assert.equal(await page.evaluate(()=>cadApolloNumber(false)),'Não verificado');
   for(const unsafe of ['http://app.apollo.io/#/contacts/lead-0','https://name:pass@app.apollo.io/#/contacts/lead-0','https://app.apollo.io:444/#/contacts/lead-0','https://app.apollo.io/?secret=fixture#/contacts/lead-0','data:text/html,fixture'])assert.equal(await page.evaluate(value=>cadApolloLink(value,'contact','lead-0'),unsafe),'');
   assert.equal(await page.locator('dt').filter({hasText:'Respostas humanas confirmadas'}).locator('..').locator('dd').innerText(),'Não verificado');
   await leads.first().locator(':scope > summary').focus();await page.keyboard.press('Enter');
   await leads.first().locator('details').nth(1).locator('summary').focus();await page.keyboard.press('Enter');
   assert.equal(await leads.first().locator('pre').nth(1).textContent(),original);
   assert.equal(await page.locator('#cad-apollo-reader img').count(),0);
   assert.equal(await page.evaluate(()=>window.injected),undefined);
   assert.match(await leads.first().innerText(),/Agendado na fonte · ainda não enviado/);
   assert.match(await leads.first().innerText(),/Sinal de resposta Apollo · original não disponível/);
   assert.match(await leads.first().innerText(),/Humana confirmada pela fonte/);
   await page.screenshot({path:path.join(output,`apollo-original-${width}.png`),fullPage:false});
   await leads.first().locator('details').nth(1).locator('summary').focus();
   await page.evaluate(()=>carregarApolloReader());
   await page.waitForFunction(()=>document.getElementById('cad-apollo-reader').getAttribute('aria-busy')==='false');
   assert.equal(await leads.first().locator('details').nth(1).getAttribute('open'),'','original stays expanded after refresh');
   assert.equal(await page.evaluate(()=>document.activeElement.dataset.apolloFocus),'event:lead-0:sent-original');
   await leads.first().locator(':scope > summary').click();await page.evaluate(()=>scrollTo(0,0));
   await page.screenshot({path:path.join(output,`apollo-${width}.png`),fullPage:false});
   await page.locator('#cad-apollo-next').click();assert.equal(await leads.count(),100);assert.match(await page.locator('#cad-apollo-page').innerText(),/2 de 3/);
   await page.locator('#cad-apollo-next').click();assert.equal(await leads.count(),5);assert.match(await page.locator('#cad-apollo-count').innerText(),/201–205 de 205/);
   await page.locator('#cad-apollo-prev').click();assert.match(await page.locator('#cad-apollo-page').innerText(),/2 de 3/);
   for(const query of ['fixture204@','Pessoa 204','Última Empresa']){await page.locator('#cad-apollo-search').fill(query);assert.equal(await leads.count(),1);assert.match(await leads.innerText(),/Pessoa 204/);}
   await page.locator('#cad-apollo-search').fill('nenhum');assert.match(await page.locator('#cad-apollo-leads').innerText(),/Nenhum lead encontrado/);
   await page.locator('#cad-apollo-search').fill('');
   state.leads[0]={...state.leads[0],name:attack,company:attack,source_url:'javascript:alert(1)',hubspot_contact_id:'123',hubspot_contact_url:'https://evil.example.invalid/contacts/1/contact/123'};
   state.source_url='https://app.apollo.io.evil.invalid/#/sequences/sequence-fixture';
   await refresh();await leads.first().locator(':scope > summary').click();
   assert.equal(await page.locator('#cad-apollo-reader img').count(),0);assert.equal(await page.locator('#cad-apollo-reader a').filter({hasText:'HubSpot'}).count(),0);
   assert.equal(await leads.first().locator('a').count(),0);
   state.leads[0].source_url='https://app.apollo.io/#/contacts/other-lead';await refresh();assert.equal(await leads.first().locator('a').count(),0,'mismatched source ID refused');
   state.leads[0].source_url='https://app.apollo.io/#/contacts/lead-0';state.leads[0].hubspot_contact_url='https://app.hubspot.com/contacts/12345/contact/123';
   await refresh();assert.equal(await leads.first().locator('a').count(),2);
   state.leads[0].hubspot_contact_url='https://app.hubspot.com/contacts/12345/contact/999';await refresh();assert.equal(await leads.first().locator('a').count(),1);
   state.leads[0].events.push({provider_id:'abcdef12',provider_thread_id:'abcdef11',event_type:'third_party_reply',sender:'Colega <colega@example.invalid>',subject:'Resposta de colega',text:'Mensagem original do terceiro',content_complete:true,source_url:'https://mail.google.com/mail/u/ygor.bueno%40thundermustard.com/#all/abcdef11'});await refresh();
   assert.match(await leads.first().innerText(),/Resposta de outro remetente/);await leads.first().locator('details').last().locator('summary').click();assert.match(await leads.first().innerText(),/Colega <colega@example.invalid>/);
   assert.equal(await leads.first().locator('a[href*="mail.google.com"]').count(),1);
   state.leads[0].events.at(-1).source_url='https://mail.google.com/mail/u/outra%40example.invalid/#all/abcdef11';await refresh();assert.equal(await leads.first().locator('a[href*="mail.google.com"]').count(),0,'wrong mailbox source refused');
   state.leads[0].events.pop();await refresh();
   state.crm_sync={enabled:false,status:'configuration_pending',permission_status:'unverified',error_code:'email_write_permission_unverified'};await refresh();assert.match(await page.locator('#cad-apollo-content').innerText(),/Permissão.*EMAIL.*não verificada/);
   state.crm_sync={enabled:false,status:'blocked',readiness:{email_write_permission:'REQUIRES_PERMISSION_MODIFICATION'}};await refresh();assert.match(await page.locator('#cad-apollo-content').innerText(),/necessário ajustar a permissão/);
   state.status='partial';state.coverage.contacts_complete=false;state.metrics={contacts:null,sent:null,reply_signals:2,human_replies:null};await refresh();
   assert.match(await page.locator('#cad-apollo-status').innerText(),/Leitura parcial/);assert.match(await page.locator('#cad-apollo-content').innerText(),/Contatos: parcial/);
   assert.equal(await page.locator('dt').filter({hasText:'Envios confirmados'}).locator('..').locator('dd').innerText(),'Não verificado');
   fail=true;await refresh();assert.match(await page.locator('#cad-apollo-status').innerText(),/Cache indisponível.*dados anteriores/);assert.equal(await leads.count(),100);
   fail=false;state={enabled:true,status:'error',campaign_id:'BR-APOLLO-01',leads:[]};await refresh();assert.equal(await leads.count(),100,'reader error preserves observed snapshot');
   assert.equal(await page.locator('#cad-instantly-reader').getAttribute('aria-busy'),'false','missing Instantly cache remains recoverable');
   state=fixture();state.status='error';state.error_code='apollo_rate_limited';state.rate_limit_window='hour';state.next_attempt_at='2031-03-12T13:30:00Z';state.poll_interval_seconds=900;state.coverage.contacts_cached=true;state.coverage.contacts_observed_at='2031-03-12T10:30:00Z';await refresh();
   assert.match(await page.locator('#cad-apollo-status').innerText(),/Limite de consultas.*janela horária.*Nova tentativa não antes/);
   assert.match(await page.locator('#cad-apollo-status').innerText(),/fonte Apollo a cada 15 min.*cache a cada 60 s/);
   assert.match(await page.locator('#cad-apollo-content').innerText(),/Lista de contatos reutilizada.*Novas inscrições/);
   assert.equal(await leads.count(),100,'quota preserves prior originals');
   state.error_code='apollo_endpoint_budget_exhausted';state.rate_limit_window='day';await refresh();assert.match(await page.locator('#cad-apollo-status').innerText(),/Orçamento de consultas deste leitor atingido nesta janela diária/);
   state.leads[0].events.push({provider_id:'abcdef13',provider_thread_id:'abcdef11',event_type:'thread_message_observed',direction:'outgoing',context_only:true,receipt_eligible:false,sender:'Ygor <ygor.bueno@thundermustard.com>',subject:'Retorno do Ygor',text:'Texto original da resposta do Ygor',content_complete:true,source_url:'https://mail.google.com/mail/u/ygor.bueno%40thundermustard.com/#all/abcdef11'});await refresh();
   await leads.first().evaluate(el=>el.open=true);assert.match(await leads.first().innerText(),/Mensagem enviada nesta conversa · contexto, sem recibo Apollo/);
   await leads.first().locator('details').last().locator('summary').click();assert.match(await leads.first().innerText(),/Texto original da resposta do Ygor/);
   assert.equal(await page.locator('dt').filter({hasText:'Envios confirmados'}).locator('..').locator('dd').innerText(),'1');
   fail=false;state=fixture();state.status='stale';await refresh();assert.match(await page.locator('#cad-apollo-status').innerText(),/desatualizada/);
   state={enabled:false,status:'disabled',campaign_id:'BR-APOLLO-01'};await refresh();assert.equal(await leads.count(),0);assert.match(await page.locator('#cad-apollo-status').innerText(),/desativado/);
   fail=true;await refresh();assert.equal(await leads.count(),0,'disabled cache is never resurrected');
   fail=false;state={enabled:true,status:'pending',campaign_id:'BR-APOLLO-01'};await refresh();assert.match(await page.locator('#cad-apollo-status').innerText(),/Primeira leitura pendente/);
   state=fixture();state.leads=[];state.metrics={contacts:0,sent:0};await refresh();assert.match(await page.locator('#cad-apollo-leads').innerText(),/Nenhum lead disponível/);
   state=fixture();delete state.last_success_at;state.status='partial';state.coverage.complete=false;await refresh();assert.equal(await leads.count(),100,'partial first snapshot is shown');assert.match(await page.locator('#cad-apollo-status').innerText(),/Último sucesso: não verificado/);
   hold=true;const before=reads;await page.locator('#cad-apollo-refresh').click();await page.waitForFunction(()=>document.getElementById('cad-apollo-reader').getAttribute('aria-busy')==='true');
   await page.evaluate(()=>carregarApolloReader());assert.equal(reads,before+1,'concurrent duplicate refresh is blocked');assert.equal(await page.locator('#cad-apollo-refresh').isDisabled(),true);hold=false;release();
   await page.waitForFunction(()=>document.getElementById('cad-apollo-reader').getAttribute('aria-busy')==='false');
   const count=reads;await page.locator('#cad-read-refresh').click();await page.waitForFunction(()=>!document.getElementById('cad-read-refresh').disabled);assert.equal(reads,count+1,'shared manual refresh reads Apollo');
   const beforePoll=reads;await page.clock.runFor(60000);await page.waitForFunction(()=>!document.getElementById('cad-read-refresh').disabled);assert.equal(reads,beforePoll+1,'60-second polling reads Apollo');
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'no body horizontal overflow');
   assert.deepEqual(errors,[]);assert.deepEqual(external,[]);assert(requests.every(r=>r.method==='GET'));
   report.push({width,checks:'pagination, search, original, keyboard, escaped HTML, scoped links, unknown metrics, partial, outage, disabled, pending, empty, recovery, concurrent refresh, GET only',requests:requests.length,externalRequests:external.length,pageErrors:errors});
   await context.close();
  }
  fs.writeFileSync(path.join(output,'browser-results.json'),JSON.stringify(report,null,2)+'\n');
  console.log('PASS S46: Chromium 1440/390, all states, 205 leads, originals, links, GET-only, zero external network');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
