"""Temporary exact OAuth callback; restores nginx and never starts SEQD."""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import uuid
import signal
from seqd_mail_readonly import IMAGE, KEYS

SITE = Path('/etc/nginx/sites-available/seqd.r3xdigital.tech')
BACKUP = Path('/var/lib/thunder-os-integrations/seqd-reconnect/nginx-before.conf')
ROUTE = '''    location = /api/mailboxes/auth/callback {
        access_log off;
        error_log /dev/null crit;
        proxy_pass http://127.0.0.1:8795;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 60;
    }

'''
BOOT = '''let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',c=>s+=c);
process.stdin.on('end',async()=>{try{const p=JSON.parse(s);s='';
for(const [k,v] of Object.entries(p.env))process.env[k]=v;
const M=require('module'),m=new M('/app/reconnect.cjs');m.filename='/app/reconnect.cjs';m.paths=M._nodeModulePaths('/app');
m._compile(p.code,m.filename);p.env={};await m.exports.main();process.exit(0);
}catch{console.log(JSON.stringify({event:'failed',code:'bootstrap_failed'}));process.exit(1)}});'''

def command(args):
    subprocess.run(args, check=True, capture_output=True, timeout=15)

def main():
    info=json.loads(subprocess.check_output(['docker','inspect','seqd-web-1']))[0]
    assert info['Image']==IMAGE and not info['State']['Running'], 'SEQD changed'
    original=SITE.read_text()
    assert original.count('    location / {')==1 and '8795' not in original, 'nginx changed'
    changed=original.replace('    location / {',ROUTE+'    location / {',1)
    BACKUP.parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    if BACKUP.exists():
        assert BACKUP.read_text()==original, 'previous backup differs'
    else:
        BACKUP.write_text(original);BACKUP.chmod(0o600)
    env=dict(x.split('=',1) for x in info['Config']['Env'] if x.split('=',1)[0] in KEYS)
    payload=json.dumps({'env':env,'code':Path(__file__).with_suffix('.cjs').read_text()})
    name='thunder-seqd-reconnect-'+uuid.uuid4().hex[:12]
    process=None
    try:
        SITE.write_text(changed)
        command(['nginx','-t']);command(['systemctl','reload','nginx'])
        process=subprocess.Popen(['docker','run','--rm','-i','--name',name,
            '--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
            '--memory','256m','--pids-limit','64','--network','seqd_default',
            '-p','127.0.0.1:8795:3000','--entrypoint','node',IMAGE,'-e',BOOT],
            stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
        process.stdin.write(payload);process.stdin.close();payload='';env={}
        for line in process.stdout:
            try:
                item=json.loads(line)
                if item.get('event') in ('ready','reconnected','failed'):print(json.dumps(item),flush=True)
            except (ValueError,AttributeError):pass
        process.wait(timeout=20)
    finally:
        signal.alarm(0)
        try:
            subprocess.run(['docker','rm','-f',name],capture_output=True,timeout=15)
        except Exception:
            print(json.dumps({'event':'failed','code':'container_cleanup_unverified'}),flush=True)
        if SITE.read_text()==changed:
            SITE.write_text(original)
            command(['nginx','-t']);command(['systemctl','reload','nginx'])
            print(json.dumps({'event':'cleanup','nginx_restored':True}),flush=True)
        elif SITE.read_text()==original:
            print(json.dumps({'event':'cleanup','nginx_restored':True}),flush=True)
        else:
            print(json.dumps({'event':'cleanup','nginx_restored':False,'reason':'concurrent_change'}),flush=True)

if __name__=='__main__':
    def deadline(signum, frame):
        raise TimeoutError('reconnect_deadline')
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(660)
    try:main()
    except Exception:print(json.dumps({'event':'failed','code':'reconnect_setup_failed'}));sys.exit(1)
