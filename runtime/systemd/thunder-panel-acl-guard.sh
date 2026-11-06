#!/bin/bash
# Re-applies the narrow ACLs the non-root panel needs when another process
# (Hermes) resets a directory mode, which zeroes the ACL mask.
set -u
P=/root/.hermes/profiles/ygor-personal
fixed=0
for d in /root/.hermes "$P"; do
  line=$(getfacl -p "$d" 2>/dev/null | grep "^user:thunder-panel:" || true)
  case "$line" in ""|*"#effective:---"*) setfacl -m u:thunder-panel:--x "$d" && fixed=1 ;; esac
done
if getfacl -p "$P/brain" 2>/dev/null | grep -q "^user:thunder-panel:.*#effective:---"; then
  setfacl -m u:thunder-panel:rwx,m::rwx "$P/brain" && fixed=1
fi
[ "$fixed" = 1 ] && logger -t thunder-panel-acl-guard "ACL do painel reparada"
exit 0
