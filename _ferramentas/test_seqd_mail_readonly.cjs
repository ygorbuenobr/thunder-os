'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {allowed, read, guard, sanitize, parseMessage, LIMITS, MESSAGE_FIELDS, THREAD_FIELDS} = require('./seqd_mail_readonly.cjs');
const BASE = 'https://gmail.googleapis.com/gmail/v1/users/me/';
const EMAIL = 'ygor.bueno@thundermustard.com';
function message(id = 'abc', threadId = 'def', extra = {}) {
  return {id, threadId, internalDate: '1700000000000', labelIds: ['INBOX'],
    payload: {mimeType: 'text/plain', headers: [
      {name: 'From', value: 'Buyer <buyer@example.test>'}, {name: 'To', value: EMAIL},
      {name: 'Subject', value: 'Synthetic commercial proposal'}, {name: 'Date', value: 'Synthetic date'},
      {name: 'Message-ID', value: '<abc@example.test>'}, {name: 'In-Reply-To', value: '<prior@example.test>'},
      {name: 'References', value: '<prior@example.test>'}],
    body: {data: Buffer.from('Could we discuss the proposal?').toString('base64url')}}, ...extra};
}
function withHeader(m, name, value) {
  m.payload.headers = m.payload.headers.filter(h => h.name.toLowerCase() !== name.toLowerCase());
  m.payload.headers.push({name, value}); return m;
}
function parse(m) { return parseMessage(m, {remaining: LIMITS.total_text_bytes}); }
function fake({email = EMAIL, rows = [{id: 'abc', threadId: 'def'}], messages = [message()],
  nextPageToken = 'page2', threadError, threadResult, hook} = {}) {
  const calls = [];
  const transport = {request: async function(options) {
    calls.push(options);
    if (hook) hook(options);
    const route = new URL(options.url).pathname;
    if (route.endsWith('/profile')) return {data: {emailAddress: email}};
    if (route.endsWith('/messages')) return {data: {messages: rows, nextPageToken}};
    if (threadError) throw threadError;
    return {data: threadResult ? threadResult(options) : {id: options.params.id, messages}};
  }};
  const invoke = route => (params, options) => transport.request({url: BASE + route(params), params, ...options});
  return {calls, gmail: {context: {_options: {auth: {transporter: transport}}}, users: {
    getProfile: invoke(() => 'profile'), messages: {list: invoke(() => 'messages')},
    threads: {get: invoke(p => 'threads/' + p.id)}
  }}};
}
test('transport allows only OAuth POST and selected Gmail GET routes/formats', () => {
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE', 'HEAD']) {
    for (const route of ['messages', 'messages/abc', 'messages/send', 'threads/def'])
      assert.equal(allowed({method, url: BASE + route}), false);
  }
  for (const url of ['bad URL', 'http://gmail.googleapis.com/gmail/v1/users/me/profile',
    'https://evil.test/gmail/v1/users/me/profile', 'https://gmail.googleapis.com/gmail/v1/users/other/profile',
    BASE + 'messages/abc/attachments/def', BASE + 'threads', BASE + 'settings/sendAs', BASE + 'drafts',
    BASE + 'messages/abc/trash', BASE + 'profile#fragment']) assert.equal(allowed({url}), false);
  assert.equal(allowed({method: 'POST', url: 'https://oauth2.googleapis.com/token'}), true);
  assert.equal(allowed({url: 'https://oauth2.googleapis.com/token'}), false);
  assert.equal(allowed({url: BASE + 'profile'}), true);
  for (const [route, fields] of [['messages/abc', MESSAGE_FIELDS], ['threads/def', THREAD_FIELDS]]) {
    assert.equal(allowed({url: BASE + route, params: {format: 'metadata'}}), true);
    assert.equal(allowed({url: BASE + route, params: {format: 'full', fields}}), true);
    assert.equal(allowed({url: BASE + route, params: new URLSearchParams({format: 'full', fields})}), true);
    for (const params of [{}, {format: 'raw'}, {format: 'minimal'}, {format: 'full'},
      {format: 'full', fields: '*'}, {format: 'full', fields: 'raw'},
      {format: 'full', fields, alt: 'media'}, {format: 'full', fields, callback: 'x'}])
      assert.equal(allowed({url: BASE + route, params}), false);
    assert.equal(allowed({url: BASE + route + '?format=raw', params: {format: 'metadata'}}), false);
    assert.equal(allowed({url: BASE + route + '?format=full', params: {format: 'metadata'}}), false);
    assert.equal(allowed({url: BASE + route, params: {format: 'full', fields}, data: {x: 1}}), false);
  }
});
test('guard denies before transport and forces timeout, bytes, no retry or redirect', () => {
  const calls = [];
  const transport = {request(options) { calls.push(options); }};
  guard({context: {_options: {auth: {transporter: transport}}}});
  assert.throws(() => transport.request({method: 'POST', url: BASE + 'messages/send'}), /gmail_route_blocked/);
  assert.equal(calls.length, 0);
  transport.request({url: BASE + 'profile', retry: true, timeout: 99999, maxRedirects: 9});
  assert.equal(calls[0].retry, false);
  assert.equal(calls[0].retryConfig.retry, 0);
  assert.equal(calls[0].timeout, LIMITS.timeout_ms);
  assert.equal(calls[0].maxContentLength, LIMITS.response_bytes);
  assert.equal(calls[0].size, LIMITS.response_bytes);
  assert.equal(calls[0].maxRedirects, 0);
  assert.equal(calls[0].redirect, 'error');
  assert.throws(() => guard({}), /gmail_guard_unavailable/);
});
test('exact mailbox verified before list; preserves projection fields and bounded text', async () => {
  const {gmail, calls} = fake(); const result = await read(gmail);
  assert.equal(result.identity_verified, true);
  assert.equal(result.listing_complete, false);
  assert.equal(result.send_authorized, false);
  assert.equal(result.bodies_excluded, false);
  assert.equal(calls[2].params.format, 'full');
  assert.equal(calls[2].params.fields, THREAD_FIELDS);
  assert.equal(calls[1].params.q, 'in:inbox newer_than:7d');
  const item = result.items[0], m = item.messages[0];
  assert.equal(item.threadId, item.thread_id);
  assert.equal(item.context_error, null);
  for (const key of ['id', 'date', 'from', 'subject', 'threadId', 'thread_id', 'context_truncated'])
    assert.ok(Object.hasOwn(item, key), key);
  for (const key of ['id', 'date', 'from', 'to', 'subject', 'direction', 'text', 'in_reply_to', 'auto_submitted'])
    assert.ok(Object.hasOwn(m, key), key);
  assert.equal(m.text, 'Could we discuss the proposal?');
  assert.equal(m.direction, 'inbound');
  assert.equal(m.commercial_status, 'unclassified');
  assert.equal(m.in_reply_to, '<prior@example.test>');
  assert.equal(m.message_id, '<abc@example.test>');
  assert.equal(m.raw, undefined);
});
test('wrong, case-changed or missing mailbox cannot list', async () => {
  for (const email of ['other@example.com', EMAIL.toUpperCase(), null]) {
    const {gmail, calls} = fake({email});
    await assert.rejects(read(gmail), /identity_mismatch/); assert.equal(calls.length, 1);
  }
});
test('direction uses SENT, DRAFT and exact From, never a matching display name', () => {
  assert.equal(parse(message('abc', 'def', {labelIds: ['SENT']})).direction, 'outbound');
  assert.equal(parse(message('abc', 'def', {labelIds: ['DRAFT', 'INBOX']})).direction, 'unknown');
  assert.equal(parse(withHeader(message(), 'From', EMAIL)).direction, 'own');
  assert.equal(parse(withHeader(message(), 'From', EMAIL + ' <buyer@example.test>')).direction, 'inbound');
  assert.equal(parse(withHeader(message(), 'From', 'A <a@example.test>, B <' + EMAIL + '>')).direction, 'unknown');
  assert.equal(parse(message('abc', 'def', {labelIds: []})).direction, 'unknown');
});
test('autoreply and bounce flags use headers; subject alone proves neither', () => {
  const subjectOnly = parse(withHeader(message(), 'Subject', 'Auto reply: delivery failure commercial opportunity'));
  assert.equal(subjectOnly.is_autoreply, false); assert.equal(subjectOnly.is_bounce, false);
  assert.equal(subjectOnly.commercial_status, 'unclassified');
  assert.equal(parse(withHeader(message(), 'Auto-Submitted', 'auto-replied')).is_autoreply, true);
  assert.equal(parse(withHeader(message(), 'Auto-Submitted', 'no')).is_autoreply, false);
  assert.equal(parse(withHeader(message(), 'Return-Path', '<>')).is_bounce, true);
  assert.equal(parse(withHeader(message(), 'Content-Type', 'multipart/report; report-type=delivery-status')).is_bounce, true);
});
test('MIME extracts nested plain text only and excludes attachments, HTML, raw and embedded messages', () => {
  const m = message(); const plain = structuredClone(m.payload);
  m.raw = 'RAW_SECRET';
  m.payload = {mimeType: 'multipart/mixed', headers: plain.headers, parts: [
    {mimeType: 'multipart/alternative', parts: [plain,
      {mimeType: 'text/html', body: {data: Buffer.from('<script>HTML_SECRET</script>').toString('base64url')}}]},
    {...plain, filename: 'private.txt', body: {data: Buffer.from('ATTACHMENT_SECRET').toString('base64url')}},
    {...plain, body: {attachmentId: 'external', data: Buffer.from('ATTACHMENT_ID_SECRET').toString('base64url')}},
    {...plain, headers: [{name: 'Content-Disposition', value: 'attachment'}],
      body: {data: Buffer.from('DISPOSITION_SECRET').toString('base64url')}},
    {mimeType: 'message/rfc822', parts: [plain]}]};
  const result = parse(m);
  assert.equal(result.text, 'Could we discuss the proposal?');
  assert.equal(result.text_truncated, true);
  assert.equal(JSON.stringify(result).includes('SECRET'), false);
  const html = message(); html.payload.mimeType = 'text/html';
  assert.equal(parse(html).text, '');
  assert.equal(parse(html).text_truncated, true);
});
test('sanitizes before clipping: URLs, common credentials, private keys and invisible controls', () => {
  const secret = 'sensitiveValue123';
  const text = 'hello\u202e\u0000 https://user:pass@example.test/?token=' + secret +
    ' Bearer ' + secret + ' refresh_token="' + secret + '" api_key: ' + secret +
    ' -----BEGIN PRIVATE KEY-----\n' + secret + '\n-----END PRIVATE KEY----- ' +
    'eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiZmFrZSJ9.fakeSignature';
  const clean = sanitize(text);
  assert.equal(clean.includes(secret), false);
  assert.equal(clean.includes('https://'), false);
  assert.equal(clean.includes('eyJ'), false);
  assert.equal(/[\u202e\u0000]/.test(clean), false);
  const m = withHeader(message(), 'Subject', 'token=' + secret);
  m.payload.body.data = Buffer.from('x '.repeat(990) + 'token=' + secret).toString('base64url');
  const result = parse(m);
  assert.equal(JSON.stringify(result).includes(secret), false);
  assert.ok(Buffer.byteLength(result.text) <= LIMITS.text_bytes_per_message);
});
test('UTF-8 byte and cumulative text caps; decoding and MIME traversal are bounded', () => {
  const m = message(); m.payload.body.data = Buffer.from('\u00e9 '.repeat(20000)).toString('base64url');
  const result = parse(m);
  assert.equal(result.text_truncated, true);
  assert.ok(Buffer.byteLength(result.text) <= 2000);
  assert.equal(result.text.includes('\ufffd'), false);
  const budget = {remaining: 3};
  assert.ok(Buffer.byteLength(parseMessage(m, budget).text) <= 3);
  assert.equal(parseMessage(m, {remaining: 0}).text, '');
  const deep = message(); let part = deep.payload;
  for (let i = 0; i < LIMITS.mime_depth + 2; i++) {
    part.mimeType = 'multipart/mixed'; part.parts = [structuredClone(message().payload)]; part = part.parts[0];
  }
  assert.equal(parse(deep).text_truncated, true);
  const many = message(); many.payload.mimeType = 'multipart/mixed';
  many.payload.parts = Array.from({length: 100}, () => message().payload);
  assert.equal(parse(many).text_truncated, true);
});
test('invalid base64 and unsupported charset never leak body', () => {
  const invalid = message(); invalid.payload.body.data = '%%%';
  assert.throws(() => parse(invalid), /gmail_schema_invalid/);
  const other = withHeader(message(), 'Content-Type', 'text/plain; charset=iso-8859-1');
  assert.equal(parse(other).text, ''); assert.equal(parse(other).text_truncated, true);
});
test('deduplicates threads and caps context, retaining inbox anchor with chronological output', async () => {
  const messages = Array.from({length: 8}, (_, i) => message((i + 1).toString(16), 'def', {internalDate: String(1000 + i)}));
  const {gmail, calls} = fake({rows: [{id: '1', threadId: 'def'}, {id: '2', threadId: 'def'}], messages, nextPageToken: null});
  const result = await read(gmail);
  assert.equal(result.items.length, 1); assert.equal(calls.length, 3);
  assert.deepEqual(result.items[0].messages.map(m => m.id), ['1', '6', '7', '8']);
  assert.equal(result.items[0].context_truncated, true);
  assert.equal(result.listing_complete, true);
});
test('five threads, twenty messages and total body budget are enforced', async () => {
  const rows = Array.from({length: 10}, (_, i) => ({id: (i + 1).toString(16), threadId: (i + 100).toString(16)}));
  const {gmail, calls} = fake({rows, nextPageToken: null, threadResult: options => {
    const row = rows.find(r => r.threadId === options.params.id);
    const messages = Array.from({length: 4}, (_, i) => message(i ? (i + 1000).toString(16) : row.id, row.threadId));
    for (const m of messages) m.payload.body.data = Buffer.from('word '.repeat(600)).toString('base64url');
    return {id: row.threadId, messages};
  }});
  const result = await read(gmail);
  assert.equal(result.items.length, 5); assert.equal(calls.length, 7);
  assert.equal(result.listing_complete, false);
  const all = result.items.flatMap(i => i.messages);
  assert.equal(all.length, 20);
  assert.equal(all.reduce((n, m) => n + Buffer.byteLength(m.text), 0), LIMITS.total_text_bytes);
  assert.equal(result.context_truncated, true);
});
test('context failures are explicit, sanitized and not retried; other threads continue', async () => {
  const {gmail, calls} = fake({threadError: new Error('provider-secret-sensitive')});
  const result = await read(gmail);
  assert.equal(result.items[0].context_error, 'gmail_context_unavailable');
  assert.equal(result.items[0].context_truncated, true);
  assert.equal(result.items[0].messages.length, 0);
  assert.equal(JSON.stringify(result).includes('provider-secret'), false);
  assert.equal(calls.length, 3);
  const rows = [{id: 'abc', threadId: 'def'}, {id: '123', threadId: '456'}];
  const next = fake({rows, threadResult: options => {
    if (options.params.id === 'def') throw Error('failure');
    return {id: '456', messages: [message('123', '456')]};
  }});
  const continued = await read(next.gmail);
  assert.equal(continued.items[1].messages.length, 1);
});
test('mismatched thread, missing anchor, duplicate and foreign message identities fail closed', async () => {
  for (const data of [{id: 'bad', messages: [message()]}, {id: 'def', messages: []},
    {id: 'def', messages: [message(), message()]}, {id: 'def', messages: [message('abc', 'fff')]}]) {
    const {gmail} = fake({threadResult: () => data});
    const result = await read(gmail);
    assert.equal(result.items[0].context_error, 'gmail_message_mismatch');
    assert.deepEqual(result.items[0].messages, []);
  }
  const bad = fake({rows: [{id: 'abc/attachments/x', threadId: 'def'}]});
  await assert.rejects(read(bad.gmail), /gmail_message_id_invalid/);
});
test('oversized response refuses parsing and identifies truncated context', async () => {
  const {gmail} = fake({threadResult: () => ({id: 'def', messages: [message()], ignored: 'x'.repeat(LIMITS.response_bytes)})});
  const result = await read(gmail);
  assert.equal(result.items[0].context_error, 'gmail_response_too_large');
  assert.deepEqual(result.items[0].messages, []);
});
test('empty inbox returns bounded empty result, not a commercial reply assertion', async () => {
  const {gmail, calls} = fake({rows: [], nextPageToken: null}); const result = await read(gmail);
  assert.deepEqual(result.items, []); assert.equal(result.listing_complete, true);
  assert.equal(result.context_truncated, false); assert.equal(calls.length, 2);
});
test('total deadline prevents subsequent transport calls', async t => {
  const realNow = Date.now(); let now = realNow;
  t.mock.method(Date, 'now', () => now);
  const {gmail, calls} = fake({hook: options => {
    if (options.url.endsWith('/messages')) now += LIMITS.read_timeout_ms + 1;
  }});
  const result = await read(gmail);
  assert.equal(result.items[0].context_error, 'gmail_read_timeout');
  assert.equal(calls.length, 2);
});
test('hung context is aborted within per-call timeout without retry', async () => {
  const {gmail, calls} = fake(); let signal;
  gmail.users.threads.get = async (_, options) => {signal = options.signal; return new Promise(() => {});};
  const result = await read(gmail);
  assert.equal(signal.aborted, true);
  assert.equal(result.items[0].context_error, 'gmail_read_timeout');
  assert.equal(calls.length, 2);
});
