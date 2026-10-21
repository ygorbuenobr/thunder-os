// Synthetic browser journey: every request is intercepted; no live service is used.
const fs=require('node:fs');
const path=require('node:path');
const assert=require('node:assert/strict');
const {chromium}=require('playwright');
const html=fs.readFileSync(path.join(__dirname,'painel_modelo.html'),'utf8').replaceAll("/*MODO*/'artifact'","'servidor'").replaceAll('/*DADOS*/{}','{}');
const task={id:'99',title:'[PILOTO][D0] Empresa exemplo | LINKEDIN',campaign:'PILOTO',contact_ids:['123'],contact_name:'Contato exemplo',channel:'LINKEDIN',status:'NOT_STARTED',due_at:'2026-10-21T15:00:00Z'};
const unlinked={...task,id:'100',contact_ids:[],contact_name:null};
const snapshot={collected_at:'2026-10-21T13:00:00Z',coverage:{complete:false,limitations:['Consulta de atividades incompleta.']},campaigns:[{id:'PILOTO',metrics:{contacts:1,completed_with_receipt:null,pending:2,overdue:0,replies:null,meetings:null},tasks:[task]}],today:{planned:[task],awaiting_approval:[],unreplied:[]},unlinked:[unlinked]};
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  for(const viewport of [{width:1440,height:1000},{width:390,height:844}]){
   const page=await browser.newPage({viewport});const errors=[],requests=[];let fail=false,empty=false;
   await page.clock.install();
   page.on('pageerror',e=>errors.push(e.message));
   await page.route('**/*',async route=>{
    const req=route.request(),p=new URL(req.url()).pathname;requests.push({p,method:req.method()});
    if(p==='/')return route.fulfill({contentType:'text/html',body:html});
    let body=[];
    if(p==='/api/os/v1/cadencias'){
     if(fail)return route.fulfill({status:503,contentType:'application/json',body:JSON.stringify({erro:'Unavailable'})});
     body=empty?{...snapshot,campaigns:[],unlinked:[],today:{}}:snapshot;
    }else if(p==='/api/os/v1/cadencias/contacts/123')body={contact_id:'123',contact_name:'Contato exemplo',coverage:{complete:false,limitations:['Somente eventos vinculados.']},events:[{type:'email',subject:'Conversa de exemplo',text:'<img src=x onerror=alert(1)> Texto enviado',occurred_at:'2026-10-20T12:00:00Z',source:'HubSpot',receipt:'receipt:test-only'}]};
    else if(p==='/api/os/v1/panel-capabilities')body={legacy_mutations:false};
    else if(p==='/api/os/v1/today')body={priorities:[],work:[],cadences:[],replies:[],controls:{},usage:[],alerts:[]};
    else if(p==='/api/os/v1/integrations/status')body={};
    else if(p==='/api/os/v1/integrations/drafts')body={drafts:[]};
    else if(p==='/api/quentes')body={items:[]};
    else if(p==='/api/sugestoes')body={items:[]};
    return route.fulfill({contentType:'application/json',body:JSON.stringify(body)});
   });
   await page.goto('http://cadencias.test/#cadencias');
   await page.locator('#cad-read-campaigns').getByRole('cell',{name:'PILOTO',exact:true}).waitFor();
   assert.match(await page.locator('#cad-read-status').innerText(),/Consulta parcial/);
   assert.match(await page.locator('#cad-read-unlinked').innerText(),/Sem vínculo/);
   const before=requests.filter(r=>r.p==='/api/os/v1/cadencias').length;
   await page.clock.runFor(60001);
   await page.waitForFunction(()=>!document.querySelector('#cad-read-refresh').disabled);
   assert.equal(requests.filter(r=>r.p==='/api/os/v1/cadencias').length,before+1);
   await page.locator('#cad-read-today [data-cad-contact]').first().click();
   await page.locator('#cad-read-events li').waitFor();
   assert.match(await page.locator('#cad-read-events').innerText(),/Texto enviado/);
   assert.equal(await page.locator('#cad-read-events img').count(),0);
   const overflow=await page.evaluate(()=>({width:innerWidth,scroll:document.documentElement.scrollWidth,elements:[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().width&&e.getBoundingClientRect().right>innerWidth+1).slice(0,8).map(e=>({tag:e.tagName,id:e.id,cls:e.className,right:e.getBoundingClientRect().right}))}));
   fs.mkdirSync(path.join(__dirname,'../.scratch'),{recursive:true});
   await page.screenshot({path:path.join(__dirname,`../.scratch/cadencias-${viewport.width}.png`),fullPage:true});
   assert.equal(overflow.scroll>overflow.width,false,JSON.stringify(overflow));
   fail=true;await page.locator('#cad-read-refresh').click();
   await page.getByText('Não foi possível atualizar. Os dados abaixo são da consulta anterior.').waitFor();
   fail=false;empty=true;await page.locator('#cad-read-refresh').click();
   await page.getByText('Nenhuma tarefa de campanha encontrada nesta consulta.').waitFor();
   assert.equal(requests.filter(r=>r.method!=='GET').length,0);
   assert.deepEqual(errors,[]);
   await page.close();
  }
  console.log('PASS: desktop/mobile, timeline, escaping, partial, stale failure, empty, GET-only');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
