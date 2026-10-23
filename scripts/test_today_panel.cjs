// PRD4 acceptance journey. Synthetic responses only; every browser request is intercepted.
// Checks: four tabs/aliases; three independent blocks; MX27 exclusion/counts; one action;
// archive without mutations; safe links/text; GET cache/poll/recovery; explicit draft only;
// field.sources compatibility; keyboard, focus and responsive layout. No local server.
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, '_ferramentas/painel_modelo.html'), 'utf8')
  .replaceAll("/*MODO*/'artifact'", "'servidor'").replaceAll('/*DADOS*/{}', '{}');
const output = path.join(root, '.scratch/prd4-today-ui');
const collected = '2026-10-23T13:00:00Z';
const contextHash = 'a'.repeat(64);
const source = {name: 'HubSpot sintético', status: 'ok'};
const item = (id, campaign = 'BR-CADENCIA-V1') => ({id, owner: 'Responsável sintético', campaign_id: campaign,
  title: `Trabalho ${id}`, date: collected, last_message: 'Última mensagem sintética', context_hash: contextHash, draft_request_available: true, contact_id: '456',
  action: {label: 'Abrir no HubSpot', url: 'https://app.hubspot.com/contacts/123/record/0-1/456'}});
const empty = () => ({read_only: true, collected_at: collected,
  blocks: Object.fromEntries(['respond', 'do', 'approve'].map(key => [key, {items: [], sources: [source]}])),
  archive: {count: 0, items: []}});
const populated = () => {
  const state = empty();
  state.blocks.respond.items = [{...item('ivan'), title: 'Ivan · responder à conversa',
    thread: [{sender: 'Contato sintético', date: collected, text: 'Mensagem inicial completa'},
      {sender: 'Ygor sintético', date: collected, text: 'Contexto completo da resposta'}]}];
  state.blocks.do.items = [item('alex-bra'), item('mexico', 'MX27-CDMX')];
  state.blocks.approve.items = [{...item('review'), action: {label: 'Revisar ação', target: 'hoje-connectsafely'}}];
  state.archive = {count: 37, items: Array.from({length: 37}, (_, index) => ({...item('legacy-' + index),
    title: 'Registro legado ' + index, texto: 'Preservado para consulta', status: 'pendente'}))};
  return state;
};
const cadence = {collected_at: collected, coverage: {complete: true}, campaigns: [
  {id: 'BR-CADENCIA-V1', metrics: {}, tasks: []}, {id: 'MX27-CDMX', metrics: {}, tasks: []}
], today: {}, unlinked: []};
const sheet = () => ({schema_version: 1, read_only: true, campaigns: [{campaign_id: 'BR-CADENCIA-V1',
  status: 'complete', version: 'synthetic-v1', missing_fields: [], fields:
    ['offer', 'icp', 'proofs', 'objective', 'restrictions', 'cta', 'language', 'country'].map(key => ({key, status: 'ok',
      path: 'old/' + key, content: 'Legacy field content', source_updated_at: collected,
      expected_sha256: 'b'.repeat(64), observed_sha256: 'b'.repeat(64),
      ...(key === 'offer' ? {sources: [1, 2].map(index => ({path: `refs/offer-${index}.md`, status: 'ok',
        content: 'Source paragraph ' + index, source_updated_at: collected,
        expected_sha256: 'c'.repeat(64), observed_sha256: 'c'.repeat(64)}))} : {})}))}]});

(async () => {
  const browser = await chromium.launch({headless: true});
  try {
    for (const viewport of [{width: 1440, height: 1000}, {width: 390, height: 844}, {width: 320, height: 800}]) {
      const page = await browser.newPage({viewport});
      const requests = [], errors = [];
      let state = empty(), failure = false, hold = false, release, draftFailure = false, draftHold = false, releaseDraft;
      let sheets = sheet();
      await page.clock.install();
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', async route => {
        const request = route.request(), pathname = new URL(request.url()).pathname;
        requests.push({pathname, method: request.method(), body: request.postDataJSON()});
        if (pathname === '/') return route.fulfill({contentType: 'text/html', body: html});
        let data = [];
        if (pathname === '/api/os/v1/today-work') {
          assert.equal(request.method(), 'GET');
          if (hold) await new Promise(resolve => { release = resolve; });
          if (failure) return route.fulfill({status: 503, contentType: 'application/json', body: '{}'});
          data = state;
        } else if (pathname === '/api/os/v1/today-work/draft') {
          assert.equal(request.method(), 'POST');
          assert.equal(request.headers()['x-thunder-nonce'], 'synthetic-nonce');
          assert.deepEqual(request.postDataJSON(), {item_id: 'ivan', context_hash: contextHash});
          if (draftHold) await new Promise(resolve => { releaseDraft = resolve; });
          if (draftFailure) return route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({erro: 'Rascunho indisponível'})});
          data = {draft: {text: 'Rascunho sintético para revisão. Sem envio.', origin: 'Hermes sintético · contexto ' + contextHash}};
        } else {
          assert.equal(request.method(), 'GET', 'No other write is authorized in this synthetic journey');
          if (pathname === '/api/os/v1/session') data = {nonce: 'synthetic-nonce', actor: 'synthetic'};
          if (pathname === '/api/os/v1/panel-capabilities') data = {legacy_mutations: false};
          if (pathname === '/api/os/v1/cadencias') data = cadence;
          if (pathname === '/api/os/v1/campaign-sheets') data = sheets;
          if (pathname === '/api/os/v1/today') data = {priorities: [], work: [], cadences: [], replies: [], controls: {}, usage: [], alerts: []};
          if (pathname === '/api/os/v1/hermes/contacts') data = {contacts: [{id: '456', name: 'Contato BR sintético'}, {id: '999', name: 'Contato MX sintético'}], complete: true};
          if (['/api/itens', '/api/os/v1/items'].includes(pathname)) data = Array.from({length: 69}, (_, index) => ({id: 'old-mx-' + index, campaign_id: 'MX27-CDMX', tipo: 'pedido', status: 'pendente', titulo: 'Legacy item must not count'}));
        }
        return route.fulfill({contentType: 'application/json', body: JSON.stringify(data)});
      });
      const settled = () => page.waitForFunction(() => !document.querySelector('#today-refresh').disabled);
      const refresh = async () => { await page.locator('#today-refresh').click(); await settled(); };
      const todayCalls = () => requests.filter(request => request.pathname === '/api/os/v1/today-work').length;
      const draftCalls = () => requests.filter(request => request.pathname === '/api/os/v1/today-work/draft').length;
      await page.goto('https://today-panel.test/');
      await settled();
      assert.deepEqual(await page.locator('#abas [role=tab]').evaluateAll(tabs => tabs.map(tab => tab.firstChild.textContent)), ['Hoje', 'Campanhas', 'Contas', 'Sistema']);
      assert.equal(await page.locator('body').getAttribute('data-aba'), 'hoje');
      assert.deepEqual(await page.locator('#sec-hoje .today-block h2').allTextContents(), ['RESPONDER 0', 'FAZER 0', 'APROVAR 0']);
      assert.equal(await page.locator('#qt-hoje').textContent(), '0', 'Legacy snapshots never contribute to Hoje counts');
      assert.equal(await page.locator('#hoje-pedido').getAttribute('open'), null);
      assert.equal(requests.some(request => request.pathname.includes('/hermes/')), false, 'Collapsed Hermes must not query or invoke a model');
      assert.equal(await page.locator('#sec-hoje button:disabled:visible').count(), 0);
      assert.doesNotMatch(await page.locator('#sec-hoje').innerText(), /Não verificado|Prioridades do briefing|Integrações/);
      console.log(`PASS ${viewport.width}: empty, four tabs, first screen, isolated counts, collapsed Hermes`);

      state = populated(); await refresh();
      assert.equal(await page.locator('#sec-hoje [data-today-work-id]').count(), 3);
      assert.equal(await page.locator('#qt-hoje').textContent(), '3');
      assert.doesNotMatch(await page.locator('#sec-hoje').innerText(), /MX27|mexico/);
      assert.doesNotMatch((await page.locator('#today-do, #today-approve').allTextContents()).join(' '), /Rascunho/);
      for (const card of await page.locator('#sec-hoje [data-today-work-id]').all()) {
        assert.equal(await card.locator('a,button').count(), 1, 'Each Today item has exactly one working action');
        assert.match(await card.innerText(), /Responsável: Responsável sintético · Campanha: BR-CADENCIA-V1 · 23\/10\/2026/);
      }
      const fullMessage = 'Mensagem longa ' + 'X'.repeat(20000) + ' FULL_MESSAGE_END';
      state.blocks.respond.items[0].last_message = fullMessage;
      state.blocks.respond.items[0].thread.push({sender: 'Contato sintético', date: collected, text: fullMessage});
      await refresh();
      assert((await page.locator('#today-respond article > p:not([class])').textContent()).length <= 241);
      assert.doesNotMatch(await page.locator('#today-respond').innerText(), /FULL_MESSAGE_END/);
      await page.locator('[data-today-open=ivan]').click();
      assert((await page.locator('#today-reply-thread').innerText()).includes(fullMessage));
      await page.locator('#today-reply-close').click();
      state = populated(); await refresh();
      const search = page.locator('#today-filter');
      const beforeSearch = todayCalls();
      await search.fill('Alex-BRA');
      assert.equal(await page.locator('#sec-hoje [data-today-work-id]').count(), 1);
      assert.equal(await page.locator('#qt-hoje').textContent(), '3');
      await search.fill('missing');
      assert.equal(await page.locator('#sec-hoje [data-today-work-id]').count(), 0);
      assert.match(await page.locator('#today-do').innerText(), /Nenhum item encontrado/);
      await search.fill('');
      assert.equal(todayCalls(), beforeSearch, 'Filtering uses the cached snapshot');
      state.blocks.do.items = Array.from({length: 12}, (_, i) => item('bounded-' + i));
      await refresh();
      assert.equal(await page.locator('#today-do article').count(), 5);
      assert.equal(await page.locator('#today-do-count').textContent(), '12');
      assert.equal(await page.locator('#qt-hoje').textContent(), '14');
      await page.locator('[data-today-expand=do]').click();
      assert.equal(await page.locator('#today-do article').count(), 12);
      assert.equal(await page.locator('[data-today-expand=do]').getAttribute('aria-expanded'), 'true');
      await page.keyboard.press('Enter');
      assert.equal(await page.locator('#today-do article').count(), 5);
      state = populated(); await refresh();
      state.blocks.do.items = [{...item('legacy-review'), action: {label: 'Revisar contexto', review_target: {kind: 'legacy_card', id: 'legacy-0'}}}];
      await refresh();
      await page.locator('#today-do a').click();
      assert.equal(await page.locator('body').getAttribute('data-aba'), 'sistema');
      assert.equal(await page.locator('[data-today-archive-id="legacy-0"] details').getAttribute('open'), '');
      await page.locator('#today-archive > summary').click();
      await page.locator('#abas [data-aba=hoje]').click();
      state = populated(); await refresh();
      const afterBounded = todayCalls();
      const external = page.locator('#today-do a');
      assert.equal(await external.getAttribute('href'), 'https://app.hubspot.com/contacts/123/record/0-1/456');
      await page.locator('#today-approve a').click();
      assert.equal(await page.locator('body').getAttribute('data-aba'), 'sistema');
      assert.equal(await page.locator('#hoje-connectsafely').isVisible(), true);
      assert.equal(await page.locator('#hoje-connectsafely').evaluate(el => el === document.activeElement), true);
      assert.equal(await page.locator('#today-archive').getAttribute('open'), null);
      await page.locator('#today-archive > summary').focus(); await page.keyboard.press('Enter');
      assert.equal(await page.locator('#today-archive article').count(), 37);
      assert.equal(await page.locator('#today-archive button, #today-archive a, #today-archive input, #today-archive form').count(), 0);
      await page.locator('#today-archive article summary').first().click();
      assert.match(await page.locator('#today-archive').innerText(), /Preservado para consulta/);
      for (const id of ['sec-agentes', 'hoje-integracoes', 'cad-reply-control', 'cad-linkedin-preparation', 'cad-source-list']) {
        assert.equal(await page.locator('#' + id).isVisible(), true, id + ' belongs in Sistema');
      }
      await page.locator('#abas [data-aba=cadencias]').click();
      await page.waitForFunction(() => !document.querySelector('#cad-read-refresh').disabled);
      assert.match(await page.locator('#cad-read-campaigns').innerText(), /MX27-CDMX/);
      assert.equal(await page.locator('#cad-reply-control').isVisible(), false);
      const campaignSheet = page.locator('[data-cad-sheet="BR-CADENCIA-V1"]');
      const offer = campaignSheet.locator(':scope > details').first();
      await offer.locator(':scope > summary').click();
      assert.equal(await offer.locator('.cad-sheet-source').count(), 2);
      assert.match(await offer.innerText(), /refs\/offer-1.md/);
      assert.match(await offer.innerText(), /Source paragraph 2/);
      assert.match(await offer.innerText(), /23\/10\/2026/);
      await campaignSheet.locator(':scope > details').nth(1).locator(':scope > summary').click();
      assert.match(await campaignSheet.innerText(), /Legacy field content/);
      console.log(`PASS ${viewport.width}: populated/filter/MX27, one action, review navigation, 37 read-only archive, sources compatibility`);

      // Old bookmarks remain routable while the visible navigation has only four tabs.
      for (const [alias, expected] of [['agentes', 'sistema'], ['conversas', 'sistema'], ['aprovar', 'sistema'], ['campanhas', 'cadencias'], ['cadencias', 'cadencias']]) {
        await page.evaluate(hash => { location.hash = hash; }, '#' + alias);
        await page.waitForFunction(value => document.body.dataset.aba === value, expected);
      }
      await page.locator('#abas [data-aba=hoje]').click(); await settled();
      assert.equal(todayCalls(), afterBounded, 'Returning within 60s uses cached Today data');
      await page.locator('#abas [data-aba=hoje]').focus(); await page.keyboard.press('ArrowRight');
      assert.equal(await page.locator('#abas [data-aba=cadencias]').getAttribute('aria-selected'), 'true');
      await page.keyboard.press('Home'); await settled();
      assert.equal(await page.locator('#abas [data-aba=hoje]').evaluate(el => el === document.activeElement), true);

      // Full reply detail, no draft generation on open, explicit generation with auth/nonce.
      await page.locator('[data-today-open=ivan]').click();
      assert.match(await page.locator('#today-reply-thread').innerText(), /Mensagem inicial completa/);
      assert.match(await page.locator('#today-reply-thread').innerText(), /Contexto completo da resposta/);
      assert.equal(draftCalls(), 0);
      draftFailure = true;
      await page.locator('#today-draft-generate').click();
      await page.waitForFunction(() => !document.querySelector('#today-draft-generate').disabled);
      assert.match(await page.locator('#today-draft-status').innerText(), /indisponível/i);
      assert.doesNotMatch(await page.locator('#today-reply-draft').innerText(), /Rascunho sintético/);
      draftFailure = false; draftHold = true;
      await page.locator('#today-draft-generate').click();
      await page.waitForFunction(() => document.querySelector('#today-draft-generate').disabled);
      await page.clock.runFor(60001);
      assert.equal(draftCalls(), 2, 'Polling never generates a draft or overlaps the explicit request');
      draftHold = false; releaseDraft();
      await page.waitForFunction(() => document.querySelector('#today-draft-generate').hidden);
      assert.match(await page.locator('#today-reply-draft').innerText(), /Rascunho sintético para revisão/);
      assert.match(await page.locator('#today-reply-draft').innerText(), /Origem: Hermes sintético/);
      assert.equal(await page.locator('#today-reply-dialog button').count(), 2);
      await page.keyboard.press('Escape');
      assert.equal(await page.locator('[data-today-open=ivan]').evaluate(el => el === document.activeElement), true);
      await page.locator('[data-today-open=ivan]').click();
      assert.match(await page.locator('#today-reply-draft').innerText(), /Rascunho sintético para revisão/);
      assert.equal(await page.locator('#today-draft-generate').isVisible(), false);
      await page.locator('#today-reply-close').click();
      console.log(`PASS ${viewport.width}: keyboard/aliases/cache, full thread, explicit draft/auth/nonce/error/retry/no overlap, no auto-send`);

      // Per-source failure preserves independent actions and cannot erase the other blocks.
      state.blocks.respond.sources = [{name: 'LinkedIn sintético', status: 'error', message: 'internal_error_code', limitations: ['persisted_gmail_observation_not_live_sync', 'crm_history_not_authoritative']}];
      state.blocks.do.items.push(item('ivan')); // same identity must not duplicate visually.
      await refresh();
      assert.match(await page.locator('#today-respond-sources').innerText(), /LinkedIn sintético · Fonte indisponível/);
      assert.doesNotMatch(await page.locator('#sec-hoje').innerText(), /internal_error_code|persisted_gmail_observation_not_live_sync|crm_history_not_authoritative/);
      assert.equal(await page.locator('#today-respond article').count(), 1);
      assert.equal(await page.locator('#today-do article').count(), 1);
      state.blocks.approve = null; await refresh();
      assert.match(await page.locator('#today-approve').innerText(), /Não foi possível consultar revisões/);
      assert.equal(await page.locator('#today-do article').count(), 1);
      state = populated();
      const attack = '"><img src=x onerror=alert(1)><script>window.bad=1</script>&';
      state.blocks.do.items = [
        {...item('attack'), title: attack, owner: attack, last_message: attack, draft: {text: attack, origin: attack}},
        ...['javascript:alert(1)', 'data:text/html,bad', 'https://app.hubspot.com.evil.invalid/contacts/123',
          'https://user:pass@app.hubspot.com/contacts/123', 'https://app.hubspot.com:444/contacts/123',
          'https://app.hubspot.com/settings/123', '/api/os/v1/effects', '//evil.invalid', '#missing'].map((url, i) => ({...item('bad-' + i), action: {label: attack, url}})),
        {...item('bad-target'), action: {label: attack, target: 'missing'}}];
      await refresh();
      assert.equal(await page.locator('#today-do article').count(), 1);
      assert.equal(await page.locator('#sec-hoje img, #sec-hoje script').count(), 0);
      assert.match(await page.locator('#today-do').innerText(), /Há itens sem destino de ação válido/);
      assert.equal(await page.evaluate(() => window.bad), undefined);
      assert.equal(await page.locator('#qt-hoje').textContent(), '3');
      assert((await page.locator('#today-do').innerText()).includes(attack));
      const beforePoll = todayCalls();
      await page.locator('#today-do a').focus();
      await page.clock.runFor(60001); await settled();
      assert.equal(todayCalls(), beforePoll + 1);
      assert.equal(await page.locator('#today-do a').evaluate(el => el === document.activeElement), true);

      // Hold transport to exercise loading and exclusion of overlapping refreshes.
      hold = true; await page.locator('#today-refresh').click();
      await page.waitForFunction(() => document.querySelector('#today-respond').getAttribute('aria-busy') === 'true');
      const held = todayCalls(); await page.clock.runFor(60001);
      assert.equal(todayCalls(), held);
      hold = false; release(); await settled();
      failure = true; await refresh();
      assert.match(await page.locator('#today-status').innerText(), /Não foi possível consultar o dia/);
      assert.equal(await page.locator('#sec-hoje article').count(), 0);
      assert.equal(await page.locator('#qt-hoje').textContent(), '');
      failure = false; state = {...populated(), read_only: false}; await refresh();
      assert.match(await page.locator('#today-status').innerText(), /Não foi possível consultar o dia/);
      state = populated(); await refresh();
      assert.equal(await page.locator('#sec-hoje article').count(), 3);
      await page.locator('#abas [data-aba=contas]').click();
      const offTab = todayCalls(); await page.clock.runFor(60001);
      assert.equal(todayCalls(), offTab);
      await page.locator('#abas [data-aba=hoje]').click(); await settled();
      assert.equal(todayCalls(), offTab + 1);
      await page.evaluate(() => Object.defineProperty(document, 'hidden', {configurable: true, value: true}));
      const hiddenCalls = todayCalls(); await page.clock.runFor(60001);
      assert.equal(todayCalls(), hiddenCalls);
      await page.evaluate(() => Object.defineProperty(document, 'hidden', {configurable: true, value: false}));
      await page.locator('#hoje-pedido > summary').click();
      await page.waitForFunction(() => document.querySelector('#os-pedido-contato').options.length === 2);
      assert.deepEqual(await page.locator('#os-pedido-contato option').evaluateAll(options => options.map(option => option.value)), ['', '456']);
      assert.doesNotMatch(await page.locator('#hoje-pedido').innerText(), /Contato MX sintético|999/);
      await page.evaluate(() => {
        const select = document.querySelector('#os-pedido-contato');
        select.add(new Option('Injected stale contact', '999')); select.value = '999';
        document.querySelector('#os-pedido-texto').value = 'Pergunta sintética';
      });
      await page.locator('#os-pedido button[type=submit]').click();
      assert.match(await page.locator('#os-pedido-msg').innerText(), /Contato fora do trabalho do dia/);
      assert.equal(requests.some(request => request.pathname === '/api/os/v1/hermes/verify'), false);
      await page.locator('#hoje-pedido > summary').click();
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
      await page.locator('#today-refresh').focus();
      await page.keyboard.press('Tab'); await page.keyboard.press('Shift+Tab');
      assert.notEqual(await page.locator('#today-refresh').evaluate(el => getComputedStyle(el).outlineStyle), 'none');
      assert.deepEqual(errors, []);
      assert.equal(requests.filter(request => request.method !== 'GET').length, 2, 'Only the two explicit mocked draft requests may write');
      fs.mkdirSync(output, {recursive: true});
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({path: path.join(output, `today-${viewport.width}.png`), fullPage: true});
      console.log(`PASS ${viewport.width}: partial, missing block, dedupe, escaping/allowlist, polling/loading/no overlap, read_only/error recovery, off-tab/hidden, responsive`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
