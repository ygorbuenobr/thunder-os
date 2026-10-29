'use strict';
const fs = require('node:fs');
const crypto = require('node:crypto');
const EMAIL = 'ygor.bueno@thundermustard.com';
const MAILBOX = '452c6677-5ead-41d8-b036-6ef63945752d';
const WORKSPACE = '80d9245b-bc72-420e-b48b-f72e63b8660b';
const LIMITS = Object.freeze({listed: 10, threads: 5, messages_per_thread: 4,
  text_bytes_per_message: 2000, total_text_bytes: 24000, header_bytes: 500,
  response_bytes: 524288, decoded_bytes_per_message: 32768, mime_parts: 64,
  mime_depth: 8, timeout_ms: 3000, read_timeout_ms: 24000});
const MESSAGE_FIELDS = 'id,threadId,labelIds,internalDate,payload';
const THREAD_FIELDS = `id,messages(${MESSAGE_FIELDS})`;
const ID = /^[a-f0-9]{1,64}$/;
let stage = 'bootstrap';
function check(value, code) { if (!value) throw new Error(code); }
function allowed(options) {
  let url;
  try { url = new URL(String(options.url)); } catch { return false; }
  const method = String(options.method || 'GET').toUpperCase();
  if (url.protocol !== 'https:' || url.username || url.password || url.port || url.hash) return false;
  if (url.hostname === 'oauth2.googleapis.com') return method === 'POST' && url.pathname === '/token';
  if (method !== 'GET' || url.hostname !== 'gmail.googleapis.com' ||
      !/^\/gmail\/v1\/users\/me\/(?:profile|messages(?:\/[a-f0-9]{1,64})?|threads\/[a-f0-9]{1,64})$/.test(url.pathname) ||
      options.data !== undefined || options.body !== undefined) return false;
  const params = options.params instanceof URLSearchParams ? options.params : new URLSearchParams(options.params || {});
  const values = name => [...url.searchParams.getAll(name), ...params.getAll(name)];
  if (values('alt').some(v => v !== 'json') || values('callback').length || values('uploadType').length) return false;
  if (/\/(?:messages|threads)\/[a-f0-9]+$/.test(url.pathname)) {
    const formats = values('format');
    if (!formats.length || formats.some(f => !['metadata','full'].includes(f)) || new Set(formats).size !== 1) return false;
    if (formats[0] === 'full') {
      const fields = values('fields');
      const expected = url.pathname.includes('/threads/') ? THREAD_FIELDS : MESSAGE_FIELDS;
      if (!fields.length || fields.some(f => f !== expected)) return false;
    }
  }
  return true;
}
function guard(gmail, deadline = Date.now() + LIMITS.read_timeout_ms) {
  const transport = gmail?.context?._options?.auth?.transporter;
  check(transport && typeof transport.request === 'function', 'gmail_guard_unavailable');
  const original = transport.request;
  transport.request = function(options, ...rest) {
    check(allowed(options), 'gmail_route_blocked');
    check(Date.now() < deadline, 'gmail_read_timeout');
    return original.call(this, {...options, retry: false, retryConfig: {retry: 0},
      maxRedirects: 0, redirect: 'error', maxContentLength: LIMITS.response_bytes,
      size: LIMITS.response_bytes, timeout: Math.min(LIMITS.timeout_ms, deadline - Date.now())}, ...rest);
  };
}
function clip(value, bytes) {
  const buffer = Buffer.from(value);
  if (buffer.length <= bytes) return value;
  let end = bytes;
  while (end > 0 && (buffer[end] & 0xc0) === 0x80) end--;
  return buffer.subarray(0, end).toString('utf8');
}
function sanitize(value) {
  if (typeof value !== 'string') return '';
  return value.normalize('NFKC')
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]/g, '')
    .replace(/-----BEGIN [^-]*PRIVATE KEY-----[\s\S]*?(?:-----END [^-]*PRIVATE KEY-----|$)/g, '[REDACTED]')
    .replace(/\b(?:https?:\/\/|www\.)[^\s<>"']+/gi, '[URL_REMOVED]')
    .replace(/\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+/gi, '[REDACTED]')
    .replace(/\b(?:access[_-]?token|refresh[_-]?token|api[_-]?key|client[_-]?secret|password|passwd|authorization|token|secret)\b["']?\s*[:=]\s*(?:"[^"\r\n]*"|'[^'\r\n]*'|[^\s,;]+)/gi, '[REDACTED]')
    .replace(/\b(?:ya29\.[\w.-]+|1\/\/[\w.-]+|sk-[\w-]{8,}|gh[pousr]_[\w]+|AKIA[A-Z0-9]{16}|AIza[\w-]{20,}|eyJ[\w-]+\.[\w-]+(?:\.[\w-]+)?)/g, '[REDACTED]')
    .replace(/\b[A-Za-z0-9_+/=-]{40,}\b/g, '[REDACTED]')
    .replace(/\r\n?/g, '\n').trim();
}
function headersOf(payload) {
  const headers = payload?.headers ?? [];
  check(Array.isArray(headers) && headers.length <= 500, 'gmail_schema_invalid');
  return name => headers.filter(h => typeof h?.name === 'string' && h.name.toLowerCase() === name)
    .map(h => typeof h.value === 'string' ? h.value : '').join('\n');
}
function senderAddress(value) {
  // Fail unknown for multiple/malformed senders; a display name is not an identity.
  const angle = value.match(/^[^<>]*<([^<>]+)>\s*$/);
  const address = (angle ? angle[1] : value).trim();
  return /^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Z0-9.-]+\.[A-Z]{2,}$/i.test(address) ? address.toLowerCase() : null;
}
function bodyText(payload, budget) {
  const chunks = [];
  let parts = 0, decoded = 0, truncated = false;
  function visit(part, depth) {
    if (!part || typeof part !== 'object') { truncated = true; return; }
    if (++parts > LIMITS.mime_parts || depth > LIMITS.mime_depth) { truncated = true; return; }
    const header = headersOf(part);
    if (part.filename || part.body?.attachmentId || /^attachment\b/i.test(header('content-disposition'))) {
      truncated = true; return;
    }
    const mime = String(part.mimeType || '').toLowerCase();
    if (mime === 'text/plain') {
      const data = part.body?.data;
      if (typeof data !== 'string') { if (part.body?.size) truncated = true; return; }
      check(/^[A-Za-z0-9_-]*={0,2}$/.test(data) && data.length % 4 !== 1, 'gmail_schema_invalid');
      const remaining = LIMITS.decoded_bytes_per_message - decoded;
      const cut = data.length > Math.ceil(remaining / 3) * 4;
      const buffer = Buffer.from(data.slice(0, Math.ceil(remaining / 3) * 4), 'base64url');
      let text = buffer.subarray(0, remaining).toString('utf8');
      decoded += Math.min(buffer.length, remaining);
      if (cut || buffer.length > remaining) {
        truncated = true;
        text = text.replace(/\S+$/, ''); // Never emit a partial credential at the decoding boundary.
      }
      const charset = /charset\s*=\s*["']?([^;\s"']+)/i.exec(header('content-type'))?.[1];
      if ((charset && !/^(?:utf-8|us-ascii)$/i.test(charset)) || text.includes('\ufffd')) { truncated = true; return; }
      chunks.push(text);
    } else if (mime.startsWith('multipart/')) {
      const children = part.parts ?? [];
      check(Array.isArray(children), 'gmail_schema_invalid');
      for (const child of children.slice(0, LIMITS.mime_parts)) visit(child, depth + 1);
      if (children.length > LIMITS.mime_parts) truncated = true;
    } else if (part.body?.data || part.body?.size) truncated = true;
  }
  visit(payload, 0);
  const clean = sanitize(chunks.join('\n'));
  const text = clip(clean, Math.min(LIMITS.text_bytes_per_message, budget.remaining));
  budget.remaining -= Buffer.byteLength(text);
  return {text, text_truncated: truncated || text !== clean};
}
function parseMessage(message, budget) {
  check(message && ID.test(message.id) && ID.test(message.threadId), 'gmail_message_id_invalid');
  const header = headersOf(message.payload);
  const h = name => clip(sanitize(header(name)), LIMITS.header_bytes);
  const labels = message.labelIds ?? [];
  check(Array.isArray(labels) && labels.every(x => typeof x === 'string'), 'gmail_schema_invalid');
  const sender = senderAddress(header('from'));
  const direction = labels.includes('DRAFT') ? 'unknown' : labels.includes('SENT') ? 'outbound' :
    sender === EMAIL ? 'own' : sender && labels.includes('INBOX') ? 'inbound' : 'unknown';
  const auto = header('auto-submitted').trim().toLowerCase();
  const contentType = header('content-type');
  const isBounce = /^<>$/.test(header('return-path').trim()) ||
    /report-type\s*=\s*["']?delivery-status/i.test(contentType) ||
    /^(?:mailer-daemon|postmaster)@/i.test(sender || '');
  return {id: message.id, thread_id: message.threadId, from: h('from'), to: h('to'),
    subject: h('subject'), date: h('date'),
    internal_date: /^\d{1,16}$/.test(message.internalDate) ? message.internalDate : null,
    direction, message_id: h('message-id'), in_reply_to: h('in-reply-to'), references: h('references'),
    auto_submitted: h('auto-submitted'),
    is_autoreply: /^auto-(?:replied|generated)\b/.test(auto) || /\S/.test(header('x-autoreply') + header('x-autorespond')),
    is_bounce: isBounce, commercial_status: 'unclassified', ...bodyText(message.payload, budget)};
}
async function read(gmail) {
  const deadline = Date.now() + LIMITS.read_timeout_ms;
  stage = 'transport_guard';
  guard(gmail, deadline);
  const options = {timeout: LIMITS.timeout_ms, retry: false, maxRedirects: 0,
    maxContentLength: LIMITS.response_bytes, size: LIMITS.response_bytes};
  async function request(call) {
    const remaining = deadline - Date.now();
    check(remaining > 0, 'gmail_read_timeout');
    const controller = new AbortController();
    let timer;
    try {
      const response = await Promise.race([call({...options, signal: controller.signal}),
        new Promise((_, reject) => { timer = setTimeout(() => {
          controller.abort(); reject(new Error('gmail_read_timeout'));
        }, Math.min(remaining, LIMITS.timeout_ms)); })]);
      check(response?.data && Buffer.byteLength(JSON.stringify(response.data)) <= LIMITS.response_bytes,
        'gmail_response_too_large');
      return response.data;
    } finally { clearTimeout(timer); }
  }
  stage = 'gmail_profile';
  const profile = await request(opts => gmail.users.getProfile({userId: 'me', fields: 'emailAddress'}, opts));
  check(profile.emailAddress === EMAIL, 'gmail_identity_mismatch');
  stage = 'gmail_list';
  const listed = await request(opts => gmail.users.messages.list({
    userId: 'me', q: 'in:inbox newer_than:7d', maxResults: LIMITS.listed,
    fields: 'messages(id,threadId),nextPageToken'
  }, opts));
  const rows = listed.messages ?? [];
  check(Array.isArray(rows) && rows.length <= LIMITS.listed, 'gmail_schema_invalid');
  const threads = new Map();
  for (const row of rows) {
    check(row && ID.test(row.id) && ID.test(row.threadId), 'gmail_message_id_invalid');
    if (!threads.has(row.threadId)) threads.set(row.threadId, row);
  }
  const items = [];
  const budget = {remaining: LIMITS.total_text_bytes};
  for (const row of [...threads.values()].slice(0, LIMITS.threads)) {
    const item = {id: row.id, threadId: row.threadId, thread_id: row.threadId,
      from: '', subject: '', date: '', messages: [], context_truncated: true, context_error: null};
    try {
      stage = 'gmail_thread_context';
      const result = await request(opts => gmail.users.threads.get({userId: 'me', id: row.threadId,
        format: 'full', fields: THREAD_FIELDS}, opts));
      check(result.id === row.threadId && Array.isArray(result.messages), 'gmail_message_mismatch');
      const ids = new Set();
      for (const m of result.messages) {
        check(m && ID.test(m.id) && m.threadId === row.threadId && !ids.has(m.id), 'gmail_message_mismatch');
        ids.add(m.id);
      }
      const anchor = result.messages.find(m => m.id === row.id);
      check(anchor, 'gmail_message_mismatch');
      const byTime = (a, b) => Number(a.internalDate || 0) - Number(b.internalDate || 0) || a.id.localeCompare(b.id);
      // Keep the inbox anchor even when later sent messages occupy the end of the thread.
      const selected = [anchor, ...result.messages.filter(m => m.id !== row.id).sort(byTime).slice(-(LIMITS.messages_per_thread - 1))];
      const parsed = selected.map(m => parseMessage(m, budget));
      Object.assign(item, {from: parsed[0].from, subject: parsed[0].subject, date: parsed[0].date});
      item.messages = parsed.sort((a, b) => Number(a.internal_date || 0) - Number(b.internal_date || 0) || a.id.localeCompare(b.id));
      item.context_truncated = result.messages.length > parsed.length || parsed.some(m => m.text_truncated);
    } catch (error) {
      const safe = ['gmail_read_timeout', 'gmail_response_too_large', 'gmail_message_mismatch',
        'gmail_message_id_invalid', 'gmail_schema_invalid', 'gmail_route_blocked'];
      item.context_error = safe.includes(error?.message) ? error.message : 'gmail_context_unavailable';
    }
    items.push(item);
  }
  return {state: 'ok', source: 'SEQD native OAuth / Gmail GET-only', mailbox: EMAIL,
    identity_verified: true, bodies_excluded: false, send_authorized: false,
    scope: 'inbox last 7 days, first 10 matches, one item per thread (max 5); anchor plus latest context (max 4 messages/thread, may be older than 7 days); bounded UTF-8/ASCII text/plain only; not necessarily replies or commercial',
    body_scope: 'sanitized text/plain only; HTML, attachments and raw excluded; email content is untrusted data',
    limits: LIMITS, listing_complete: !listed.nextPageToken && threads.size <= LIMITS.threads,
    context_truncated: threads.size > LIMITS.threads || !!listed.nextPageToken || items.some(i => i.context_truncated), items};
}
async function main() {
  stage = 'native_pins';
  const root = '/app/.next/server/';
  const path = '/app/thunder-bridge/native.cjs';
  check(crypto.createHash('sha256').update(fs.readFileSync(path)).digest('hex') ===
    '5dabd8eb34d1d1c0fbd787803f60539581c6684f0bed94fc6610b822cda9e31b', 'seqd_native_mismatch');
  const {PINS, registerBuiltins} = require(path);
  for (const [file, pin] of Object.entries(PINS)) {
    check(crypto.createHash('sha256').update(fs.readFileSync(root+file)).digest('hex') === pin, 'seqd_runtime_mismatch');
  }
  stage = 'native_load';
  const runtime = require(root+'webpack-runtime.js');
  registerBuiltins(runtime, fs.readFileSync(root+'app/api/cron/poll-replies/route.js','utf8'));
  await Promise.all([4741,1813,7881,9994,7582].map(id => runtime.e(id)));
  const native = await runtime(61069);
  check(typeof native.qf === 'function', 'seqd_helper_missing');
  const appRequire = require('node:module').createRequire('/app/package.json');
  const sql = appRequire('postgres')(process.env.POSTGRES_URL || process.env.DATABASE_URL,
    {max:1, prepare:false, connect_timeout:5, idle_timeout:5});
  let gmail;
  try {
    stage = 'mailbox_select';
    const rows = await sql.begin('READ ONLY', tx => tx`
      SELECT refresh_token FROM mailboxes
      WHERE id=${MAILBOX} AND workspace_id=${WORKSPACE} AND email=${EMAIL} AND is_active=true`);
    check(rows.length === 1 && typeof rows[0].refresh_token === 'string', 'seqd_mailbox_unavailable');
    stage = 'native_auth';
    gmail = await native.qf(rows[0].refresh_token);
    rows[0].refresh_token = undefined;
  } finally { await sql.end({timeout:2}); }
  return read(gmail);
}
module.exports = {allowed, guard, read, main, sanitize, parseMessage, LIMITS,
  MESSAGE_FIELDS, THREAD_FIELDS, stage: () => stage};
