// Refero direct-build binding: NovaJornada.dc.html + existing Jornada/Conta.
// Prototype owns header/back, five numbered steps, source choices, checks, full
// sequence review and manual handoff. Panel owns dark canvas, mustard CTA,
// purple review state, system fonts, <=8px radius and unframed sections.
// F2-2: exact trail/source order, per-screen containment, two activation regions
// and local completion are asserted below; this does not resolve real checker P1.
// Product/API constraints replace sample facts and activation claims. No DC code,
// framework, fonts, provider credentials or external network are used here.
// This is browser fixture evidence, not live provider/integration verification.
// The principal owns the existing TLC checklist and independent review.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {createHash} = require('node:crypto');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const sourceHTML = fs.readFileSync(path.join(root, '_ferramentas/painel_modelo.html'), 'utf8');
const html = sourceHTML.replaceAll("/*MODO*/'artifact'", "'servidor'").replaceAll('/*DADOS*/{}', '{}');
const output = path.join(root, '.scratch/phase2-new-journey-ui');
const base = '/api/os/v1/new-journey';
const campaign = 'BR-APOLLO-01'; // Existing Registry reference, never a new campaign ID.
const id = 'journey-synthetic-browser-fixture';
const date = '2031-03-12T12:30:00Z';
const attack = '<img src=x onerror="window.injected=true">';
const csv = '\ufeffname,email,company,var_note\r\n"Pessoa, sintética",fixture@example.invalid,"Empresa ""A""","Linha 1\nLinha 2"\r\n"' + attack.replaceAll('"', '""') + '",second@example.invalid,Teste,Original\r\n';
const original = 'Texto original sintético.\n\n  Espaçamento preservado.\n' + 'Mensagem integral. '.repeat(600) + '\nFULL_ORIGINAL_END\n' + attack;
const promptOriginal = 'Instrução original Apollo.\n  Preservar {{company.name}} e {{thunder_research}}.\n' + attack;
const promptVariants = [{touch_id:'touch-fixture',template_id:'template-fixture',subject:'Assunto original {{first_name}}',content:'Olá {{first_name}}\n{{thunder_research}}',body_text:'Olá {{first_name}}\n{{thunder_research}}',body_html:null,template_type:'prompt_template',has_ai_variables:true,include_signature:false,prompt_template:{id:'prompt-fixture',team_id:'team-fixture',prompt_template:promptOriginal,system_instructions:'Só provas.',instructions:'Não inventar métricas.',raw_variables:{thunder_research:{value:'Pesquisa sintética original.',fallback:'Sem prova'}}},generation_options:[{field_name:'thunder_research',fallback:'Sem prova'}],generation_tone:'formal',attachments:[]}];
const keys = ['icp', 'exclusions', 'client', 'open_deal', 'refusal', 'membership'];
const provenance = {path:'fixtures/campaign.md', section:'Ficha sintética', status:'ok', expected_sha256:'a'.repeat(64), observed_sha256:'a'.repeat(64), source_updated_at:date};
const sheet = complete => ({status:complete?'complete':'incomplete', version:'fixture-v1', fingerprint:'c'.repeat(64),
  fields:['offer','icp','proofs','objective','restrictions','cta','language','country'].map(key=>({key,label:key,
    content:'Conteúdo original sintético ' + key + '\n' + attack, ...provenance,
    sources:key==='proofs'?[provenance,{...provenance,path:'fixtures/second.md',section:'Segunda prova'}]:undefined})), missing_fields:complete?[]:['cta']});

(async()=>{
  console.log('HTML SHA-256 '+createHash('sha256').update(sourceHTML).digest('hex')+'; current worktree fixture; no server/provider HTTP');
  const browser=await chromium.launch({headless:true});
  fs.mkdirSync(output,{recursive:true});
  const deficiencies=[];
  try{
    for(const viewport of [{width:1440,height:1100},{width:390,height:844},{width:320,height:800}]){
      const page=await browser.newPage({viewport,acceptDownloads:true});
      await page.clock.install();
      const traffic=[],errors=[],downloads=[];
      const uiCheck=(ok,message)=>{if(!ok)deficiencies.push(viewport.width+': '+message);};
      let draft=null,complete=false,checker='unknown',failCatalog=true,emptyCatalog=false,failGet=false,denySource=false,denySequences=false;
      let holdApprove=false,releaseApprove,selectedSequence='',observedEnrollment=false;
      let parityStatus='verified',readinessType='auto_email';
      let companyPreview=null,promptHash='e'.repeat(64);
      const row = (n=1,email='fixture@example.invalid')=>({source_key:'source-'+n,name:'Pessoa sintética '+n,email,company:'Empresa sintética',linkedin_url:'https://www.linkedin.com/in/fixture-'+n,blockers:[],variables:{}});
      const checkRows=()=>draft.rows.map(r=>({source_key:r.source_key,qualification_context:checker==='pass'?{company_id:'123',contact_id:'42',company_name:'Empresa sintética',domain:'example.invalid',binding_hash:'f'.repeat(64)}:null,checks:keys.map(key=>({key,status:checker,
        reason:checker==='pass'?'Prova sintética verificada.':'identity_unlinked',source:'fixtures/checks.md · '+date,checked_at:date}))}));
      const view=()=>{
        const eligible=draft.rows.filter(r=>r.email&&draft.checks.find(c=>c.source_key===r.source_key)?.checks.every(c=>c.status==='pass')).length;
        const blockers=[];
        if(!complete)blockers.push('journey_sheet_incomplete');
        if(!eligible)blockers.push('journey_no_eligible_rows');
        if(!draft.steps.some(s=>s.channel==='email'))blockers.push('journey_email_steps_required');
        if(!draft.sequence)blockers.push('journey_sequence_required');
        if(draft.sequence&&parityStatus!=='verified')blockers.push('journey_apollo_text_unverified');
        const steps=draft.steps.map(s=>({...s,approval_status:draft.approvals[s.id]?'approved':'pending',can_approve:!blockers.length,blockers:[...blockers]}));
        if(steps.some(s=>s.approval_status!=='approved'))blockers.push('journey_step_approval_required');
        return {...structuredClone(draft),approval_mode:draft.approval_mode||'exact_copy_v1',sheet:sheet(complete),review_hash:String(draft.version).padStart(64,'0'),
          commercial_accounts:checker==='pass'?Object.fromEntries(draft.rows.map(r=>[r.source_key,{status:'available',company:r.company,contact:r.name,
            source:{path:'fixtures/account.md',sha256:'8'.repeat(64),updated_at:date},
            sections:['business','fit','moment','people','thesis','next_action'].map(key=>({key,items:[
              {text:'COMMERCIAL_'+key+' · '+attack,classification:key==='thesis'?'hipotese':'nao_classificado',published_on:key==='moment'?'2023-01-01':null,source_url:key==='fit'?'javascript:alert(1)':'https://example.invalid/research',context_only:key==='moment'},
              {text:'SECOND_'+key,classification:'reserva'}, {text:'LONG_ORIGINAL_'+key+' '.repeat(2)+'Detalhe original. '.repeat(100),classification:'nao_classificado'}]})),
            originals:{account_id:'source-fixture',source_text:'CRM histórico de 2023: não existe contato. ORIGINAL_RESEARCH_END',tabs:{context:[{path:'fixtures/brain.md',sha256:'7'.repeat(64)}]}},limitations:['synthetic_fixture']
          }])):{},
          steps,rows:draft.rows.map(r=>({...r,eligible:!!(r.email&&draft.checks.find(c=>c.source_key===r.source_key)?.checks.every(c=>c.status==='pass')),
            checks:draft.checks.find(c=>c.source_key===r.source_key)?.checks||keys.map(key=>({key,status:'unknown',reason:'Prova ausente.'}))})),
          blockers,can_export:!blockers.length,counts:{total:draft.rows.length,eligible,retained:draft.rows.length-eligible},
          apollo_text_parity:{status:parityStatus,reason:parityStatus==='verified'?'apollo_static_text_exact_match':'apollo_template_text_mismatch',steps:draft.steps.map(s=>({...s,...(draft.approval_mode==='apollo_prompts_v1'?{variants:promptVariants}:{})})),...(draft.approval_mode==='apollo_prompts_v1'?{input_hash:promptHash,approval_kind:'prompts',final_email_parity:false}:{})},
          operational_readiness:{execution_ready:false,pilot:{current:false,e1_observed_type:readinessType}},
          enrollment_enabled:false,external_writes:0,cost:{credits:0,paid_lookup_enabled:false},credits:0};
      };
      const changed=()=>{draft.version++;return view();};
      page.on('pageerror',e=>errors.push(e.message));
      page.on('download',d=>downloads.push(d.suggestedFilename()));
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
          if(op==='hubspot-execute'){
            assert.deepEqual(body,{preview_id:'import-fixture',expected_version:1,preview_hash:'d'.repeat(64),confirm:true});
            return send({kind:'journey_import_v1',id:'import-fixture',version:2,journey_id:id,journey_version:draft.version,source_key:'source-1',preview_hash:'d'.repeat(64),portal_id:'51412240',state:'processed',can_execute:false,contact:{properties:{email:'fixture@example.invalid'}},company:{properties:{name:'Empresa sintética',domain:'example.invalid'}},association:{status:'verified'},blockers:[],result:{status:'verified_state'}});
          }
          if(op==='company-execute'){
            assert(companyPreview,'An exact company preview must precede confirmation');
            assert.deepEqual(body,{preview_id:companyPreview.id,expected_version:companyPreview.version,preview_hash:companyPreview.preview_hash,confirm:true});
            assert.equal(companyPreview.journey_version,draft.version,'Confirmation cannot apply a stale draft');
            assert.equal(companyPreview.kind,'company_qualification_v1');
            assert.equal(companyPreview.plan.method,'PATCH');
            assert.equal(companyPreview.plan.record_id,'123','Only the bound existing company is proposed');
            assert.equal(traffic.filter(t=>t.path.endsWith('/company-execute')).length,1,'A single explicit confirmation');
            draft.qualifications={'source-1':{actor:'ygor',reviewed_at:date,facts:companyPreview.facts}};
            draft.approvals={};draft.version++;
            return send({...companyPreview,state:'processed',write_enabled:false,result:{status:'verified_state'},external_writes:1});
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
            }else{assert.equal(body.query,'Contato confirmado');assert.equal(body.page,1);draft.rows=[row(),row(2,'second@example.invalid')];}
            draft.checks=[];draft.approvals={};draft.reconciliation=null;return send(changed());
          }
          if(op==='hubspot-preview'){
            assert.equal(body.review_hash,view().review_hash);assert.equal(body.data.source_key,'source-1');
            assert.equal(body.data.contact.email,'fixture@example.invalid');assert.equal(body.data.company.domain,'example.invalid');
            assert(Object.values(body.data.source_refs).every(r=>r.verified===true&&r.ref==='https://example.invalid/source'));
            return send({kind:'journey_import_v1',id:'import-fixture',version:1,journey_id:id,journey_version:draft.version,source_key:'source-1',preview_hash:'d'.repeat(64),portal_id:'51412240',state:'review_required',can_execute:true,contact:{properties:body.data.contact,action:'propose_create'},company:{properties:body.data.company,action:'propose_create'},association:{status:'planned'},source_refs:body.data.source_refs,blockers:[]});
          }
          if(op==='check'){assert.deepEqual(Object.keys(body),['expected_version']);draft.checks=checkRows();draft.approvals={};return send(changed());}
          if(op==='company-preview'){
            assert.deepEqual(Object.keys(body).sort(),['expected_version','facts','review_hash','source_key']);
            assert.equal(body.review_hash,view().review_hash);
            assert.equal(body.source_key,'source-1');assert.equal(body.facts.is_b2b,true);
            assert.equal(body.facts.annual_revenue_brl,20000000);assert.equal(body.facts.country,'BR');
            assert.equal(body.facts.source_url,'https://example.invalid/report');
            assert.equal(body.facts.source_date,'2031-03-01');
            assert.equal(body.facts.not_customer,true);assert.equal(body.facts.not_excluded,true);
            assert.equal(body.facts.decision,'qualified');assert.equal(body.facts.sector,'industria_b2b');
            assert.deepEqual(Object.keys(body.facts).sort(),['annual_revenue_brl','country','decision','is_b2b','not_customer','not_excluded','rationale','sector','source_date','source_url']);
            const changes={thunder_icp_status:'qualified',thunder_is_customer:'false',thunder_icp_exclusion_reason:'',country:'BR',hs_country_code:'BR',thunder_icp_sector:'industria_b2b',thunder_annual_revenue_estimate:'20000000',thunder_last_enriched_at:'2031-03-12',thunder_enrichment_source:JSON.stringify(body.facts)};
            companyPreview={kind:'company_qualification_v1',id:'company-fixture',version:1,journey_id:id,journey_version:draft.version,source_key:'source-1',preview_hash:'b'.repeat(64),company_identity:{company_id:'123',company_name:'Empresa sintética',domain:'example.invalid'},facts:body.facts,before:{properties:{thunder_icp_status:'unknown',thunder_is_customer:'',thunder_icp_exclusion_reason:''}},after:{properties:changes},plan:{method:'PATCH',record_id:'123',changes},write_enabled:true,state:'review_required',external_writes:0};
            return send(companyPreview);
          }
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
          if(op==='apollo-prompts'){
            assert.deepEqual(body,{expected_version:draft.version,apollo_sequence_id:'apollo-sequence-fixture',apollo_sequence_query:'Sequência confirmada pelo usuário'});
            draft.approval_mode='apollo_prompts_v1';draft.approvals={};
            draft.steps=[{id:'prompt-step-1',channel:'email',subject:'Assunto gerado pelo Apollo',content:'Corpo gerado pelo Apollo',order:1,interval_hours:0}];
            return send(changed());
          }
          if(op==='approve-step'){
            assert.deepEqual(Object.keys(body).sort(),['expected_version','review_hash','step_id']);assert.equal(body.review_hash,view().review_hash);
            assert.equal(view().steps.find(s=>s.id===body.step_id).can_approve,true,'Fixture mirrors core can_approve');
            if(holdApprove)await new Promise(resolve=>{releaseApprove=resolve;});
            draft.approvals[body.step_id]={scope_hash:'fixture-only',kind:draft.approval_mode||'exact_copy_v1',...(draft.approval_mode==='apollo_prompts_v1'?{input_hash:promptHash}:{})};return send(changed());
          }
          if(op==='export'||op==='export-preview'){
            assert.equal(view().can_export,true);assert.equal(body.review_hash,view().review_hash);assert.equal(selectedSequence,'apollo-sequence-fixture');
            assert.deepEqual(Object.keys(body).sort(),['expected_version','review_hash']);
            const prompts=draft.approval_mode==='apollo_prompts_v1';
            return send({filename:'fixture-approved.csv',csv_text:'\ufeffname,email'+(prompts?',approval_kind,approved_prompt_hash':'')+'\r\n"Pessoa, sintética",fixture@example.invalid'+(prompts?',apollo_prompts_v1,'+promptHash:'')+'\r\nPessoa sintética 2,second@example.invalid'+(prompts?',apollo_prompts_v1,'+promptHash:'')+'\r\n',rows:2,credits:0,review_hash:body.review_hash,external_writes:0,enrollment_enabled:false,approval_kind:draft.approval_mode||'exact_copy_v1',...(op==='export-preview'?{preview_only:true}:{})});
          }
          if(op==='reconcile'){
            assert.deepEqual(Object.keys(body),['expected_version']);
            draft.reconciliation={status:observedEnrollment?'observed':'configuration_pending',observed_at:date,pagination:{processed_count:observedEnrollment?draft.rows.length:0,total:draft.rows.length},sequence_id:observedEnrollment?selectedSequence:null,rows:draft.rows.map(r=>({source_key:r.source_key,status:observedEnrollment?'enrolled':'unknown',reason:observedEnrollment?'sequence_membership_observed':'configuration_pending'}))};
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
      const composition=async()=>{
        const result=await page.locator('#new-journey').evaluate(root=>{
          const failures=[],check=(ok,label)=>{if(!ok)failures.push(label);};
          const before=(a,b)=>!!(a&&b&&(a.compareDocumentPosition(b)&Node.DOCUMENT_POSITION_FOLLOWING));
          const contains=(parent,...nodes)=>!!parent&&nodes.every(n=>n&&parent.contains(n));
          const fits=(parent,child)=>{if(!parent||!child)return false;const p=parent.getBoundingClientRect(),c=child.getBoundingClientRect();return c.left>=p.left-1&&c.right<=p.right+1&&c.top>=p.top-1&&c.bottom<=p.bottom+1;};
          const header=root.querySelector(':scope > header'),trail=root.querySelector('#nj-steps'),body=root.querySelector('#nj-body'),footer=root.querySelector('.nj-bottom');
          const current=trail.querySelector('[aria-current="step"]'),step=Number(current.dataset.njStep),panel=body.querySelector(':scope > section'),paths=body.querySelector(':scope > [aria-label="Tipos de cadência"]');
          check(JSON.stringify([...trail.querySelectorAll('button')].map(n=>n.innerText.replace(/\s+/g,' ').trim()))===JSON.stringify(['01 Ficha','02 Fonte','03 Checagem','04 Sequência','05 Ativar manual']),'literal five-step trail order');
          check(contains(header,root.querySelector('#nj-title'),root.querySelector('#nj-back')),'header owns title and return');
          check(before(header,trail)&&before(trail,body)&&before(body,footer),'header, trail, screen, footer order');
          check(body.children.length===2&&contains(body,paths)&&before(paths,panel)&&['Apollo:','Instantly:','Thunder:'].every(label=>paths?.textContent.includes(label)),'three distinct cadence paths precede the selected screen');
          check(panel.id==='nj-panel-'+step&&panel.firstElementChild.id==='nj-heading','one named region for the selected screen');
          check(fits(root,header)&&fits(root,trail)&&fits(root,body)&&fits(root,footer)&&fits(body,panel),'primary regions remain contained');
          check(!panel.contains(footer)&&contains(footer,root.querySelector('#nj-prev'),root.querySelector('#nj-next')),'navigation stays outside screen content');
          if(step===1){
            const form=panel.querySelector('#nj-create-form'),sheet=panel.querySelector('.nj-sheet');
            check(contains(panel,sheet),'Ficha owns source-backed facts');
            if(form)check(contains(form,form.querySelector('.nj-grid'),form.querySelector('#nj-name'),form.querySelector('#nj-campaign'))&&before(form,sheet),'Ficha identification and reference precede facts');
            check([...sheet.children].every(n=>n.querySelector('dt')&&n.querySelector('dd')&&fits(sheet,n)),'fact/source groups stay within Ficha');
          }
          if(step===2){
            const sources=panel.querySelector('.nj-sources'),blocks=[...sources.querySelectorAll(':scope > .nj-source')],cost=panel.querySelector('.nj-cost'),form=panel.querySelector('#nj-source-form');
            check(JSON.stringify(blocks.map(n=>n.querySelector('strong').textContent))===JSON.stringify(['Buscar no Apollo','Importar lista do Apollo','Vagas abertas','Engajadores de posts']),'four literal source blocks in binding order');
            check(blocks.every(n=>n.querySelector('p')&&n.querySelector('small')&&fits(sources,n)),'every source owns title, description and availability');
            check(contains(blocks[0],panel.querySelector('#nj-apollo-modes'))&&panel.querySelectorAll('[name="nj-source"]').length===2,'saved contacts belong inside Apollo, not another source');
            check(blocks.slice(2).every(n=>n.getAttribute('aria-disabled')==='true'&&!n.querySelector('button,input,a,select,[tabindex]')),'unavailable sources have no action');
            check(contains(panel,sources,cost,form)&&before(sources,cost)&&before(cost,form),'source selection, count/cost, input order and containment');
          }
          if(step===3){
            const intro=panel.querySelector(':scope > p'),records=[...panel.querySelectorAll('.nj-record')];
            check(records.length>0&&before(intro,records[0]),'check introduction precedes the people');
            for(const record of records){
              const h=record.querySelector(':scope > header'),identity=h.firstElementChild,verdict=h.querySelector(':scope > .nj-state'),research=record.querySelector('.nj-commercial,.nj-commercial-empty'),checks=record.querySelector(':scope > .nj-live-checks'),details=record.querySelector(':scope > details');
              check(contains(h,identity.querySelector('h4'),identity.querySelector('.msg'),verdict)&&!identity.contains(verdict)&&before(h,research)&&before(research,checks)&&before(checks,details),'identity, commercial context, current checks and diagnostic in binding order');
              check(fits(record,h)&&fits(h,identity)&&fits(h,verdict),'identity and verdict stay in their record header');
              check(JSON.stringify([...checks.querySelectorAll('[data-nj-check]')].map(n=>n.dataset.njCheck))===JSON.stringify(['icp','exclusions','client','open_deal','refusal','membership']),'six actionable checks preserve literal order');
              check([...checks.querySelectorAll('[data-nj-check]')].every(n=>n.querySelector('h6')&&n.querySelector('[data-state]')&&n.querySelectorAll('p').length>=2),'each signal owns cause, status and next action');
            }
          }
          if(step===4){
            const selection=panel.querySelector('#nj-sequence-id'),editors=[...panel.querySelectorAll('.nj-step-editor')];
            check(contains(panel,selection,panel.querySelector('#nj-sequence-query'))&&editors.length>0&&before(selection,editors[0]),'Apollo executor selection precedes sequence steps');
            for(const editor of editors){
              const h=editor.querySelector(':scope > header'),fields=editor.querySelector(':scope > .nj-fields'),approval=editor.querySelector(':scope > .nj-actions');
              check(contains(editor,h,fields,approval)&&before(h,fields)&&before(fields,approval),'each step owns identification, full editing and review in order');
              check(contains(fields,editor.querySelector('[data-nj-field="channel"]'),editor.querySelector('[data-nj-field="interval_hours"]'),editor.querySelector('[data-nj-field="subject"]'),editor.querySelector('[data-nj-field="content"]')),'all original step fields belong to that editor');
              check(contains(approval,editor.querySelector('[data-nj-approval-state]'),editor.querySelector('[data-nj-approve]'))&&fits(panel,editor),'state and approval belong to the same step');
            }
          }
          if(step===5){
            const activation=panel.querySelector('#nj-activation'),manual=panel.querySelector('#nj-manual'),automatic=panel.querySelector('#nj-automatic');
            check(activation.children.length===2&&activation.children[0]===manual&&activation.children[1]===automatic,'manual first and automatic second, exactly two regions');
            check(contains(manual,panel.querySelector('[data-nj-action="export"]'),panel.querySelector('[data-nj-action="reconcile"]'),panel.querySelector('#nj-enrollment'))&&!automatic.querySelector('button,input,a'),'manual owns download/reconciliation, automatic has no action');
            check(manual.getAttribute('role')==='region'&&automatic.getAttribute('role')==='region'&&/Inscrição automática: (OFF|desligada)/.test(automatic.textContent),'both activation regions are named, automatic stays off');
            check(before(panel.querySelector('.nj-cost'),activation)&&fits(activation,manual)&&fits(activation,automatic),'cost precedes contained activation regions');
            const m=manual.getBoundingClientRect(),a=automatic.getBoundingClientRect();
            check(innerWidth===1440?a.left>=m.right-1&&Math.abs(a.top-m.top)<=1:a.top>=m.bottom-1,'activation regions are beside on desktop and stacked on mobile');
            check(root.querySelector('#nj-prev').textContent==='Voltar'&&root.querySelector('#nj-next').textContent==='Concluir'&&!root.querySelector('#nj-next').hidden,'fifth footer keeps Voltar and Concluir');
          }
          return {step,failures};
        });
        assert.deepEqual(result.failures,[],'F2-2 screen '+result.step+' composition');
      };
      const noOverflow=async()=>{
        const pageOverflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);
        const bad=await page.locator('#new-journey *').evaluateAll(nodes=>[...new Set(nodes.filter(n=>n.getClientRects().length&&getComputedStyle(n).position!=='absolute').filter(n=>n.getBoundingClientRect().right>innerWidth+1||n.getBoundingClientRect().left< -1).map(n=>n.id||n.tagName+(n.className?'.'+String(n.className).replaceAll(' ','.'):'')))]);
        assert.deepEqual({pageOverflow,bad},{pageOverflow:false,bad:[]},'Every visible element fits, not just the clipped page');
      };
      const shot=async(name,collect=false)=>{
        await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:path.join(output,name+'-'+viewport.width+'.png'),fullPage:true});
        try{await composition();await noOverflow();}catch(e){if(!collect)throw e;uiCheck(false,name+': '+e.message);}
      };
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
      const diagnostic=page.locator('.nj-record > details').first();
      await diagnostic.locator(':scope > summary').click();
      assert.equal(await diagnostic.locator(':scope > ul > li').count(),6,'All six checks remain available');
      await diagnostic.locator(':scope > ul > li > details > summary').first().click();
      await diagnostic.locator('.nj-technical > summary').first().click();
      assert.match(await page.locator('.nj-record details').first().innerText(),/Desconhecido · retido/);
      assert.match(await page.locator('.nj-record details').first().innerText(),/Exclusões/);
      assert.match(await page.locator('.nj-record details').first().innerText(),/fixtures\/checks.md/);
      assert.match(await page.locator('.nj-record details').first().innerText(),/Conferida em:/);
      await diagnostic.locator(':scope > summary').click();await shot('03-checagem-retida');
      const imp=page.locator('form[data-nj-import]').first();
      await page.locator('.nj-import > summary').first().click();
      await imp.locator('[name="company.domain"]').fill('example.invalid');
      await imp.locator('[name=source_ref]').fill('https://example.invalid/source');
      await imp.locator('[name=verified]').check();
      await click('form[data-nj-import] button >> nth=0');
      assert.equal(traffic.filter(t=>t.path.endsWith('/hubspot-execute')).length,0,'Preview never applies CRM changes');
      await page.locator('.nj-import > summary').first().click();
      assert.equal(await page.locator('[data-nj-import-confirm]').count(),1);
      await page.locator('form[data-nj-import]').first().locator('[name="company.domain"]').fill('changed.invalid');
      assert.equal(await page.locator('[data-nj-import-confirm]').count(),0,'Editing removes stale confirmation');
      await page.locator('form[data-nj-import]').first().locator('[name="company.domain"]').fill('example.invalid');
      await page.locator('form[data-nj-import]').first().locator('[name=source_ref]').fill('https://example.invalid/source');
      await page.locator('form[data-nj-import]').first().locator('[name=verified]').check();
      await click('form[data-nj-import] button >> nth=0');
      await page.locator('.nj-import > summary').first().click();
      await click('[data-nj-import-confirm]');
      assert.equal(traffic.filter(t=>t.path.endsWith('/hubspot-execute')).length,1,'Only the exact explicit confirm reaches apply');
      await go(5);assert.equal(await page.locator('[data-nj-action=export]').isDisabled(),true);
      await go(2);await page.locator('[name=nj-source][value=apollo_people]').check();
      assert.equal(await page.locator('#nj-titles').inputValue(),'','Sheet hypotheses never become filters');
      await click('#nj-source-form button');assert.match(await page.locator('#nj-status').innerText(),/filtros explícitos/);
      await page.locator('#nj-titles').fill('Diretor;Gerente');await page.locator('#nj-locations').fill('Brasil');await page.locator('#nj-keywords').fill('industrial');
      await click('#nj-source-form button');assert.match(await page.locator('#nj-body').innerText(),/Cobertura parcial/);
      await page.locator('[name=nj-apollo-mode][value=apollo_contacts]').check();
      assert.equal(await page.locator('#nj-apollo-modes [value=apollo_contacts]').isChecked(),true);
      assert.equal(await page.locator('.nj-sources > .nj-source').count(),4,'Saved contacts never adds a fifth source');
      await composition();await page.locator('#nj-query').fill('Contato confirmado');await click('#nj-source-form button');
      checker='pass';complete=true;await go(3);await click('[data-nj-action=check]');await shot('03-checagem-verificada');
      assert.equal(await page.locator('.nj-record').count(),2,'Two contacts remain visible for the same bound company');
      const commercial=page.locator('.nj-record').first();
      assert.deepEqual(await commercial.locator('[data-nj-commercial-section]').evaluateAll(nodes=>nodes.map(n=>n.dataset.njCommercialSection)),['business','fit','moment','people','thesis','next_action'],'Six commercial regions in the approved order');
      for(const key of ['business','fit','moment','people','thesis','next_action'])assert.match(await commercial.innerText(),new RegExp('COMMERCIAL_'+key));
      assert.equal(await commercial.locator('.nj-history[open]').count(),0,'Originals and diagnostics start closed');
      assert.equal(await commercial.locator('a[href^="javascript:"]').count(),0,'Source links never execute input');
      assert.equal(await page.evaluate(()=>window.injected),undefined,'Research remains escaped text');
      assert.equal((await commercial.innerText()).includes('ORIGINAL_RESEARCH_END'),false,'Old CRM assertions are not current status');
      assert.match(await commercial.innerText(),/Contexto histórico/,'Old publication remains context');
      assert.match(await commercial.innerText(),/Hipótese/,'Hypothesis is not promoted to fact');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth),true,'Commercial view has no horizontal overflow');
      await commercial.locator('.nj-commercial > .nj-history').locator(':scope > summary').click();
      await commercial.getByText('Documento original integral',{exact:true}).click();
      assert.match(await commercial.innerText(),/ORIGINAL_RESEARCH_END/,'Full original remains accessible');
      await commercial.locator('.nj-commercial > .nj-history').locator(':scope > summary').click();

      assert.equal(await page.locator('form[data-nj-qualification]').count(),1,'Exactly one review per company, not one per contact');
      await page.locator('.nj-qualification > summary').click();
      const form=page.locator('form[data-nj-qualification]');
      await form.locator('[name=decision]').selectOption('qualified');await form.locator('[name=is_b2b]').selectOption('true');
      await form.locator('[name=country]').selectOption('BR');await form.locator('[name=sector]').selectOption('industria_b2b');
      await form.locator('[name=annual_revenue_brl]').fill('20000000');await form.locator('[name=source_url]').fill('https://example.invalid/report');
      await form.locator('[name=source_date]').fill('2031-03-01');await form.locator('[name=rationale]').fill('Decisão sintética com fonte explícita.');
      const fillCompany=async()=>{
        await form.locator('[name=decision]').selectOption('qualified');await form.locator('[name=is_b2b]').selectOption('true');
        await form.locator('[name=country]').selectOption('BR');await form.locator('[name=sector]').selectOption('industria_b2b');
        await form.locator('[name=annual_revenue_brl]').fill('20000000');await form.locator('[name=source_url]').fill('https://example.invalid/report');
        await form.locator('[name=source_date]').fill('2031-03-01');await form.locator('[name=rationale]').fill('Decisão sintética com fonte explícita.');
        await form.locator('[name=company_confirmation]').check();
      };
      // Required confirmation must block even the read-only preview until reviewed.
      await click('form[data-nj-qualification] button');
      assert.equal(traffic.filter(t=>t.path.endsWith('/company-preview')).length,0,'No preview without explicit human company review');
      await form.locator('[name=company_confirmation]').check();await click('form[data-nj-qualification] button');
      const preview=page.locator('[aria-label="Prévia da revisão da empresa"]');
      assert.equal(await preview.count(),1);
      assert.match(await preview.innerText(),/https:\/\/example.invalid\/report.*2031-03-01/);
      assert.match(await preview.innerText(),/Agora no HubSpot.*Após confirmar/si);
      assert.match(await preview.innerText(),/Desconhecido.*Aderente ao ICP/s);
      assert.match(await preview.innerText(),/Recusa continua somente leitura/);
      assert.equal(traffic.filter(t=>t.path.endsWith('/company-execute')).length,0,'Company preview never dispatches a write');
      await shot('03-empresa-previa',true);
      await page.locator('.nj-qualification > summary').click();
      for(const [name,value] of Object.entries({decision:'qualified',is_b2b:'true',country:'BR',sector:'industria_b2b',annual_revenue_brl:'20000000',source_url:'https://example.invalid/report',source_date:'2031-03-01',rationale:'Decisão sintética com fonte explícita.'})){
        uiCheck(await form.locator('[name='+name+']').inputValue()===value,'Submitted company '+name+' must remain prefilled while reviewing its preview');
      }
      await form.locator('[name=source_url]').fill('https://example.invalid/changed');
      const staleConfirm=page.locator('[data-nj-action=company-confirm]');
      uiCheck(await staleConfirm.count()===0||await staleConfirm.isDisabled(),'Editing company source must remove or disable the stale preview confirmation');
      await fillCompany();await click('form[data-nj-qualification] button');
      await click('#nj-reload');
      assert.equal(await page.locator('[aria-label="Prévia da revisão da empresa"]').count(),0,'Reload invalidates company preview instead of reusing its old confirmation');
      await page.locator('.nj-qualification > summary').click();await fillCompany();await click('form[data-nj-qualification] button');
      await click('[data-nj-action=company-confirm]');
      assert.equal(traffic.filter(t=>t.path.endsWith('/company-execute')).length,1);
      assert.match(await page.locator('#nj-status').innerText(),/Empresa conferida de volta.*Refazer as checagens/s);
      await page.locator('.nj-qualification > summary').click();assert.match(await page.locator('#nj-body').innerText(),/Revisado por ygor/);
      assert.equal(await form.locator('[name=source_url]').inputValue(),'https://example.invalid/report');
      assert.equal(await form.locator('[name=source_date]').inputValue(),'2031-03-01');
      assert.equal(await form.locator('[name=annual_revenue_brl]').inputValue(),'20000000');
      assert.equal(await form.locator('[name=decision]').inputValue(),'qualified');
      await click('#nj-reload');await page.locator('.nj-qualification > summary').click();
      assert.equal(await form.locator('[name=source_url]').inputValue(),'https://example.invalid/report','Saved company review survives reload');
      assert.equal(await form.locator('[name=source_date]').inputValue(),'2031-03-01');
      await go(4);assert.equal(await page.locator('#nj-sequence-id').inputValue(),'');
      await page.locator('#nj-sequence-query').fill('Sequência confirmada pelo usuário');
      denySequences=true;await click('[data-nj-action=sequences]');assert.match(await page.locator('#nj-status').innerText(),/Sem permissão/);
      assert.equal(await page.locator('#nj-sequence-id option').count(),1,'403 has no fallback sequence');
      denySequences=false;await click('[data-nj-action=sequences]');await page.locator('#nj-sequence-id').selectOption('apollo-sequence-fixture');
      await click('[data-nj-action=add-step]');await page.locator('#nj-subject-0').fill('Original');await page.locator('#nj-content-0').fill(original);
      assert.equal(await page.locator('[data-nj-approve]').isDisabled(),true,'Explicit save precedes approval');
      await click('[data-nj-action=save-sequence]');assert.equal(await page.locator('#nj-content-0').inputValue(),original);
      assert.match(await page.locator('#nj-parity').innerText(),/igual nesta leitura/);
      assert.match(await page.locator('#nj-parity').innerText(),/24 horas/);
      parityStatus='mismatch';await click('#nj-reload');assert.equal(await page.locator('[data-nj-approve]').isDisabled(),true);
      assert.match(await page.locator('#nj-parity').innerText(),/diferente do rascunho/);
      parityStatus='verified';await click('#nj-reload');
      assert.equal(await page.locator('[data-nj-approve]').isEnabled(),true);await shot('04-sequencia');
      holdApprove=true;await page.locator('[data-nj-approve]').click();await page.waitForFunction(()=>document.querySelector('[data-nj-approve]').disabled);
      await page.waitForTimeout(20);assert.equal(traffic.filter(t=>t.path.endsWith('/approve-step')).length,1);
      holdApprove=false;releaseApprove();await settled();
      assert.match(await page.locator('[data-nj-approval-state]').innerText(),/Aprovado no servidor/);
      await go(5);assert.equal(await page.locator('[data-nj-action=export]').isEnabled(),true);await shot('05-manual');
      const previewVersion=draft.version,previewDownloads=downloads.length,previewTraffic=traffic.length;
      await click('[data-nj-action=export-preview]');
      assert.match(await page.locator('#nj-manual pre').innerText(),/"Pessoa, sintética"/);
      assert.match(await page.locator('#nj-status').innerText(),/Prévia gerada, sem inscrição nem envio/);
      assert.equal(draft.version,previewVersion,'Export preview does not persist a new draft version');
      assert.equal(downloads.length,previewDownloads,'Export preview never downloads a file');
      assert.deepEqual(traffic.slice(previewTraffic).filter(t=>t.method!=='GET').map(t=>t.path),[base+'/'+id+'/export-preview'],'Preview never calls export, company execution or provider writes');
      await shot('05-previa-exportacao-exata');
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
      assert.match(await page.locator('#nj-body').innerText(),/Inscrição automática: (OFF|desligada)/);
      await shot('05-reconciliacao-desconhecida');
      observedEnrollment=true;await click('[data-nj-action=reconcile]');assert.match(await page.locator('#nj-enrollment').innerText(),/2 inscrições observadas.*não confirma envio/);
      assert.match(await page.locator('#nj-reconcile-progress').innerText(),/Conferidos 2 de 2/);
      failGet=true;await click('#nj-reload');assert.match(await page.locator('#nj-status').innerText(),/Leitura indisponível/);
      assert.equal(await page.locator('[data-nj-action=export]').isDisabled(),true);
      failGet=false;await click('#nj-reload');await page.reload();await settled();
      assert.equal(await page.locator('#nj-steps [aria-current=step]').getAttribute('data-nj-step'),'5','Hash restores draft and step');
      await go(4);assert.equal(await page.locator('#nj-content-0').inputValue(),original+'\nRevisão local.');
      await page.locator('#nj-steps button').first().focus();await page.keyboard.press('End');assert.equal(await page.locator('#nj-steps [aria-current=step]').getAttribute('data-nj-step'),'5');
      await composition();const postsBeforeFinish=traffic.filter(t=>t.method==='POST').length;
      await click('#nj-next');assert.equal(new URL(page.url()).hash,'#jornada');
      assert.equal(await page.locator('#new-journey').isHidden(),true);
      assert.equal(await page.locator('#journey-new').evaluate(n=>n===document.activeElement),true,'Local conclusion returns focus to Jornada');
      assert.equal(traffic.filter(t=>t.method==='POST').length,postsBeforeFinish,'Concluir is local navigation, never an API mutation');
      for(const tab of ['cadencias','contas','sistema','jornada']){await page.locator('#abas [data-aba='+tab+']').click();assert.equal(await page.locator('#abas [aria-selected=true]').getAttribute('data-aba'),tab);}
      await page.locator('#journey-new').click();await page.reload();await settled();
      assert.equal(await page.locator('[data-nj-resume]').count(),1);await click('[data-nj-resume]');assert.match(page.url(),new RegExp(id+'/1$'));
      // Apollo prompt approval is a separate mode; preserve the exact-mode path above.
      await go(4);assert.match(await page.locator('#nj-body').innerText(),/Apollo, Instantly e Cadência Thunder são caminhos distintos/);
      await click('[data-nj-action=select-prompts]');
      assert.equal(draft.approval_mode,'apollo_prompts_v1');
      assert.equal(await page.locator('#nj-body input,#nj-body textarea,#nj-body select,[data-nj-remove]').count(),0,'Original Apollo prompts have no editor or mutation control');
      assert.match(await page.locator('#nj-body').innerText(),/Modo Apollo · aprovação da versão dos prompts/);
      assert.doesNotMatch(await page.locator('#nj-body').innerText(),/A aprovação libera somente o texto exato/);
      assert.match(await page.locator('#nj-body').innerText(),/Não confirma um e-mail dinâmico ainda não gerado/);
      await page.getByText('Prompts e variáveis originais · passo 1',{exact:true}).click();
      assert.deepEqual(JSON.parse(await page.locator('#nj-body pre').innerText()),promptVariants,'Full original prompts, instructions and variables survive escaping');
      assert.equal(await page.locator('#new-journey img,#new-journey script').count(),0);
      assert.equal(await page.evaluate(()=>window.injected),undefined);
      await noOverflow();await page.screenshot({path:path.join(output,'04-prompts-originais-'+viewport.width+'.png'),fullPage:true});
      await click('[data-nj-approve]');
      assert.equal(draft.approvals['prompt-step-1'].kind,'apollo_prompts_v1','Prompt approval retains its separate server approval kind');
      assert.match(await page.locator('[data-nj-approve]').innerText(),/prompts aprovada/);
      await go(5);const promptPreviewDownloads=downloads.length,promptPreviewTraffic=traffic.length;
      const readiness=page.locator('#nj-operational-readiness');
      assert.match(await readiness.innerText(),/E1 automático observado/);
      assert.match(await readiness.innerText(),/não confirma o estado atual/);
      assert.match(await readiness.innerText(),/manter a sequência Apollo existente/);
      assert.match(await readiness.innerText(),/não inicia uma campanha nem aplica um teto dentro do Apollo/);
      assert.match(await readiness.innerText(),/atividade EMAIL no HubSpot/);
      assert.match(await readiness.innerText(),/parada externa com leitura de volta/);
      assert.equal(await page.locator('[data-nj-action=export-preview]').isEnabled(),true,'Diagnostic does not gate preparatory export');

      await click('[data-nj-action=export-preview]');
      assert.match(await page.locator('#nj-manual pre').innerText(),/approval_kind,approved_prompt_hash/);
      assert.match(await page.locator('#nj-manual pre').innerText(),new RegExp('apollo_prompts_v1,'+promptHash));
      assert.equal(downloads.length,promptPreviewDownloads,'Prompt export preview never downloads');
      assert.deepEqual(traffic.slice(promptPreviewTraffic).filter(t=>t.method!=='GET').map(t=>t.path),[base+'/'+id+'/export-preview']);
      await shot('05-previa-exportacao-prompts');
      await page.reload();await settled();
      assert.match(await page.locator('#nj-manual').innerText(),/Inscrição manual no Apollo/);
      assert.equal(await page.locator('#nj-manual pre').count(),0,'Reload clears export preview and never reuses a stale generated CSV');
      await go(4);assert.match(await page.locator('[data-nj-approval-state]').innerText(),/Aprovado no servidor/);
      // Model a changed authoritative snapshot. This only exercises browser consumption;
      // backend hash invalidation is covered by the main agent's domain tests.
      promptHash='f'.repeat(64);draft.approvals={};draft.version++;
      await click('#nj-reload');assert.match(await page.locator('[data-nj-approval-state]').innerText(),/Aguardando aprovação/);
      await go(5);assert.equal(await page.locator('[data-nj-action=export-preview]').isDisabled(),true,'Changed prompt inputs revoke export until renewed review');
      assert.equal(traffic.filter(t=>t.path.endsWith('/apollo-prompts')).length,1,'Reading originals and approval never alters Apollo prompts');
      for(const type of ['manual_email',null,'<img src=x onerror=alert(1)>']){
        readinessType=type;await click('#nj-reload');await go(5);
        const text=await page.locator('#nj-operational-readiness').innerText();
        assert.match(text,type==='manual_email'?/E1 manual observado/:/Tipo do E1 não comprovado/);
        assert.match(text,/não confirma o estado atual/);
        assert.match(text,/não autorizam expansão|nem autorizam expansão/);
        await noOverflow();
      }

      assert.equal(await page.locator('#new-journey img,#new-journey script').count(),0);assert.equal(await page.evaluate(()=>window.injected),undefined);
      assert.deepEqual(errors,[]);await noOverflow();
      assert.equal(traffic.filter(t=>t.method!=='GET'&&!t.path.startsWith(base+'/')).length,0,'No effect calls');
      console.log('EXERCISED '+viewport.width+': exact-mode regression; grouped company review/source+date/explicit company PATCH intent/readback+reload; original readonly Apollo prompts/separate approval_kind/hash refresh; export previews without download or writes; catalog/error+403/retry/CSV/escaping/keyboard/reflow/no external network; commercial six-region view, original provenance and safe links; pending final aggregated assertions');
      await page.close();
    }
    assert.deepEqual(deficiencies,[],'S27 browser deficiencies requiring main-owned HTML fixes');
    console.log('PASS S28 browser fixture: all assertions at 1440, 390 and 320px; zero provider HTTP');
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
