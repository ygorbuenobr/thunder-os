// Real browser, synthetic sources: every request intercepted; never a provider call.
const assert=require('node:assert/strict');
const fs=require('node:fs');
const path=require('node:path');
const os=require('node:os');
const {chromium}=require('playwright');
const source=fs.readFileSync(path.join(__dirname,'painel_modelo.html'),'utf8');
const start=source.indexOf('/* THUNDER_DELEGATED_IDENTITY_BEGIN */');
const end=source.indexOf('/* THUNDER_DELEGATED_IDENTITY_END */');
assert.ok(start>=0&&end>start);
const markupStart=source.indexOf('<details id="delegated-identity-review">');
const markupEnd=source.indexOf('</section>',markupStart);
const markup=source.slice(markupStart,markupEnd);
const style=source.match(/<style[^>]*>([\s\S]*?)<\/style>/)[1];
const moduleSource=source.slice(start,end);
const now=Date.parse('2026-10-29T15:00:00Z')/1000;
const attack='<img src=x onerror="window.identityXss=true">';
const fixture=()=>({id:'synthetic-preview',task_id:'12345',version:1,
  review_hash:'a'.repeat(64),expires_at:now+600,simulation:true,state:'review_required',
  plan:{action:'dm',channel:'linkedin',account_id:'synthetic-account',recipient_profile_urn:'urn:li:fsd_profile:synthetic',content:'Olá, esta é uma mensagem de demonstração para conferir o destinatário. Nenhum envio será realizado.'},
  identity:{matches:false,mismatches:['linkedin_url','name','company'],observation_hash:'b'.repeat(64),
    contact:{id:'42',linkedin_url:'https://www.linkedin.com/in/synthetic-old',name:'Contato Exemplo',company:'Empresa Anterior'},
    profile:{account_id:'synthetic-account',profile_urn:'urn:li:fsd_profile:synthetic',linkedin_url:'https://www.linkedin.com/in/synthetic-current',name:'Contato Exemplo Atual',company:'Empresa Atual'}},
  identity_confirmation:null});
const confirmation=p=>({actor:'ygor',preview_id:p.id,review_hash:p.review_hash,observation_hash:p.identity.observation_hash});
const output=process.env.THUNDER_UI_EVIDENCE_DIR||fs.mkdtempSync(path.join(os.tmpdir(),'thunder-identity-ui-'));
fs.mkdirSync(output,{recursive:true});

(async()=>{
  const browser=await chromium.launch({headless:true,...(process.env.THUNDER_UI_CHROME?{executablePath:process.env.THUNDER_UI_CHROME}:{})});
  try{
    for(const width of [1280,390]){
      const page=await browser.newPage({viewport:{width,height:1000}});
      let record=fixture(),mode='ok',fullPanel=false;const requests=[],errors=[];
      page.on('pageerror',e=>{errors.push(e.message);console.error('Unexpected panel browser error: '+e.message);});
      await page.route('**/*',async route=>{
        const request=route.request(),url=new URL(request.url());
        requests.push({method:request.method(),path:url.pathname,body:request.postDataJSON()});
        if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:fullPanel?source.replaceAll("/*MODO*/'artifact'","'servidor'").replaceAll('/*DADOS*/{}','{}'):'<!doctype html><html lang="pt-BR"><meta charset="utf-8"><style>'+style+'</style><body>'+markup+'</body></html>'});
        if(url.pathname==='/api/os/v1/linkedin-cadence-previews')return route.fulfill({contentType:'application/json',body:JSON.stringify({read_only:true,task_id:'12345',previews:[record]})});
        if(fullPanel&&request.method()==='GET'){
          let result=[];
          if(url.pathname==='/api/os/v1/session')result={actor:'ygor',nonce:'synthetic-browser-nonce'};
          if(url.pathname==='/api/os/v1/panel-capabilities')result={legacy_mutations:false};
          if(url.pathname==='/api/os/v1/journey')result={read_only:true,collected_at:'2026-10-29T15:00:00Z',stages:[],metrics:{},campaigns:[]};
          if(url.pathname==='/api/os/v1/today')result={priorities:[],work:[],cadences:[],replies:[],controls:{},usage:[],alerts:[]};
          if(url.pathname==='/api/os/v1/hermes/contacts')result={contacts:[],complete:true};
          return route.fulfill({contentType:'application/json',body:JSON.stringify(result)});
        }
        assert.equal(url.pathname,'/api/os/v1/linkedin-cadence-identity-confirmations','Only identity confirmation may write');
        assert.equal(request.method(),'POST');
        assert.deepEqual(request.postDataJSON(),{preview_id:record.id,review_hash:record.review_hash,observation_hash:record.identity.observation_hash,identity_confirmation:'CONFIRMO A IDENTIDADE DIVERGENTE'});
        if(mode==='denied')return route.fulfill({status:403,contentType:'application/json',body:'{}'});
        const result=confirmation(record);
        if(mode!=='readback-missing')record.identity_confirmation=result;
        return route.fulfill({contentType:'application/json',body:JSON.stringify(mode==='wrong-hash'?{...result,observation_hash:'c'.repeat(64)}:result)});
      });
      const setup=async()=>{
        await page.goto('https://identity-ui.test/');
        await page.addScriptTag({content:moduleSource});
        await page.evaluate(({now})=>{
          window.syntheticClock=now;
          ThunderDelegatedIdentity.mount(document.querySelector('#delegated-identity-review'),{
            clock:()=>window.syntheticClock,
            api:async(path,method='GET',body)=>{const response=await fetch(path,{method,headers:{'Content-Type':'application/json'},...(body?{body:JSON.stringify(body)}:{})});if(!response.ok)throw Error('request failed');return response.json();}});
        },{now});
        await page.locator('#delegated-identity-review>summary').click();
      };
      const read=async()=>{await page.locator('#di-task-id').fill('12345');await page.getByRole('button',{name:'Consultar prévias desta tarefa'}).click();await page.waitForFunction(()=>!document.querySelector('[data-di-load]').disabled);};
      const confirm=async()=>{await page.locator('[data-di-acknowledge]').check();await page.locator('[data-di-confirm]').click();await page.waitForFunction(()=>!document.querySelector('[data-di-load]').disabled);};
      await setup();await read();
      const card=page.locator('[data-di-preview]');
      assert.equal(await card.locator('tr').count(),4);
      const visible=await card.innerText();for(const field of ['Perfil LinkedIn · diferente','Nome · diferente','Empresa atual · diferente','Empresa Anterior','Empresa Atual','synthetic-old','synthetic-current',record.plan.content])assert.ok(visible.toLowerCase().includes(field.toLowerCase()),field+': '+visible);
      assert.equal(await card.locator('[data-di-confirm]').isDisabled(),true);
      assert.equal(requests.filter(r=>r.method==='POST').length,0);
      assert.equal(await card.locator('img,script').count(),0);
      assert.equal(await page.evaluate(()=>window.identityXss),undefined);
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false);
      await page.screenshot({path:path.join(output,'identity-divergence-'+width+'.png'),fullPage:true});
      record.plan.content='Texto sintético.\n'+attack;await read();
      assert.ok((await card.innerText()).includes(attack));
      assert.equal(await card.locator('img,script').count(),0);
      assert.equal(await page.evaluate(()=>window.identityXss),undefined);
      await confirm();assert.match(await page.locator('[data-di-status]').innerText(),/conferida no registro/);
      assert.match(await card.innerText(),/Identidade divergente confirmada/);
      assert.equal(requests.filter(r=>r.method==='POST').length,1);
      assert.equal(await card.locator('[data-di-confirm]').count(),0);
      // Re-load persists only the matching server readback, not local UI state.
      await setup();await read();assert.match(await card.innerText(),/Identidade divergente confirmada/);
      for(const testMode of ['denied','wrong-hash','readback-missing']){
        record=fixture();mode=testMode;await read();await confirm();
        assert.match(await page.locator('[data-di-status]').innerText(),/confirmação não foi comprovada/);
        assert.equal(await card.locator('[data-di-confirm]').isDisabled(),true);
      }
      mode='ok';record=fixture();record.expires_at=now-1;await read();
      assert.match(await card.innerText(),/Prévia expirada/);assert.equal(await card.locator('[data-di-confirm]').count(),0);
      record=fixture();await read();await page.locator('[data-di-acknowledge]').check();const before=requests.filter(r=>r.method==='POST').length;
      await page.evaluate(()=>{window.syntheticClock+=1000;});await page.locator('[data-di-confirm]').click();
      assert.match(await page.locator('[data-di-status]').innerText(),/prévia expirou/);assert.equal(requests.filter(r=>r.method==='POST').length,before);
      await page.evaluate(({now})=>{window.syntheticClock=now;},{now});
      record=fixture();record.identity.matches=true;record.identity.mismatches=[];await read();
      assert.match(await card.innerText(),/coincidem nesta observação/);assert.equal(await card.locator('[data-di-confirm]').count(),0);
      record=fixture();record.plan.action='invite';record.plan.content='';await read();
      assert.match(await card.innerText(),/Convite sem mensagem\./);await confirm();
      assert.match(await page.locator('[data-di-status]').innerText(),/conferida no registro/);
      record=fixture();record.plan.content='';await read();
      assert.equal(await card.count(),0);assert.match(await page.locator('[data-di-status]').innerText(),/Não foi possível conferir/);
      record=fixture();record.plan.content='  \n  ';await read();
      assert.equal(await card.count(),0);assert.match(await page.locator('[data-di-status]').innerText(),/Não foi possível conferir/);
      for(const mutate of [p=>p.task_id='999',p=>p.identity.observation_hash='',p=>p.identity.profile.linkedin_url='javascript:alert(1)',p=>p.identity.profile.company='',p=>p.identity.mismatches=['unknown'],p=>p.plan.account_id='different-account',p=>p.plan.recipient_profile_urn='different-profile']){
        record=fixture();mutate(record);await read();assert.equal(await card.count(),0);assert.match(await page.locator('[data-di-status]').innerText(),/Não foi possível conferir/);
      }
      record=fixture();await read();await page.locator('#di-task-id').fill('54321');assert.equal(await card.count(),0);
      // Load the entire actual panel as well: exercise its mounted module and
      // shared session/nonce request adapter, still with fully synthetic I/O.
      record=fixture();record.expires_at=Date.now()/1000+600;fullPanel=true;
      await page.goto('https://identity-ui.test/?full=1#sistema');
      assert.deepEqual(errors,[]);
      await page.screenshot({path:path.join(output,'full-panel-'+width+'.png'),fullPage:true});
      await page.locator('#delegated-identity-review>summary').click();await read();
      assert.match(await card.innerText(),/Contato Exemplo Atual/);
      await page.locator('#delegated-identity-review').screenshot({path:path.join(output,'full-panel-identity-'+width+'.png')});
      await confirm();assert.match(await page.locator('[data-di-status]').innerText(),/conferida no registro/);
      assert.ok(requests.some(r=>r.path==='/api/os/v1/session'));
      assert.deepEqual(errors,[]);await page.close();
    }
    console.log('Delegated identity browser: desktop/mobile comparison, exact confirmation/readback, denial, changed hash, expiry, invalid identity and XSS: OK');
    console.log('Synthetic screenshots: '+output);
  }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
