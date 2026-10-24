// V24: reference-locked composition over synthetic fixtures; zero provider network.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, '_ferramentas/painel_modelo.html'), 'utf8')
  .replaceAll("/*MODO*/'artifact'", "'servidor'").replaceAll('/*DADOS*/{}', '{}');
const stages = [['icp','ICP'],['abordagem','Abordagem'],['resposta','Resposta'],['crm','CRM'],['linkedin','LinkedIn'],['followup','Follow-up'],['preparacao','Preparação']];
const source = {label:'Fonte sintética comprovada',status:'ok',url:'https://source.example.invalid/record'};
const date = '2031-03-12T12:30:00Z';
const state = {
  read_only:true,collected_at:date,campaigns:[{id:'BR-TEST',label:'Campanha sintética'}],
  metrics:{
    reached:{value:12,source:[source],reason:'unique_people_first_observed_touch_provider_receipt',coverage:{complete:false,observed_count:12}},
    replied:{value:null,source:[source],reason:'partial_human_reply_coverage'},
    hot:{value:null,source:[source],reason:'positive_classification_unavailable'},
    cost_per_hot:{value:999,source:[source],reason:'verified_cost_source_unavailable'}
  },
  stages:stages.map(([id,label])=>({id,label,status:id==='preparacao'?'unavailable':'partial',count:null,reason:'Cobertura parcial da fonte sintética.',items:id==='preparacao'?[]:Array.from({length:50},(_,i)=>({id:`${id}-${i}`,name:`Pessoa sintética ${i}`,company:'Empresa sintética com nome bastante longo para testar reflow',campaign_id:'BR-TEST',date,owner:'Responsável sintético',readiness:'Revisão pendente',summary:'Resumo sintético preservado para consulta.',action:{label:'Abrir',type:'detail',id:`detail-${i}`}}))})),
  timeline:{name:'Pessoa sintética',company:'Empresa sintética',events:[{id:'event',date,label:'Evento sintético',summary:'Contexto de teste',detail_id:'detail-0'}]},
  log:[{date,message:'Consulta sintética. Nenhum efeito externo executado.'},{date,message:'Outro registro sintético disponível no histórico.'}],sources:[source]
};
const output = path.join(root,'.scratch/jornada-reference-ui');
fs.mkdirSync(output,{recursive:true});
(async()=>{
  const browser=await chromium.launch({headless:true});
  try{
    for(const width of [1440,800,390,320]){
      const context=await browser.newContext({viewport:{width,height:1000},reducedMotion:'reduce'});
      const page=await context.newPage();const errors=[];const methods=[];let fail=false;
      page.on('pageerror',e=>errors.push(e.message));
      await page.route('**/*',route=>{
        const req=route.request(),url=new URL(req.url());methods.push(req.method());
        assert.equal(req.method(),'GET','The reference journey must never write');
        if(url.pathname==='/')return route.fulfill({contentType:'text/html',body:html});
        let data={};
        if(url.pathname==='/api/os/v1/journey'){
          if(fail)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({erro:'Fonte sintética indisponível.'})});
          data=state;
        }else if(url.pathname==='/api/os/v1/session')data={actor:'ygor',nonce:'synthetic-nonce'};
        else if(url.pathname==='/api/os/v1/panel-capabilities')data={legacy_mutations:false};
        return route.fulfill({contentType:'application/json',body:JSON.stringify(data)});
      });
      await page.goto('https://journey-reference.test/#jornada');
      await page.locator('#journey-stages .journey-stage-icon').first().waitFor();
      assert.equal(await page.locator('#journey-stages .journey-stage-icon svg').count(),7);
      assert.equal(await page.locator('#journey-stages button').count(),7);
      const icon=await page.locator('.journey-stage-icon').first().evaluate(el=>({w:el.getBoundingClientRect().width,h:el.getBoundingClientRect().height,r:getComputedStyle(el).borderRadius}));
      assert(icon.w>=44&&icon.w<=60&&icon.w===icon.h&&icon.r==='50%','Circular milestone touch target');
      if(width>=800){
        const tops=await page.locator('#journey-stages button').evaluateAll(nodes=>nodes.map(n=>Math.round(n.getBoundingClientRect().top)));
        assert.equal(new Set(tops).size,1,'Seven connected milestones stay on one row');
      }
      if(width===1440){
        const core=await page.locator('.journey-funnel').boundingBox();
        const panel=await page.locator('#journey-stage-panel').boundingBox();
        assert(core.height<=140,'Reference KPI strip remains compact while proofs are closed');
        assert(panel.y<=520,'Primary stage panel starts in the first desktop viewport');
      }
      const selectedInk=await page.locator('#abas [aria-selected=true]').evaluate(el=>getComputedStyle(el).color);
      assert.equal(selectedInk,'rgb(0, 0, 0)','Cyan selected tab uses legible black ink');
      assert.equal(await page.locator('[data-journey-metric=reached] strong').textContent(),'12');
      assert.equal(await page.locator('[data-journey-metric=replied] strong').textContent(),'?');
      assert.equal(await page.locator('[data-journey-metric=hot] strong').textContent(),'?');
      assert.equal(await page.locator('[data-journey-metric=cost_per_hot] strong').textContent(),'?','No cost without proven hot denominator');
      const proof=page.locator('[data-journey-metric=reached] details');
      assert.equal(await proof.count(),1,'Metric evidence remains accessible');
      await proof.locator('summary').focus();await page.keyboard.press('Enter');
      assert.equal(await proof.getAttribute('open'),'');
      assert.match(await proof.innerText(),/Fonte sintética comprovada/);
      await proof.locator('summary').click();
      for(const [id] of stages){
        const button=page.locator(`[data-journey-stage=${id}]`);
        await button.click();assert.equal(await button.getAttribute('aria-pressed'),'true');
        assert.equal(await page.locator('#journey-stage-items article').count(),id==='preparacao'?0:50);
      }
      await page.locator('[data-journey-stage=icp]').click();
      const first=page.locator('#journey-stage-items article').first();
      assert.equal(await first.locator('button').count(),1,'One action per item');
      assert.match(await first.textContent(),/BR-TEST/);assert.match(await first.textContent(),/Responsável sintético/);
      const scroll=await page.locator('#journey-stage-items').evaluate(el=>({scroll:el.scrollHeight,client:el.clientHeight}));
      assert(scroll.scroll>scroll.client,'All 50 rows remain available in the compact scrolling panel');
      assert.equal(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth),false,`No body overflow at ${width}`);
      const layout=await page.evaluate(()=>({stage:document.querySelector('#journey-stage-panel').getBoundingClientRect().toJSON(),log:document.querySelector('#journey-log').getBoundingClientRect().toJSON()}));
      assert(layout.log.top>=layout.stage.bottom,'Log follows the principal stage panel');
      assert.equal(await page.locator('#journey-log details').count(),1,'Full agent history is expandable');
      await page.locator('#journey-log summary').click();
      assert.match(await page.locator('#journey-log').innerText(),/Outro registro sintético/);
      await page.locator('#journey-log summary').click();
      await page.screenshot({path:path.join(output,`reference-${width}.png`),fullPage:true});
      fail=true;await page.locator('#journey-refresh').click();
      await page.waitForFunction(()=>!document.querySelector('#journey-refresh').disabled);
      assert.match(await page.locator('#journey-status').innerText(),/indisponível|Falha|falha|Não|Erro/);
      assert.deepEqual(errors,[]);assert(methods.every(m=>m==='GET'));
      await context.close();console.log(`PASS ${width}: stepper/compact panel/sources/unknown costs/50 rows/reflow/error/reduced motion/no writes`);
    }
  }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
