// Phase 1 UI checks, one delegated slice. Binding: full Brain Jornada/Conta prototypes.
// Arrangement: funnel, seven stages, selected list, timeline/log; account five tabs.
// Proofs below cover source-backed metrics, BR-only scope, originals, local editing,
// empty/loading/error/retry, polling, races, escaping, links, keyboard and reflow.
// No backend, provider, credentials, persistence or execution evidence is claimed.
// Independent TLC verifier is the principal's responsibility (no child agents).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, '_ferramentas/painel_modelo.html'), 'utf8')
  .replaceAll("/*MODO*/'artifact'", "'servidor'").replaceAll('/*DADOS*/{}', '{}');
const output = path.join(root, '.scratch/phase1-journey-ui');
const collected = '2031-03-12T12:30:00Z';
const br = 'BR-UI-FIXTURE', other = 'BR-OTHER-FIXTURE', mx = 'MX27-EXCLUDED';
const stages = [['icp', 'ICP'], ['abordagem', 'Abordagem'], ['resposta', 'Resposta'], ['crm', 'CRM'], ['linkedin', 'LinkedIn'], ['followup', 'Follow-up'], ['preparacao', 'Preparação']];
const attack = '<img src=x onerror="window.injected=true"><script>window.injected=true</script>';
const original = 'Mensagem original sintética\n\n  Espaçamento preservado.\n' + 'Texto integral de teste. '.repeat(900) + '\nFULL_ORIGINAL_END';
const draft = 'Rascunho sintético para revisão local.';
const source = {label: 'Fonte sintética', status: 'ok', url: 'https://source.example.invalid/evidence/record'};
const metric = value => ({value, source: [source], reason: '', coverage: 'parcial'});
const journey = () => ({read_only: true, collected_at: collected,
  campaigns: [{id: br, label: 'Campanha BR sintética'}, {id: other, label: 'Outra campanha BR'}, {id: mx, label: 'Nunca mostrar MX'}, {id: 'unlinked', label: 'Sem vínculo'}],
  metrics: {reached: metric(12), replied: {...metric(null), reason: 'Sem leitura de respostas humanas.'}, hot: {...metric(null), reason: 'Classificação não disponível.'}, cost_per_hot: {value: null, source: [], reason: 'Sem custo comprovado.'}},
  stages: stages.map(([id, label]) => ({id, label, count: id === 'preparacao' ? null : 1, status: id === 'preparacao' ? 'off' : 'partial',
    reason: id === 'preparacao' ? 'Fonte de preparação desligada.' : 'Cobertura parcial da fonte.',
    items: [{id: id + '-row', name: 'Pessoa sintética ' + label, company: 'Empresa sintética', campaign_id: br, date: collected, owner: 'Pessoa responsável sintética', summary: 'Resumo sintético ' + attack,
      action: {label: 'Abrir', type: id === 'icp' ? 'account' : 'detail', id: id === 'icp' ? 'account:opaque/fixture' : 'detail:' + id + '/opaque'}},
    {id: 'excluded', name: 'MX nunca aparecer', campaign_id: mx}, {id: 'unknown', name: 'Sem identidade não entra', campaign_id: null}]})),
  timeline: {name: 'Pessoa sintética', company: 'Empresa sintética', events: [{id: 'event', date: collected, label: 'Resposta', summary: 'Resumo do evento', detail_id: 'detail:resposta/opaque'}]},
  log: [{date: collected, message: 'Registro sintético somente leitura.'}], sources: [source]});
const detail = id => ({id, title: 'Original integral sintético', meta: 'Origem sintética · ' + collected,
  blocks: [{label: 'Mensagem original', text: original + '\n' + attack, date: collected, sender: 'Remetente sintético', direction: 'inbound'}],
  source_links: [source, {label: 'Link inválido', url: 'javascript:alert(1)'}, {label: 'Não é registro', url: 'https://source.example.invalid/'}, {label: 'Credenciais não', url: 'https://name:pass@source.example.invalid/item'}],
  draft: {text: draft}, editable: true, approval_available: false, approval_reason: 'Sem vínculo com toque canônico. Aprovação bloqueada.'});
const account = () => ({id: 'account:opaque/fixture', name: 'Conta sintética', company: 'Empresa sintética', campaign_id: br,
  source: {path: 'fixtures/account.md', sha256: 'a'.repeat(64)}, limitations: ['Fonte sintética sem aprovação canônica.'],
  tabs: {research: [{text: 'Fato original sintético. ' + attack, label: 'Fonte pública sintética', published_on: collected, source_url: source.url}],
    dossier: [{key: 'M1', classification: 'hipótese', text: 'Hipótese original sem reescrita.', sources: [source]}],
    context: [{path: 'fixtures/context.md', sha256: 'b'.repeat(64), status: 'ok', usage: 'Uso original do contexto.'}],
    cadence: ['D0', 'D3', 'D7', 'D12', 'D15'].map((day, i) => ({id: 'touch:' + day, day, date: collected, channel: i % 2 ? 'LinkedIn' : 'E-mail', ammunition: 'M' + (i + 1), stop_condition: 'Parar se houver resposta.', status: 'aguardando revisão', subject: 'Assunto sintético ' + day, text: 'Original ' + day + '\n\n' + draft + '\nTOUCH_END_' + day, source_hash: String(i + 1).repeat(64)})),
    before_approval: [{text: 'Confirmar vínculo canônico.', blocking: true}, {text: 'Nota contextual.', blocking: false}]}});

(async () => {
  const browser = await chromium.launch({headless: true});
  fs.mkdirSync(output, {recursive: true});
  try {
    for (const viewport of [{width: 1440, height: 1100}, {width: 390, height: 844}, {width: 320, height: 800}]) {
      const page = await browser.newPage({viewport});
      const requests = [], errors = [];
      let state = journey(), accountState = account(), fail = false, detailFail = false, accountFail = false, listFail = false, hold = false, release;
      let canonical = false, canonicalBlocked = false, reviewFail = false, visitorMode = false;
      let version = 5;
      const stepText = {D0: draft, D3: 'Outro toque sintético original.'}, approved = new Set(), discarded = new Set();
      const visitors = Array.from({length: 10}, (_, i) => ({id: 'visitor-' + i, name: 'Conta visitante sintética ' + i, company: 'Empresa visitante sintética', campaign_id: null, source_group: 'visitantes-2026-10-22', reviewable: i < 6, readiness: i < 6 ? 'Pronto para revisão local' : i === 8 ? 'Bloqueado por região' : i === 9 ? 'Descartado' : 'Aguardar manhã'}));
      const reviewFor = step => ({cadence_id: 'canonical-fixture', step_id: step, expected_version: version, review_hash: String(version).repeat(64).slice(0, 64),
        context_hash: 'c'.repeat(64), content: stepText[step], subject: 'Assunto canônico ' + step,
        can_approve: !canonicalBlocked, blockers: canonicalBlocked ? ['Controle global pausado na fixture.'] : [],
        execution_confirmation: canonicalBlocked ? null : {confirmed: true, batch_id: 'canonical-fixture', batch_version: version, control_version: 0, scope_hash: (step === 'D0' ? 'd' : 'e').repeat(64),
          context: {platform: 'thunder_os', campaign_id: br, campaign_version: 'fixture-v1', rules_version: 'rules-fixture-v1', rules_hash: 'a'.repeat(64), configuration_hash: 'b'.repeat(64), run_id: 'fixture-run', providers: {email: 'fixture_only'}}}});
      await page.clock.install();
      page.on('pageerror', error => errors.push(error.message));
      await page.route('**/*', async route => {
        const request = route.request(), url = new URL(request.url());
        requests.push({path: url.pathname, query: url.search, method: request.method(), body: request.postDataJSON()});
        if (url.pathname === '/') return route.fulfill({contentType: 'text/html', body: html});
        let data = [];
        const send = value => route.fulfill({contentType: 'application/json', body: JSON.stringify(value)});
        const failure = () => route.fulfill({status: 503, contentType: 'application/json', body: '{}'});
        if (url.pathname.startsWith('/api/os/v1/journey/steps/')) {
          assert.equal(canonical, true, 'Raw sources cannot write');
          assert.equal(request.method(), 'POST');
          assert.equal(request.headers()['x-thunder-nonce'], 'fixture-nonce');
          const body = request.postDataJSON(), operation = url.pathname.split('/').at(-1);
          assert.equal(body.cadence_id, 'canonical-fixture');
          assert(['D0', 'D3'].includes(body.step_id));
          const review = reviewFor(body.step_id);
          const fields = ['cadence_id', 'step_id'];
          if (operation !== 'review') fields.push('expected_version', 'review_hash');
          if (operation === 'approve') fields.push('execution_confirmation');
          if (operation === 'revise') fields.push('content', 'subject');
          if (operation === 'discard') fields.push('reason');
          assert.deepEqual(Object.keys(body).sort(), fields.sort(), 'Strict exact-step schema');
          if (operation === 'review') { if (reviewFail) return route.fulfill({status: 503, contentType: 'application/json', body: JSON.stringify({erro: 'Revisão sintética indisponível. Tente novamente.'})}); return send(review); }
          assert.equal(body.expected_version, review.expected_version);
          assert.equal(body.review_hash, review.review_hash);
          if (operation === 'approve') { assert.deepEqual(body.execution_confirmation, review.execution_confirmation); approved.add(body.step_id); }
          else if (operation === 'revise') { stepText[body.step_id] = body.content; assert.equal(body.subject, review.subject); }
          else if (operation === 'discard') { assert(body.reason.trim()); discarded.add(body.step_id); }
          else assert.fail('No dispatch or other operation is allowed');
          version++;
          return send({applied: true, sent: false, status: operation === 'approve' ? 'approved' : 'ready_for_review', cadence: {id: 'canonical-fixture', version, steps: ['D0', 'D3'].map(id => ({id, content: stepText[id], status: approved.has(id) ? 'approved' : 'draft'}))}});
        }
        assert.equal(request.method(), 'GET', 'All other traffic is read-only');
        if (url.pathname === '/api/os/v1/journey') {
          if (hold) await new Promise(resolve => { release = resolve; });
          if (fail) return failure();
          data = structuredClone(state);
          if (visitorMode) data.stages.find(row => row.id === 'icp').items = visitors.map(row => ({...row, date: collected, summary: 'Do not show message for held visitors', action: {label: 'Abrir', type: 'account', id: row.id}}));
          if (url.searchParams.get('campaign') === other) {
            data.stages.forEach(stage => { stage.items = []; stage.count = 0; });
            data.timeline = {events: []}; data.log = []; data.metrics.reached.value = 0;
          }
        } else if (url.pathname.startsWith('/api/os/v1/journey/details/')) {
          if (detailFail) return failure();
          data = detail(decodeURIComponent(url.pathname.slice('/api/os/v1/journey/details/'.length)));
          if (canonical) {
            const step = data.id.includes('followup') ? 'D3' : 'D0';
            Object.assign(data, {canonical_cadence_id: 'canonical-fixture', canonical_step_id: step, approval_available: true, subject: 'Assunto canônico ' + step, draft: {text: stepText[step]}});
          }
        } else if (url.pathname === '/api/os/v1/journey/accounts') {
          if (listFail) return failure();
          data = {accounts: [{id: accountState.id, name: accountState.name, company: accountState.company, campaign_id: br}, {id: 'mx', name: 'Excluded MX account', campaign_id: mx}, {id: 'unknown', name: 'Unknown account', campaign_id: null}], limitations: ['Consulta sintética.']};
          if (visitorMode) data.accounts = visitors;
        } else if (url.pathname.startsWith('/api/os/v1/journey/accounts/')) {
          if (accountFail) return failure();
          data = accountState;
          if (canonical) {
            data = structuredClone(accountState);
            data.tabs.cadence.forEach(row => { if (['D0', 'D3'].includes(row.day)) Object.assign(row, {canonical_cadence_id: 'canonical-fixture', canonical_step_id: row.day, approval_available: true, text: stepText[row.day], subject: 'Assunto canônico ' + row.day}); });
          }
          if (visitorMode) data = {...account(), ...visitors.find(row => row.id === decodeURIComponent(url.pathname.split('/').at(-1)))};
        } else if (url.pathname === '/api/os/v1/session') data = {actor: 'ygor', nonce: 'fixture-nonce'};
        else if (url.pathname === '/api/os/v1/panel-capabilities') data = {legacy_mutations: false};
        else if (url.pathname === '/api/os/v1/today-work') data = {read_only: true, collected_at: collected, blocks: {}, archive: {items: [], count: 0}};
        else if (url.pathname === '/api/os/v1/cadencias') data = {campaigns: [], today: {}, unlinked: []};
        else if (url.pathname === '/api/os/v1/today') data = {priorities: [], work: [], cadences: [], replies: [], controls: {}, usage: [], alerts: []};
        return send(data);
      });
      const settled = () => page.waitForFunction(() => !document.querySelector('#journey-refresh').disabled);
      const refresh = async () => { await page.locator('#journey-refresh').click(); await settled(); };
      const callCount = () => requests.filter(row => row.path === '/api/os/v1/journey').length;
      const noOverflow = async () => {
        assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
        assert.equal(await page.locator('#journey-dialog').evaluate(node => node.open && node.scrollWidth > node.clientWidth), false);
      };
      await page.goto('https://journey-panel.test/#jornada'); await settled();
      assert.equal(await page.locator('body').getAttribute('data-aba'), 'jornada');
      assert.deepEqual(await page.locator('#abas [role=tab]').evaluateAll(nodes => nodes.map(node => node.firstChild.textContent)), ['Jornada', 'Campanhas', 'Contas', 'Sistema']);
      assert.equal(await page.locator('#journey-brand').isVisible(), true);
      assert.equal(await page.locator('body').evaluate(node => getComputedStyle(node).backgroundColor), 'rgb(14, 13, 11)');
      assert.deepEqual(await page.locator('#journey-campaign option').evaluateAll(nodes => nodes.map(node => node.value)), ['', br, other]);
      assert.equal(await page.locator('#journey-stages button').count(), 7);
      assert.match(await page.locator('[data-journey-metric=reached]').innerText(), /12/);
      assert.match(await page.locator('[data-journey-metric=replied]').innerText(), /\?[\s\S]*Sem leitura/);
      assert.equal(await page.locator('[data-journey-metric=cost_per_hot]').count(), 0);
      if (viewport.width === 1440) {
        const ys = await page.locator('#journey-stages button').evaluateAll(nodes => nodes.map(node => node.getBoundingClientRect().y));
        assert.equal(new Set(ys).size, 1, 'Seven stage tiles share one desktop row');
        assert.equal(await page.locator('.journey-bottom').evaluate(node => getComputedStyle(node).gridTemplateColumns.split(' ').length), 2);
      }
      for (const [id, label] of stages) {
        await page.locator(`[data-journey-stage=${id}]`).click();
        assert.match(await page.locator('#journey-stage-heading').innerText(), new RegExp(label));
        assert.equal(await page.locator('#journey-stage-items article').count(), 1);
        assert.equal(await page.locator('#journey-stage-items article button').count(), 1);
      }
      assert.match(await page.locator('[data-journey-stage=preparacao]').innerText(), /off/);
      assert.match(await page.locator('#journey-stage-reason').innerText(), /Fonte de preparação desligada/);
      await page.locator('[data-journey-stage=resposta]').click();
      const beforeFilter = callCount();
      await page.locator('#journey-filter').fill('not-in-fixture');
      assert.match(await page.locator('#journey-stage-items').innerText(), /Nenhum item encontrado/);
      await page.locator('#journey-filter').fill('Empresa sintética');
      assert.equal(await page.locator('#journey-stage-items article').count(), 1);
      assert.equal(callCount(), beforeFilter);
      assert.equal(await page.locator('#sec-jornada img,#sec-jornada script').count(), 0);
      assert.equal(await page.evaluate(() => window.injected), undefined);
      const open = page.locator('#journey-stage-items [data-journey-open]');
      await open.focus(); await page.keyboard.press('Enter');
      await page.waitForFunction(() => !document.querySelector('#journey-detail-body').getAttribute('aria-busy') || document.querySelector('#journey-detail-body').getAttribute('aria-busy') === 'false');
      assert.equal(await page.locator('#journey-dialog').getAttribute('open'), '');
      assert.equal(await page.locator('#journey-detail-body pre').first().textContent(), original + '\n' + attack);
      assert.equal(await page.locator('#journey-detail-body a').count(), 1);
      assert.equal(await page.locator('#journey-detail-body a').getAttribute('href'), source.url);
      assert.equal(await page.locator('#journey-detail-body img,#journey-detail-body script').count(), 0);
      const editor = page.locator('#journey-detail-body [data-journey-editor]');
      const textarea = editor.locator('textarea');
      assert.equal(await textarea.evaluate(node => getComputedStyle(node).color), 'rgb(244, 241, 234)', 'Draft text remains legible in the dark drawer');
      await textarea.fill('Um dois três');
      assert.equal(await editor.locator('[data-journey-words]').textContent(), '3 palavras');
      assert.match(await editor.locator('[data-journey-approval]').innerText(), /Aprovar versão editada/);
      assert.match(await editor.locator('[data-journey-approval-reason]').innerText(), /Sem vínculo com toque canônico/);
      assert.equal(await editor.locator('button').filter({hasText: /Aprovar/}).count(), 0, 'Unavailable approval is a reason, not a dead button');
      const beforePoll = callCount(); await page.clock.runFor(60001); await settled();
      assert.equal(callCount(), beforePoll + 1);
      assert.equal(await textarea.inputValue(), 'Um dois três', 'Polling cannot replace typed edits');
      await editor.locator('[data-journey-reset]').click();
      assert.equal(await textarea.inputValue(), draft);
      await textarea.fill('Edição descartável'); await editor.locator('[data-journey-discard]').click();
      assert.equal(await textarea.inputValue(), draft);
      await textarea.fill('Texto local mantido');
      await noOverflow();
      await page.screenshot({path: path.join(output, `detail-${viewport.width}.png`)});
      await page.keyboard.press('Escape');
      await page.waitForFunction(() => !document.querySelector('#journey-dialog').open && document.querySelector('#journey-stage-items [data-journey-open]') === document.activeElement);
      assert.equal(await page.locator('#journey-stage-items [data-journey-open]').evaluate(node => node === document.activeElement), true);
      await page.locator('#journey-stage-items [data-journey-open]').click();
      assert.equal(await textarea.inputValue(), 'Texto local mantido');
      await page.locator('#journey-close').click();
      detailFail = true; await page.locator('#journey-stage-items [data-journey-open]').click();
      await page.getByRole('button', {name: 'Tentar novamente', exact: true}).waitFor();
      assert.match(await page.locator('#journey-detail-status').innerText(), /Não foi possível/);
      detailFail = false; await page.getByRole('button', {name: 'Tentar novamente', exact: true}).click();
      await page.locator('#journey-detail-body pre').first().waitFor(); await page.keyboard.press('Escape');
      console.log(`PASS ${viewport.width}: arrangement/seven stages/BR/unknown/safe originals/deep links/edit/reset/discard/wordcount/no approval/poll/focus/detail retry`);

      await page.locator('#journey-campaign').selectOption(other); await settled();
      assert.match(requests.filter(row => row.path === '/api/os/v1/journey').at(-1).query, /campaign=BR-OTHER-FIXTURE/);
      assert.equal(await page.locator('#journey-stage-items article').count(), 0);
      await page.locator('#journey-campaign').selectOption(br); await settled();
      await page.locator('#journey-stage-items [data-journey-open]').click();
      await page.locator('#journey-detail-body pre').first().waitFor();
      assert.equal(requests.filter(row => row.path.includes('/details/')).at(-1).query, '?campaign=' + br);
      await page.keyboard.press('Escape');
      state.metrics.cost_per_hot = metric(25); await refresh();
      assert.match(await page.locator('[data-journey-metric=cost_per_hot]').innerText(), /25,00/);
      state.metrics.reached = {value: 900, source: [], reason: 'Sem comprovação.'}; await refresh();
      assert.match(await page.locator('[data-journey-metric=reached]').innerText(), /\?/);
      state = journey(); await refresh();
      await page.locator('[data-journey-stage=icp]').click();
      await page.locator('#journey-stage-items [data-journey-kind=account]').click();
      await page.locator('#journey-account-tabs').waitFor();
      assert.equal(await page.locator('body').getAttribute('data-aba'), 'contas');
      assert.deepEqual(await page.locator('#journey-account-tabs button').allTextContents(), ['Pesquisa ABM', 'Dossiê', 'Contexto usado', 'Cadência', 'Antes de aprovar (2)']);
      assert.match(await page.locator('#journey-account-body').innerText(), /fixtures\/account.md/);
      for (const [key, content] of [['research','Fato original sintético'], ['dossier','Hipótese original sem reescrita'], ['context','Uso original do contexto'], ['before_approval','Confirmar vínculo canônico']]) {
        await page.locator(`[data-journey-tab=${key}]`).click();
        assert((await page.locator('#journey-account-panel').innerText()).includes(content));
      }
      await page.locator('[data-journey-tab=cadence]').click();
      for (const touch of accountState.tabs.cadence) {
        const block = page.locator(`[data-journey-touch="${touch.id}"]`);
        if (!await block.evaluate(node => node.open)) await block.locator('summary').click();
        assert.equal(await block.locator('pre').textContent(), touch.text);
        await block.locator('textarea').fill('Edição local ' + touch.day);
        assert.equal(await block.locator('[data-journey-words]').textContent(), '3 palavras');
        await block.locator('[data-journey-reset]').click();
        assert.equal(await block.locator('textarea').inputValue(), touch.text);
        await block.locator('textarea').fill('Preservado ' + touch.day);
      }
      await page.locator('[data-journey-tab=cadence]').focus(); await page.keyboard.press('ArrowRight');
      assert.equal(await page.locator('[data-journey-tab=before_approval]').getAttribute('aria-selected'), 'true');
      await page.keyboard.press('ArrowLeft');
      assert.equal(await page.locator('[data-journey-touch="touch:D0"] textarea').inputValue(), 'Preservado D0');
      const offTab = callCount(); await page.clock.runFor(60001); assert.equal(callCount(), offTab);
      await noOverflow();
      await page.screenshot({path: path.join(output, `account-${viewport.width}.png`)});
      await page.locator('#journey-account-list-back').click();
      await page.waitForFunction(() => !document.querySelector('#journey-accounts-refresh').disabled);
      assert.equal(await page.locator('#journey-accounts-items article').count(), 1);
      listFail = true; await page.locator('#journey-accounts-refresh').click();
      await page.waitForFunction(() => !document.querySelector('#journey-accounts-refresh').disabled);
      assert.match(await page.locator('#journey-accounts-status').innerText(), /Não foi possível/);
      listFail = false; await page.locator('#journey-accounts-refresh').click();
      await page.waitForFunction(() => !document.querySelector('#journey-accounts-refresh').disabled);
      accountFail = true; await page.locator('[data-journey-account]').click();
      await page.getByRole('button', {name: 'Tentar novamente', exact: true}).waitFor();
      accountFail = false; await page.getByRole('button', {name: 'Tentar novamente', exact: true}).click();
      await page.locator('#journey-account-tabs').waitFor();
      await page.locator('#journey-account-back').click(); await settled();
      assert.equal(await page.locator('body').getAttribute('data-aba'), 'jornada');
      await page.evaluate(() => { location.hash = '#hoje'; });
      await page.waitForFunction(() => document.body.dataset.aba === 'jornada');
      assert.equal(await page.locator('#sec-hoje').isVisible(), false);
      await page.locator('#abas [data-aba=jornada]').focus(); await page.keyboard.press('ArrowRight');
      assert.equal(await page.locator('#abas [data-aba=cadencias]').getAttribute('aria-selected'), 'true');
      await page.keyboard.press('Home'); await settled();
      hold = true; await page.locator('#journey-refresh').click();
      await page.waitForFunction(() => document.querySelector('#journey-content').getAttribute('aria-busy') === 'true');
      const held = callCount(); await page.clock.runFor(60001); assert.equal(callCount(), held);
      hold = false; release(); await settled();
      fail = true; await refresh();
      assert.match(await page.locator('#journey-status').innerText(), /Não foi possível/);
      assert.equal(await page.locator('#journey-stages').count(), 0);
      fail = false; state = {...journey(), read_only: false}; await refresh();
      assert.match(await page.locator('#journey-status').innerText(), /Não foi possível/);
      state = journey(); await refresh();
      await page.evaluate(() => Object.defineProperty(document, 'hidden', {configurable: true, value: true}));
      const hidden = callCount(); await page.clock.runFor(60001); assert.equal(callCount(), hidden);
      await page.evaluate(() => Object.defineProperty(document, 'hidden', {configurable: true, value: false}));
      assert.equal(requests.some(row => /hermes|draft|steps\/(approve|revise|review|discard)/.test(row.path)), false, 'No model or mutation on GET');
      assert.equal(await page.locator('#sec-jornada').evaluate(node => getComputedStyle(node).letterSpacing), 'normal');
      await noOverflow();
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.screenshot({path: path.join(output, `journey-${viewport.width}.png`), fullPage: true});
      assert.deepEqual(errors, []);
      assert.equal(requests.every(row => row.method === 'GET'), true);
      console.log(`PASS ${viewport.width}: five account tabs/all original touches/local editing/keyboard/empty/campaign query/cost evidence/HTTP recovery/poll exclusion/reflow/no writes`);

      // Canonical fixture: explicit user actions authorize only D0, never D3 or a whole pack.
      canonical = true;
      await page.locator('[data-journey-stage=resposta]').click();
      await page.locator('#journey-stage-items [data-journey-open]').click();
      const canonicalEditor = page.locator('#journey-detail-body [data-journey-editor]');
      await canonicalEditor.locator('[data-journey-review]').waitFor();
      const mutations = () => requests.filter(row => row.method === 'POST');
      assert.equal(mutations().length, 0, 'Opening canonical content does not review or approve automatically');
      reviewFail = true; await canonicalEditor.locator('[data-journey-review]').click();
      await page.waitForFunction(() => !document.querySelector('#journey-detail-body [data-journey-review]').disabled);
      assert.match(await canonicalEditor.locator('[data-journey-operation]').innerText(), /indisponível/);
      reviewFail = false; canonicalBlocked = true;
      await canonicalEditor.locator('[data-journey-review]').click();
      await page.waitForFunction(() => !document.querySelector('#journey-detail-body [data-journey-review]').disabled);
      assert.match(await canonicalEditor.locator('[data-journey-review-body]').innerText(), /Controle global pausado/);
      assert.equal(await canonicalEditor.locator('[data-journey-approve]').count(), 0);
      canonicalBlocked = false;
      await canonicalEditor.locator('[data-journey-review]').click();
      await canonicalEditor.locator('[data-journey-confirm]').waitFor();
      assert.match(await canonicalEditor.locator('[data-journey-review-body]').innerText(), /BR-UI-FIXTURE[\s\S]*fixture-v1[\s\S]*rules-fixture-v1/);
      assert.equal(await canonicalEditor.locator('[data-journey-approve]').isDisabled(), true);
      await canonicalEditor.locator('[data-journey-confirm]').check();
      await page.screenshot({path: path.join(output, `canonical-review-${viewport.width}.png`)});
      await canonicalEditor.locator('[data-journey-approve]').click();
      await page.waitForFunction(() => !document.querySelector('#journey-detail-body [data-journey-review]').disabled);
      assert.match(await canonicalEditor.locator('[data-journey-operation]').innerText(), /Autorização registrada para o toque D0[\s\S]*Envio não confirmado/);
      assert.deepEqual([...approved], ['D0']);
      assert.equal(mutations().filter(row => row.path.endsWith('/approve')).length, 1);
      assert.equal(mutations().find(row => row.path.endsWith('/approve')).body.step_id, 'D0');
      await page.keyboard.press('Escape');
      await page.locator('[data-journey-stage=followup]').click();
      await page.locator('#journey-stage-items [data-journey-open]').click();
      await canonicalEditor.locator('[data-journey-review]').waitFor();
      await canonicalEditor.locator('[data-journey-review]').click();
      await canonicalEditor.locator('[data-journey-confirm]').waitFor();
      await canonicalEditor.locator('[data-journey-confirm]').check();
      await canonicalEditor.locator('[data-journey-text]').fill('D3 editado localmente para revisão exata.');
      assert.equal(await canonicalEditor.locator('[data-journey-approve]').isDisabled(), true, 'Editing invalidates approval of the previous review');
      assert.equal(await canonicalEditor.locator('[data-journey-confirm]').isChecked(), false);
      const sequenceStart = mutations().length;
      await canonicalEditor.locator('[data-journey-revise]').click();
      await page.waitForFunction(() => !document.querySelector('#journey-detail-body [data-journey-review]').disabled);
      assert.deepEqual(mutations().slice(sequenceStart).map(row => row.path.split('/').at(-1)), ['revise', 'review'], 'Persisted edit requires a fresh review before another approval');
      assert.equal(mutations().slice(sequenceStart)[0].body.step_id, 'D3');
      assert.equal(stepText.D3, 'D3 editado localmente para revisão exata.');
      assert.equal(await canonicalEditor.locator('[data-journey-approve]').isDisabled(), true, 'Fresh explicit confirmation required after revise');
      assert.match(await canonicalEditor.locator('[data-journey-approve]').innerText(), /versão editada/);
      assert.equal(await canonicalEditor.locator('[data-journey-review-body] pre').first().textContent(), stepText.D3);
      await canonicalEditor.locator('details').last().locator('summary').click();
      await canonicalEditor.locator('[data-journey-discard-reason]').fill('Descartar somente D3 na fixture.');
      await canonicalEditor.locator('[data-journey-discard-step]').click();
      await page.waitForFunction(() => !document.querySelector('#journey-detail-body [data-journey-review]').disabled);
      assert.deepEqual([...discarded], ['D3']);
      assert.deepEqual([...approved], ['D0'], 'Revision/discard must not authorize D3');
      assert.match(await canonicalEditor.locator('[data-journey-operation]').innerText(), /Descarte registrado para o toque D3/);
      assert.equal(requests.some(row => /dispatch|execute|send|hermes|hubspot/.test(row.path)), false);
      await noOverflow();
      await page.screenshot({path: path.join(output, `canonical-${viewport.width}.png`)});
      await page.keyboard.press('Escape');
      await page.locator('#abas [data-aba=contas]').click();
      await page.locator('[data-journey-account]').click();
      await page.locator('#journey-account-tabs').waitFor();
      assert.equal(await page.locator('#journey-account-panel [data-journey-review]').count(), 2, 'Only canonically linked account touches can review');
      canonicalBlocked = true;
      const accountEditor = page.locator('[data-journey-touch="touch:D0"] [data-journey-editor]');
      await accountEditor.locator('[data-journey-review]').click();
      await page.waitForFunction(() => !document.querySelector('[data-journey-touch="touch:D0"] [data-journey-review]').disabled);
      assert.match(await accountEditor.locator('[data-journey-review-body]').innerText(), /Controle global pausado/);
      canonicalBlocked = false;
      await page.locator('#journey-account-back').click(); await settled();
      console.log(`PASS ${viewport.width}: canonical POST nonce/exact schemas/review blockers/explicit D0-only approval/edit invalidation/revise then fresh review/D3 discard/no dispatch`);

      // The explicitly designated draft source group is not a Registry campaign.
      canonical = false; visitorMode = true;
      await page.locator('#journey-campaign').selectOption(''); await settled();
      await page.locator('[data-journey-stage=icp]').click();
      assert.equal(await page.locator('#journey-stage-items article').count(), 10);
      assert.match(await page.locator('#journey-stage-items').innerText(), /Visitantes · rascunhos/);
      assert.equal(await page.locator('#journey-campaign option').count(), 3, 'Draft source group never becomes a campaign');
      assert.equal(await page.locator('#journey-stage-items article').filter({hasText: 'Do not show message for held visitors'}).count(), 6, 'Held visitors expose status only');
      await page.locator('#journey-filter').fill('Do not show message for held visitors');
      assert.equal(await page.locator('#journey-stage-items article').count(), 6, 'Search cannot expose the hidden message of a status-only visitor');
      await page.locator('#journey-filter').fill('');
      await page.locator('#abas [data-aba=contas]').click();
      await page.locator('#journey-accounts-refresh').click();
      await page.waitForFunction(() => !document.querySelector('#journey-accounts-refresh').disabled);
      assert.equal(await page.locator('#journey-accounts-items article').count(), 10);
      const beforeVisitors = mutations().length;
      for (let i = 0; i < 10; i++) {
        await page.locator(`[data-journey-account=visitor-${i}]`).click();
        await page.waitForFunction(name => document.querySelector('#journey-account-name')?.textContent === name, 'Conta visitante sintética ' + i);
        if (i < 6) {
          assert.equal(await page.locator('#journey-account-tabs button').count(), 5);
          assert.equal(await page.locator('#journey-account-panel [data-journey-review]').count(), 0);
          await page.locator('#journey-account-panel [data-journey-text]').first().fill('Edição visitante local.');
        } else {
          assert.equal(await page.locator('#journey-account-tabs').count(), 0);
          assert.equal(await page.locator('#journey-account-body textarea, #journey-account-body [data-journey-approve]').count(), 0);
          assert.match(await page.locator('#journey-account-body').innerText(), /Somente status/);
        }
        await page.locator('#journey-account-list-back').click();
        await page.waitForFunction(() => !document.querySelector('#journey-accounts-refresh').disabled);
      }
      assert.equal(mutations().length, beforeVisitors, 'Visitor drafts cannot review, approve or persist');
      await noOverflow();
      assert.deepEqual(errors, []);
      console.log(`PASS ${viewport.width}: ten explicit draft visitors/six locally editable/four status-only/no Registry identity/no Mexico messages/no mutations`);
      await page.close();
    }
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
