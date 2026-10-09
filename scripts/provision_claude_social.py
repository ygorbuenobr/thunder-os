#!/usr/bin/env python3
"""Explicit, local-VPS credential provisioning after reviewed schema installation.

No shared environment edits, credential output, provider calls or service changes.
Rollback: ALTER ROLE thunder_claude_social NOLOGIN; retain credential for review.
"""
import json
import base64
import hashlib
import hmac
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys

CONFIG = Path('/root/.config/thunder-social/claude-db.json')
ROLE = 'thunder_claude_social'

ADMIN = r'''
import json, sys, psycopg2
from psycopg2 import sql
request=json.load(sys.stdin)
con=psycopg2.connect(dbname='thunder_gtm',port=54322,user='postgres')
try:
 with con:
  with con.cursor() as c:
   c.execute("SET LOCAL log_statement='none'")
   c.execute("SET LOCAL log_min_error_statement='panic'")
   c.execute("SET LOCAL log_parameter_max_length_on_error=0")
   c.execute('SELECT pg_advisory_xact_lock(871290431)')
   c.execute('SELECT current_database(),inet_server_port()')
   if c.fetchone()!=('thunder_gtm',None): raise ValueError('wrong database transport')
   c.execute('SELECT rolcanlogin,rolsuper,rolcreatedb,rolcreaterole,rolbypassrls FROM pg_roles WHERE rolname=%s',(request['user'],))
   if c.fetchone()!=(False,False,False,False,False): raise ValueError('role not ready')
   c.execute("SELECT has_table_privilege(%s,'public.toques','INSERT,UPDATE,DELETE,TRIGGER'),has_schema_privilege(%s,'public','CREATE')",(request['user'],request['user']))
   if c.fetchone()!=(False,False): raise ValueError('excess privilege')
   for signature in ('claude_reserve(text,text,text,text,text,text)','claude_complete(bigint,text,text,text)','claude_readback(bigint)'):
    c.execute("SELECT p.prosecdef,r.rolname,p.proconfig,has_function_privilege(%s,p.oid,'EXECUTE'),EXISTS(SELECT 1 FROM aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a WHERE a.grantee=0 AND a.privilege_type='EXECUTE') FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner WHERE p.oid=to_regprocedure(%s)",(request['user'],'thunder_social_coordination.'+signature))
    row=c.fetchone()
    if not row or row[0] is not True or row[1]!='thunder_social_owner' or row[3:]!=(True,False): raise ValueError('schema or function grant absent')
    if [v.replace(' ','') for v in (row[2] or [])]!=['search_path=pg_catalog,thunder_social_coordination']: raise ValueError('unsafe function search path')
   c.execute('SELECT 1 FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member WHERE r.rolname=%s',(request['user'],))
   if c.fetchone(): raise ValueError('unexpected inherited role')
   c.execute(sql.SQL('ALTER ROLE {} LOGIN PASSWORD {}').format(sql.Identifier(request['user']),sql.Literal(request['verifier'])))
 print('credential_role_enabled')
except Exception:
 con.rollback(); print('credential_provision_refused',file=sys.stderr); sys.exit(1)
finally: con.close()
'''


def scram_verifier(password):
    salt = secrets.token_bytes(16)
    salted = hashlib.pbkdf2_hmac('sha256', password.encode('ascii'), salt, 4096)
    stored = hashlib.sha256(hmac.new(salted, b'Client Key', 'sha256').digest()).digest()
    server = hmac.new(salted, b'Server Key', 'sha256').digest()
    b64 = lambda value: base64.b64encode(value).decode('ascii')
    return 'SCRAM-SHA-256$4096:' + b64(salt) + '$' + b64(stored) + ':' + b64(server)


def main():
    if os.geteuid() != 0 or socket.gethostname() != 'srv1433640':
        raise SystemExit('Refused: expected root on srv1433640')
    CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if CONFIG.parent.is_symlink() or CONFIG.parent.stat().st_mode & 0o077:
        raise SystemExit('Refused: credential directory must be private')
    config = dict(host='127.0.0.1', port=54322, dbname='thunder_gtm', user=ROLE,
                  password=secrets.token_urlsafe(48))
    # O_EXCL prevents replacing an existing credential or following a symlink.
    fd = os.open(CONFIG, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as output:
        json.dump(config, output); output.flush(); os.fsync(output.fileno())
    result = subprocess.run(['runuser','-u','postgres','--','python3','-c',ADMIN],
                            input=json.dumps(dict(user=ROLE,verifier=scram_verifier(config['password']))),
                            text=True, capture_output=True)
    if result.returncode:
        raise SystemExit('Refused: role not enabled; private config retained for recovery, do not retry blindly')
    print('Dedicated credential installed; no shared environment changed')


if __name__ == '__main__':
    main()
