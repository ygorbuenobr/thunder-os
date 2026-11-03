// "Autorizar LinkedIn" browser fixtures: isolated Chromium, every request intercepted.
// No provider, no send route: the fake API only mirrors the five existing endpoints.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const source = fs.readFileSync(path.join(root, '_ferramentas/painel_modelo.html'), 'utf8');
const output = path.join(root, '.scratch/linkedin-authorization');
const between = (from,to) => {const a=source.indexOf(from),b=source.indexOf(to,a+from.length);assert(a>=0&&b>a,from);return source.slice(a,b);};
const css = between('<style>','</style>')+'</style>';
const markup = between('    <section id="cad-li-auth"','\n  <section class="cad-read-section"><h3>Fontes');
const screen = between('/* THUNDER_LINKEDIN_AUTHORIZATION_BEGIN */','/* THUNDER_LINKEDIN_AUTHORIZATION_END */');
assert(!screen.includes('innerHTML'), 'screen must build DOM with textContent only');
assert(!/conversations\/send|\/execute|\/connect'/.test(screen), 'screen must not reference send routes');
const attack = '<img src=x onerror="window.injected=true">';
const TEXT1 = 'Oi Laura, aqui é o Ygor. Este é o teste do Thunder OS que combinamos.\n\nPode ignorar. '+attack;
const ACCOUNT = '6abe5fc087213e2040fb9b1b', URN = 'urn:li:fsd_profile:ACoAADfwURQBEDaHhGr7YJr53N6VvhahAPfkQr0';
const PROFILE = 'laura-myslinsky-330373222', CONTACT = '258958235509';
const harness = `<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">${css}</head><body data-aba="cadencias"><main style="padding:16px;max-width:1100px;margin:auto">${markup}</main><script>
const $=id=>document.getElementById(id);
const apiServidor=async(route,method='GET',body)=>{const r=await fetch(route,{method,credentials:'same-origin',headers:body===undefined?{}:{'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});const data=await r.json();if(!r.ok){const e=new Error(data.erro||'Não consegui concluir.');e.status=r.status;throw e;}return data;};
${screen}
ThunderLinkedInAuthorization.mount($('cad-li-auth'),{api:apiServidor});
</script></body></html>`;

function fakeServer(){
  const now = () => Date.now()/1000;
  const s = {delegation:null, agent:{enabled:true,paused:true,version:14}, approvals:[], bindings:[], confirmations:{},
             previews:{}, campaigns:[{id:'LI-THUNDER-OS-00',platform:'thunder_os',eligible:true,reason:null},{id:'LI-THUNDER-OS-01',platform:'thunder_os',eligible:true,reason:null},
                                    {id:'MX27-CDMX',platform:'codex_local',eligible:false,reason:'campaign_platform_not_thunder_os'}]};
  const blockers = () => [
    ...(s.delegation&&s.delegation.enabled?[]:['delegation_off']),
    ...(s.agent.paused?['agent_paused']:[]),'executor_identity_verifier_missing'];
  const state = () => ({read_only:true,contact_id:CONTACT,observed_at:now(),
    sender:{account_id:ACCOUNT,public_id:'ygorbuenobr',profile_url:'https://www.linkedin.com/in/ygorbuenobr/',source:'direct_runtime_live'},
    delegation:s.delegation,agent_control:s.agent,global_pause:false,
    approvals:s.approvals.map(a=>({...a,expired:false,steps:a.steps.map(st=>({marker:st.marker,subject:st.subject,action:st.plan.action,recipient_public_id:st.plan.recipient_public_id,recipient_profile_urn:st.plan.recipient_profile_urn,account_public_id:st.plan.account_public_id,content:st.plan.content}))})),
    tasks:s.bindings.map(b=>({task_id:b.task_id,binding:b,campaign_confirmation:s.confirmations[b.task_id]||null,execution:null,alert:null,previews:(s.previews[b.task_id]||[]).length})),
    campaigns:s.campaigns,blockers:blockers()});
  const subjects = {cad_li_0:['LinkedIn: convite','invite'],cad_li_1:['LinkedIn: msg 1','dm'],cad_li_2:['LinkedIn: msg 2','dm'],cad_li_3:['LinkedIn: msg 3','dm']};
  function handle(method, url, body){
    const p = url.pathname;
    if(method==='GET'&&p==='/api/os/v1/linkedin-authorization'){assert.equal(url.searchParams.get('contact_id'),CONTACT);return [200,state()];}
    if(method==='GET'&&p==='/api/os/v1/linkedin-authorization/tasks')return [200,{read_only:true,contact_id:CONTACT,complete:true,tasks:[
      {id:'40001',subject:'LinkedIn: msg 1',body:'cad_li_1',status:'NOT_STARTED',due_at:'2026-11-03T12:00:00Z',marker:'cad_li_1',contract_ok:true},
      {id:'40002',subject:'LinkedIn: msg 2',body:'texto solto',status:'NOT_STARTED',due_at:'2026-11-05T12:00:00Z',marker:'cad_li_2',contract_ok:false}]}];
    if(method==='GET'&&p==='/api/os/v1/linkedin-cadence-previews'){const task=url.searchParams.get('task_id');return [200,{read_only:true,task_id:task,previews:s.previews[task]||[]}];}
    if(method!=='POST')return [404,{erro:'rota'}];
    if(p==='/api/os/v1/linkedin-campaign-drafts'){assert.deepEqual(body,{campaign_id:'LI-CANARIO-LAURA-01',sheet_path:'projetos/thunder-os/ficha-li-canario-laura-01.md'});
      return [200,{read_only:true,persisted:false,sheet_sha256:'b'.repeat(64),existing:false,sections:[{key:'offer',label:'Oferta',content:'Nenhuma oferta. '+attack},{key:'restrictions',label:'Restrições',content:'Uma única DM.'}],
        request:{expected_version:0,config:{campaign_id:'LI-CANARIO-LAURA-01',platform:'thunder_os',providers:{linkedin:'connectsafely'},configuration:{flow_version:'linkedin-cadence-v1',provider_accounts:{linkedin:ACCOUNT}},briefing:{version:'1',fields:{offer:{path:'projetos/thunder-os/ficha-li-canario-laura-01.md',sha256:'b'.repeat(64),section:'offer'}}}}}}];}
    if(p==='/api/os/v1/campaigns'){assert.equal(body.expected_version,0);s.campaigns.push({id:body.config.campaign_id,platform:'thunder_os',eligible:true,reason:null,version:1});return [200,{id:body.config.campaign_id,version:1}];}
    const revokeCampaign=/^\/api\/os\/v1\/campaigns\/([^/]+)\/revoke$/.exec(p);
    if(revokeCampaign){const id=decodeURIComponent(revokeCampaign[1]),c=s.campaigns.find(x=>x.id===id);assert.equal(body.expected_version,c.version);
      // Real campaign_options skips revoked campaigns (binding() fails), so it leaves the list.
      s.campaigns=s.campaigns.filter(x=>x.id!==id);return [200,{...c,active:false,version:c.version+1}];}
    if(p==='/api/os/v1/delegations/hermes'){assert.deepEqual(Object.keys(body).sort(),['daily_limit','enabled','scopes']);s.delegation={...body,scopes:[...new Set(body.scopes)].sort(),actor:'ygor',version:(s.delegation?.version||0)+1};return [200,s.delegation];}
    if(p==='/api/os/v1/agent/control'){assert.equal(body.expected_version,s.agent.version);s.agent={...s.agent,paused:body.action==='pause',version:s.agent.version+1};return [200,s.agent];}
    if(p==='/api/os/v1/linkedin-cadence-plan-drafts'){
      assert.deepEqual(Object.keys(body).sort(),['hubspot_contact_id','profile','texts','valid_hours']);
      const steps=Object.keys(subjects).map(m=>({marker:m,subject:subjects[m][0],plan:{account_id:ACCOUNT,account_public_id:'ygorbuenobr',action:subjects[m][1],recipient_public_id:PROFILE,recipient_profile_urn:URN,channel:'linkedin',messaging_channel:'linkedin_inbox',content:body.texts[m],conversation_urn:null}}));
      return [200,{read_only:true,persisted:false,approval:{id:'li-'+CONTACT+'-abc',hubspot_contact_id:CONTACT,cadence_id:'li-'+CONTACT+'-f1',expires_at:'2026-11-05T15:00:00Z',steps},
        sender:{account_id:ACCOUNT,public_id:'ygorbuenobr',profile_url:'https://www.linkedin.com/in/ygorbuenobr/'},
        recipient:{public_id:PROFILE,profile_urn:URN,profile_url:'https://www.linkedin.com/in/'+PROFILE+'/',label:'Laura Myslinsky',urn_source:'connectsafely_profile_read'},
        hubspot_contact:{id:CONTACT,name:'Laura Myslinsky',company:'Clínica',linkedin_url:'https://www.linkedin.com/in/'+PROFILE+'/',cad_status:'rascunho'},hubspot_error:null,
        plan_hashes:{},approval_hash_preview:'f'.repeat(64)}];
    }
    if(p==='/api/os/v1/linkedin-cadence-approvals'){s.approvals.push({...body,version:1,status:'active',approved_by:'ygor',review_hash:'f'.repeat(64)});return [201,s.approvals.at(-1)];}
    if(p==='/api/os/v1/linkedin-cadence-approvals/revoke'){const a=s.approvals.find(x=>x.id===body.approval_id);assert.equal(body.expected_version,a.version);a.status='revoked';a.version++;return [200,a];}
    if(p==='/api/os/v1/linkedin-cadence-task-bindings'){
      const a=s.approvals.find(x=>x.id===body.approval_id);assert.equal(body.review_hash,a.review_hash);
      s.bindings.push({...body,contact_id:CONTACT,cadence_id:a.cadence_id,actor:'ygor'});
      // The executor's first bounded run would create this preview; simulated here.
      const step=a.steps.find(st=>st.marker===body.marker);
      s.previews[body.task_id]=[{id:'prev1',task_id:body.task_id,version:1,review_hash:'e'.repeat(64),expires_at:now()+300,state:'review_required',simulation:false,plan:step.plan,job:null,
        identity:{matches:true,mismatches:[],observation_hash:'d'.repeat(64),contact:{id:CONTACT,linkedin_url:'https://www.linkedin.com/in/'+PROFILE,name:'Laura Myslinsky',company:'Clínica'},profile:{account_id:ACCOUNT,profile_urn:URN,linkedin_url:'https://www.linkedin.com/in/'+PROFILE,name:'Laura Myslinsky',company:'Clínica'}},identity_confirmation:null}];
      return [201,body];
    }
    if(p==='/api/os/v1/integrations/connectsafely/campaign-review'){assert.deepEqual(body,{preview_id:'prev1',campaign_id:'LI-THUNDER-OS-01'});
      return [200,{confirmed:true,context:{campaign_id:'LI-THUNDER-OS-01',platform:'thunder_os'},batch_id:'connectsafely:prev1',batch_version:1,scope_hash:'c'.repeat(64),control_version:0}];}
    if(p==='/api/os/v1/linkedin-cadence-campaign-confirmations'){
      assert.deepEqual(Object.keys(body).sort(),['execution_confirmation','expected_version','preview_id','review_hash','task_id']);
      assert.equal(body.execution_confirmation.context.campaign_id,'LI-THUNDER-OS-01');
      s.confirmations[body.task_id]={preview_id:body.preview_id,campaign_approval_id:'op-1',review_hash:body.review_hash,actor:'ygor'};return [201,s.confirmations[body.task_id]];
    }
    return [404,{erro:'rota não simulada'}];
  }
  return {handle,s};
}

const ALLOWED_POSTS = new Set(['/api/os/v1/linkedin-campaign-drafts','/api/os/v1/campaigns','/api/os/v1/campaigns/LI-CANARIO-LAURA-01/revoke','/api/os/v1/delegations/hermes','/api/os/v1/agent/control','/api/os/v1/linkedin-cadence-plan-drafts',
  '/api/os/v1/linkedin-cadence-approvals','/api/os/v1/linkedin-cadence-approvals/revoke','/api/os/v1/linkedin-cadence-task-bindings',
  '/api/os/v1/integrations/connectsafely/campaign-review','/api/os/v1/linkedin-cadence-campaign-confirmations']);

(async()=>{
  fs.mkdirSync(output,{recursive:true});
  const browser = await chromium.launch({headless:true});
  try{
    for(const width of [1440,390]){
      const context = await browser.newContext({viewport:{width,height:1000},serviceWorkers:'block'});
      const page = await context.newPage();
      const server = fakeServer(), requests = [], external = [], errors = [];
      page.on('pageerror',e=>errors.push(e.message));
      await context.route('**/*',async route=>{
        const req=route.request(),url=new URL(req.url());
        if(url.origin!=='https://panel.example.invalid'){external.push(req.url());return route.abort();}
        if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:harness});
        const body=req.postData()?JSON.parse(req.postData()):undefined;
        requests.push({method:req.method(),path:url.pathname,body});
        const [status,value]=server.handle(req.method(),url,body);
        return route.fulfill({status,contentType:'application/json',body:JSON.stringify(value)});
      });
      await page.goto('https://panel.example.invalid/');
      const status = () => page.locator('[data-la-status]').innerText();
      const posts = () => requests.filter(r=>r.method==='POST');
      const lastPost = () => posts().at(-1);
      const settle = async () => page.waitForFunction(()=>document.getElementById('cad-li-auth').getAttribute('aria-busy')!=='true');

      await page.fill('#la-contact-id', CONTACT);
      await page.click('[data-la-load]'); await settle();
      assert.match(await status(), /Estado lido/);
      for(const code of ['delegation_off','agent_paused','executor_identity_verifier_missing'])
        assert.equal(await page.locator(`[data-la-blocker="${code}"]`).count(),1,code);
      assert.match(await page.locator('[data-la-state="delegation"]').innerText(),/Nunca configurada/);
      assert.equal(posts().length,0,'reading state never writes');

      // 0 · Campaign registration from a Brain sheet, read-only draft first.
      await page.click('[data-la-campaign-draft-load]'); await settle();
      const regCard = page.locator('[data-la-campaign-draft-card]');
      assert.match(await regCard.innerText(),/nada foi gravado[\s\S]*connectsafely|ConnectSafely/);
      assert.match(await regCard.innerText(),/Nenhuma oferta/);
      assert.equal(await page.evaluate(()=>window.injected),undefined);
      const register = page.locator('[data-la-action="campaign-register"]');
      assert(await register.isDisabled());
      await regCard.locator('input[type="checkbox"]').check();
      await register.click(); await settle();
      assert.equal(lastPost().path,'/api/os/v1/campaigns');
      assert.equal(lastPost().body.config.campaign_id,'LI-CANARIO-LAURA-01');
      assert.match(await status(),/Registrar campanha: registrado e conferido/);
      assert.match(await page.locator('[data-la-campaign="LI-CANARIO-LAURA-01"]').innerText(),/elegível/);

      // 1 · Delegation on, with the exact request visible before the click.
      await page.locator('[data-la-exact="Ligar delegação"] summary').click();
      assert.match(await page.locator('[data-la-exact="Ligar delegação"] pre').innerText(),/"daily_limit": 1[\s\S]*"enabled": true/);
      assert(await page.locator('[data-la-action="delegation-off"]').isDisabled());
      await page.click('[data-la-action="delegation-on"]'); await settle();
      assert.deepEqual(lastPost().body,{scopes:['linkedin_cadence'],daily_limit:1,enabled:true});
      assert.match(await status(),/Ligar delegação: registrado e conferido/);
      assert.match(await page.locator('[data-la-state="delegation"]').innerText(),/Ligada/);

      // 2 · Agent resume with the expected version.
      await page.click('[data-la-action="agent-resume"]'); await settle();
      assert.deepEqual(lastPost().body,{action:'resume',expected_version:14});
      assert.match(await page.locator('[data-la-state="agent"]').innerText(),/Ativo/);

      // 3 · Draft is read-only, shows sender/recipient/exact text, then approval.
      await page.fill('#la-profile','https://www.linkedin.com/in/'+PROFILE+'/');
      await page.fill('#la-text-cad_li_1',TEXT1);
      await page.fill('#la-text-cad_li_2','Segunda mensagem');
      await page.fill('#la-text-cad_li_3','Terceira mensagem');
      await page.click('[data-la-draft-load]'); await settle();
      assert.equal(lastPost().path,'/api/os/v1/linkedin-cadence-plan-drafts');
      assert.deepEqual(lastPost().body.texts,{cad_li_0:'',cad_li_1:TEXT1,cad_li_2:'Segunda mensagem',cad_li_3:'Terceira mensagem'});
      const draftCard = page.locator('[data-la-draft-card]');
      const draftText = await draftCard.innerText();
      assert.match(draftText,/Quem envia[\s\S]*ygorbuenobr/);
      assert.match(draftText,/Quem recebe[\s\S]*Laura Myslinsky/);
      assert.match(draftText,new RegExp(URN));
      assert.match(draftText,/status de cadência: rascunho/);
      assert.match(draftText,/só roda a tarefa com o status de cadência/);
      assert.equal(await page.locator('[data-la-draft-step="cad_li_1"] pre').innerText(),TEXT1);
      assert.match(await page.locator('[data-la-draft-step="cad_li_0"] pre').innerText(),/Convite sem mensagem/);
      assert.equal(await page.evaluate(()=>window.injected),undefined);
      const approve = page.locator('[data-la-action="approve"]');
      assert(await approve.isDisabled(),'approval needs the explicit check');
      await page.locator('[data-la-draft-card] input[type="checkbox"]').check();
      await approve.click(); await settle();
      assert.equal(lastPost().path,'/api/os/v1/linkedin-cadence-approvals');
      assert.equal(lastPost().body.steps.find(st=>st.marker==='cad_li_1').plan.content,TEXT1);
      assert.match(await status(),/Aprovar cadência: registrado e conferido/);
      assert.equal(await page.locator('[data-la-approval]').count(),1);

      // Editing the text after a draft discards it; nothing approves stale copy.
      await page.click('[data-la-draft-load]'); await settle();
      await page.fill('#la-text-cad_li_2','Segunda mensagem alterada');
      assert.equal(await page.locator('[data-la-draft-card]').count(),0);
      assert.match(await status(),/Monte a prévia de novo/);

      // 4 · Read-only HubSpot task list, then explicit binding of the valid one.
      await page.click('[data-la-action="tasks"]'); await settle();
      assert.match(await page.locator('[data-la-task="40002"]').innerText(),/precisa ser exatamente cad_li_2/);
      assert.equal(await page.locator('[data-la-task="40002"] [data-la-action="bind"]').count(),0);
      const bind = page.locator('[data-la-task="40001"] [data-la-action="bind"]');
      assert(await bind.isDisabled());
      await page.locator('[data-la-task="40001"] input[type="checkbox"]').check();
      await bind.click(); await settle();
      assert.deepEqual(lastPost().body,{task_id:'40001',approval_id:'li-'+CONTACT+'-abc',approval_version:1,review_hash:'f'.repeat(64),marker:'cad_li_1'});
      assert.equal(await page.locator('[data-la-bound="40001"]').count(),1);

      // 5 · Preview from the executor, campaign review, explicit confirmation.
      await page.click('[data-la-campaign-task="40001"] [data-la-action="previews"]'); await settle();
      const preview = page.locator('[data-la-preview="prev1"]');
      assert.equal(await preview.locator('pre.li-auth-text').innerText(),TEXT1);
      assert.match(await preview.innerText(),/Identidade confere/);
      assert.equal(await preview.locator('option').count(),3,'only eligible campaigns are offered');
      await preview.locator('select').selectOption('LI-THUNDER-OS-01');
      assert.equal(await page.locator('[data-la-preview="prev1"] select').inputValue(),'LI-THUNDER-OS-01','choice survives re-render');
      await page.locator('[data-la-preview="prev1"] [data-la-action="campaign-review"]').click(); await settle();
      assert.equal(await page.locator('[data-la-preview="prev1"] select').inputValue(),'LI-THUNDER-OS-01');
      assert.equal(lastPost().path,'/api/os/v1/integrations/connectsafely/campaign-review');
      const confirm = page.locator('[data-la-preview="prev1"] [data-la-action="campaign-confirm"]');
      assert(await confirm.isDisabled());
      await page.locator('[data-la-preview="prev1"] input[type="checkbox"]').check();
      await confirm.click(); await settle();
      assert.equal(lastPost().path,'/api/os/v1/linkedin-cadence-campaign-confirmations');
      assert.equal(lastPost().body.task_id,'40001');
      assert.match(await page.locator('[data-la-campaign-task="40001"]').innerText(),/Campanha confirmada na prévia prev1/);
      assert.match(await page.locator('[data-la-step="agent"]').innerText(),/controle é global/);

      await page.screenshot({path:path.join(output,`autorizar-linkedin-${width}.png`),fullPage:true});
      const overflow = await page.evaluate(()=>document.documentElement.scrollWidth-window.innerWidth);
      assert(overflow<=0,`horizontal overflow at ${width}px: ${overflow}`);

      // Undo from the same screen: revoke approval, pause agent, kill switch.
      await page.locator('[data-la-approval] [data-la-action="revoke"]').click(); await settle();
      assert.deepEqual(lastPost().body,{approval_id:'li-'+CONTACT+'-abc',expected_version:1});
      assert.match(await page.locator('[data-la-approval] summary').innerText(),/revogada/);
      await page.click('[data-la-action="agent-pause"]'); await settle();
      assert.deepEqual(lastPost().body,{action:'pause',expected_version:15});
      await page.click('[data-la-action="delegation-off"]'); await settle();
      assert.deepEqual(lastPost().body,{scopes:['linkedin_cadence'],daily_limit:1,enabled:false});
      assert.match(await page.locator('[data-la-state="delegation"]').innerText(),/Desligada/);

      await page.locator('[data-la-action="campaign-revoke"]').last().click(); await settle();
      assert.deepEqual(lastPost(),{method:'POST',path:'/api/os/v1/campaigns/LI-CANARIO-LAURA-01/revoke',body:{expected_version:1}});
      assert.equal(await page.locator('[data-la-campaign="LI-CANARIO-LAURA-01"]').count(),0);

      // A failed write is visible and does not claim success.
      await page.route('**/api/os/v1/agent/control',route=>route.fulfill({status:409,contentType:'application/json',body:JSON.stringify({erro:'Versão desatualizada.'})}));
      await page.click('[data-la-action="agent-resume"]'); await settle();
      assert.match(await status(),/não foi concluído: Versão desatualizada/);

      for(const r of posts())assert(ALLOWED_POSTS.has(r.path),'unexpected POST '+r.path);
      assert.deepEqual(external,[]);
      assert.deepEqual(errors,[]);
      await context.close();
      console.log(`ok ${width}px · ${posts().length} POSTs · overflow ${overflow}`);
    }
  } finally { await browser.close(); }
})().catch(error=>{console.error(error);process.exit(1);});
