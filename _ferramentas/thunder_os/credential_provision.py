"""Operator-only materialization of the one approved existing HubSpot credential.

Run on the same host, without values on argv/stdout. Never mount the source dotenv
into the web/agent sandbox. No ChatGPT connector, new token or network operation.
"""
import os
from pathlib import Path
import shlex
import stat


def provision_hubspot(source,destination,*,expected_uid=0):
    source,destination=Path(source),Path(destination)
    if not source.is_absolute() or not destination.is_absolute():raise ValueError('absolute_paths_required')
    descriptor=os.open(source,os.O_RDONLY|os.O_NOFOLLOW)
    value=None
    try:
        info=os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=expected_uid or info.st_mode&0o077 or info.st_size>1_000_000:
            raise ValueError('source_credential_permissions')
        with os.fdopen(descriptor,'r') as stream:
            descriptor=None
            for line in stream:
                key,sep,raw=line.partition('=')
                if sep and key.strip().removeprefix('export ')=='HUBSPOT_ACCESS_TOKEN':
                    if value is not None:raise ValueError('duplicate_credential_key')
                    parts=shlex.split(raw,comments=True)
                    if len(parts)!=1 or not parts[0] or len(parts[0])>4096 or any(c.isspace() for c in parts[0]):raise ValueError('invalid_credential')
                    value=parts[0]
    finally:
        if descriptor is not None:os.close(descriptor)
    if value is None:raise ValueError('credential_key_missing')
    parent=destination.parent
    parent.mkdir(mode=0o700,parents=True,exist_ok=True)
    info=parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=expected_uid or info.st_mode&0o077:
        raise ValueError('destination_credential_permissions')
    if destination.is_symlink() or destination.exists():raise ValueError('credential_destination_exists')
    fd=os.open(destination,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(fd,'w') as stream:
        stream.write(value);stream.flush();os.fsync(stream.fileno())
    return {'credential':'hubspot_access','created':True}
