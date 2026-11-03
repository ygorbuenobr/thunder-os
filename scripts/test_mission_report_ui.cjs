// Fixture-only checks of the actual panel formatter; no browser or providers.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('_ferramentas/painel_modelo.html', 'utf8');
const match = html.match(/\/\* MISSION_REPORT_FORMAT_BEGIN \*\/([\s\S]*?)\/\* MISSION_REPORT_FORMAT_END \*\//);
assert.ok(match, 'panel report formatter exists');
const box = {};
vm.runInNewContext(match[1] + '\nthis.format = missionReportText;', box);
const out = box.format({enabled:true, campaign_id:'BR-APOLLO-01', observed_at:'2026-11-03T01:00:00Z',
  coverage:{complete:false, contacts_complete:false, messages_complete:true, retained_observation:true, contacts_observed_at:'2026-10-27T12:00:00Z', messages_observed_at:'2026-11-03T00:59:00Z'},
  hunter_preview:{observed_leads:278, total_leads:null}, crm_sync:{status:'configuration_pending'}});
assert.match(out, /278/);
assert.match(out, /Total de inscritos: não comprovado/);
assert.match(out, /HubSpot.*não verificada/i);
assert.match(out, /revisão não comprovada/i);
assert.match(out, /2026-10-27T12:00:00Z/);
assert.match(out, /2026-11-03T00:59:00Z/);
assert.match(out, /Dados retidos de observação anterior/);
assert.match(box.format({campaign_id:'BR-APOLLO-01', status:'partial', coverage:{complete:true}, hunter_preview:{total_leads:999}}), /Total de inscritos: não comprovado/);
assert.match(box.format({enabled:false,campaign_id:'BR-APOLLO-01',hunter_preview:{observed_leads:0}}), /Contatos observados: não informado/);
assert.throws(() => box.format({campaign_id:'other'}));
assert.match(box.format({campaign_id:'BR-APOLLO-01'}), /não informado/i);
assert.ok(html.includes("apiServidor('/api/os/v1/integrations/apollo/reader')"));
console.log('mission report panel fixtures: PASS');

// Exercise the panel's actual request lifecycle, including uncertain outcomes.
(async()=>{
  const part=html.slice(html.indexOf("const MISSION_A_STORAGE="),html.indexOf('let sugestaoAtual=null;'));
  const nodes={};
  for(const id of ['mission-a-request','mission-a-check','mission-a-new','mission-a-cost','mission-a-result'])
    nodes[id]={disabled:id==='mission-a-new',checked:false,textContent:'',addEventListener(_event,fn){this.click=fn;}};
  let saved='old-request',status='uncertain',posts=0;
  const context={
    $:id=>nodes[id],crypto:{randomUUID:()=> 'new-request'},
    sessionStorage:{getItem:()=>saved,setItem:(_k,v)=>{saved=v;},removeItem:()=>{saved=null;}},
    apiServidor:async(_path,method)=>{if(method==='POST'){posts++;return {status:'scheduled'};}return {status,execution_id:'e1',output:'fixture'};}
  };
  vm.runInNewContext(part,context);
  await nodes['mission-a-check'].click();
  assert.equal(nodes['mission-a-new'].disabled,true);
  await nodes['mission-a-new'].click();
  assert.equal(saved,'old-request');
  status='result_unreviewed';await nodes['mission-a-check'].click();
  assert.equal(nodes['mission-a-new'].disabled,false);
  await nodes['mission-a-new'].click();
  assert.equal(saved,null);assert.equal(posts,0);assert.equal(nodes['mission-a-cost'].checked,false);
  nodes['mission-a-cost'].checked=true;await nodes['mission-a-request'].click();
  assert.equal(saved,'new-request');assert.equal(posts,1);
  console.log('mission request lifecycle fixtures: PASS');
})().catch(e=>{console.error(e);process.exitCode=1;});
