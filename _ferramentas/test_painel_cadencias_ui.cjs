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
(async()=>{
 snapshot.contact_conflicts=[{contact_id:'123',campaign:'BR-APOLLO-01',task_campaign:'V'.repeat(100),reason:'task_campaign_differs_from_current_campaign'},{contact_id:'123',campaign:'BR-APOLLO-01',task_campaign:'<img src=x onerror=alert(1)>',reason:'task_campaign_differs_from_current_campaign'}];
 const browser=await chromium.launch({headless:true});
 try{
  for(const viewport of [{width:1440,height:1000},{width:390,height:844}]){
   const page=await browser.newPage({viewport});const errors=[],requests=[];let fail=false,empty=false,registryState=null;
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
   assert.deepEqual(errors,[]);
   await page.close();
  }
  console.log('PASS: 1440/390, timeline, escaping, partial, stale failure, unknown empty, registered empty/partial, nonempty partial totals unknown, null outcomes, provider/current-campaign labels, observed contact count, keyboard details, no overflow, GET-only on load; one nonce-authenticated manual reply POST after explicit submit');
  console.log('Screenshots: '+outputDir);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
