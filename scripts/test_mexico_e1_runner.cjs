'use strict';
const assert = require('node:assert/strict');
const {test} = require('node:test');
const path = require('node:path');
const {spawnSync, spawn} = require('node:child_process');
const {once} = require('node:events');
const fs = require('node:fs/promises');
const {runMexicoE1, createMexicoContext, makeToquesBridge, copyHash, htmlFor, MARKER} = require('./mexico_e1_runner.cjs');
let serial = 0;
const clone = value => JSON.parse(JSON.stringify(value));

function fixture(count = 1) {
  const directory = process.env.THUNDER_TEST_SQLITE_DIR;
  if (!directory) throw Error('Run through test_thunder_os_mexico_runner (isolated SQLite required)');
  const db = path.join(directory, `${++serial}.db`);
  const python = process.env.THUNDER_TEST_PYTHON || 'python3';
  const init = spawnSync(python, ['_ferramentas/test_thunder_os_mexico_runner.py', '--fixture', db],
    {env: {...process.env, PYTHONPATH: '_ferramentas', PYTHONDONTWRITEBYTECODE: '1'}, encoding: 'utf8'});
  assert.equal(init.status, 0, init.stderr);
  const bridge = makeToquesBridge({dependencyPath: process.env.THUNDER_TEST_TOQUES_PATH, python,
    env: {PATH: process.env.PATH, TOQUES_SQLITE: db, THUNDER_DB_URL: ''}});
  let clock = Date.parse('2026-10-21T14:10:00Z');
  const queue = Array.from({length: count}, (_, n) => {
    const c = {id: String(101 + n), task_id: String(1 + n), email: `synthetic${n}@example.test`,
      email_status: 'Verified', subject: 'Synthetic thread', text: 'First paragraph.\n\nSecond paragraph.',
      linkedin_url: `https://www.linkedin.com/in/synthetic-${n}/`, account: 'synthetic-linkedin'};
    c.hash = copyHash(c);
    return c;
  });
  const scope = {campaign_id: 'MX27-CDMX', batch: 'MX27-CDMX-D0-20261021',
    metadata_campaign: 'Mexico2027-networking', sender: 'ygor.bueno@thundermustard.com',
    portal_id: '51412240', owner_id: '92901049', identity_holds: [],
    window_start: '2026-10-21T14:00:00Z', window_end: '2026-10-22T06:00:00Z'};
  const tasks = new Map(queue.map(c => [c.task_id, {id: c.task_id, portal_id: scope.portal_id,
    association_complete: true, contact_ids: [c.id], properties: {hubspot_owner_id: scope.owner_id,
      hs_task_status: 'WAITING', hs_task_body: '<pre>' + JSON.stringify({contact_id: c.id,
        campaign: scope.metadata_campaign, batch: scope.batch, task_key: `${scope.batch}:${c.id}:E1`,
        account: scope.sender, recipient: c.email, provider: 'Gmail',
        sha256_subject_newline_text: c.hash, outreach_authorized: true, execution_blocked: false}) + '</pre>' + c.text}}]));
  const ledger = [], sends = [], updates = [], checkpoints = [], sleeps = [];
  let identities = 0;
  const ctx = {queue, scope, state: {}, toques: bridge, now: () => clock, random: () => 0.5,
    sleep: async ms => { clock += ms; sleeps.push(ms); }, yieldControl: async () => {}, lockHeld: async () => true,
    readPrepared: async c => ({...c, step: 'E1', contact_id: c.id, sha256: c.hash, execution_blocked: false}),
    readWorkbook: async c => [{destination: null, url: c.linkedin_url}],
    save: async row => { ledger.push(clone(row)); }, readLedger: async () => clone(ledger),
    checkpoint: async rows => checkpoints.push(clone(rows)), notify: () => {}, tools: {
      identity: async () => { identities++; return {portal_id: scope.portal_id, owner_id: scope.owner_id, mailbox: scope.sender}; },
      getTask: async id => clone(tasks.get(String(id))),
      getContact: async id => ({id, properties: {email: queue.find(c => c.id === id).email,
        hs_email_optout: 'false', hs_email_bounce: '0', hs_sales_email_last_replied: null}}),
      searchGmail: async () => ({message_ids: []}), conversationExists: async () => ({success: true, hasConversation: false}),
      searchActivities: async () => ({total: 0}),
      gmail_send_email: async args => { sends.push({args, at: clock}); return {id: `abc${sends.length.toString().padStart(3, '0')}`}; },
      gmail_read_email: async ({message_id, format}) => {
        assert.equal(format, 'full');
        const sent = sends.at(-1).args;
        return {id: message_id, label_ids: ['SENT'], payload: {headers: [
          {name: 'From', value: sent.from_address}, {name: 'To', value: sent.to}, {name: 'Subject', value: sent.subject}
        ], body: {content: sent.payload.body.content}}};
      },
      updateTask: async (id, props) => { updates.push({id, props}); Object.assign(tasks.get(id).properties, props); return {accepted: true}; }
    }};
  const b = c => ({campaign_id: scope.campaign_id, batch: scope.batch, hubspot_task_id: String(c.task_id),
    contact_id: c.id, touch: 'E1', sender: scope.sender, recipient: c.email, payload_sha256: c.hash});
  return {ctx, ledger, sends, updates, checkpoints, sleeps, tasks, b, bridge, identities: () => identities};
}
async function run(f, indexes = [0]) { return runMexicoE1(f.ctx, indexes); }
async function lookup(f, n = 0) { const c = f.ctx.queue[n]; return f.bridge({op: 'lookup', binding: f.b(c), subject: c.subject}); }

test('runner calls real CLI reserve/confirm, full readback, linked task receipt, replay is CRM only', async () => {
  const f = fixture();
  const original = f.ctx.tools.gmail_send_email;
  f.ctx.tools.gmail_send_email = async args => { assert.equal((await lookup(f)).status, 'reservado'); return original(args); };
  assert.equal((await run(f))[0].status, 'CONFIRMED_COMPLETED');
  assert.equal((await lookup(f)).status, 'feito');
  const receipt = (await lookup(f)).receipt;
  assert.equal(receipt.payload_sha256, copyHash(f.ctx.queue[0]));
  assert.equal(receipt.hubspot_task_id, '1');
  assert.equal(receipt.campaign_id, 'MX27-CDMX');
  assert.equal(f.sends[0].args.payload.body.content, '<p>First paragraph.</p><p>Second paragraph.</p>');
  assert.equal((await run(f))[0].status, 'CONFIRMED_COMPLETED');
  assert.equal(f.sends.length, 1);
  assert.equal(f.updates.length, 1);
  assert.equal(f.tasks.get('1').properties.hs_task_body.split(MARKER).length - 1, 1);
  assert.equal(f.checkpoints.at(-1).length, 1);
});

for (const failure of ['update', 'readback', 'partial-body', 'receipt-save', 'confirm-response', 'confirm-before-write']) {
  test(`confirmed remains terminal after ${failure}; replay reconciles without provider retry`, async () => {
    const f = fixture();
    const update = f.ctx.tools.updateTask, get = f.ctx.tools.getTask, save = f.ctx.save;
    let pending = true;
    if (failure === 'update') f.ctx.tools.updateTask = async (...args) => {
      if (pending) { pending = false; throw Error('CRM_UPDATE_FAILED'); } return update(...args);
    };
    if (failure === 'readback') f.ctx.tools.getTask = async id => {
      const row = await get(id);
      if (pending && f.updates.length) { pending = false; row.properties.hs_task_status = 'WAITING'; }
      return row;
    };
    if (failure === 'partial-body') f.ctx.tools.updateTask = async (id, props) => {
      if (pending) {
        pending = false;
        await update(id, {hs_task_body: props.hs_task_body});
        return {accepted: true};
      }
      return update(id, props);
    };
    if (failure === 'receipt-save') f.ctx.save = async row => {
      if (pending && row.status === 'PROVIDER_CONFIRMED') { pending = false; throw Error('LEDGER_WRITE_FAILED'); }
      return save(row);
    };
    if (failure.startsWith('confirm-')) f.ctx.toques = async request => {
      if (pending && request.op === 'confirm') {
        pending = false;
        if (failure === 'confirm-response') await f.bridge(request);
        throw Error('TOQUES_PROCESS_FAILED');
      }
      return f.bridge(request);
    };
    assert.equal((await run(f))[0].status, 'PROVIDER_CONFIRMED_CRM_PENDING');
    assert.equal((await lookup(f)).status, failure === 'confirm-before-write' ? 'reservado' : 'feito');
    assert.equal((await run(f))[0].status, 'CONFIRMED_COMPLETED');
    assert.equal((await lookup(f)).status, 'feito');
    assert.equal(f.sends.length, 1);
    assert.equal(f.tasks.get('1').properties.hs_task_body.split(MARKER).length - 1, 1);
  });
}

test('unknown provider outcome is persisted incerto and never retried', async () => {
  const f = fixture();
  let sends = 0;
  f.ctx.tools.gmail_send_email = async () => { sends++; throw Error('PROVIDER_TIMEOUT'); };
  assert.equal((await run(f))[0].status, 'UNCERTAIN_NO_RETRY');
  assert.equal((await lookup(f)).status, 'incerto');
  assert.equal((await run(f))[0].status, 'UNCERTAIN_NO_RETRY');
  assert.equal(sends, 1);
  assert.equal(f.updates.length, 0);
});

for (const kind of ['id', 'from', 'to', 'subject', 'body', 'label']) {
  test(`Gmail ${kind} mismatch is uncertain, no CRM write or retry`, async () => {
    const f = fixture(), read = f.ctx.tools.gmail_read_email;
    f.ctx.tools.gmail_read_email = async args => {
      const row = await read(args);
      if (kind === 'id') row.id = 'abc999';
      else if (kind === 'body') row.payload.body.content += 'changed';
      else if (kind === 'label') row.label_ids = ['INBOX'];
      else row.payload.headers.find(h => h.name.toLowerCase() === kind).value = 'wrong@example.test';
      return row;
    };
    assert.equal((await run(f))[0].status, 'UNCERTAIN_NO_RETRY');
    assert.equal((await run(f))[0].status, 'UNCERTAIN_NO_RETRY');
    assert.equal(f.sends.length, 1);
    assert.equal(f.updates.length, 0);
  });
}

test('four legacy numeric uncertain rows and numeric intent block before any provider read', async () => {
  for (const status of ['UNCERTAIN_NO_RETRY', 'UNCERTAIN_NO_RETRY', 'UNCERTAIN_NO_RETRY', 'UNCERTAIN_NO_RETRY', 'SEND_INTENT']) {
    const f = fixture();
    f.ledger.push({task_id: 1, contact_id: '101', status});
    assert.equal((await run(f))[0].status, 'UNCERTAIN_NO_RETRY');
    assert.equal(await lookup(f), null);
    assert.equal(f.sends.length, 0);
    assert.equal(f.identities(), 0);
    assert.equal(f.checkpoints.at(-1).length, 1);
    assert.equal(f.checkpoints.at(-1)[0].task_id, '1');
  }
});

test('unknown item does not stop independent approved item; 60-120s pacing is preserved', async () => {
  const f = fixture(2), send = f.ctx.tools.gmail_send_email;
  let calls = 0;
  f.ctx.tools.gmail_send_email = async args => { const result = await send(args); if (++calls === 1) throw Error('PROVIDER_TIMEOUT'); return result; };
  const rows = await run(f, [0, 1]);
  assert.deepEqual(rows.map(r => r.status), ['UNCERTAIN_NO_RETRY', 'CONFIRMED_COMPLETED']);
  const elapsed = f.sends[1].at - f.sends[0].at;
  assert.ok(elapsed >= 60000 && elapsed <= 120000);
});

test('existing reservation blocks duplicate provider effect', async () => {
  const f = fixture(), c = f.ctx.queue[0];
  await f.bridge({op: 'reserve', binding: f.b(c), subject: c.subject});
  assert.equal((await run(f))[0].status, 'UNCERTAIN_NO_RETRY');
  assert.equal(f.sends.length, 0);
});

for (const kind of ['task', 'campaign', 'contact', 'hash', 'sender', 'unlinked', 'receipt-conflict']) {
  test(`CRM reconcile ${kind} mismatch refuses writes and preserves confirmed receipt`, async () => {
    const f = fixture();
    await run(f);
    const receipt = (await lookup(f)).receipt;
    const task = f.tasks.get('1');
    if (kind === 'task') task.id = '2';
    else if (kind === 'unlinked') task.association_complete = false;
    else if (kind === 'contact') task.contact_ids = ['999'];
    else if (kind === 'receipt-conflict') task.properties.hs_task_body = task.properties.hs_task_body.replace('abc001', 'abc999');
    else {
      const pair = {campaign: ['Mexico2027-networking', 'OTHER'], hash: [f.ctx.queue[0].hash, 'b'.repeat(64)],
        sender: [f.ctx.scope.sender, 'other@example.test']}[kind];
      task.properties.hs_task_body = task.properties.hs_task_body.replaceAll(...pair);
    }
    assert.equal((await run(f))[0].status, 'PROVIDER_CONFIRMED_CRM_PENDING');
    assert.equal(f.updates.length, 1);
    assert.equal(f.sends.length, 1);
    assert.deepEqual((await lookup(f)).receipt, receipt);
  });
}

test('scope cannot change portal or owner even when injected identity agrees', async () => {
  for (const field of ['portal_id', 'owner_id']) {
    const f = fixture();
    f.ctx.scope[field] = '999';
    await assert.rejects(run(f), /RUNNER_SCOPE_INVALID/);
    assert.equal(f.sends.length, 0);
  }
});

test('wrong authenticated mailbox, closed window, lost lock and changed copy block before sends', async () => {
  for (const kind of ['mailbox', 'window', 'lock', 'hash', 'literal-backslash']) {
    const f = fixture();
    if (kind === 'mailbox') f.ctx.tools.identity = async () => ({...f.ctx.scope, mailbox: 'other@example.test'});
    if (kind === 'window') f.ctx.scope.window_end = '2026-10-21T14:01:00Z';
    if (kind === 'lock') f.ctx.lockHeld = async () => false;
    if (kind === 'hash') f.ctx.queue[0].text += 'changed';
    if (kind === 'literal-backslash') {
      const crypto = require('node:crypto');
      const c = f.ctx.queue[0];
      c.hash = crypto.createHash('sha256').update(c.subject + '\\n' + c.text).digest('hex');
    }
    assert.equal((await run(f))[0].status, 'SKIPPED_BLOCKED', kind);
    assert.equal(f.sends.length, 0);
    assert.equal(f.updates.length, 0);
  }
});

test('missing/invalid suppression fields fail closed; explicit empty fields are allowed', async () => {
  for (const field of ['hs_email_optout', 'hs_email_bounce', 'hs_sales_email_last_replied']) {
    for (const value of [undefined, {}, false, 0]) {
      const f = fixture(), get = f.ctx.tools.getContact;
      f.ctx.tools.getContact = async id => {
        const row = await get(id);
        if (value === undefined) delete row.properties[field]; else row.properties[field] = value;
        return row;
      };
      assert.equal((await run(f))[0].status, 'SKIPPED_BLOCKED');
      assert.equal(f.sends.length, 0);
      assert.equal(f.updates.length, 0);
      assert.equal(await lookup(f), null);
    }
  }
  const f = fixture(), get = f.ctx.tools.getContact;
  f.ctx.tools.getContact = async id => {
    const row = await get(id);
    for (const field of ['hs_email_optout', 'hs_email_bounce', 'hs_sales_email_last_replied']) row.properties[field] = null;
    return row;
  };
  assert.equal((await run(f))[0].status, 'CONFIRMED_COMPLETED');
});

test('legacy STOP_ON_REPLY without task ID stops contact and survives checkpoint', async () => {
  const f = fixture();
  const stop = {at: '2026-10-21T15:30:00Z', channels: ['email', 'linkedin'], contact_id: '101',
    email: 'synthetic0@example.test', event: 'STOP_ON_REPLY', hubspot_readback: {objects: []},
    name: 'Synthetic', reason: 'Human reply'};
  f.ledger.push(stop);
  assert.deepEqual(await run(f, []), []);
  assert.equal((await run(f))[0].status, 'SKIPPED_REPLY');
  assert.equal(f.identities(), 0);
  assert.equal(f.sends.length, 0);
  assert.deepEqual(f.checkpoints.at(-1).find(r => r.event === 'STOP_ON_REPLY'), stop);
});

test('real context consumer maps current MCP tools, prepared file, durable ledger, workbook and held lock', async () => {
  const f = fixture(), c = f.ctx.queue[0];
  const root = path.join(process.env.THUNDER_TEST_SQLITE_DIR, 'consumer');
  const work = path.join(root, '.work'), lockPath = path.join(work, 'lote02-03', '.send.lock');
  await fs.mkdir(path.dirname(lockPath), {recursive: true});
  const python = process.env.THUNDER_TEST_PYTHON || 'python3';
  const workbookPython = process.env.THUNDER_TEST_WORKBOOK_PYTHON || python;
  const workbook = path.join(root, 'synthetic.xlsx');
  const setup = spawnSync(workbookPython, ['-c', "import openpyxl,sys\nw=openpyxl.Workbook();w.active.title='Todos';w.create_sheet('Fora da lista original');w['Todos'].cell(7,18,'101');w['Todos'].cell(7,9,sys.argv[2]);w.save(sys.argv[1]);w.close()", workbook, c.linkedin_url], {encoding: 'utf8'});
  assert.equal(setup.status, 0, setup.stderr);
  const auth = {sender: f.ctx.scope.sender, start_at: f.ctx.scope.window_start, expires_local_date: '2026-10-21',
    timezone: 'America/Mexico_City', approved_by: 'synthetic', blocked_contact_ids: []};
  const pkg = {batch: f.ctx.scope.batch, email_execution_authorization: auth,
    tasks: [{step: 'E1', contact_id: 101, subject: c.subject, text: c.text, sha256: c.hash,
      properties: clone(f.tasks.get('1').properties), email_execution: {task_id: 1}}],
    contacts: [{hubspot_id: '101', email_execution: {task_id: 1}, email: c.email, email_status: c.email_status,
      linkedin_url: c.linkedin_url, linkedin_account: '6abe5fc087213e2040fb9b1b'}]};
  const packageText = JSON.stringify(pkg);
  await fs.writeFile(path.join(work, 'cdmx-d0-prepared.json'), packageText);
  await fs.writeFile(path.join(work, 'cdmx-email-send-ledger.jsonl'), '');
  const holder = spawn(python, ['-u', '-c', "import fcntl,sys\nf=open(sys.argv[1],'a');fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB);print('LOCK_HELD',flush=True);sys.stdin.read()", lockPath], {stdio: ['pipe', 'pipe', 'pipe']});
  const acknowledgement = String((await once(holder.stdout, 'data'))[0]);
  let associations = 0;
  const mcp = {
    mcp__codex_apps__hubspot_get_user_details: async () => ({accountId: 51412240, userId: 42,
      userInformation: {ownerId: 92901049}, onboarded: true, guidance: null}),
    mcp__codex_apps__gmail_get_profile: async () => ({email_address: f.ctx.scope.sender}),
    mcp__codex_apps__hubspot_get_crm_objects: async args => ({objects: [await (args.objectType === 'TASK'
      ? f.ctx.tools.getTask(String(args.objectIds[0])) : f.ctx.tools.getContact(String(args.objectIds[0])))]}),
    mcp__codex_apps__hubspot_search_crm_objects: async args => {
      if (args.objectType === 'CONTACT') {
        associations++;
        assert.equal(args.filterGroups[0].associatedWith[0].objectType, 'TASK');
        return {objects: [{id: '101'}], total: 1, offset: 0};
      }
      return {objects: [], total: 0, offset: 0};
    },
    mcp__codex_apps__gmail_search_email_ids: f.ctx.tools.searchGmail,
    mcp__connectsafely__conversation_exists: f.ctx.tools.conversationExists,
    mcp__connectsafely_loa__conversation_exists: f.ctx.tools.conversationExists,
    mcp__codex_apps__gmail_send_email: f.ctx.tools.gmail_send_email,
    mcp__codex_apps__gmail_read_email: f.ctx.tools.gmail_read_email,
    mcp__codex_apps__hubspot_manage_crm_objects: async args => {
      assert.equal(args.confirmationStatus, 'CONFIRMED');
      const item = args.updateRequest.objects[0];
      return f.ctx.tools.updateTask(String(item.objectId), item.properties);
    }
  };
  try {
    const ctx = await createMexicoContext({tools: mcp, operationRoot: root,
      dependencyPath: process.env.THUNDER_TEST_TOQUES_PATH, workbookPath: workbook,
      sendLock: holder, lockAcknowledgement: acknowledgement, python, workbookPython,
      toquesEnv: {PATH: process.env.PATH, TOQUES_SQLITE: path.join(process.env.THUNDER_TEST_SQLITE_DIR, `${serial}.db`)}, now: f.ctx.now});
    assert.equal(await ctx.lockHeld(), true);
    assert.throws(() => ctx.tools.conversationExists({...ctx.queue[0], account: 'unknown'}), /LINKEDIN_ACCOUNT_UNAPPROVED/);
    assert.equal((await ctx.tools.conversationExists({...ctx.queue[0], account: '6ad640ba6e465bee3a2d9350'})).success, true);
    assert.equal((await runMexicoE1(ctx, [0]))[0].status, 'CONFIRMED_COMPLETED');
    assert.equal(f.sends.length, 1);
    assert.ok(associations >= 3);
    const ledger = await ctx.readLedger();
    assert.ok(ledger.some(row => row.status === 'PROVIDER_CONFIRMED'));
    const checkpoint = JSON.parse(await fs.readFile(path.join(work, 'cdmx-email-results.json'), 'utf8'));
    assert.equal(checkpoint[0].status, 'CONFIRMED_COMPLETED');
    assert.equal(await fs.readFile(path.join(work, 'cdmx-d0-prepared.json'), 'utf8'), packageText);
    assert.equal((await runMexicoE1(ctx, [0]))[0].status, 'CONFIRMED_COMPLETED');
    assert.equal(f.sends.length, 1);
  } finally {
    holder.stdin.end();
    if (holder.exitCode === null) await once(holder, 'close');
  }
});
