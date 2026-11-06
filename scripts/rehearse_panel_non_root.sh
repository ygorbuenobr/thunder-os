#!/bin/bash -p
# Offline isolated rehearsal; execute only as root on a Linux host.
# Input is an already-built, read-only candidate release path. No send, prod
# credentials, or production services are used. The copied brain/db are private.
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
unset PYTHONPATH PYTHONHOME ENV BASH_ENV CDPATH

if [[ $(uname -s) != Linux || ${EUID:-$(id -u)} != 0 ]]; then
  printf '%s\n' 'Este ensaio exige Linux com systemd e root.' >&2
  exit 2
fi
if [[ $# != 1 ]]; then
  printf 'Uso: sudo %s /caminho/absoluto/da-release-candidata\n' "$0" >&2
  exit 2
fi
for command in systemd-run systemctl sqlite3 python3 cp chown chmod useradd userdel getent nsenter curl stat realpath pgrep; do
  command -v "$command" >/dev/null || { printf 'Dependência ausente: %s\n' "$command" >&2; exit 2; }
done
release=$(realpath -e -- "$1")
[[ -d $release && -f $release/_ferramentas/painel_servidor.py && -f $release/_ferramentas/painel_modelo.html ]] || {
  printf '%s\n' 'Release candidata inválida.' >&2; exit 2;
}
# Only a root-owned, non-group/world-writable release can be loaded by the unit.
python3 -I - "$release" <<'PY'
import os,stat,sys
p=sys.argv[1]
for component in (p, os.path.join(p, '_ferramentas'), os.path.join(p, '_ferramentas', 'painel_servidor.py')):
 s=os.stat(component, follow_symlinks=False)
 if s.st_uid != 0 or stat.S_IMODE(s.st_mode) & 0o022: raise SystemExit('Release insegura')
PY
brain=/root/.hermes/profiles/ygor-personal/brain
db=/root/.hermes/profiles/ygor-personal/painel-dados/painel.db
work=/var/tmp/thunder-panel-rehearsal
user=thunder-panel-rehearsal
unit="thunder-panel-rehearsal-$$"
writer_unit="${unit}-writer"
backup_unit="${unit}-backup"
created_dir=0
created_user=0
launched_backup=0
launched_http=0
launched_writer=0
uid=
work_inode=
lockdir=/run/lock/thunder-panel-rehearsal.lockdir
# Atomic mkdir never follows an existing symlink. Never reuse another run's lock.
mkdir -m 0700 -- "$lockdir" || { printf '%s\n' 'Lock de ensaio já existe; operação recusada.' >&2; exit 2; }
lock_inode=$(stat -c '%d:%i:%u' "$lockdir")
cleanup() {
  status=$?
  trap - EXIT INT TERM
  set +e
  for service in "${backup_unit}:$launched_backup" "${writer_unit}:$launched_writer" "${unit}:$launched_http"; do
    name=${service%:*}; launched=${service##*:}
    if (( launched )); then
      systemctl stop "${name}.service" >/dev/null 2>&1
      if (( $? != 0 )); then
        state=$(systemctl show --value -p ActiveState "${name}.service" 2>/dev/null)
        if (( $? != 0 )) || [[ $state != inactive && $state != failed ]]; then
          printf '%s\n' 'Unit de ensaio não parou ou estado desconhecido; preservando usuário, dados e lock.' >&2
          exit 1
        fi
      fi
    fi
  done
  if (( created_user )); then
    if [[ -z $uid || $(id -u "$user" 2>/dev/null) != "$uid" ]]; then
      printf '%s\n' 'UID de ensaio desconhecido ou alterado; preservando usuário, dados e lock.' >&2
      exit 1
    fi
    pgrep -u "$uid" >/dev/null 2>&1
    process_status=$?
    if (( process_status != 1 )); then
      printf '%s\n' 'Ainda há processos com UID de ensaio ou a verificação falhou; preservando usuário, dados e lock.' >&2
      exit 1
    fi
    userdel "$user" >/dev/null 2>&1 || {
      printf '%s\n' 'Falha removendo usuário; preservando dados e lock.' >&2; exit 1;
    }
  fi
  if (( created_dir )); then
    if [[ -d $work && ! -L $work && $(stat -c '%d:%i:%u' "$work") == "$work_inode" ]]; then
      rm -rf --one-file-system -- "$work" || status=1
    else
      printf '%s\n' 'Diretório de ensaio mudou; limpeza recusada.' >&2; status=1
    fi
  fi
  if [[ -d $lockdir && ! -L $lockdir && $(stat -c '%d:%i:%u' "$lockdir") == "$lock_inode" ]]; then
    rmdir -- "$lockdir" || status=1
  else
    printf '%s\n' 'Lock de ensaio mudou; limpeza recusada.' >&2; status=1
  fi
  exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
if getent passwd "$user" >/dev/null || getent group "$user" >/dev/null || [[ -e $work || -L $work ]]; then
  printf '%s\n' 'Usuário, grupo ou diretório de ensaio já existe; recusando alteração.' >&2; exit 2
fi
for name in "$unit" "$writer_unit" "$backup_unit"; do
  [[ $(systemctl show --value -p LoadState "${name}.service") == not-found ]] || {
    printf '%s\n' 'Nome de unit de ensaio já existe; operação recusada.' >&2; exit 2;
  }
done
[[ -d $brain && -f $db && ! -L $brain && ! -L $db ]] || { printf '%s\n' 'Origem de leitura indisponível ou simbólica.' >&2; exit 2; }
mkdir -m 0711 -- "$work"
created_dir=1
work_inode=$(stat -c '%d:%i:%u' "$work")
useradd --system --user-group --no-create-home --shell /usr/sbin/nologin "$user"
created_user=1
uid=$(id -u "$user")
gid=$(id -g "$user")
set +e
pgrep -u "$uid" >/dev/null 2>&1
process_status=$?
set -e
if (( process_status != 1 )); then
  printf '%s\n' 'UID já possui processos ou verificação falhou; operação recusada.' >&2; exit 2
fi
mkdir -m 0700 -- "$work/brain" "$work/db" "$work/credentials" "$work/root-db"
# SQLite read-only connection with the WAL included. A read-only bind mount of
# the source directory was rejected: when no other connection holds the
# database open, -wal/-shm do not exist and a WAL reader cannot create -shm on
# a read-only mount ("unable to open database file", observed on the VPS
# 05/11/2026). mode=ro never writes database pages; at most it creates -shm,
# exactly like the panel's own readers.
launched_backup=1
systemd-run --quiet --collect --wait --pipe --unit="$backup_unit" \
  --property=User=root --property=NoNewPrivileges=yes \
  --property=ProtectSystem=strict \
  --property=RestrictSUIDSGID=yes --property=PrivateNetwork=yes \
  '--property=RestrictAddressFamilies=AF_INET AF_INET6' \
  "--property=ReadWritePaths=${db%/*} $work/db" \
  /usr/bin/sqlite3 -init /dev/null -readonly "file:${db}?mode=ro" ".backup '$work/db/painel.db'"
# Make the root WAL probe copy before granting the rehearsal UID any access.
# Its parent remains root:root 0700 for the whole test.
# A normal (not mode=ro) connection on this private copy: a read-only WAL
# reader leaves root-owned -wal/-shm behind, which later made the rehearsal
# UID fail with "attempt to write a readonly database" (observed 05/11/2026).
# The last normal connection checkpoints and removes them on close.
sqlite3 -init /dev/null "$work/db/painel.db" ".backup '$work/root-db/painel.db'"
for suffix in -wal -shm; do
  [[ ! -e $work/db/painel.db$suffix ]] || { printf '%s\n' 'WAL/SHM remanescente na cópia; recusando.' >&2; exit 1; }
done
chown "$uid:$gid" "$work/root-db/painel.db"
chmod 0660 "$work/root-db/painel.db"
cp -a -x -- "$brain/." "$work/brain/"
chown -R -P "$uid:$gid" -- "$work/brain"
chown "$uid:$gid" "$work/db/painel.db"
chmod 0660 "$work/db/painel.db"
chown "0:$gid" "$work/db"
chmod 2770 "$work/db"
printf '%s\n' 'synthetic-rehearsal-only' > "$work/credentials/probe"
chmod 0600 "$work/credentials/probe"
printf '{"id":"rehearsal_%s","tipo":"rascunho","status":"pendente"}\n' "$$" > "$work/input.json"
chown "$uid:$gid" "$work/input.json"
chmod 0600 "$work/input.json"
# PrivateNetwork blocks external IP traffic; AF_UNIX is excluded so no host
# Unix socket can be reached. The root home is hidden and no production env or
# credentials are injected. PrivateTmp is kept enabled; BindPaths publishes only
# the temporary copy to the service namespace.
props=(
  "--property=User=$user" "--property=Group=$user"
  '--property=NoNewPrivileges=yes' '--property=ProtectSystem=strict'
  '--property=ProtectHome=yes' '--property=PrivateTmp=yes' '--property=PrivateNetwork=yes'
  '--property=CapabilityBoundingSet=' '--property=AmbientCapabilities='
  '--property=RestrictSUIDSGID=yes' '--property=RestrictNamespaces=yes' '--property=UMask=0007'
  '--property=RestrictAddressFamilies=AF_INET AF_INET6'
  "--property=BindPaths=$work:/run/thunder-panel-rehearsal"
  # PrivateTmp hides /var/tmp; publish the candidate release read-only so a
  # release staged there remains the WorkingDirectory (observed 05/11/2026).
  "--property=BindReadOnlyPaths=$release"
  '--property=ReadWritePaths=/run/thunder-panel-rehearsal'
  "--property=LoadCredential=rehearsal_probe:$work/credentials/probe"
  "--property=WorkingDirectory=$release"
)
# The loopback port is private to this service's new network namespace.
port=18790
# env -i prevents environment flags and real credential paths from leaking in.
launcher='test -r "$CREDENTIALS_DIRECTORY/rehearsal_probe" || exit 1; exec /usr/bin/env -i PATH=/usr/bin:/bin PYTHONPATH="$1/_ferramentas" THUNDER_BRAIN_ROOT=/run/thunder-panel-rehearsal/brain PAINEL_DB=/run/thunder-panel-rehearsal/db/painel.db PAINEL_PORTA="$2" CREDENTIALS_DIRECTORY="$CREDENTIALS_DIRECTORY" THUNDER_EXTERNAL_EFFECTS_ENABLED=false THUNDER_OS_SEQD_SEND_ENABLED=false THUNDER_OS_HUBSPOT_WRITE_ENABLED=false THUNDER_OS_GMAIL_CADENCE_ENABLED=false THUNDER_OS_GMAIL_CRM_ENABLED=false THUNDER_OS_GMAIL_REPLY_INGEST_ENABLED=false /usr/bin/python3 "$1/_ferramentas/painel_servidor.py" "$3"'
launched_http=1
systemd-run --quiet --collect --unit="$unit" "${props[@]}" /bin/sh -c "$launcher" sh "$release" "$port" servir
pid=$(systemctl show --value -p MainPID "${unit}.service")
[[ $pid =~ ^[1-9][0-9]*$ ]] || { printf '%s\n' 'Painel isolado não iniciou.' >&2; exit 1; }
# Pin the namespace to a descriptor, so PID reuse cannot redirect curl to the
# host network. Refuse the host namespace and a PID no longer owned by the unit.
[[ $(systemctl show --value -p MainPID "${unit}.service") == "$pid" ]] || exit 1
exec {network_fd}<"/proc/$pid/ns/net"
[[ $(stat -Lc '%d:%i' "/proc/$$/fd/$network_fd") != $(stat -Lc '%d:%i' "/proc/$$/ns/net") ]] || {
  printf '%s\n' 'Namespace de rede não isolado.' >&2; exit 1;
}
# Poll boundedly; no provider network and no production paths are contacted.
ready=0
for _ in {1..30}; do
  http_status=$(/usr/bin/env -i PATH=/usr/bin:/bin nsenter --net="/proc/self/fd/$network_fd" curl --disable --silent --fail --noproxy '*' --max-time 1 --output "$work/health.json" --write-out '%{http_code}' "http://127.0.0.1:$port/saude" 2>/dev/null || true)
  if [[ $http_status == 200 ]]; then ready=1; break; fi
  systemctl is-active --quiet "${unit}.service" || break
  sleep 1
done
(( ready )) || { printf '%s\n' 'HTTP de ensaio indisponível.' >&2; exit 1; }
python3 -I - "$work/health.json" <<'PY'
import json,sys
with open(sys.argv[1]) as f: result=json.load(f)
if result.get('status')!='ok' or result.get('external_effects_enabled') is not False: raise SystemExit('Estado do painel inseguro')
PY
# Exercise the panel's own CLI writer under the same identity and restrictions.
# Input is a synthetic record; it can only reach the copied DB and brain.
cat > "$work/readback.py" <<'PY'
import json,sqlite3,sys
with sqlite3.connect('file:'+sys.argv[1]+'?mode=ro',uri=True) as db:
 row=db.execute('SELECT dados FROM itens WHERE id=?',(sys.argv[2],)).fetchone()
if not row or json.loads(row[0]).get('tipo')!='rascunho': raise SystemExit('Escrita do painel sem readback')
PY
chmod 0644 "$work/readback.py"
writer='test -r "$CREDENTIALS_DIRECTORY/rehearsal_probe" || exit 1; /usr/bin/env -i PATH=/usr/bin:/bin PYTHONPATH="$1/_ferramentas" THUNDER_BRAIN_ROOT=/run/thunder-panel-rehearsal/brain PAINEL_DB=/run/thunder-panel-rehearsal/db/painel.db CREDENTIALS_DIRECTORY="$CREDENTIALS_DIRECTORY" THUNDER_EXTERNAL_EFFECTS_ENABLED=false /usr/bin/python3 "$1/_ferramentas/painel_servidor.py" upsert < /run/thunder-panel-rehearsal/input.json >/dev/null && exec /usr/bin/python3 -I /run/thunder-panel-rehearsal/readback.py /run/thunder-panel-rehearsal/db/painel.db "$2"'
launched_writer=1
systemd-run --quiet --collect --wait --pipe --unit="$writer_unit" "${props[@]}" /bin/sh -c "$writer" sh "$release" "rehearsal_$$" >/dev/null
# Close the panel before observing a root-created WAL in the isolated copy.
systemctl stop "${unit}.service"
( umask 0007
python3 -I - "$work/root-db/painel.db" "$uid" "$gid" <<'PY'
import os,sqlite3,stat,sys
path,uid,gid=sys.argv[1],int(sys.argv[2]),int(sys.argv[3])
if os.path.lexists(path+'-wal') or os.path.lexists(path+'-shm'):
 raise SystemExit('WAL/SHM remanescente; não é possível provar criação por root')
with sqlite3.connect(path) as db:
 if db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower()!='wal': raise SystemExit('WAL indisponível')
 db.execute('CREATE TABLE IF NOT EXISTS rehearsal_root_probe (id INTEGER)')
 db.execute('INSERT INTO rehearsal_root_probe VALUES (1)')
 db.commit()
 for suffix in ('-wal','-shm'):
  info=os.stat(path+suffix,follow_symlinks=False)
  if info.st_uid!=uid or info.st_gid!=gid or stat.S_IMODE(info.st_mode)&0o660!=0o660:
   raise SystemExit('Permissões ou dono de '+suffix+' divergentes')
print('SQLite: na cópia, root com umask 0007 criou WAL/SHM com dono/grupo e modo esperados')
PY
)
printf '%s\n' 'Ensaio HTTP e escrita da cópia passaram; rede externa e envios bloqueados.'
