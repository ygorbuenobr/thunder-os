// Refero direct-build binding: NovaJornada.dc.html + existing Jornada/Conta.
// Prototype owns header/back, five numbered steps, source choices, checks, full
// sequence review and manual handoff. Panel owns dark canvas, mustard CTA,
// purple review state, system fonts, <=8px radius and unframed sections.
// Product/API constraints replace sample facts and activation claims. No DC code,
// framework, fonts, provider credentials or external network are used here.
// This is browser fixture evidence, not live provider/integration verification.
// The principal owns the existing TLC checklist and independent review.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, '_ferramentas/painel_modelo.html'), 'utf8')
  .replaceAll("/*MODO*/'artifact'", "'servidor'").replaceAll('/*DADOS*/{}', '{}');
const output = path.join(root, '.scratch/phase2-new-journey-ui');
const base = '/api/os/v1/new-journey';
const campaign = 'BR-APOLLO-01'; // Existing Registry reference, never a new campaign ID.
const id = 'journey-synthetic-browser-fixture';
const date = '2031-03-12T12:30:00Z';
const attack = '<img src=x onerror="window.injected=true">';
const csv = '\ufeffname,email,company,var_note\r\n"Pessoa, sintética",fixture@example.invalid,"Empresa ""A""","Linha 1\nLinha 2"\r\n"' + attack.replaceAll('"', '""') + '",second@example.invalid,Teste,Original\r\n';
const original = 'Texto original sintético.\n\n  Espaçamento preservado.\n' + 'Mensagem integral. '.repeat(600) + '\nFULL_ORIGINAL_END\n' + attack;
const keys = ['icp', 'exclusions', 'client', 'open_deal', 'refusal', 'membership'];
const provenance = {path:'fixtures/campaign.md', section:'Ficha sintética', status:'ok', expected_sha256:'a'.repeat(64), observed_sha256:'a'.repeat(64), source_updated_at:date};
const sheet = complete => ({status:complete?'complete':'incomplete', version:'fixture-v1', fingerprint:'c'.repeat(64),
  fields:['offer','icp','proofs','objective','restrictions','cta','language','country'].map(key=>({key,label:key,
    content:'Conteúdo original sintético ' + key + '\n' + attack, ...provenance,
    sources:key==='proofs'?[provenance,{...provenance,path:'fixtures/second.md',section:'Segunda prova'}]:undefined})), missing_fields:complete?[]:['cta']});

(async()=>{
  const browser=await chromium.launch({headless:true});
  fs.mkdirSync(output,{recursive:true});
  try{
    for(const viewport of [{width:1440,height:1100},{width:390,height:844},{width:320,height:800}]){
      const page=await browser.newPage({viewport,acceptDownloads:true});
      await page.clock.install();
      const traffic=[],errors=[];
      let draft=null,complete=false,checker='unknown',failCatalog=true,emptyCatalog=false,failGet=false,denySource=false,denySequences=false;
      let holdApprove=false,releaseApprove,selectedSequence='',observedEnrollment=false;
      const row = (n=1,email='fixture@example.invalid')=>({source_key:'source-'+n,name:'Pessoa sintética '+n,email,company:'Empresa sintética',linkedin_url:'https://www.linkedin.com/in/fixture-'+n,blockers:[],variables:{}});
      const checkRows=()=>draft.rows.map(r=>({source_key:r.source_key,checks:keys.map(key=>({key,status:checker,
        reason:checker==='pass'?'Prova sintética verificada.':'identity_unlinked',source:'fixtures/checks.md · '+date,checked_at:date}))}));
      const view=()=>{
        const eligible=draft.rows.filter(r=>r.email&&draft.checks.find(c=>c.source_key===r.source_key)?.checks.every(c=>c.status==='pass')).length;
        const blockers=[];
        if(!complete)blockers.push('journey_sheet_incomplete');
        if(!eligible)blockers.push('journey_no_eligible_rows');
        if(!draft.steps.some(s=>s.channel==='email'))blockers.push('journey_email_steps_required');
        if(!draft.sequence)blockers.push('journey_sequence_required');
        const steps=draft.steps.map(s=>({...s,approval_status:draft.approvals[s.id]?'approved':'pending',can_approve:!blockers.length,blockers:[...blockers]}));
        if(steps.some(s=>s.approval_status!=='approved'))blockers.push('journey_step_approval_required');
        return {...structuredClone(draft),sheet:sheet(complete),review_hash:String(draft.version).padStart(64,'0'),
          steps,rows:draft.rows.map(r=>({...r,eligible:!!(r.email&&draft.checks.find(c=>c.source_key===r.source_key)?.checks.every(c=>c.status==='pass')),
            checks:draft.checks.find(c=>c.source_key===r.source_key)?.checks||keys.map(key=>({key,status:'unknown',reason:'Prova ausente.'}))})),
          blockers,can_export:!blockers.length,counts:{total:draft.rows.length,eligible,retained:draft.rows.length-eligible},
          enrollment_enabled:false,external_writes:0,cost:{credits:0,paid_lookup_enabled:false},credits:0};
      };
      const changed=()=>{draft.version++;return view();};
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('**/*',async route=>{
        const req=route.request(),url=new URL(req.url()),body=req.postDataJSON();
        assert.equal(url.host,'new-journey.test','No external network allowed');
        traffic.push({path:url.pathname,method:req.method(),body});
        const send=(value,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
        if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
        if(url.pathname===base+'/catalog'){
          if(failCatalog)return send({erro:'Catálogo temporariamente indisponível.'},503);
          return send({campaigns:emptyCatalog?[]:[{id:campaign,platform:'apollo',active:true,sheet:sheet(complete)}],
            drafts:draft?[{id,name:draft.name,campaign_id:campaign,version:draft.version,updated_at:date}]:[],enrollment_enabled:false});
        }
        if(url.pathname===base+'/'+id&&req.method()==='GET')return failGet?send({erro:'Leitura indisponível.'},503):send(view());
        if(url.pathname.startsWith(base+'/')&&req.method()==='POST'){
          assert.equal(req.headers()['x-thunder-nonce'],'fixture-nonce','Existing nonce mechanism is mandatory');
          const op=url.pathname.split('/').at(-1);
          if(op==='create'){
            assert.deepEqual(Object.keys(body).sort(),['campaign_id','name']);assert.equal(body.campaign_id,campaign);
            assert.equal(draft,null,'No double create');
            draft={id,version:1,name:body.name,campaign_id:campaign,source:null,rows:[],checks:[],steps:[],approvals:{},sequence:null,reconciliation:null};
            return send(view());
          }
          assert.equal(body.expected_version,draft.version,'Every operation binds current version');
          if(op==='source'){
            if(denySource)return send({erro:'Busca sem permissão na fixture.'},403);
            assert.deepEqual(Object.keys(body).sort(),(body.mode==='csv'?['expected_version','mode','csv_text']:body.mode==='apollo_people'?['expected_version','mode','filters','page']:['expected_version','mode','query','page']).sort());
            draft.source={mode:body.mode,complete:body.mode!=='apollo_people'};
            if(body.mode==='csv'){assert.equal(body.csv_text,csv,'Original CSV, BOM, escaped quotes and quoted newline survive');draft.rows=[row(),row(2,'second@example.invalid')];}
            else if(body.mode==='apollo_people'){
              assert.deepEqual(body.filters,{person_titles:['Diretor','Gerente'],person_locations:['Brasil'],q_keywords:'industrial'});
              assert.equal(body.page,1);draft.rows=[row(3,'')];
            }else{assert.equal(body.query,'Contato confirmado');assert.equal(body.page,1);draft.rows=[row()];}
            draft.checks=[];draft.approvals={};draft.reconciliation=null;return send(changed());
          }
          if(op==='check'){assert.deepEqual(Object.keys(body),['expected_version']);draft.checks=checkRows();draft.approvals={};return send(changed());}
          if(op==='sequences'){
            assert.deepEqual(Object.keys(body).sort(),['expected_version','query']);
            if(denySequences)return send({erro:'Apollo sem permissão para consultar sequências.'},403);
            assert.equal(body.query,'Sequência confirmada pelo usuário');
            return send({status:'ok',credit_cost:0,journey_id:id,version:draft.version,items:[{id:'apollo-sequence-fixture',name:'Sequência sintética retornada pela API',active:true,archived:false,user_id:'fixture-owner'}]});
          }
          if(op==='sequence'){
            assert.deepEqual(Object.keys(body).sort(),['apollo_sequence_id','apollo_sequence_query','expected_version','steps']);
            assert.equal(body.apollo_sequence_id,'apollo-sequence-fixture');assert.equal(body.apollo_sequence_query,'Sequência confirmada pelo usuário');
            assert.equal(body.steps[0].content,original+(body.steps[0].subject==='Revisado'?'\nRevisão local.':''));
            draft.steps=body.steps;draft.approvals={};draft.sequence={apollo_sequence_id:body.apollo_sequence_id,query:body.apollo_sequence_query};selectedSequence=body.apollo_sequence_id;return send(changed());
          }
          if(op==='approve-step'){
            assert.deepEqual(Object.keys(body).sort(),['expected_version','review_hash','step_id']);assert.equal(body.review_hash,view().review_hash);
            assert.equal(view().steps.find(s=>s.id===body.step_id).can_approve,true,'Fixture mirrors core can_approve');
            if(holdApprove)await new Promise(resolve=>{releaseApprove=resolve;});
            draft.approvals[body.step_id]={scope_hash:'fixture-only'};return send(changed());
          }
          if(op==='export'){
            assert.equal(view().can_export,true);assert.equal(body.review_hash,view().review_hash);assert.equal(selectedSequence,'apollo-sequence-fixture');
            assert.deepEqual(Object.keys(body).sort(),['expected_version','review_hash']);
            return send({filename:'fixture-approved.csv',csv_text:'\ufeffname,email\r\n"Pessoa, sintética",fixture@example.invalid\r\n',rows:1,credits:0,review_hash:body.review_hash,external_writes:0,enrollment_enabled:false});
          }
          if(op==='reconcile'){
            assert.deepEqual(Object.keys(body),['expected_version']);
            draft.reconciliation={status:observedEnrollment?'observed':'configuration_pending',observed_at:date,sequence_id:observedEnrollment?selectedSequence:null,rows:draft.rows.map(r=>({source_key:r.source_key,status:observedEnrollment?'enrolled':'unknown',reason:observedEnrollment?'sequence_membership_observed':'configuration_pending'}))};
            return send(changed());
          }
          assert.fail('No other POST operation is allowed: '+op);
        }
        assert.equal(req.method(),'GET','Other tabs must not mutate during this journey');
        if(url.pathname==='/api/os/v1/session')return send({actor:'ygor',nonce:'fixture-nonce'});
        if(url.pathname==='/api/os/v1/journey')return send({read_only:true,collected_at:date,campaigns:[],metrics:{},stages:[],timeline:{events:[]},log:[]});
        if(url.pathname==='/api/os/v1/panel-capabilities')return send({legacy_mutations:false});
        if(url.pathname==='/api/os/v1/cadencias')return send({campaigns:[],today:{},unlinked:[]});
        if(url.pathname==='/api/os/v1/today')return send({priorities:[],work:[],cadences:[],replies:[],controls:{},usage:[],alerts:[]});
        return send([]);
      });
      const settled=()=>page.waitForFunction(()=>document.querySelector('#nj-body').getAttribute('aria-busy')==='false');
      const go=async step=>{await page.locator('#nj-steps [data-nj-step="'+step+'"]').click();await settled();assert.equal(await page.locator('#nj-steps [aria-current=step]').getAttribute('data-nj-step'),String(step));};
      const click=async selector=>{await page.locator(selector).click();await settled();};
      const noOverflow=async()=>{
        assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
        const bad=await page.locator('#new-journey *').evaluateAll(nodes=>nodes.filter(n=>n.getClientRects().length&&getComputedStyle(n).position!=='absolute').filter(n=>n.getBoundingClientRect().right>innerWidth+1||n.getBoundingClientRect().left< -1).map(n=>n.id||n.className));
        assert.deepEqual(bad,[],'Every visible element fits, not just the clipped page');
      };
      const shot=async name=>{await noOverflow();await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:path.join(output,name+'-'+viewport.width+'.png'),fullPage:true});};
      await page.goto('https://new-journey.test/#jornada');
      await page.locator('#journey-new').click();await settled();
      assert.equal(await page.locator('#nj-title').evaluate(n=>n===document.activeElement),true);
      assert.match(await page.locator('#nj-status').innerText(),/Catálogo temporariamente/);
      failCatalog=false;emptyCatalog=true;await click('#nj-retry');
      assert.match(await page.locator('#nj-body').innerText(),/Nenhuma campanha Apollo BR/);
      assert.equal(await page.locator('[data-nj-resume]').count(),0);
      assert.equal(await page.locator('#nj-create').isDisabled(),true);
      emptyCatalog=false;await page.locator('#nj-back').click();await page.locator('#journey-new').click();
      // A fresh entry refreshes the catalog, including saved drafts.
      await page.reload();await settled();
      assert.deepEqual(await page.locator('#abas [role=tab]').evaluateAll(nodes=>nodes.map(n=>n.firstChild.textContent)),['Jornada','Campanhas','Contas','Sistema']);
      assert.equal(await page.locator('#abas [aria-selected=true]').getAttribute('data-aba'),'jornada');
      assert.equal(await page.locator('#nj-steps button').count(),5);
      await page.locator('#nj-name').fill('Jornada sintética '+attack);
      await page.locator('#nj-campaign').selectOption(campaign);
      assert.match(await page.locator('#nj-body').innerText(),/Ficha incompleta/);
      assert.match(await page.locator('#nj-body').innerText(),/SHA-256 esperado: a{64}/);
      await page.locator('.nj-sheet details summary').first().click();
      await shot('01-ficha');
      await click('#nj-create');assert.match(page.url(),new RegExp('#jornada/nova/'+id+'/1$'));
      await go(2);assert.match(await page.locator('#nj-body').innerText(),/Desconhecido/);
      await page.locator('#nj-file').setInputFiles({name:'too-large.csv',mimeType:'text/csv',buffer:Buffer.alloc(100001,65)});
      assert.match(await page.locator('#nj-status').innerText(),/excede 100 KB/);
      await page.locator('#nj-file').setInputFiles({name:'fixture.csv',mimeType:'text/csv',buffer:Buffer.from(csv)});
      await page.waitForFunction(expected=>document.querySelector('#nj-csv')?.value===expected&&document.querySelector('#nj-body').getAttribute('aria-busy')==='false',csv.replaceAll('\r\n','\n'));
      assert.equal(await page.locator('#nj-csv').inputValue(),csv.replaceAll('\r\n','\n'),'Textarea normalizes display; wire assertion preserves original upload');
      assert.match(await page.locator('#nj-csv-preview').innerText(),/2 contatos/);
      denySource=true;await click('#nj-source-form button');assert.match(await page.locator('#nj-status').innerText(),/Sem permissão/);
      denySource=false;await click('#nj-reload');assert.equal(await page.locator('#nj-csv').inputValue(),csv.replaceAll('\r\n','\n'));
      await click('#nj-source-form button');await shot('02-fonte');
      await go(3);await click('[data-nj-action=check]');
      assert.match(await page.locator('#nj-body').innerText(),/identidade desta pessoa ainda não foi vinculada/);
      assert.equal(await page.locator('.nj-record details[open]').count(),0,'Proof details start collapsed, preserving one-row/person binding');
      await page.locator('.nj-record summary').first().click();
      assert.equal(await page.locator('.nj-record details').first().locator('li').count(),6,'All six checks remain available');
      assert.match(await page.locator('.nj-record details').first().innerText(),/Desconhecido · retido/);
      assert.match(await page.locator('.nj-record details').first().innerText(),/Exclusões/);
      assert.match(await page.locator('.nj-record details').first().innerText(),/fixtures\/checks.md/);
      assert.match(await page.locator('.nj-record details').first().innerText(),/Conferida em: 2031-03-12/);
      await page.locator('.nj-record summary').first().click();await shot('03-checagem-retida');
      await go(5);assert.equal(await page.locator('[data-nj-action=export]').isDisabled(),true);
      await go(2);await page.locator('[name=nj-source][value=apollo_people]').check();
      assert.equal(await page.locator('#nj-titles').inputValue(),'','Sheet hypotheses never become filters');
      await click('#nj-source-form button');assert.match(await page.locator('#nj-status').innerText(),/filtros explícitos/);
      await page.locator('#nj-titles').fill('Diretor;Gerente');await page.locator('#nj-locations').fill('Brasil');await page.locator('#nj-keywords').fill('industrial');
      await click('#nj-source-form button');assert.match(await page.locator('#nj-body').innerText(),/Cobertura parcial/);
      await page.locator('[name=nj-source][value=apollo_contacts]').check();await page.locator('#nj-query').fill('Contato confirmado');await click('#nj-source-form button');
      checker='pass';complete=true;await go(3);await click('[data-nj-action=check]');await shot('03-checagem-verificada');
      await go(4);assert.equal(await page.locator('#nj-sequence-id').inputValue(),'');
      await page.locator('#nj-sequence-query').fill('Sequência confirmada pelo usuário');
      denySequences=true;await click('[data-nj-action=sequences]');assert.match(await page.locator('#nj-status').innerText(),/Sem permissão/);
      assert.equal(await page.locator('#nj-sequence-id option').count(),1,'403 has no fallback sequence');
      denySequences=false;await click('[data-nj-action=sequences]');await page.locator('#nj-sequence-id').selectOption('apollo-sequence-fixture');
      await click('[data-nj-action=add-step]');await page.locator('#nj-subject-0').fill('Original');await page.locator('#nj-content-0').fill(original);
      assert.equal(await page.locator('[data-nj-approve]').isDisabled(),true,'Explicit save precedes approval');
      await click('[data-nj-action=save-sequence]');assert.equal(await page.locator('#nj-content-0').inputValue(),original);
      assert.equal(await page.locator('[data-nj-approve]').isEnabled(),true);await shot('04-sequencia');
      holdApprove=true;await page.locator('[data-nj-approve]').click();await page.waitForFunction(()=>document.querySelector('[data-nj-approve]').disabled);
      await page.waitForTimeout(20);assert.equal(traffic.filter(t=>t.path.endsWith('/approve-step')).length,1);
      holdApprove=false;releaseApprove();await settled();
      assert.match(await page.locator('[data-nj-approval-state]').innerText(),/Aprovado no servidor/);
      await go(5);assert.equal(await page.locator('[data-nj-action=export]').isEnabled(),true);await shot('05-manual');
      await go(4);await page.locator('#nj-content-0').fill(original+'\nRevisão local.');await page.locator('#nj-subject-0').fill('Revisado');
      assert.match(await page.locator('[data-nj-approval-state]').innerText(),/invalidada/);
      await go(5);assert.equal(await page.locator('[data-nj-action=export]').isDisabled(),true,'Unsaved edit disables export');
      await go(4);assert.equal(await page.locator('#nj-content-0').inputValue(),original+'\nRevisão local.');
      await click('[data-nj-action=save-sequence]');await click('[data-nj-approve]');await go(5);
      const downloaded=page.waitForEvent('download');await click('[data-nj-action=export]');const download=await downloaded;
      assert.equal(download.suggestedFilename(),'fixture-approved.csv');
      const stream=await download.createReadStream();let exported='';for await(const chunk of stream)exported+=chunk.toString();
      assert.match(exported,/"Pessoa, sintética"/);assert.match(await page.locator('#nj-status').innerText(),/ainda não confirmada/);
      await click('[data-nj-action=reconcile]');assert.match(await page.locator('#nj-enrollment').innerText(),/Desconhecido.*não configurada/);
      assert.match(await page.locator('#nj-body').innerText(),/Inscrição automática: OFF/);
      await shot('05-reconciliacao-desconhecida');
      observedEnrollment=true;await click('[data-nj-action=reconcile]');assert.match(await page.locator('#nj-enrollment').innerText(),/1 inscrições observadas.*não confirma envio/);
      failGet=true;await click('#nj-reload');assert.match(await page.locator('#nj-status').innerText(),/Leitura indisponível/);
      assert.equal(await page.locator('[data-nj-action=export]').isDisabled(),true);
      failGet=false;await click('#nj-reload');await page.reload();await settled();
      assert.equal(await page.locator('#nj-steps [aria-current=step]').getAttribute('data-nj-step'),'5','Hash restores draft and step');
      await go(4);assert.equal(await page.locator('#nj-content-0').inputValue(),original+'\nRevisão local.');
      await page.locator('#nj-steps button').first().focus();await page.keyboard.press('End');assert.equal(await page.locator('#nj-steps [aria-current=step]').getAttribute('data-nj-step'),'5');
      for(const tab of ['cadencias','contas','sistema','jornada']){await page.locator('#abas [data-aba='+tab+']').click();assert.equal(await page.locator('#abas [aria-selected=true]').getAttribute('data-aba'),tab);}
      await page.locator('#journey-new').click();await page.reload();await settled();
      assert.equal(await page.locator('[data-nj-resume]').count(),1);await click('[data-nj-resume]');assert.match(page.url(),new RegExp(id+'/1$'));
      assert.equal(await page.locator('#new-journey img,#new-journey script').count(),0);assert.equal(await page.evaluate(()=>window.injected),undefined);
      assert.deepEqual(errors,[]);await noOverflow();
      assert.equal(traffic.filter(t=>t.method!=='GET'&&!t.path.startsWith(base+'/')).length,0,'No effect calls');
      console.log('PASS '+viewport.width+': five steps/catalog/error+403/retry/CSV bounds+escaping/newlines/free searches/exact sequence ID/full text/save+approve/stale invalidation/export/reconcile/resume/hash/keyboard/reflow/no external network');
      await page.close();
    }
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
