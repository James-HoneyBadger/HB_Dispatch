# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Local per-account schedules with macOS Keychain and launchd."""
import argparse, hashlib, json, os, plistlib, re, subprocess, sys, tempfile
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from . import db
from .dispatch import Archive
from .encryption import is_encrypted_archive
from .sync_lock import LOCK_SUPPORTED, SyncBusy, file_lock

# Retained under its historical name for compatibility; it now covers
# Windows as well as POSIX.
HAS_FLOCK = LOCK_SUPPORTED

def utcnow():
    return datetime.now(timezone.utc)

def read_json(path, default):
    try: return json.loads(path.read_text())
    except FileNotFoundError: return default

def write_json(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.tmp-')
    temp = Path(name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)

def profile(name,host,user,folders,minutes=None,daily=None,enabled=True):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}',name): raise ValueError('Invalid profile name')
    if not re.fullmatch(r'[A-Za-z0-9.-]+',host) or not user or any(c in user for c in '\r\n\x00'): raise ValueError('Invalid server or username')
    if not folders or any(not x or any(c in x for c in '\r\n\x00') for x in folders): raise ValueError('Invalid mailbox')
    if (minutes is None)==(daily is None): raise ValueError('Choose interval OR daily local time')
    if minutes is not None and (type(minutes) is not int or not 5<=minutes<=10080): raise ValueError('Interval must be 5–10080 minutes')
    if daily is not None and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d',daily): raise ValueError('Daily time must be HH:MM')
    return dict(name=name,host=host,user=user,folders=list(dict.fromkeys(folders)),minutes=minutes,daily=daily,enabled=bool(enabled))

def service(p): return 'HB Dispatch IMAP '+p['host'].lower()

def credential(p, root=None, allow_insecure_fallback=False):
    from .schedulers import fallback_credential_file_present, get_credential_store
    # An existing fallback file means the user already opted in when storing it;
    # unattended runs must still be able to read it back.
    allow = allow_insecure_fallback or fallback_credential_file_present(root)
    return get_credential_store(root, allow_insecure_fallback=allow).get(
        service(p), p['user'], root)

def due(p,state,now):
    if not p['enabled']: return False
    last=state.get('last_attempt')
    if p['minutes'] is not None:
        return not last or (now-datetime.fromisoformat(last)).total_seconds()>=p['minutes']*60
    local=now.astimezone()
    return local.strftime('%H:%M')>=p['daily'] and (not last or datetime.fromisoformat(last).astimezone().date()!=local.date())

def config(root): return read_json(root/'schedule.json',{})

def tick(root,force=None,now=None,get_password=None):
    if get_password is None:
        get_password=lambda p: credential(p,root)
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    scheduler_lock = file_lock(root/'schedule.lock') if HAS_FLOCK else nullcontext()
    try:
        scheduler_lock.__enter__()
    except SyncBusy:
        return {'status':'scheduler already running'}
    try:
        profiles=config(root)
        if force and force not in profiles: raise ValueError('Unknown profile')
        states=read_json(root/'schedule-status.json',{})
        if is_encrypted_archive(root) and not db.session_unlocked(root):
            stamp=now or utcnow()
            for name,p in profiles.items():
                if name==force or (force is None and due(profile(**p),states.get(name,{}),stamp)):
                    states[name]={'status':'deferred: encrypted archive locked'}
            write_json(root/'schedule-status.json',states)
            return states
        try:
            ar=Archive(root)
        except SyncBusy:
            # Another archive operation is running (including its migration);
            # defer due profiles without recording an attempt.
            stamp=now or utcnow()
            for name,p in profiles.items():
                if name==force or (force is None and due(profile(**p),states.get(name,{}),stamp)):
                    states[name]={'status':'deferred: archive busy'}
            write_json(root/'schedule-status.json',states)
            return states
        try:
            for name,p in profiles.items():
                p=profile(**p); stamp=now or utcnow()
                if not (name==force or (force is None and due(p,states.get(name,{}),stamp))): continue
                if not ar.source_enabled('email', f"{p['host']}|{p['user']}"):
                    states.setdefault(name,{})['status']='disabled'
                    write_json(root/'schedule-status.json',states)
                    continue
                previous=states.get(name,{}).copy()
                state=states.setdefault(name,{})
                state.update(last_attempt=stamp.isoformat(),status='running',imported=0)
                write_json(root/'schedule-status.json',states)
                try:
                    password=get_password(p)
                    for folder in p['folders']:
                        state['imported']+=ar.sync(p['host'],p['user'],password,folder)
                    state.update(status='ok',last_success=(now or utcnow()).isoformat())
                except SyncBusy:
                    states[name]=previous
                    states[name]['status']='deferred: archive busy'
                except Exception as exc:
                    state['status']='error: '+type(exc).__name__
                finally: write_json(root/'schedule-status.json',states)
        finally: ar.db.close()
        return states
    finally:
        scheduler_lock.__exit__(None, None, None)

def agent(root):
    from .schedulers import get_scheduler
    scheduler=get_scheduler()
    return scheduler.label(root),scheduler.path(root)

def install(root,account=None,interval=None,every=None,at=None):
    from .schedulers import get_scheduler
    return get_scheduler().install(root,account,interval,every,at)

def agent_status(root):
    from .schedulers import get_scheduler
    try: return get_scheduler().status(root)
    except Exception: return 'Not supported on this platform'

def uninstall(root):
    from .schedulers import get_scheduler
    get_scheduler().uninstall(root)

def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--archive',default='~/.hb-dispatch')
    p.add_argument('--allow-insecure-credential-fallback',action='store_true',
                   help='accept the encrypted-file credential store when no OS credential store is available')
    sub=p.add_subparsers(dest='cmd',required=True)
    q=sub.add_parser('add');q.add_argument('name');q.add_argument('host');q.add_argument('user');q.add_argument('--folder',action='append',required=True)
    g=q.add_mutually_exclusive_group(required=True);g.add_argument('--minutes',type=int);g.add_argument('--daily')
    for cmd in ('pause','resume','run'):
        q=sub.add_parser(cmd);q.add_argument('name')
    q=sub.add_parser('install');q.add_argument('--account');q.add_argument('--interval',choices=('minutes','daily'));q.add_argument('--every',type=int);q.add_argument('--at')
    q=sub.add_parser('credential');c=q.add_subparsers(dest='ccmd',required=True)
    for cmd in ('set','delete'):
        cq=c.add_parser(cmd);cq.add_argument('name')
    for cmd in ('tick','status','uninstall','gui'):sub.add_parser(cmd)
    a=p.parse_args(argv);root=Path(a.archive).expanduser().resolve()
    if a.cmd=='gui':
        from .tk.schedule_center import launch
        launch(root);return
    profiles=config(root)
    if a.cmd=='add':
        item=profile(a.name,a.host,a.user,a.folder,a.minutes,a.daily)
        profiles[a.name]=item;write_json(root/'schedule.json',profiles)
        print('Credential item name:',service(item),'Account:',item['user'])
        print('Store the password with: credential set',a.name)
    elif a.cmd in ('pause','resume'):
        profiles[a.name]['enabled']=a.cmd=='resume';write_json(root/'schedule.json',profiles)
    elif a.cmd in ('run','tick'): print(json.dumps(tick(root,a.name if a.cmd=='run' else None),indent=2))
    elif a.cmd=='status': print(json.dumps({'scheduler':agent_status(root),'profiles':profiles,'status':read_json(root/'schedule-status.json',{})},indent=2))
    elif a.cmd=='install':
        every,at=a.every,a.at
        if a.interval is None and a.account in profiles:
            minutes=profiles[a.account]['minutes']
            if minutes is not None: a.interval,every='minutes',minutes
            else: a.interval,at='daily',profiles[a.account]['daily']
        if a.interval=='daily' and not at: at='12:00'
        print(install(root,a.account,a.interval,every,at))
    elif a.cmd=='uninstall': uninstall(root)
    elif a.cmd=='credential':
        from .schedulers import get_credential_store
        if a.name not in profiles: raise SystemExit('Unknown profile: '+a.name)
        item=profiles[a.name]
        store=get_credential_store(root,allow_insecure_fallback=a.allow_insecure_credential_fallback)
        if a.ccmd=='set':
            import getpass
            store.set(service(item),item['user'],getpass.getpass('Mail password: '),root)
            print('Stored credential in',store.name)
        else:
            store.delete(service(item),item['user'],root)
            print('Deleted credential from',store.name)
if __name__=='__main__': main()
