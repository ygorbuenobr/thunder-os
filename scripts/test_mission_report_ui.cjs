// Fixture-only checks of the actual panel formatter; no browser or providers.
const fs = require('node:fs');
const vm = require('node:vm');
const assert = require('node:assert/strict');
const html = fs.readFileSync('_ferramentas/painel_modelo.html', 'utf8');
const match = html.match(/\/\* MISSION_REPORT_FORMAT_BEGIN \*\/([\s\S]*?)\/\* MISSION_REPORT_FORMAT_END \*\//);
assert.ok(match, 'panel report formatter exists');
const box = {};
vm.runInNewContext(match[1] + '\nthis.format = missionReportText;', box);
const out = box.format({campaign_id:'BR-APOLLO-01', observed_at:'2026-11-03T01:00:00Z',
  coverage:{complete:false, contacts_complete:false, messages_complete:true},
  hunter_preview:{observed_leads:278, total_leads:null}, crm_sync:{status:'configuration_pending'}});
assert.match(out, /278/);
assert.match(out, /Total de inscritos: não comprovado/);
assert.match(out, /HubSpot.*não verificada/i);
assert.match(out, /revisão não comprovada/i);
assert.match(out, /2026-11-03T01:00:00Z/);
assert.match(box.format({campaign_id:'BR-APOLLO-01', status:'partial', coverage:{complete:true}, hunter_preview:{total_leads:999}}), /Total de inscritos: não comprovado/);
assert.throws(() => box.format({campaign_id:'other'}));
assert.match(box.format({campaign_id:'BR-APOLLO-01'}), /não informado/i);
assert.ok(html.includes("apiServidor('/api/os/v1/integrations/apollo/reader')"));
console.log('mission report panel fixtures: PASS');
