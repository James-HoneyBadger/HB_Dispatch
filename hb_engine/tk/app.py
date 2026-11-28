# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Local correspondence workstation. Plain text only; no remote rendering."""
def launch(archive):
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
    from pathlib import Path
    from hb_engine.preferences import load
    from ..search_worker import SearchController, find_readonly, recent_readonly
    prefs=load(archive.root)
    root=tk.Tk()
    root.title('HB Continuum — Historical Research Workstation')
    root.geometry(f"{prefs['window_width']}x{prefs['window_height']}")
    style=ttk.Style(root)
    if prefs['theme']!='System':
        style.theme_use('clam')
        bg,fg=('#24272b','#ededed') if prefs['theme']=='Dark' else ('#f4f5f6','#202124')
        style.configure('.',background=bg,foreground=fg)
        style.configure('Treeview',fieldbackground=bg,background=bg,foreground=fg)
        style.configure('TEntry',fieldbackground=bg,foreground=fg)
        root.configure(bg=bg)
    root.minsize(720,480)
    toolbar=ttk.Frame(root,padding=10)
    toolbar.pack(fill='x')
    query=ttk.Entry(toolbar)
    query.pack(side='left',fill='x',expand=True)
    status=tk.StringVar()
    pane=ttk.Panedwindow(root,orient=prefs['layout'])
    pane.pack(fill='both',expand=True,padx=10,pady=5)
    listing=ttk.Frame(pane)
    rows=ttk.Treeview(listing,columns=('subject','sender'),show='headings',selectmode='extended')
    rows.heading('subject',text='Subject')
    rows.heading('sender',text='Sender')
    rows.column('subject',width=prefs['subject_width'])
    rows.column('sender',width=prefs['sender_width'])
    scroll=ttk.Scrollbar(listing,orient='vertical',command=rows.yview)
    rows.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right',fill='y')
    rows.pack(fill='both',expand=True)
    pane.add(listing,weight=2)
    reader=ttk.Frame(pane)
    family={'System':'TkDefaultFont','Monospace':'TkFixedFont','Serif':'Times'}[prefs['font_family']]
    body=tk.Text(reader,wrap=prefs['wrap'],font=(family,prefs['font_size']),state='disabled',padx=12,pady=10)
    if prefs['theme']!='System':body.configure(background=bg,foreground=fg,insertbackground=fg)
    scroll2=ttk.Scrollbar(reader,orient='vertical',command=body.yview)
    body.configure(yscrollcommand=scroll2.set)
    scroll2.pack(side='right',fill='y')
    body.pack(fill='both',expand=True)
    pane.add(reader,weight=3)
    ttk.Label(root,textvariable=status,padding=10).pack(fill='x')
    def display(text):
        body.configure(state='normal')
        body.delete('1.0','end')
        body.insert('1.0',text)
        body.configure(state='disabled')
    def make_query_fn(prefs):
        def run(conn, term):
            if term: return find_readonly(conn, term, prefs['search_limit'])
            return recent_readonly(conn, prefs['recent_limit'])
        return run
    controller=SearchController(archive.root, make_query_fn(prefs), root.after, root.after_cancel)
    def _busy():
        search_btn.configure(state='disabled')
        progress.pack(side='left',padx=4)
        progress.start(12)
        status.set('Searching\u2026')
    def _idle():
        search_btn.configure(state='normal')
        progress.stop()
        progress.pack_forget()
    def show_results(hits, stats):
        _idle()
        rows.delete(*rows.get_children())
        display('')
        for h,s,f in hits: rows.insert('', 'end',iid=h,values=(s,f))
        status.set(f"{len(hits)} shown  |  {stats['messages']} archived messages  |  {stats['origins']} mailbox origins")
    def show_search_error(exc):
        _idle()
        messagebox.showerror('Search',str(exc))
    controller.on_result=show_results
    controller.on_error=show_search_error
    def run_search(*_):
        term=query.get().strip()
        if term and prefs['search_mode']=='Exact phrase': term='"'+term.replace('"','""')+'"'
        _busy()
        controller.submit(term)
    def select(*_):
        selection=rows.selection()
        if not selection: return
        try:
            h=selection[0]
            data=archive.details(h)
            keys=('subject','sender','date','message_id','reply_to') if prefs['show_message_ids'] else ('subject','sender','date')
            header='\n'.join(f"{k}: {data[k]}" for k in keys)
            attachments=archive.attachments(h)
            listing=('\nAttachments (indexed metadata):\n'+'\n'.join(
                f'{name} | {mime} | {size} indexed characters' for name,mime,size in attachments)) if attachments else ''
            display(header+('\nSHA-256: '+h if prefs['show_hash'] else '')+listing+'\n\n'+data['body'])
        except Exception as e: messagebox.showerror('Message',str(e))
    def show_thread():
        selected=rows.selection()
        if not selected: return messagebox.showinfo('Conversation','Select a message first.')
        try:
            messages=archive.thread(selected[0])
            display('\n\n'.join(
                f"{m['date']} | {m['sender']}\n{m['subject']}\nSHA-256: {m['sha256']}\n\n{m['body']}"
                for m in messages))
        except Exception as e: messagebox.showerror('Conversation',str(e))
    def show_timeline():
        try:
            hits=archive.timeline(limit=prefs['timeline_limit'])
            display('\n\n'.join(f"{date or 'Unknown date'} | {sender}\n{subject}\nSHA-256: {h}" for h,subject,sender,date in hits))
            status.set(f'{len(hits)} messages in timeline; dates are sender-declared.')
        except Exception as e: messagebox.showerror('Timeline',str(e))
    def show_correspondents():
        try:
            display('\n'.join(f'{count:>6}  {sender or "(unknown sender)"}' for sender,count in archive.correspondents(prefs['correspondent_limit'])))
        except Exception as e: messagebox.showerror('Correspondents',str(e))
    def import_files():
        try:
            for f in filedialog.askopenfilenames(initialdir=prefs['import_directory'] or None,filetypes=[('Email','*.eml')]):
                path=Path(f)
                if path.stat().st_size>64*1024*1024: raise ValueError('Message exceeds 64 MiB limit')
                archive.ingest(path.read_bytes())
            run_search()
        except Exception as e: messagebox.showerror('Import',str(e))
    def verify():
        try:
            bad=archive.verify()
            if bad: messagebox.showwarning('Integrity check',f'{len(bad)} messages failed verification.\n'+'\n'.join(bad[:10]))
            else: messagebox.showinfo('Integrity check','All indexed original messages match their stored hashes.')
        except Exception as e: messagebox.showerror('Integrity check',str(e))
    def export():
        selected=rows.selection()
        if not selected: return messagebox.showinfo('Export','Select one or more messages first.')
        parent=filedialog.askdirectory(initialdir=prefs['export_directory'] or None,title='Choose parent folder for evidence package')
        if not parent: return
        from datetime import datetime,timezone
        dest=Path(parent)/('evidence-'+datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ'))
        try:
            archive.export(selected,dest)
            messagebox.showinfo('Export complete',str(dest))
        except Exception as e: messagebox.showerror('Export',str(e))
    search_btn=ttk.Button(toolbar,text='Search',command=run_search)
    search_btn.pack(side='left',padx=3)
    for label,fn in [('Import EML',import_files),('Verify',verify),('Export selected',export)]:
        ttk.Button(toolbar,text=label,command=fn).pack(side='left',padx=3)
    progress=ttk.Progressbar(toolbar,mode='indeterminate',length=110)
    analysis_bar=ttk.Frame(root,padding=(10,0))
    analysis_bar.pack(fill='x',before=pane)
    for label,fn in [('Conversation',show_thread),('Timeline',show_timeline),('Correspondents',show_correspondents)]:
        ttk.Button(analysis_bar,text=label,command=fn).pack(side='left',padx=3)
    def scheduling():
        from .schedule_center import launch as launch_schedule
        launch_schedule(archive.root.resolve(), root)
    ttk.Button(analysis_bar,text='Synchronization center',command=scheduling).pack(side='left',padx=3)
    def configure():
        from .preferences_dialog import launch as launch_preferences
        launch_preferences(archive.root,root)
    ttk.Button(analysis_bar,text='Configuration center',command=configure).pack(side='left',padx=3)
    if prefs['startup_view']=='Saved query':query.insert(0,prefs['startup_query'])
    if prefs['verify_on_start']:root.after(100,verify)
    def research_workbench():
        from .research_workbench import launch as launch_research
        launch_research(archive,root,rows.selection)
    ttk.Button(analysis_bar,text='Research workbench',command=research_workbench).pack(side='left',padx=3)
    query.bind('<Return>',run_search)
    root.bind('<Control-f>',lambda _:query.focus_set())
    rows.bind('<<TreeviewSelect>>',select)
    root.protocol('WM_DELETE_WINDOW',lambda:(controller.destroy(),root.destroy()))
    run_search()
    root.mainloop()
