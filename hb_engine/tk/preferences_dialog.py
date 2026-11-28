# SPDX-License-Identifier: GPL-3.0-only
# Copyright (c) 2026 Honey Badger Universe
"""Schema-driven graphical preferences editor."""
def launch(root,parent,on_save=None):
    import tkinter as tk
    from tkinter import ttk,messagebox,filedialog
    from hb_engine.preferences import FIELDS,DEFAULTS,load,save,decode,export
    from hb_engine.schedule import read_json
    w=tk.Toplevel(parent);w.title('HB Continuum | Configuration center');w.geometry('820x610')
    frame=ttk.Frame(w,padding=16);frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='Configuration center',font=('TkDefaultFont',20,'bold')).pack(anchor='w')
    ttk.Label(frame,text='Archive-specific settings • No programming required').pack(anchor='w',pady=(0,10))
    tabs=ttk.Notebook(frame);tabs.pack(fill='both',expand=True)
    groups={};variables={}
    for k,(group,label,default,allowed) in FIELDS.items():
        if group not in groups:
            page=ttk.Frame(tabs,padding=12);tabs.add(page,text=group)
            groups[group]=(page,0)
            page.columnconfigure(1,weight=1)
        page,row=groups[group];groups[group]=(page,row+1)
        var=tk.BooleanVar() if isinstance(default,bool) else tk.StringVar()
        variables[k]=var
        ttk.Label(page,text=label,wraplength=330).grid(row=row,column=0,sticky='w',pady=5,padx=(0,12))
        if isinstance(default,bool):widget=ttk.Checkbutton(page,variable=var)
        elif isinstance(default,int):widget=ttk.Spinbox(page,textvariable=var,from_=allowed[0],to=allowed[1],width=12)
        elif allowed:widget=ttk.Combobox(page,textvariable=var,values=allowed,state='readonly')
        else:widget=ttk.Entry(page,textvariable=var)
        widget.grid(row=row,column=1,sticky='ew',pady=5)
    def populate(data):
        for k,v in data.items():variables[k].set(v)
    def collect():
        from hb_engine.preferences import validate
        return validate({k:int(v.get()) if type(DEFAULTS[k]) is int else v.get() for k,v in variables.items()})
    def apply():
        try:
            save(root,collect())
            if on_save:on_save()
            messagebox.showinfo('Saved','Preferences saved. Restart the workstation to apply layout and appearance changes.',parent=w)
        except Exception as e:messagebox.showerror('Configuration',str(e),parent=w)
    def import_settings():
        path=filedialog.askopenfilename(parent=w,filetypes=[('JSON','*.json')])
        if path:
            try:populate(decode(read_json(__import__('pathlib').Path(path),None)))
            except Exception as e:messagebox.showerror('Import settings',str(e),parent=w)
    def export_settings():
        path=filedialog.asksaveasfilename(parent=w,defaultextension='.json',filetypes=[('JSON','*.json')])
        if path:
            try:export(path,collect())
            except Exception as e:messagebox.showerror('Export settings',str(e),parent=w)
    bar=ttk.Frame(frame);bar.pack(fill='x',pady=(12,0))
    for title,fn in [('Save',apply),('Restore defaults',lambda:populate(DEFAULTS)),('Import settings',import_settings),('Export settings',export_settings),('Close',w.destroy)]:
        ttk.Button(bar,text=title,command=fn).pack(side='left',padx=3)
    populate(load(root))
