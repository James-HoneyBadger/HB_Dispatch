# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Native Tk account scheduler; never reads or displays passwords."""
def launch(root, parent=None):
    import tkinter as tk
    from tkinter import ttk, messagebox
    import subprocess, sys
    from pathlib import Path
    from hb_engine.schedule import profile, config, read_json, write_json, service, agent_status
    w=tk.Toplevel(parent) if parent else tk.Tk()
    w.title('HB Continuum | Synchronization center')
    w.geometry('1040x620');w.minsize(820,540)
    frame=ttk.Frame(w,padding=18);frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='Synchronization center',font=('TkDefaultFont',20,'bold')).pack(anchor='w')
    ttk.Label(frame,text='Local archive • Read-only IMAP • Credentials in macOS Keychain').pack(anchor='w',pady=(2,14))
    columns=('account','schedule','status','last','imported')
    tree=ttk.Treeview(frame,columns=columns,show='headings',height=7,selectmode='browse')
    for col,title,width in zip(columns,('Account','Schedule','Status','Last successful (local)','New messages'),(245,130,170,180,110)):
        tree.heading(col,text=title);tree.column(col,width=width)
    tree.pack(fill='both',expand=True)
    vars={k:tk.StringVar() for k in ('name','host','user','folders','mode','time')}
    vars['mode'].set('Interval');vars['time'].set('30');vars['folders'].set('INBOX')
    enabled=tk.BooleanVar(value=False)
    editor=ttk.LabelFrame(frame,text='Account profile',padding=12);editor.pack(fill='x',pady=12)
    for i,(k,label) in enumerate((('name','Profile name'),('host','IMAP server'),('user','Username'),('folders','Folders (semicolon-separated)'))):
        row,col=divmod(i,2)
        ttk.Label(editor,text=label).grid(row=row*2,column=col,sticky='w',padx=5)
        ttk.Entry(editor,textvariable=vars[k]).grid(row=row*2+1,column=col,sticky='ew',padx=5,pady=(0,8))
    editor.columnconfigure(0,weight=1);editor.columnconfigure(1,weight=1)
    timing=ttk.Frame(editor);timing.grid(row=4,column=0,columnspan=2,sticky='ew')
    ttk.Label(timing,text='Schedule:').pack(side='left')
    ttk.Combobox(timing,textvariable=vars['mode'],values=('Interval','Daily'),state='readonly',width=12).pack(side='left',padx=5)
    ttk.Entry(timing,textvariable=vars['time'],width=10).pack(side='left')
    ttk.Label(timing,text='minutes or HH:MM local time').pack(side='left',padx=5)
    ttk.Checkbutton(timing,text='Enabled',variable=enabled).pack(side='right')
    note=tk.StringVar();ttk.Label(frame,textvariable=note,wraplength=960).pack(anchor='w')
    def refresh():
        from datetime import datetime
        try:
            profiles=config(root);states=read_json(root/'schedule-status.json',{})
            selected=tree.selection()
            for i in tree.get_children(): tree.delete(i)
            for name,p in profiles.items():
                s=states.get(name,{})
                last=s.get('last_success')
                last=datetime.fromisoformat(last).astimezone().strftime('%Y-%m-%d %H:%M') if last else 'Never'
                freq=f"Every {p['minutes']} min" if p['minutes'] else f"Daily {p['daily']}"
                tree.insert('', 'end',iid=name,values=(name+' — '+p['user'],freq,s.get('status','Not run') if p['enabled'] else 'Paused',last,s.get('imported','—')))
            if selected and tree.exists(selected[0]): tree.selection_set(selected[0])
            note.set('Background service: '+agent_status(root)+'. Save profiles and configure Keychain before enabling.')
        except Exception as e: note.set('Status unavailable: '+str(e))
    def select(event=None):
        selected=tree.selection()
        if not selected: return
        p=config(root).get(selected[0])
        if not p:return
        for k in ('name','host','user'): vars[k].set(p[k])
        vars['folders'].set('; '.join(p['folders']))
        vars['mode'].set('Interval' if p['minutes'] is not None else 'Daily')
        vars['time'].set(str(p['minutes'] if p['minutes'] is not None else p['daily']))
        enabled.set(p['enabled'])
    def new():
        tree.selection_remove(*tree.selection())
        for k in ('name','host','user'):vars[k].set('')
        vars['folders'].set('INBOX');vars['mode'].set('Interval');vars['time'].set('30');enabled.set(False)
    def save():
        try:
            is_interval=vars['mode'].get()=='Interval'
            p=profile(vars['name'].get().strip(),vars['host'].get().strip(),vars['user'].get().strip(),[x.strip() for x in vars['folders'].get().split(';') if x.strip()],int(vars['time'].get()) if is_interval else None,None if is_interval else vars['time'].get(),enabled.get())
            profiles=config(root);profiles[p['name']]=p;write_json(root/'schedule.json',profiles);refresh();tree.selection_set(p['name'])
            note.set('Saved. Keychain item: '+service(p)+' | Account: '+p['user'])
        except Exception as e: messagebox.showerror('Save profile',str(e),parent=w)
    def keychain():
        try:
            p=config(root)[vars['name'].get()]
            messagebox.showinfo('Keychain setup','In Keychain Access, select the login keychain and create a new password item.\n\nKeychain Item Name: '+service(p)+'\nAccount Name: '+p['user']+'\nPassword: the mail provider app password.\n\nAllow the security command to read this item when macOS prompts. Never paste a password into the profile.',parent=w)
            subprocess.Popen(['/usr/bin/open','-a','Keychain Access'])
        except Exception as e: messagebox.showerror('Keychain',str(e),parent=w)
    def command(cmd,*args):
        try:
            # Run asynchronously without capturing credentials or blocking Tk.
            proc=subprocess.Popen([sys.executable,str(Path(__file__).with_name('schedule.py').resolve()),'--archive',str(root),cmd,*args],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            note.set('Requested '+cmd+'; refresh to see job status.')
            def poll():
                if proc.poll() is None: w.after(1000,poll)
                else:
                    refresh()
                    if proc.returncode: note.set('Manual job failed; check profile and Keychain.')
            w.after(1000,poll)
        except Exception as e: messagebox.showerror('Scheduler',str(e),parent=w)
    def install_agent():
        from hb_engine.schedule import install
        try:
            if not messagebox.askyesno('Install scheduler','Enable local background synchronization? Finish any older manual import first. Enabled accounts may sync immediately.',parent=w):return
            install(root);refresh()
        except Exception as e:messagebox.showerror('Install scheduler',str(e),parent=w)
    def remove_agent():
        from hb_engine.schedule import uninstall
        try:
            if not messagebox.askyesno('Remove scheduler','Stop and remove the background service? A running sync may be interrupted. The archive and account profiles will be retained.',parent=w):return
            uninstall(root);refresh()
        except Exception as e:messagebox.showerror('Remove scheduler',str(e),parent=w)
    def pause_resume():
        selected=tree.selection()
        if not selected:return
        profiles=config(root);p=profiles[selected[0]];p['enabled']=not p['enabled'];write_json(root/'schedule.json',profiles);refresh();select()
    bar=ttk.Frame(frame);bar.pack(fill='x',pady=(10,0))
    for title,fn in [('New',new),('Save profile',save),('Keychain setup',keychain),('Pause / resume',pause_resume),('Run now',lambda:command('run',tree.selection()[0]) if tree.selection() else None),('Install',install_agent),('Remove',remove_agent),('Refresh',refresh)]:
        ttk.Button(bar,text=title,command=fn).pack(side='left',padx=3)
    tree.bind('<<TreeviewSelect>>',select);refresh()
    if parent is None:w.mainloop()
