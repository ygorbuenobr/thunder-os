'use strict';

// Parse strings as data. Never evaluate a historical tool call or its autorun.
const crypto = require('node:crypto');
function literal(input, offset) {
  const quote = input[offset];
  if (!['"', "'", '`'].includes(quote)) throw Error('STRING_EXPECTED');
  let text = '';
  for (let i = offset + 1; i < input.length; i++) {
    const c = input[i];
    if (c === quote) return {value: text, end: i + 1};
    if (c === '$' && input[i + 1] === '{' && quote === '`') throw Error('INTERPOLATION_FORBIDDEN');
    if (c !== '\\') { text += c; continue; }
    const e = input[++i];
    const escapes = {n: '\n', r: '\r', t: '\t', b: '\b', f: '\f', v: '\v', '0': '\0'};
    if (e === 'u' || e === 'x') throw Error('UNEXPECTED_ESCAPE');
    text += escapes[e] ?? e;
  }
  throw Error('UNTERMINATED_STRING');
}
function recover(jsonl) {
  const rows = jsonl.split('\n');
  const get = number => {
    const record = JSON.parse(rows[number - 1]);
    if (record.type !== 'response_item' || record.payload?.type !== 'custom_tool_call'
        || record.payload.name !== 'exec') throw Error('PROVENANCE_RECORD_MISMATCH');
    return {input: record.payload.input, timestamp: record.timestamp, line: number};
  };
  const initial = get(32661), continuation = get(32944), persistence = get(35212), invocation = get(35217);
  if (!initial.input.startsWith("store('email_runner',`")) throw Error('RUNNER_STORE_MISSING');
  let source = literal(initial.input, initial.input.indexOf('`')).value;
  const original = source;
  const stop = 'if(effect&&result.status==="UNCERTAIN_NO_RETRY"){text({reviewRequired:true});break;}';
  if (!source.includes(stop) || !continuation.input.includes('email_runner')) throw Error('CONTINUATION_PATCH_MISSING');
  source = source.replace(stop, '');
  const oldStart = persistence.input.indexOf('const old=') + 'const old='.length;
  const newStart = persistence.input.indexOf('const neu=') + 'const neu='.length;
  const old = literal(persistence.input, oldStart).value;
  const replacement = literal(persistence.input, newStart).value;
  if (!source.includes(old)) throw Error('PERSISTENCE_PATCH_MISSING');
  source = source.replace(old, replacement);
  if (!invocation.input.includes('email_runner_safe_persistence')) throw Error('INVOCATION_MISSING');
  const hash = s => crypto.createHash('sha256').update(s).digest('hex');
  return {source, provenance: {records: [initial, continuation, persistence, invocation].map(({line, timestamp}) => ({line, timestamp})),
    original_chars: original.length, original_sha256: hash(original),
    safe_chars: source.length, safe_sha256: hash(source)}};
}
module.exports = {recover};
if (require.main === module) {
  const fs = require('node:fs');
  const result = recover(fs.readFileSync(process.argv[2], 'utf8'));
  // Source remains private; only hashes and record coordinates go to stdout.
  console.log(JSON.stringify(result.provenance, null, 2));
}
