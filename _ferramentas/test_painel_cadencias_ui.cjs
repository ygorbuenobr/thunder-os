// Synthetic browser journey: every request is intercepted; no live service is used.
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const outputDir=path.join(__dirname,'../.scratch/task3-reader-round2-ui');
const html=fs.readFileSync(path.join(__dirname,'painel_modelo.html'),'utf8').replaceAll("/*MODO*/'artifact'","'servidor'").replaceAll('/*DADOS*/{}','{}');
const task={id:'99',title:'[PILOTO][D0] Empresa exemplo | LINKEDIN',campaign:'PILOTO',contact_ids:['123'],contact_name:'Contato exemplo',channel:'LINKEDIN',status:'NOT_STARTED',due_at:'2026-10-21T15:00:00Z'};
const unlinked={...task,id:'100',contact_ids:[],contact_name:null};
const snapshot={collected_at:'2026-10-21T13:00:00Z',coverage:{complete:false,limitations:['Consulta de atividades incompleta.']},campaigns:[{id:'PILOTO',task_state:'observed',task_coverage_complete:true,metrics:{contacts:1,completed_with_receipt:null,pending:2,overdue:0,replies:null,meetings:null},tasks:[task]}],today:{planned:[task],awaiting_approval:[],unreplied:[]},unlinked:[unlinked]};
const registered={id:'REGISTRY-ONLY',registry:{id:'REGISTRY-ONLY',platform:'codex_local',active:true},task_state:'empty',task_coverage_complete:true,metrics:{contacts:0,tasks:0,completed_with_receipt:null,pending:0,overdue:0,replies:null,meetings:null},tasks:[]};
async function testInstantlyReader(browser){
 const readerPath='/api/os/v1/integrations/instantly/reader';
 const attack='<img src=x onerror="window.__instantlyXss=1">';
 const bodyAttack='</pre><script>window.__instantlyXss=1</script><b>plain text</b>';
 const base={schema_version:1,read_only:true,enabled:true,status:'ok',last_attempt_at:'2026-10-22T12:01:00Z',last_success_at:'2026-10-22T12:00:00Z',poll_interval_seconds:60,campaign_id:'BR-INSTANTLY-01',external_campaign_id:'synthetic-campaign',account_id:'synthetic-account',coverage:{complete:true,pages:1,invalid_records:0,limit_reached:false},metrics:{sent_messages:7,leads_with_sent:1,human_replies:1,automatic_replies:1,unclassified_replies:1},leads:[{email:'lead@example.invalid',events:[
  {provider_id:'sent-1',event_type:'email_sent',occurred_at:'2026-10-22T11:00:00Z',subject:attack,text:bodyAttack,content_complete:true,classification_source:'provider'},
  {provider_id:'auto-1',event_type:'auto_reply_received',occurred_at:'2026-10-22T11:01:00Z',subject:'Automatic response',text:'Out of office',content_complete:true,classification_source:'subject'},
  {provider_id:'unknown-1',event_type:'unclassified_reply',occurred_at:'2026-10-22T11:02:00Z',subject:'Unknown reply',text:'Classification absent',content_complete:false,classification_source:'unknown'},
  {provider_id:'reply-1',event_type:'reply_received',occurred_at:'2026-10-22T11:03:00Z',subject:'Reply',text:'Provider non-auto flag only',content_complete:true,classification_source:'provider'}]}],error_code:null,crm_write_enabled:false,external_actions_enabled:false};
 for(const viewport of [{width:1440,height:1000},{width:390,height:844},{width:320,height:800}]){
  const page=await browser.newPage({viewport}),requests=[],errors=[];
  let state={...base,status:'disabled',enabled:false,last_attempt_at:null,last_success_at:null,metrics:null,leads:[]},httpFailure=false,hold=false,release;
  await page.clock.install();
  page.on('pageerror',e=>errors.push(e.message));
  await page.route('**/*',async route=>{
   const req=route.request(),p=new URL(req.url()).pathname;
   requests.push({p,method:req.method()});
   assert.equal(req.method(),'GET','Instantly journey must only read');
   if(p==='/')return route.fulfill({contentType:'text/html',body:html});
   let result=[];
   if(p===readerPath){
    if(hold)await new Promise(resolve=>{release=resolve;});
    if(httpFailure)return route.fulfill({status:503,contentType:'application/json',body:'{}'});
    result=state;
   }else if(p==='/api/os/v1/cadencias')result=snapshot;
   else if(p==='/api/os/v1/panel-capabilities')result={legacy_mutations:false};
   else if(p==='/api/os/v1/today')result={priorities:[],work:[],cadences:[],replies:[],controls:{},usage:[],alerts:[]};
   else if(p==='/api/os/v1/integrations/external-email/status')result={schema_version:1,automatic_reader_enabled:false,crm_import_enabled:false,webhook_enabled:false};
   else if(p==='/api/os/v1/hermes/contacts')result={contacts:[],complete:true};
   return route.fulfill({contentType:'application/json',body:JSON.stringify(result)});
  });
  const reader=page.locator('#cad-instantly-reader'),content=page.locator('#cad-instantly-content'),status=page.locator('#cad-instantly-status');
  const settled=()=>page.waitForFunction(()=>document.querySelector('#cad-instantly-reader').getAttribute('aria-busy')==='false'&&!document.querySelector('#cad-read-refresh').disabled);
  const refresh=async()=>{await page.locator('#cad-read-refresh').click();await settled();};
  await page.goto('https://instantly-reader.test/#cadencias');await settled();
  assert.match(await status.innerText(),/Leitor desativado/);
  assert.equal(await content.locator('dd').count(),0);
  // Even accidental zero metrics without a successful timestamp must remain unknown.
  state={...state,status:'pending',enabled:true,metrics:{sent_messages:0}};await refresh();
  assert.match(await status.innerText(),/Primeira leitura pendente/);
  assert.equal(await content.locator('dd').count(),0);
  state={...state,status:'error',error_code:'synthetic_error'};await refresh();
  assert.match(await status.innerText(),/Falha na leitura.*Sem leitura bem-sucedida/);
  assert.equal(await content.locator('dd').count(),0);
  state={...base,status:'partial',coverage:{complete:false,pages:2,invalid_records:3,limit_reached:true}};
  await refresh();
  assert.match(await content.innerText(),/contagens observadas, não totais globais/);
  assert.match(await content.innerText(),/2 páginas · 3 registros inválidos · limite da consulta atingido/);
  assert.equal(await content.locator('dt').filter({hasText:'observado'}).count(),5);
  state=base;await refresh();
  const hubspotBefore=await page.locator('#cad-read-campaigns').innerText();
  assert.equal(await content.locator('dd').first().innerText(),'7');
  const summary=content.locator('summary').first();
  await summary.focus();await page.keyboard.press('Enter');
  assert.equal(await content.locator('details').first().getAttribute('open'),'');
  assert.match(await content.innerText(),/Resposta automática/);
  assert.match(await content.innerText(),/heurística do assunto; não confirmada pelo provider/);
  assert.match(await content.innerText(),/Resposta sem classificação/);
  assert.match(await content.innerText(),/humana não confirmada/);
  assert.match(await content.innerText(),/Conteúdo incompleto na origem/);
  assert.match(await content.innerText(),/entrega não verificada/);
  assert.ok((await content.innerText()).includes(attack));
  assert.ok((await content.innerText()).includes(bodyAttack));
  assert.equal(await reader.locator('img,script,b:has-text("plain text")').count(),0);
  assert.equal(await page.evaluate(()=>window.__instantlyXss),undefined);
  assert.equal(await reader.locator('button,a,form').count(),0);
  assert.equal(await reader.getByRole('searchbox',{name:'Buscar lead por e-mail'}).count(),1);
  // The same 60 s cycle refreshes the cache once and keeps expansion and focus.
  const before=requests.filter(r=>r.p===readerPath).length;
  await page.clock.runFor(60001);await settled();
  assert.equal(requests.filter(r=>r.p===readerPath).length,before+1);
  assert.equal(await content.locator('details').first().getAttribute('open'),'');
  assert.equal(await summary.evaluate(el=>el===document.activeElement),true);
  assert.equal(await page.locator('#cad-read-campaigns').innerText(),hubspotBefore);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  fs.mkdirSync(outputDir,{recursive:true});
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:path.join(outputDir,`instantly-populated-${viewport.width}.png`),fullPage:true});
  // Backend failures/staleness may themselves carry the dated last-good snapshot.
  state={...base,status:'error',last_attempt_at:'2026-10-22T12:02:00Z'};await refresh();
  assert.match(await status.innerText(),/Falha na leitura.*Última leitura disponível: 22\/10\/2026, 09:00/);
  assert.equal(await content.locator('dd').first().innerText(),'7');
  state={...base,status:'stale'};await refresh();
  assert.match(await status.innerText(),/Leitura desatualizada.*Última leitura disponível/);
  // A transport error must retain the same dated data, without claiming freshness.
  httpFailure=true;await refresh();
  assert.match(await status.innerText(),/Cache indisponível.*Última leitura disponível/);
  assert.equal(await content.locator('dd').first().innerText(),'7');
  httpFailure=false;state={...base,schema_version:2};await refresh();
  assert.match(await status.innerText(),/Cache indisponível/);
  // Hold one response while another cycle fires: no duplicate cache requests.
  state=base;hold=true;
  const pending=page.waitForRequest(r=>new URL(r.url()).pathname===readerPath);
  await page.locator('#cad-read-refresh').click();await pending;
  await page.waitForFunction(()=>document.querySelector('#cad-instantly-reader').getAttribute('aria-busy')==='true');
  assert.match(await status.innerText(),/Atualizando leitura do cache/);
  const heldCount=requests.filter(r=>r.p===readerPath).length;
  await page.clock.runFor(60001);
  assert.equal(requests.filter(r=>r.p===readerPath).length,heldCount);
  hold=false;release();await settled();
  state={...base,leads:[],metrics:{sent_messages:0,leads_with_sent:0,human_replies:0,automatic_replies:0,unclassified_replies:0}};await refresh();
  assert.match(await content.innerText(),/Nenhum lead observado/);
  assert.equal(await content.locator('dd').first().innerText(),'0');
  // Explicit rendering bounds are separate from provider coverage.
  const event={...base.leads[0].events[0],subject:attack+'S'.repeat(400),text:bodyAttack+'X'.repeat(4500)};
  state={...base,leads:Array.from({length:51},(_,i)=>({email:i===0?attack+'E'.repeat(350):`lead-${i}@example.invalid`,events:Array.from({length:i===0?21:1},()=>event)}))};
  await refresh();
  assert.equal(await content.locator('details').count(),50);
  await content.locator('summary').first().focus();await page.keyboard.press('Enter');
  assert.equal(await content.locator('details').first().locator('li').count(),20);
  assert.equal((await content.locator('pre').first().textContent()).length,4000);
  assert.match(await content.innerText(),/primeiros 50 leads/);
  assert.match(await content.innerText(),/primeiros 20 eventos/);
  assert.match(await content.innerText(),/Trecho limitado nesta tela/);
  assert.equal(await reader.locator('img,script').count(),0);
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  const focusStyle=await content.locator('summary').first().evaluate(el=>getComputedStyle(el).outlineStyle);
  assert.notEqual(focusStyle,'none');
  // Search all cached leads before slicing: the 51st lead remains reachable.
  const lateLead='pavlack@example.invalid';
  state={...base,leads:[...state.leads.slice(0,50),{email:lateLead,events:base.leads[0].events}]};
  await refresh();
  assert.equal(await content.getByText(lateLead,{exact:false}).count(),0);
  const search=reader.getByRole('searchbox',{name:'Buscar lead por e-mail'});
  const searchRequests=requests.length;
  await search.fill('PAVLACK');
  assert.equal(requests.length,searchRequests,'Search must only filter the cached snapshot');
  assert.equal(await content.locator('details').count(),1);
  assert.match(await content.locator('summary').innerText(),/pavlack@example.invalid/);
  assert.equal(await search.evaluate(el=>el===document.activeElement),true);
  await page.clock.runFor(60001);await settled();
  assert.equal(await search.inputValue(),'PAVLACK');
  assert.equal(await search.evaluate(el=>el===document.activeElement),true);
  assert.equal(await content.locator('details').count(),1);
  assert.equal(await content.locator('dd').first().innerText(),'7');
  assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
  await page.evaluate(()=>window.scrollTo(0,0));
  await page.screenshot({path:path.join(outputDir,`instantly-search-${viewport.width}.png`),fullPage:true});
  await search.fill('missing@example.invalid');
  assert.match(await content.innerText(),/Nenhum lead encontrado/);
  await search.fill('');
  assert.equal(await content.locator('details').count(),50);
  // Explicit disable revokes retained data, even after a successful read/search.
  state={...base,status:'disabled',enabled:false,metrics:null,leads:[],last_success_at:null};await refresh();
  assert.match(await status.innerText(),/Leitor desativado.*Sem leitura bem-sucedida/);
  assert.equal(await content.locator('dd,details').count(),0);
  assert.equal(await search.isVisible(),false);
  assert.equal(await page.locator('#cad-instantly-search').inputValue(),'');
  assert.doesNotMatch(await reader.innerText(),/pavlack|synthetic-account|Última leitura disponível/);
  httpFailure=true;await refresh();
  assert.equal(await content.locator('dd,details').count(),0);
  assert.match(await status.innerText(),/Cache indisponível.*Sem leitura bem-sucedida/);
  assert.deepEqual(errors,[]);
  assert.equal(requests.some(r=>r.method!=='GET'),false);
  await page.locator('#abas button[data-aba="hoje"]').click();
  const offTabCount=requests.filter(r=>r.p===readerPath).length;
  await page.clock.runFor(60001);
  assert.equal(requests.filter(r=>r.p===readerPath).length,offTabCount);
  await page.close();
 }
 console.log('PASS: independent Instantly cache journey 1440/390/320; disabled clears retained snapshot; pending/partial/error/stale/empty; dated retention; XSS plain text; keyboard/focus; bounded leads/events/content; cached search reaches lead beyond 50 and preserves focus; 60s GET-only polling without overlap or HubSpot totals changes');
}
(async()=>{
 snapshot.contact_conflicts=[{contact_id:'123',campaign:'BR-APOLLO-01',task_campaign:'V'.repeat(100),reason:'task_campaign_differs_from_current_campaign'},{contact_id:'123',campaign:'BR-APOLLO-01',task_campaign:'<img src=x onerror=alert(1)>',reason:'task_campaign_differs_from_current_campaign'}];
 const browser=await chromium.launch({headless:true});
 try{
  await testInstantlyReader(browser);
  for(const viewport of [{width:1440,height:1000},{width:390,height:844}]){
   const page=await browser.newPage({viewport});const errors=[],requests=[];let fail=false,empty=false,registryState=null,holdVerify=false,releaseVerify;
   await page.clock.install();
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',async route=>{
    const req=route.request(),p=new URL(req.url()).pathname;requests.push({p,method:req.method()});
    if(p==='/')return route.fulfill({contentType:'text/html',body:html});
    let body=[];
    if(p==='/api/os/v1/cadencias'){
     if(fail)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({erro:'Unavailable'})});
     body=empty?{...snapshot,campaigns:[],unlinked:[],today:{}}:snapshot;
     if(registryState){
      const complete=registryState==='complete';
      const group=complete?registered:{...registered,task_state:'unknown',task_coverage_complete:false,metrics:{contacts:null,tasks:null,completed_with_receipt:null,pending:null,overdue:null,replies:null,meetings:null}};
      body={...snapshot,campaigns:[...snapshot.campaigns,group]};
      if(registryState==='nonempty-partial')body.campaigns=[{...group,id:'PILOTO',registry:null,task_state:'observed',tasks:[{...task,status:'COMPLETED'}],metrics:{...group.metrics,observed_tasks:1,observed_completed_status:1,observed_pending:0,observed_overdue:0}}];
      if(registryState==='evidence')body.campaigns=[{...snapshot.campaigns[0],metrics:{...snapshot.campaigns[0].metrics,completed_with_receipt:1,external_sent:1,observed_uncertain:1,observed_blocked:1},tasks:[
       {...task,id:'201',status:'COMPLETED',outcome:'sent_with_receipt',receipt:{provider:'gmail',message_id:'abc123',verification:'recorded_confirmation'}},
       {...task,id:'202',status:'WAITING',outcome:'uncertain_no_retry',reason:'Sem recibo; resultado não confirmado.'},
       {...task,id:'203',status:'WAITING',outcome:'blocked',reason:'Histórico incompleto <img src=x onerror=alert(1)>'},
       {...task,id:'204',status:'COMPLETED',outcome:'completed_without_receipt'}],activities:[{id:'emails:901',provider:'apollo',origin_provider:'apollo',attribution:'explicit_integration_binding',sent_evidence:true,subject:'Mensagem sintética',occurred_at:'2026-10-21T10:00:00Z',receipt:null}]}];
      if(registryState==='contact-current')body.campaigns=[['BR-APOLLO-01','apollo'],['BR-INSTANTLY-01','instantly']].map(([id,platform])=>({
       id,registry:{id,platform,active:true},task_state:'empty',task_coverage_complete:true,external_activity_state:'complete',tasks:[],
       metrics:{contacts:1,observed_contacts:1,completed_with_receipt:null,pending:0,overdue:0,replies:null,meetings:null,external_sent:1},
       activities:[{id:'emails:'+platform,campaign:id,contact_ids:['123'],provider:platform,origin_provider:null,provider_semantics:'registry_platform_not_origin',attribution:'contact_current_campaign',sent_evidence:true,crm_recorded_sent:true,delivery_verified:false,historical_membership_verified:false,subject:'Mensagem sintética <img src=x onerror=alert(1)>',occurred_at:'2026-10-21T10:00:00Z',receipt:null}]
      }));
     }
    }else if(p==='/api/os/v1/cadencias/contacts/123')body={contact_id:'123',contact_name:'Contato exemplo',coverage:{complete:false,limitations:['Somente eventos vinculados.']},events:[{type:'email',subject:'Conversa de exemplo',text:'<img src=x onerror=alert(1)> Texto enviado',occurred_at:'2026-10-20T12:00:00Z',source:'HubSpot',receipt:'receipt:test-only'}]};
    else if(p==='/api/os/v1/integrations/external-email/status')body={schema_version:1,automatic_reader_enabled:false,crm_import_enabled:false,webhook_enabled:false};
    else if(p==='/api/os/v1/reply-control')body={external_pause_enabled:false,alerts:[{title:'Synthetic alert <img src=x onerror=alert(1)>',reason:'Pausa externa desligada.',content:'X'.repeat(4000),campaign_id:'C'.repeat(100),hubspot_contact_id:'123'}]};
    else if(p==='/api/os/v1/linkedin-preparation')body={executor_enabled:false,quota_reader_configured:false};
    else if(p==='/api/os/v1/campaign-sources')body={automatic_collection_enabled:false,candidates:[{company:'Synthetic <img src=x onerror=alert(1)>','domain':'D'.repeat(253),signals:[{fact:'F'.repeat(1000),url:'https://www.linkedin.com/jobs/view/123',published_on:'2026-10-21'}]}],held:[{company:'Held','reason':'icp_not_confirmed'}]};
    else if(p==='/api/os/v1/campaign-scoreboard')body={read_only:true,campaigns:[{id:'PILOTO',metrics:{replies:null,meetings:null},learning:{status:'observation_only',requires_human_approval:true}}]};
    else if(p==='/api/os/v1/hermes/contacts')body={contacts:[{id:'123',name:'Synthetic Person '+'X'.repeat(200)}],complete:false};
    else if(p==='/api/os/v1/hermes/verify'){
     assert.deepEqual(req.postDataJSON(),{contact_id:'123',question:'Why E2?'});
     assert.equal(req.headers()['x-thunder-nonce'],'synthetic-nonce');
     if(holdVerify)await new Promise(resolve=>{releaseVerify=resolve;});
     body={answer:'Synthetic <img src=x onerror=alert(1)> '+'A'.repeat(750),contact_id:'123',read_only:true,external_effects:false,sources:[{id:'task:99',url:'javascript:alert(1)'}],uncertainties:[],server_limits:['CRM activity does not prove delivery.','Partial coverage'],collected_at:'2026-10-21T12:00:00Z',runtime:{provider:'hermes_codex_oauth',model:'synthetic-model'}};
    }
    else if(p==='/api/os/v1/session')body={actor:'ygor',nonce:'synthetic-nonce'};
    else if(p==='/api/os/v1/reply-control/manual'){
     const sent=req.postDataJSON();
     assert.equal(sent.contact_id,'123');assert.equal(sent.campaign_id,'BR-APOLLO-01');
     assert.equal(sent.content,'Synthetic human reply');assert.equal(req.headers()['x-thunder-nonce'],'synthetic-nonce');
     body={state:'needs_classification',external_pause_enabled:false};
    }
    else if(p==='/api/os/v1/panel-capabilities')body={legacy_mutations:false};
    else if(p==='/api/os/v1/today')body={priorities:[],work:[],cadences:[],replies:[],controls:{},usage:[],alerts:[]};
    else if(p==='/api/os/v1/integrations/status')body={};
    else if(p==='/api/os/v1/integrations/drafts')body={drafts:[]};
    else if(p==='/api/quentes')body={items:[]};
    else if(p==='/api/sugestoes')body={items:[]};
    return route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
   });
   await page.goto('https://cadencias.test/#cadencias');
   await page.locator('#cad-read-campaigns').getByRole('cell',{name:'PILOTO',exact:true}).waitFor();
   assert.match(await page.locator('#cad-read-status').innerText(),/Consulta parcial/);
   assert.match(await page.locator('#cad-read-unlinked').innerText(),/Sem vínculo/);
   assert.equal(await page.locator('#cad-read-conflicts-section').isVisible(),true);
   assert.match(await page.locator('#cad-read-conflicts').innerText(),/BR-APOLLO-01/);
   assert.equal(await page.locator('#cad-read-conflicts img').count(),0);
   assert.equal(await page.locator('#cad-email-integrations').getByRole('cell',{name:'Desligado',exact:true}).count(),4);
   assert.match(await page.locator('#cad-reply-control').innerText(),/Pausas externas: desligadas/);
   assert.match(await page.locator('#cad-linkedin-preparation').innerText(),/Executor desligado/);
   assert.match(await page.locator('#cad-source-list').innerText(),/aguardando aprovação/);
   assert.equal(await page.locator('#cad-source-list img').count(),0);
   assert.equal(await page.locator('#cad-reply-control img').count(),0);
   const before=requests.filter(r=>r.p==='/api/os/v1/cadencias').length;
   await page.clock.runFor(60001);
   await page.waitForFunction(()=>!document.querySelector('#cad-read-refresh').disabled);
   assert.equal(requests.filter(r=>r.p==='/api/os/v1/cadencias').length,before+1);
   await page.locator('#cad-read-today [data-cad-contact]').first().click();
   await page.locator('#cad-read-events li').waitFor();
   assert.match(await page.locator('#cad-read-events').innerText(),/Texto enviado/);
   assert.equal(await page.locator('#cad-read-events img').count(),0);
   const overflow=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,elements:[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().width&&e.getBoundingClientRect().right>innerWidth+1).slice(0,8).map(e=>({tag:e.tagName,id:e.id,cls:e.className,right:e.getBoundingClientRect().right}))}));
   fs.mkdirSync(outputDir,{recursive:true});
   await page.screenshot({path:path.join(outputDir,`cadencias-registry-timeline-${viewport.width}.png`),fullPage:true});
   assert.equal(overflow.scroll>overflow.width,false,JSON.stringify(overflow));
   fail=true;await page.locator('#cad-read-refresh').click();
   await page.getByText('Não foi possível atualizar. Os dados abaixo são da consulta anterior.').waitFor();
   fail=false;empty=true;await page.locator('#cad-read-refresh').click();
   await page.getByText('Campanhas e tarefas não verificadas: cobertura incompleta.').waitFor();
   empty=false;registryState='complete';await page.locator('#cad-read-refresh').click();
   const registryRow=page.locator('#cad-read-campaigns tbody tr').filter({hasText:'REGISTRY-ONLY'});
   await registryRow.getByText('Sem tarefas ainda nesta consulta.',{exact:true}).waitFor();
   assert.equal(await registryRow.getByRole('cell',{name:'0',exact:true}).count(),3);
   assert.equal(await registryRow.getByRole('cell',{name:'Não verificado',exact:true}).count(),3);
   await page.locator('#cad-read-campaigns details').filter({hasText:'REGISTRY-ONLY'}).locator('summary').click();
   await page.locator('#cad-read-campaigns details').filter({hasText:'REGISTRY-ONLY'}).getByText('Sem tarefas ainda nesta consulta.',{exact:true}).waitFor();
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:path.join(outputDir,`cadencias-registry-empty-${viewport.width}.png`),fullPage:true});
   registryState='partial';await page.locator('#cad-read-refresh').click();
   await registryRow.getByText('Tarefas não verificadas: cobertura incompleta.',{exact:true}).waitFor();
   assert.equal(await registryRow.getByRole('cell',{name:'Não verificado',exact:true}).count(),6);
   assert.equal(await page.locator('#cad-read-campaigns').getByText('Sem tarefas ainda nesta consulta.',{exact:true}).count(),0);
   assert.equal(await page.locator('#cad-read-campaigns').getByRole('cell',{name:'PILOTO',exact:true}).count(),1);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:path.join(outputDir,`cadencias-registry-partial-${viewport.width}.png`),fullPage:true});
   registryState='nonempty-partial';await page.locator('#cad-read-refresh').click();
   const partialRow=page.locator('#cad-read-campaigns tbody tr').filter({hasText:'PILOTO'});
   await partialRow.getByText('Tarefas observadas; totais não verificados: cobertura incompleta.',{exact:true}).waitFor();
   assert.equal(await partialRow.getByRole('cell',{name:'Não verificado',exact:true}).count(),6);
   assert.equal(await partialRow.getByRole('cell',{name:'0',exact:true}).count(),0);
   const partialDetails=page.locator('#cad-read-campaigns details').filter({hasText:'PILOTO'});
   await partialDetails.locator('summary').click();
   await partialDetails.getByRole('cell',{name:'Concluída no HubSpot',exact:true}).waitFor();
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:path.join(outputDir,`cadencias-registry-nonempty-partial-${viewport.width}.png`),fullPage:true});
   registryState='evidence';await page.locator('#cad-read-refresh').click();
   const evidenceDetails=page.locator('#cad-read-campaigns details').filter({hasText:'PILOTO'}).first();
   await evidenceDetails.locator('summary').first().click();
   await evidenceDetails.getByText('Incerto, não repetir',{exact:true}).waitFor();
   await evidenceDetails.getByText('Concluída sem recibo',{exact:true}).waitFor();
   await evidenceDetails.getByText('Enviado com recibo registrado',{exact:true}).waitFor();
   assert.match(await evidenceDetails.innerText(),/Histórico incompleto/);
   assert.match(await evidenceDetails.innerText(),/Enviado \(Apollo\)/);
   assert.doesNotMatch(await evidenceDetails.innerText(),/Campanha atual do contato;/);
   assert.equal(await evidenceDetails.locator('img').count(),0);
   assert.equal(await evidenceDetails.getByRole('button',{name:/retry|repetir|reenviar/i}).count(),0);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:path.join(outputDir,`cadencias-evidence-${viewport.width}.png`),fullPage:true});
   registryState='contact-current';await page.locator('#cad-read-refresh').click();
   await page.locator('#cad-read-campaigns').getByRole('columnheader',{name:'Contatos observados',exact:true}).waitFor();
   for(const [id,platform] of [['BR-APOLLO-01','Apollo'],['BR-INSTANTLY-01','Instantly']]){
    const currentRow=page.locator('#cad-read-campaigns tbody tr').filter({hasText:id});
    await currentRow.waitFor();
    assert.equal(await currentRow.getByRole('cell').nth(1).innerText(),'1');
    const currentDetails=page.locator('#cad-read-campaigns > details').filter({hasText:id});
    await currentDetails.locator('summary').first().focus();await page.keyboard.press('Enter');
    await currentDetails.getByText('Envio registrado · campanha '+platform,{exact:true}).waitFor();
    assert.match(await currentDetails.innerText(),/Campanha atual do contato; origem e entrega não verificadas\./);
    assert.doesNotMatch(await currentDetails.innerText(),/Enviado \((Apollo|Instantly)\)/);
    assert.equal(await currentDetails.locator('img').count(),0);
    assert.equal(await currentDetails.locator('button').count(),0);
   }
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:path.join(outputDir,`cadencias-contact-current-${viewport.width}.png`),fullPage:true});
   assert.equal(requests.filter(r=>r.method!=='GET').length,0);
   await page.locator('#cad-read-conflicts [data-cad-contact]').first().click();
   await page.locator('#cad-read-detail > details > summary').click();
   await page.locator('#cad-reply-text').fill('Synthetic human reply');
   await page.locator('#cad-reply-save').click();
   await page.waitForFunction(()=>document.querySelector('#cad-reply-result').textContent.length>0);
   assert.match(await page.locator('#cad-reply-result').innerText(),/Resposta registrada no OS/);
   assert.equal(requests.filter(r=>r.method==='POST').length,1);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.locator('#abas button[data-aba="hoje"]').click();
   await page.locator('#os-pedido-contato option[value="123"]').waitFor({state:'attached'});
   await page.locator('#os-pedido-contato').selectOption('123');
   await page.locator('#os-pedido-texto').fill('Why E2?');
   await page.locator('#os-pedido button[type="submit"]').click();
   await page.locator('#os-verificacao').waitFor({state:'visible'});
   assert.equal(await page.locator('#os-verificacao img').count(),0);
   assert.match(await page.locator('#os-verificacao-answer').innerText(),/Synthetic <img/);
   assert.equal(await page.locator('#os-verificacao-sources a').getAttribute('href'),'https://app.hubspot.com/contacts/51412240/record/0-1/123');
   assert.match(await page.locator('#os-verificacao-limits').textContent(),/CRM activity does not prove delivery/);
   assert.equal(requests.filter(r=>r.method==='POST').length,2);
   assert.equal(requests.some(r=>r.method==='POST'&&/\/work|\/send|change-route/.test(r.p)),false);
   assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
   await page.screenshot({path:path.join(outputDir,`hermes-verificar-${viewport.width}.png`),fullPage:true});
   holdVerify=true;
   const sent=page.waitForRequest(r=>new URL(r.url()).pathname==='/api/os/v1/hermes/verify');
   const returned=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/os/v1/hermes/verify');
   await page.locator('#os-pedido button[type="submit"]').click();await sent;
   await page.locator('#os-pedido-texto').fill('Why E3?');
   assert.equal(typeof releaseVerify,'function');releaseVerify();await returned;
   await page.waitForFunction(()=>!document.querySelector('#os-pedido button[type="submit"]').disabled);
   assert.equal(await page.locator('#os-verificacao').isVisible(),false);
   assert.deepEqual(errors,[]);
   await page.close();
  }
  console.log('PASS: 1440/390, timeline, escaping, partial, stale failure, unknown empty, null outcomes, no overflow; nonce-authenticated explicit manual reply and verify; server limits survive empty model limits; edited question invalidates pending answer');
  console.log('Screenshots: '+outputDir);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
