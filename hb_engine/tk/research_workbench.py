# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Research workbench for local, user-defined overlays."""
def launch(archive,parent,selected_hashes):
    import tkinter as tk
    from tkinter import ttk,messagebox,filedialog
    from hb_engine.research import Research,FIELDS,OPERATORS,TYPES,COLUMNS,GROUPS
    r=Research(archive)
    w=tk.Toplevel(parent);w.title('HB Continuum | Research workbench');w.geometry('1050x730')
    frame=ttk.Frame(w,padding=14);frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='Research workbench',font=('TkDefaultFont',20,'bold')).pack(anchor='w')
    ttk.Label(frame,text='Personal classifications and notes are separate from original archived messages.').pack(anchor='w',pady=(0,10))
    tabs=ttk.Notebook(frame);tabs.pack(fill='both',expand=True)
    output=tk.Text(frame,height=12,wrap='word');output.pack(fill='both',expand=True,pady=(10,0))
    def show(value):
        output.delete('1.0','end');output.insert('1.0',value)
    def run(fn):
        try:fn()
        except Exception as e:messagebox.showerror('Research',str(e),parent=w)
    def tab(name,fields,actions):
        page=ttk.Frame(tabs,padding=12);tabs.add(page,text=name);page.columnconfigure(1,weight=1)
        v={}
        for i,(k,label,default,choices) in enumerate(fields):
            var=tk.StringVar(value=default);v[k]=var
            ttk.Label(page,text=label).grid(row=i,column=0,sticky='w',padx=(0,12),pady=4)
            entry=ttk.Combobox(page,textvariable=var,values=choices,state='readonly') if choices else ttk.Entry(page,textvariable=var)
            entry.grid(row=i,column=1,sticky='ew',pady=4)
        bar=ttk.Frame(page);bar.grid(row=len(fields),column=0,columnspan=2,sticky='w',pady=8)
        for label,fn in actions:
            ttk.Button(bar,text=label,command=lambda f=fn:run(lambda:f({k:x.get() for k,x in v.items()}))).pack(side='left',padx=3)
        return v
    def list_table(table):
        if table not in ('workspaces','searches','rules','fields','reports'):raise ValueError('Invalid table')
        show('\n'.join(' | '.join(map(str,row)) for row in r.db.execute('SELECT * FROM '+table)) or 'No records')
    def list_results(v):
        rows=r.results(v['workspace'],v['name'])
        show(str(len(rows))+' results (limited by saved search)\n'+'\n'.join(f"{x['date']} | {x['sender']} | {x['subject']} | {x['hash']}" for x in rows))
    def selected():
        hashes=list(selected_hashes())
        if not hashes:raise ValueError('Select messages in the main workstation first')
        return hashes
    def tag(v,remove=False):
        hashes=selected()
        for h in hashes:
            (r.untag if remove else r.tag)(h,v['tag'])
        show(f'Updated manual tags for {len(hashes)} selected messages')
    def inspect(v):
        out=[]
        for h in selected():
            tags=r.db.execute('SELECT tag,source FROM tags WHERE hash=?',(h,)).fetchall()
            metadata=r.db.execute('SELECT field,value FROM metadata WHERE hash=?',(h,)).fetchall()
            out.append(h+'\nTags: '+str(tags)+'\nFields: '+str(metadata))
        show('\n\n'.join(out))
    def set_fields(v):
        hashes=selected()
        for h in hashes:r.set_metadata(h,v['field'],v['value'])
        show(f'Updated {len(hashes)} selected messages')
    def report(v):
        cols,rows=r.report(v['name'])
        show(str(len(rows))+' report rows\n'+' | '.join(cols)+'\n'+'\n'.join(' | '.join(str(x[c]) for c in cols) for x in rows))
    def export_report(v):
        path=filedialog.asksaveasfilename(parent=w,defaultextension='.csv',filetypes=[('CSV','*.csv')])
        if path:r.export_report(v['name'],path);show('Exported '+path)
    tab('Workspaces',[('name','Name','General',()),('description','Description','',())],[('Save workspace',lambda v:(r.workspace(v['name'],v['description']),list_table('workspaces'))),('List workspaces',lambda v:list_table('workspaces'))])
    tab('Saved searches',[('workspace','Workspace','General',()),('name','Search name','',()),('query','FTS expression (blank = all)','',()),('tag','Required tag (optional)','',()),('limit','Result limit','1000',())],[('Save search',lambda v:(r.save_search(v['workspace'],v['name'],v['query'],v['tag'],int(v['limit'])),list_table('searches'))),('Run search',list_results),('List searches',lambda v:list_table('searches'))])
    tab('Rules',[('name','Rule name','',()),('field','Message field','subject',FIELDS),('operator','Match','contains',OPERATORS),('value','Match text','',()),('tag','Assign tag','',()),('enabled','Enabled','Yes',('Yes','No'))],[('Save rule',lambda v:(r.save_rule(v['name'],v['field'],v['operator'],v['value'],v['tag'],v['enabled']=='Yes'),list_table('rules'))),('Rebuild rule tags',lambda v:show(f'{r.apply_rules()} rule matches rebuilt')),('List rules',lambda v:list_table('rules'))])
    tab('Tags',[('tag','Manual tag','',())],[('Tag selected',tag),('Remove manual tag',lambda v:tag(v,True)),('Inspect selected',inspect)])
    tab('Custom fields',[('field','Field name','',()),('kind','Field type','text',TYPES),('choices','Choice values (semicolon separated)','',()),('value','Value for selected messages','',())],[('Define field',lambda v:(r.define_field(v['field'],v['kind'],[x.strip() for x in v['choices'].split(';') if x.strip()]),list_table('fields'))),('Set value',set_fields),('Inspect selected',inspect),('List fields',lambda v:list_table('fields'))])
    tab('Reports',[('name','Report name','',()),('workspace','Workspace','General',()),('search','Saved search name','',()),('columns','Columns (semicolon separated)','subject;sender;date;hash',()),('group','Group by (optional)','',('',)+GROUPS)],[('Save report',lambda v:(r.save_report(v['name'],v['workspace'],v['search'],[x.strip() for x in v['columns'].split(';') if x.strip()],v['group']),list_table('reports'))),('Preview',report),('Export CSV',export_report),('List reports',lambda v:list_table('reports'))])
    def close():
        r.close();w.destroy()
    w.protocol('WM_DELETE_WINDOW',close)
