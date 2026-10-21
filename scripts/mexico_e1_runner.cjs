'use strict';

// Recovered external Codex runner: see README provenance. No OS route or autorun.
const crypto = require('node:crypto');
const path = require('node:path');
const {spawn} = require('node:child_process');
const MARKER = 'mexico_gmail_receipt_v1';
const KEYS = ['campaign_id', 'batch', 'hubspot_task_id', 'contact_id', 'touch',
  'sender', 'recipient', 'payload_sha256'];
const esc = s => s.replaceAll('&', '&amp;').replaceAll('<', '&lt;').replaceAll('>', '&gt;');
const digest = s => crypto.createHash('sha256').update(s).digest('hex');
const copyHash = c => digest(c.subject + '\n' + c.text);
const key = b => digest(JSON.stringify(Object.fromEntries(Object.keys(b).sort().map(k => [k, b[k]]))));
const htmlFor = c => c.text.split('\n\n').map(p => '<p>' + esc(p).replaceAll('\n', '<br>') + '</p>').join('');
const fail = code => { throw Error(code); };
const taskID = value => {
  if ((typeof value !== 'string' && typeof value !== 'number')
      || (typeof value === 'number' && !Number.isSafeInteger(value))
      || !/^[1-9][0-9]{0,29}$/.test(String(value))) fail('LEDGER_TASK_ID_INVALID');
  return String(value);
};
function ledgerMap(rows) {
  if (!Array.isArray(rows)) fail('LEDGER_SCHEMA_INVALID');
  const map = new Map();
  map.stops = new Set();
  map.events = [];
  for (const entry of rows) {
    if (entry?.event === 'STOP_ON_REPLY') {
      map.stops.add(taskID(entry.contact_id));
      map.events.push(cloneJSON(entry));
      continue;
    }
    const row = {...entry, task_id: taskID(entry?.task_id)};
    const previous = map.get(row.task_id);
    if (previous?.receipt && !row.receipt) {
      row.receipt = previous.receipt;
      if (row.status !== 'CONFIRMED_COMPLETED') row.status = 'PROVIDER_CONFIRMED_CRM_PENDING';
    }
    map.set(row.task_id, row);
  }
  return map;
}
const cloneJSON = value => JSON.parse(JSON.stringify(value));
const parse = r => {
  if (r?.isError) fail('PROVIDER_TOOL_ERROR');
  if (r?.structuredContent) return r.structuredContent;
  if (r?.content) {
    try { return JSON.parse(r.content.find(x => x.type === 'text').text); }
    catch { fail('PROVIDER_SCHEMA_INVALID'); }
  }
  return r;
};

function makeToquesBridge({dependencyPath, python = 'python3', env = process.env}) {
  if (!path.isAbsolute(dependencyPath || '')) fail('TOQUES_PATH_REQUIRED');
  return request => new Promise((resolve, reject) => {
    const child = spawn(python, [path.join(__dirname, 'mexico_toques_bridge.py'), dependencyPath],
      {env: {...env, PYTHONDONTWRITEBYTECODE: '1'}, stdio: ['pipe', 'pipe', 'pipe']});
    let output = '', failed = false;
    const timer = setTimeout(() => { failed = true; child.kill(); }, 15000);
    child.stdout.on('data', chunk => { output += chunk; if (output.length > 65536) { failed = true; child.kill(); } });
    child.stderr.resume();
    child.on('error', () => { clearTimeout(timer); reject(Error('TOQUES_PROCESS_FAILED')); });
    child.on('close', code => {
      clearTimeout(timer);
      try {
        const response = JSON.parse(output);
        if (failed || code !== 0 || response.ok !== true) fail(response.error || 'TOQUES_PROCESS_FAILED');
        resolve(response.result);
      } catch (e) { reject(Error(/^[A-Z_]+$/.test(e.message) ? e.message : 'TOQUES_PROCESS_FAILED')); }
    });
    child.stdin.on('error', () => {});
    child.stdin.end(JSON.stringify(request));
  });
}

function binding(c, scope) {
  const b = {campaign_id: scope.campaign_id, batch: scope.batch,
    hubspot_task_id: c.task_id, contact_id: c.id, touch: 'E1', sender: scope.sender,
    recipient: c.email, payload_sha256: c.hash};
  if (KEYS.some(k => typeof b[k] !== 'string' || !b[k]) || copyHash(c) !== c.hash
      || !/^[a-f0-9]{64}$/.test(c.hash) || !/^[1-9][0-9]{0,29}$/.test(c.id)
      || !/^[1-9][0-9]{0,29}$/.test(c.task_id)
      || !/^[^\s@;<>,]+@[^\s@;<>,]+\.[^\s@;<>,]+$/.test(c.email)
      || /[\r\n]/.test(c.subject)) fail('PAYLOAD_BINDING_INVALID');
  return b;
}

function checkReceipt(r, b) {
  if (!r || KEYS.some(k => r[k] !== b[k]) || r.key !== key(b) || r.marker !== MARKER
      || r.provider !== 'gmail' || r.state !== 'PROVIDER_CONFIRMED' || r.verification !== 'full_readback'
      || !/^[a-f0-9]{6,64}$/.test(r.message_id) || !Number.isFinite(Date.parse(r.confirmed_at))
      || !/(Z|[+-][0-9]{2}:[0-9]{2})$/.test(r.confirmed_at)) fail('RECEIPT_BINDING_MISMATCH');
  return r;
}

function taskMeta(task, c, b, scope, {reconcile = false} = {}) {
  if (!task || String(task.id) !== b.hubspot_task_id || task.portal_id !== scope.portal_id
      || task.properties?.hubspot_owner_id !== scope.owner_id
      || task.association_complete !== true || JSON.stringify(task.contact_ids) !== JSON.stringify([b.contact_id]))
    fail('TASK_IDENTITY_OR_LINK_MISMATCH');
  const p = task.properties;
  if (!(reconcile ? ['WAITING', 'COMPLETED'] : ['WAITING']).includes(p.hs_task_status)) fail('TASK_NOT_WAITING');
  const matches = [...p.hs_task_body.matchAll(/<pre>([\s\S]*?)<\/pre>/g)];
  const values = matches.map(m => {
    try { return JSON.parse(m[1].replaceAll('&quot;', '"').replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&')); }
    catch { return null; }
  }).filter(m => m && m.marker !== MARKER);
  if (values.length !== 1) fail('TASK_METADATA_AMBIGUOUS');
  const m = values[0];
  if (m.contact_id !== b.contact_id || m.account !== b.sender || m.batch !== b.batch
      || m.campaign !== scope.metadata_campaign || m.task_key !== `${b.batch}:${b.contact_id}:${b.touch}`
      || m.provider !== 'Gmail' || m.recipient !== b.recipient
      || m.sha256_subject_newline_text !== b.payload_sha256
      || m.outreach_authorized !== true || m.execution_blocked !== false
      || (m.hubspot_task_id !== undefined && m.hubspot_task_id !== b.hubspot_task_id)
      || (m.campaign_id !== undefined && m.campaign_id !== b.campaign_id)) fail('TASK_PAYLOAD_GATE_MISMATCH');
  if (!p.hs_task_body.includes(c.text) && !p.hs_task_body.includes(esc(c.text))) fail('TASK_TEXT_MISMATCH');
  return values[0];
}

async function identity(ctx) {
  const value = parse(await ctx.tools.identity());
  if (value.portal_id !== ctx.scope.portal_id || value.owner_id !== ctx.scope.owner_id
      || value.mailbox !== ctx.scope.sender) fail('ACCOUNT_IDENTITY_MISMATCH');
}

async function reconcileCRM(ctx, c, b, receipt) {
  checkReceipt(receipt, b);
  await identity(ctx);
  let task = parse(await ctx.tools.getTask(c.task_id));
  taskMeta(task, c, b, ctx.scope, {reconcile: true});
  const block = '<pre>' + esc(JSON.stringify(receipt)) + '</pre>';
  const markers = [...task.properties.hs_task_body.matchAll(/<pre>([\s\S]*?)<\/pre>/g)]
    .map(m => { try { return JSON.parse(m[1].replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&').replaceAll('&quot;', '"')); } catch { return null; } })
    .filter(m => m?.marker === MARKER);
  if (markers.length > 1 || (markers.length === 1 && JSON.stringify(markers[0]) !== JSON.stringify(receipt)))
    fail('CRM_RECEIPT_CONFLICT');
  const body = task.properties.hs_task_body + (markers.length ? '' : block);
  if (!markers.length || task.properties.hs_task_status !== 'COMPLETED') {
    if (await ctx.lockHeld() !== true) fail('LOCK_LOST');
    parse(await ctx.tools.updateTask(c.task_id, {hs_task_body: body, hs_task_status: 'COMPLETED'}));
  }
  task = parse(await ctx.tools.getTask(c.task_id));
  taskMeta(task, c, b, ctx.scope, {reconcile: true});
  if (task.properties.hs_task_status !== 'COMPLETED' || task.properties.hs_task_body !== body) fail('CRM_READBACK_FAILED');
}

async function preflight(ctx, c, b) {
  if (ctx.scope.identity_holds.includes(c.id)) fail('EXPLICIT_IDENTITY_HOLD');
  if (c.email_status !== 'Verified') fail('EMAIL_EXTRAPOLATED_NOT_VALIDATED');
  await identity(ctx);
  const prepared = await ctx.readPrepared(c);
  if (await ctx.lockHeld() !== true || prepared?.step !== 'E1' || prepared.contact_id !== c.id
      || prepared.subject !== c.subject || prepared.text !== c.text || prepared.sha256 !== c.hash
      || copyHash(prepared) !== c.hash || prepared.execution_blocked !== false) fail('LOCK_HASH_OR_GATE_FAILED');
  const workbook = await ctx.readWorkbook(c);
  if (!Array.isArray(workbook) || workbook.length !== 1 || workbook[0].destination) fail('WORKBOOK_RESERVED_OR_AMBIGUOUS');
  const norm = s => String(s || '').toLowerCase().replace(/^https?:\/\//, '').replace(/\/$/, '');
  if (!/^https:\/\/(www\.)?linkedin\.com\/in\/[^/?#]+\/?$/.test(c.linkedin_url)
      || norm(workbook[0].url) !== norm(c.linkedin_url)) fail('WORKBOOK_PROFILE_MISMATCH');
  const reads = await Promise.allSettled([
    ctx.tools.getTask(c.task_id), ctx.tools.getContact(c.id),
    ctx.tools.searchGmail({query: `in:anywhere {from:${c.email} to:${c.email}}`, max_results: 100}),
    ctx.tools.conversationExists(c), ctx.tools.searchActivities('EMAIL', c), ctx.tools.searchActivities('NOTE', c)
  ]);
  const ds = reads.map(r => { if (r.status !== 'fulfilled') fail('PREFLIGHT_READ_FAILED'); return parse(r.value); });
  taskMeta(ds[0], c, b, ctx.scope);
  const ct = ds[1];
  if (!ct || String(ct.id) !== c.id || ct.properties?.email !== c.email) fail('CONTACT_READ_MISMATCH');
  const properties = ct.properties;
  if (['hs_email_optout', 'hs_email_bounce', 'hs_sales_email_last_replied'].some(k => !Object.hasOwn(properties, k))
      || ![null, '', 'false', 'true'].includes(properties.hs_email_optout)
      || !(properties.hs_email_bounce === null || properties.hs_email_bounce === ''
        || (typeof properties.hs_email_bounce === 'string' && /^[0-9]+$/.test(properties.hs_email_bounce)))
      || !(properties.hs_sales_email_last_replied === null || properties.hs_sales_email_last_replied === ''
        || (typeof properties.hs_sales_email_last_replied === 'string'
          && (Number.isFinite(Date.parse(properties.hs_sales_email_last_replied)) || /^[0-9]{13}$/.test(properties.hs_sales_email_last_replied)))))
    fail('CONTACT_SUPPRESSION_FIELDS_INCOMPLETE');
  if (ct.properties.hs_email_optout === 'true' || Number(ct.properties.hs_email_bounce) > 0) fail('SUPPRESSION_OR_BOUNCE');
  if (ct.properties.hs_sales_email_last_replied) fail('HUBSPOT_REPLY_RECORDED');
  if (!Array.isArray(ds[2].message_ids) || ds[2].next_page_token) fail('GMAIL_HISTORY_INCOMPLETE');
  if (ds[2].message_ids.length) fail('GMAIL_HISTORY_REQUIRES_REVIEW');
  if (ds[3].success !== true || typeof ds[3].hasConversation !== 'boolean') fail('LINKEDIN_READ_FAILED');
  if (ds[3].hasConversation) fail('LINKEDIN_HISTORY_REQUIRES_REVIEW');
  if (ds[4].total !== 0 || ds[5].total !== 0 || ds[4].paging?.next || ds[5].paging?.next) fail('CRM_ACTIVITY_REQUIRES_REVIEW');
}

async function waitWindow(ctx) {
  const previous = ctx.state.lastSend;
  if (previous) {
    const next = previous + 65000 + Math.floor(ctx.random() * 15000);
    while (ctx.now() < next) { await ctx.sleep(Math.min(50000, next - ctx.now())); await ctx.yieldControl(); }
  }
  if (ctx.now() < Date.parse(ctx.scope.window_start) || ctx.now() >= Date.parse(ctx.scope.window_end)) fail('OUTSIDE_AUTHORIZED_DATE');
  if (await ctx.lockHeld() !== true) fail('LOCK_LOST');
}

async function runMexicoE1(ctx, indexes) {
  if (ctx.scope?.campaign_id !== 'MX27-CDMX' || ctx.scope.batch !== 'MX27-CDMX-D0-20261021'
      || ctx.scope.metadata_campaign !== 'Mexico2027-networking' || ctx.scope.sender !== 'ygor.bueno@thundermustard.com'
      || ctx.scope.portal_id !== '51412240' || ctx.scope.owner_id !== '92901049' || !Array.isArray(ctx.scope.identity_holds)
      || !Number.isFinite(Date.parse(ctx.scope.window_start)) || !Number.isFinite(Date.parse(ctx.scope.window_end))
      || Date.parse(ctx.scope.window_start) >= Date.parse(ctx.scope.window_end)) fail('RUNNER_SCOPE_INVALID');
  const outcomes = ledgerMap(await ctx.readLedger());
  const results = [];
  for (const idx of indexes) {
    const raw = ctx.queue[idx];
    const c = raw && {...raw, task_id: taskID(raw.task_id)};
    let b, touch, effect = false, confirmed = false, confirmationAttempted = false;
    const result = {contact_id: c?.id, task_id: c?.task_id, at: new Date(ctx.now()).toISOString()};
    try {
      b = binding(c, ctx.scope);
      if (outcomes.stops.has(c.id)) {
        result.status = 'SKIPPED_REPLY';
        result.reason = 'STOP_ON_REPLY';
        await ctx.save(result);
        const checkpoint = ledgerMap(await ctx.readLedger());
        await ctx.checkpoint([...checkpoint.values(), ...checkpoint.events]);
        results.push(result);
        continue;
      }
      const request = {binding: b, subject: c.subject};
      touch = await ctx.toques({...request, op: 'lookup'});
      const previous = outcomes.get(c.task_id);
      if (touch?.status === 'feito') {
        confirmed = true;
        result.receipt = checkReceipt(touch.receipt, b);
        await reconcileCRM(ctx, c, b, touch.receipt);
        result.status = 'CONFIRMED_COMPLETED';
      } else if (touch && previous?.receipt) {
        const receipt = checkReceipt(previous.receipt, b);
        confirmed = true;
        result.receipt = receipt;
        confirmationAttempted = true;
        const persisted = await ctx.toques({...request, op: 'confirm', touch_id: touch.touch_id, receipt});
        if (persisted.status !== 'feito') fail('TOUCH_CONFIRMATION_NOT_PERSISTED');
        await reconcileCRM(ctx, c, b, checkReceipt(persisted.receipt, b));
        result.status = 'CONFIRMED_COMPLETED';
      } else if (touch || previous) {
        // A durable intent, a lost persistence response or uncertainty never retries.
        result.status = 'UNCERTAIN_NO_RETRY';
      } else {
        await waitWindow(ctx);
        await preflight(ctx, c, b);
        await waitWindow(ctx);
        touch = await ctx.toques({...request, op: 'reserve'});
        if (!touch.created) {
          result.status = 'UNCERTAIN_NO_RETRY';
        } else {
          const intent = {...result, binding: b, status: 'SEND_INTENT', sha256: c.hash};
          await ctx.save(intent);
          outcomes.set(c.task_id, intent);
          // Verify the held lock and window immediately before the provider call.
          await waitWindow(ctx);
          effect = true;
          ctx.state.lastSend = ctx.now();
          const html = htmlFor(c);
          const sent = parse(await ctx.tools.gmail_send_email({from_address: b.sender, to: b.recipient,
            subject: c.subject, payload: {mime_type: 'text/html', charset: 'UTF-8', body: {content: html}},
            response_fields: ['id', 'thread_id', 'label_ids']}));
          ctx.state.lastSend = ctx.now();
          if (!/^[a-f0-9]{6,64}$/.test(sent?.id)) fail('SEND_ID_MISSING');
          const read = parse(await ctx.tools.gmail_read_email({message_id: sent.id, format: 'full'}));
          const headers = read?.payload?.headers;
          const header = n => {
            const rows = Array.isArray(headers) ? headers.filter(h => h.name.toLowerCase() === n) : [];
            return rows.length === 1 ? rows[0].value : '';
          };
          const address = s => { const m = s.match(/^(?:[^<>\r\n]+\s)?<([^<>]+)>$/); return m ? m[1] : s; };
          if (read.id !== sent.id || !read.label_ids?.includes('SENT') || address(header('to')) !== b.recipient
              || address(header('from')) !== b.sender || header('subject') !== c.subject
              || read.payload?.body?.content !== html) fail('GMAIL_FULL_READBACK_MISMATCH');
          const receipt = {...b, marker: MARKER, key: key(b), provider: 'gmail', state: 'PROVIDER_CONFIRMED',
            verification: 'full_readback', message_id: read.id, confirmed_at: new Date(ctx.now()).toISOString()};
          confirmed = true;
          result.receipt = receipt;
          await ctx.save({...result, binding: b, status: 'PROVIDER_CONFIRMED', receipt});
          confirmationAttempted = true;
          const persisted = await ctx.toques({...request, op: 'confirm', touch_id: touch.touch_id, receipt});
          checkReceipt(persisted.receipt, b);
          if (persisted.status !== 'feito') fail('TOUCH_CONFIRMATION_NOT_PERSISTED');
          confirmed = true;
          await reconcileCRM(ctx, c, b, persisted.receipt);
          result.status = 'CONFIRMED_COMPLETED';
        }
      }
    } catch (e) {
      result.status = confirmed ? 'PROVIDER_CONFIRMED_CRM_PENDING' : (effect || touch ? 'UNCERTAIN_NO_RETRY' : 'SKIPPED_BLOCKED');
      result.reason = /^[A-Z_]+$/.test(e.message) ? e.message : 'RUNNER_DEPENDENCY_FAILED';
      if (confirmed && touch?.created && !confirmationAttempted && result.receipt) {
        try {
          await ctx.toques({op: 'confirm', binding: b, subject: c.subject, touch_id: touch.touch_id, receipt: result.receipt});
        } catch { result.persistence = 'RECONCILIATION_REQUIRED_NO_RETRY'; }
      }
      if (touch?.created && !confirmed) {
        try {
          const persisted = await ctx.toques({op: 'uncertain', binding: b, subject: c.subject, touch_id: touch.touch_id});
          if (persisted?.status === 'feito') result.status = 'PROVIDER_CONFIRMED_CRM_PENDING';
        } catch { result.persistence = 'RECONCILIATION_REQUIRED_NO_RETRY'; }
      }
    }
    // Same contact can have independent task steps; checkpoint is derived from
    // the durable ledger, not an earlier in-memory results array.
    await ctx.save(result);
    const terminal = ledgerMap(await ctx.readLedger());
    await ctx.checkpoint([...terminal.values(), ...terminal.events]);
    outcomes.set(c?.task_id, result);
    results.push(result);
    ctx.notify({task_id: result.task_id, status: result.status, reason: result.reason});
    // Uncertainty blocks this item, while independent approved items continue.
  }
  return results;
}

// Consumer for the original Codex operation. It reuses its package, append-only
// ledger and already-held fcntl process; constructing it performs no writes.
async function createMexicoContext({tools, operationRoot, dependencyPath, workbookPath,
  sendLock, lockAcknowledgement, python = 'python3', workbookPython = python,
  toquesEnv = process.env, now = Date.now, random = Math.random, notify = () => {}, yieldControl = async () => {}}) {
  const fs = require('node:fs/promises');
  const {execFile} = require('node:child_process');
  if (!path.isAbsolute(operationRoot || '') || !path.isAbsolute(workbookPath || '')) fail('CONTEXT_PATH_REQUIRED');
  const root = await fs.realpath(operationRoot), work = path.join(root, '.work');
  const preparedPath = path.join(work, 'cdmx-d0-prepared.json');
  const ledgerPath = path.join(work, 'cdmx-email-send-ledger.jsonl');
  const checkpointPath = path.join(work, 'cdmx-email-results.json');
  const lockPath = path.join(work, 'lote02-03', '.send.lock');
  const readPackage = async () => JSON.parse(await fs.readFile(preparedPath, 'utf8'));
  const pkg = await readPackage(), auth = pkg.email_execution_authorization;
  if (pkg.batch !== 'MX27-CDMX-D0-20261021' || auth?.sender !== 'ygor.bueno@thundermustard.com'
      || auth.start_at !== '2026-10-21T14:00:00Z' || auth.expires_local_date !== '2026-10-21'
      || auth.timezone !== 'America/Mexico_City' || !auth.approved_by || !Array.isArray(auth.blocked_contact_ids))
    fail('PACKAGE_AUTHORIZATION_MISMATCH');
  const scope = {campaign_id: 'MX27-CDMX', batch: pkg.batch, metadata_campaign: 'Mexico2027-networking',
    sender: auth.sender, portal_id: '51412240', owner_id: '92901049',
    identity_holds: auth.blocked_contact_ids.map(taskID),
    window_start: auth.start_at, window_end: '2026-10-22T06:00:00Z'};
  const queue = pkg.tasks.filter(t => t.step === 'E1').map(t => {
    const contact = pkg.contacts.filter(c => String(c.hubspot_id) === String(t.contact_id));
    if (contact.length !== 1 || !t.email_execution?.task_id
        || String(contact[0].email_execution?.task_id) !== String(t.email_execution.task_id)) fail('PACKAGE_TASK_LINK_MISSING');
    const c = contact[0];
    return {id: String(t.contact_id), task_id: taskID(t.email_execution.task_id), name: c.name,
      company: c.company, email: c.email, email_status: c.email_status, linkedin_url: c.linkedin_url,
      account: c.linkedin_account, subject: t.subject, text: t.text, hash: t.sha256};
  });
  const readLedger = async () => {
    const text = await fs.readFile(ledgerPath, 'utf8');
    return text.split('\n').filter(line => line.trim()).map(JSON.parse);
  };
  const lockHeld = async () => {
    if (lockAcknowledgement?.trim() !== 'LOCK_HELD' || !Number.isInteger(sendLock?.pid)
        || sendLock.exitCode !== null || sendLock.signalCode !== null
        || sendLock.stdin?.writable !== true || !Array.isArray(sendLock.spawnargs)) return false;
    try {
      if (await fs.realpath(sendLock.spawnargs.at(-1)) !== await fs.realpath(lockPath)) return false;
      process.kill(sendLock.pid, 0);
      return true;
    } catch { return false; }
  };
  const numberID = value => { const id = Number(taskID(value)); if (!Number.isSafeInteger(id)) fail('MCP_ID_RANGE_INVALID'); return id; };
  const search = (objectType, objectId, associationType, properties) => tools.mcp__codex_apps__hubspot_search_crm_objects({objectType,
    filterGroups: [{associatedWith: [{objectType: associationType, operator: 'EQUAL',
      objectIdValues: [numberID(objectId)], objectIds: []}], filters: []}], properties,
    limit: 100, offset: 0, query: null, sorts: []});
  const object = raw => { const objects = parse(raw)?.objects; if (!Array.isArray(objects) || objects.length !== 1) fail('MCP_OBJECT_READ_INVALID'); return objects[0]; };
  let currentIdentity;
  const ports = {
    identity: async () => {
      const [hub, gmail] = await Promise.all([
        tools.mcp__codex_apps__hubspot_get_user_details({include: ['USER_INFORMATION', 'TOOL_INFORMATION']}),
        tools.mcp__codex_apps__gmail_get_profile({})]);
      const h = parse(hub), g = parse(gmail);
      currentIdentity = {portal_id: String(h.accountId ?? h.accountInformation?.hubId),
        owner_id: String(h.ownerId ?? h.userInformation?.ownerId), mailbox: g.email_address ?? g.emailAddress};
      if (currentIdentity.portal_id !== scope.portal_id || currentIdentity.owner_id !== scope.owner_id
          || currentIdentity.mailbox !== scope.sender) fail('ACCOUNT_IDENTITY_MISMATCH');
      return currentIdentity;
    },
    getTask: async id => {
      if (!currentIdentity) fail('ACCOUNT_IDENTITY_REQUIRED');
      const [raw, links] = await Promise.all([
        tools.mcp__codex_apps__hubspot_get_crm_objects({objectType: 'TASK', objectIds: [numberID(id)],
          properties: ['hs_task_body', 'hs_task_status', 'hubspot_owner_id']}),
        search('CONTACT', id, 'TASK', ['email'])]);
      const association = parse(links), rows = association.objects ?? association.results;
      if (!Array.isArray(rows) || association.total !== rows.length || association.paging?.next
          || (association.offset !== undefined && association.offset !== 0)) fail('TASK_ASSOCIATION_INCOMPLETE');
      return {...object(raw), portal_id: currentIdentity.portal_id, association_complete: true,
        contact_ids: rows.map(r => taskID(r.id)).sort()};
    },
    getContact: async id => object(await tools.mcp__codex_apps__hubspot_get_crm_objects({objectType: 'CONTACT',
      objectIds: [numberID(id)], properties: ['email', 'hs_email_optout', 'hs_email_bounce', 'hs_sales_email_last_replied']})),
    searchGmail: args => tools.mcp__codex_apps__gmail_search_email_ids(args),
    conversationExists: c => {
      if (!['6abe5fc087213e2040fb9b1b', '6ad640ba6e465bee3a2d9350'].includes(c.account)) fail('LINKEDIN_ACCOUNT_UNAPPROVED');
      const name = c.account === '6ad640ba6e465bee3a2d9350' ? 'mcp__connectsafely_loa__conversation_exists' : 'mcp__connectsafely__conversation_exists';
      return tools[name]({accountId: c.account, profileId: decodeURIComponent(c.linkedin_url.split('/in/')[1].replace(/\/$/, ''))});
    },
    searchActivities: (kind, c) => search(kind, c.id, 'CONTACT', kind === 'EMAIL'
      ? ['hs_email_status', 'hs_email_direction', 'hs_email_subject', 'hs_email_text'] : ['hs_note_body']),
    gmail_send_email: args => tools.mcp__codex_apps__gmail_send_email(args),
    gmail_read_email: args => tools.mcp__codex_apps__gmail_read_email(args),
    updateTask: async (id, properties) => {
      if (!currentIdentity || await lockHeld() !== true) fail('LOCK_LOST');
      const c = queue.find(row => row.task_id === taskID(id));
      if (!c || properties.hs_task_status !== 'COMPLETED') fail('CRM_UPDATE_SCOPE_INVALID');
      const b = binding(c, scope), task = await ports.getTask(id);
      taskMeta(task, c, b, scope, {reconcile: true});
      const receipts = [...properties.hs_task_body.matchAll(/<pre>([\s\S]*?)<\/pre>/g)]
        .map(m => { try { return JSON.parse(m[1].replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&').replaceAll('&quot;', '"')); } catch { return null; } })
        .filter(m => m?.marker === MARKER);
      if (receipts.length !== 1) fail('CRM_RECEIPT_CONFLICT');
      const receipt = checkReceipt(receipts[0], b), original = task.properties.hs_task_body;
      const block = '<pre>' + esc(JSON.stringify(receipt)) + '</pre>';
      if (properties.hs_task_body !== original && properties.hs_task_body !== original + block) fail('CRM_UPDATE_SCOPE_INVALID');
      // This is the task receipt/completion included in the exact approved E1
      // executor. It is not a session-wide waiver of CRM confirmations.
      return tools.mcp__codex_apps__hubspot_manage_crm_objects({confirmationStatus: 'CONFIRMED', createRequest: null,
        updateRequest: {objects: [{objectId: numberID(id), objectType: 'TASK', properties, associations: null}]}});
    }
  };
  const ctx = {queue, scope, state: {}, now, random, notify, yieldControl, tools: ports, readLedger, lockHeld,
    sleep: ms => new Promise(resolve => setTimeout(resolve, ms)),
    toques: makeToquesBridge({dependencyPath, python, env: toquesEnv}),
    readPrepared: async c => {
      const current = await readPackage();
      if (JSON.stringify(current.email_execution_authorization) !== JSON.stringify(auth)) fail('PACKAGE_AUTHORIZATION_CHANGED');
      const matches = current.tasks.filter(t => t.step === 'E1' && String(t.contact_id) === c.id);
      if (matches.length !== 1) fail('PACKAGE_TASK_LINK_MISSING');
      const t = matches[0];
      const metadata = [...t.properties.hs_task_body.matchAll(/<pre>([\s\S]*?)<\/pre>/g)]
        .map(m => JSON.parse(m[1].replaceAll('&quot;', '"').replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&')))
        .filter(m => !m.marker);
      if (metadata.length !== 1 || metadata[0].outreach_authorized !== true) fail('PACKAGE_TASK_GATE_INVALID');
      return {...t, contact_id: String(t.contact_id), execution_blocked: metadata[0].execution_blocked};
    },
    readWorkbook: c => new Promise((resolve, reject) => {
      const code = "import json,openpyxl,sys\nw=openpyxl.load_workbook(sys.argv[1],data_only=True,read_only=True)\nr=[]\ntry:\n for sn,start,ic,uc in [('Todos',7,17,8),('Fora da lista original',5,7,5)]:\n  for row in w[sn].iter_rows(min_row=start,values_only=True):\n   if len(row)>ic and str(row[ic])==sys.argv[2]: r.append({'destination':row[11],'url':row[uc]})\n print(json.dumps(r))\nfinally: w.close()";
      execFile(workbookPython, ['-c', code, workbookPath, c.id], {timeout: 15000, maxBuffer: 100000},
        (error, stdout) => { if (error) return reject(Error('WORKBOOK_READ_FAILED')); try { resolve(JSON.parse(stdout)); } catch { reject(Error('WORKBOOK_READ_FAILED')); } });
    }),
    save: async row => {
      if (await lockHeld() !== true) fail('LOCK_LOST');
      const file = await fs.open(ledgerPath, 'a');
      try { await file.writeFile(JSON.stringify(row) + '\n'); await file.sync(); } finally { await file.close(); }
    },
    checkpoint: async rows => {
      if (await lockHeld() !== true) fail('LOCK_LOST');
      // Only the existing derived checkpoint is replaced, never the package/queue.
      await fs.writeFile(checkpointPath, JSON.stringify(rows, null, 2));
    }
  };
  const previous = await readLedger();
  // Legacy intent timestamps participate in pacing after a resumed session.
  ctx.state.lastSend = Math.max(0, ...previous.filter(r => ['SEND_INTENT', 'PROVIDER_RESULT', 'PROVIDER_CONFIRMED'].includes(r.status))
    .map(r => Date.parse(r.at)).filter(Number.isFinite));
  return ctx;
}

module.exports = {runMexicoE1, createMexicoContext, makeToquesBridge, copyHash, htmlFor, key, MARKER};
