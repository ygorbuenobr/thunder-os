"""One-shot native OAuth reuse; no persistent credential copies or SEQD startup."""
import json
from pathlib import Path
import subprocess
import uuid
import sys

IMAGE = "sha256:b2057d00fdd7ba5ff7d946145c1866ea2599e38100ca807d0f24375b49f391cc"
KEYS = {"POSTGRES_URL", "DATABASE_URL", "GOOGLE_OAUTH_CLIENT_ID",
        "GOOGLE_OAUTH_CLIENT_SECRET", "GOOGLE_OAUTH_REDIRECT_URI", "TOKEN_ENCRYPTION_KEY"}
BOOTSTRAP = """
let input='';process.stdin.setEncoding('utf8');process.stdin.on('data',c=>input+=c);
process.stdin.on('end',async()=>{let m;try{
 const p=JSON.parse(input);input='';
 for(const [k,v] of Object.entries(p.env))process.env[k]=v;
 const Module=require('module');m=new Module('/app/chief-readonly.cjs');
 m.filename='/app/chief-readonly.cjs';m.paths=Module._nodeModulePaths('/app');
 m._compile(p.code,m.filename);p.env={};
 const timer=setTimeout(()=>{console.log(JSON.stringify({state:'unavailable',error_code:'seqd_read_timeout'}));process.exit(1)},40000);
 const result=await m.exports.main();clearTimeout(timer);
 console.log(JSON.stringify(result));process.exit(0);
 }catch(e){const codes=['seqd_native_mismatch','seqd_runtime_mismatch','seqd_helper_missing',
 'seqd_mailbox_unavailable','gmail_guard_unavailable','gmail_identity_mismatch','gmail_route_blocked',
 'gmail_schema_invalid','gmail_message_id_invalid','gmail_message_mismatch'];
 const status=e.response?.status, oauth=e.response?.data?.error;
 console.log(JSON.stringify({state:'unavailable',error_code:codes.includes(e.message)?e.message:'seqd_native_read_failed',
 stage:m?.exports?.stage?.()||'bootstrap',http_status:Number.isInteger(status)?status:null,
 auth_status:['invalid_grant','invalid_client','access_denied'].includes(oauth)?oauth:null}));process.exit(1)}});
"""

def read():
    name = "thunder-chief-mail-read-" + uuid.uuid4().hex[:12]
    try:
        inspected = subprocess.run(
            ["docker", "inspect", "seqd-web-1"], capture_output=True, text=True,
            check=True, timeout=5)
        info = json.loads(inspected.stdout)[0]
        if info["Image"] != IMAGE or info["State"]["Running"]:
            return {"state": "unavailable", "error_code": "seqd_source_state_changed"}
        if "seqd_default" not in info["NetworkSettings"]["Networks"]:
            return {"state": "unavailable", "error_code": "seqd_network_mismatch"}
        env = dict(s.split("=", 1) for s in info["Config"]["Env"] if s.split("=", 1)[0] in KEYS)
        # Secrets only transit stdin in memory; no env file, argv or container Config.Env.
        payload = json.dumps({"env": env, "code": Path(__file__).with_suffix(".cjs").read_text()})
        result = subprocess.run([
            "docker", "run", "--rm", "--interactive", "--name", name,
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--memory", "256m", "--cpus", "0.5", "--pids-limit", "64",
            "--network", "seqd_default", "--entrypoint", "node", IMAGE, "-e", BOOTSTRAP
        ], input=payload, capture_output=True, text=True, timeout=50)
        return json.loads(result.stdout)
    except subprocess.TimeoutExpired:
        return {"state": "unavailable", "error_code": "seqd_read_timeout"}
    except Exception:
        return {"state": "unavailable", "error_code": "seqd_reader_failed"}
    finally:
        # Only this invocation's disposable container, never an existing service.
        try:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=10)
        except Exception:
            print("seqd_reader_cleanup_unverified", file=sys.stderr)

if __name__ == "__main__":
    print(json.dumps(read(), ensure_ascii=False))
