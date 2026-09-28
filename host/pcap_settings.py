# SPDX-License-Identifier: MIT
"""Field editor; all device I/O is delegated to App's single serial worker."""
import json
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
from pcap_registers import FIELDS, decode, plan, DATASHEET


class SettingsDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.title('PCAP04 全部配置 / Configuration')
        self.geometry('1020x690')
        self.transient(app)
        self.grab_set()
        self.registers = None
        self.pending = False
        self.vars = {}
        self.status = tk.StringVar(value='正在读取硬件配置…')
        ttk.Label(self,text='采集已暂停。读取 → 编辑 → 应用并读回；完成后手动开始新一段采集。',padding=8).pack(anchor='w')
        nb = ttk.Notebook(self); nb.pack(fill='both',expand=True,padx=8)
        groups = {}
        for field in FIELDS.values():
            if field.group not in groups:
                page = ttk.Frame(nb); nb.add(page,text=field.group)
                cv = tk.Canvas(page,highlightthickness=0)
                sb = ttk.Scrollbar(page,orient='vertical',command=cv.yview)
                cv.configure(yscrollcommand=sb.set)
                sb.pack(side='right',fill='y'); cv.pack(side='left',fill='both',expand=True)
                body = ttk.Frame(cv,padding=8)
                window = cv.create_window((0,0),window=body,anchor='nw')
                body.bind('<Configure>',lambda e,c=cv:c.configure(scrollregion=c.bbox('all')))
                cv.bind('<Configure>',lambda e,c=cv,w=window:c.itemconfigure(w,width=e.width))
                groups[field.group] = [body,0]
            body,row = groups[field.group]; groups[field.group][1] += 1
            ttk.Label(body,text=field.name,width=24).grid(row=row,column=0,sticky='w',pady=4)
            var = tk.StringVar(); self.vars[field.name] = var
            ttk.Entry(body,textvariable=var,width=12).grid(row=row,column=1,padx=6)
            choices = '/'.join(map(str,field.choices)) if field.choices else '0..%d'%field.maximum
            ttk.Label(body,text='[%s] %s' % (choices,field.help),wraplength=570,justify='left').grid(row=row,column=2,sticky='w')
        page=ttk.Frame(nb,padding=8); nb.add(page,text='原始寄存器/说明')
        self.raw=tk.Text(page,height=12,font=('Menlo',11),wrap='word'); self.raw.pack(fill='both',expand=True)
        self.raw.insert('end','只读快照：保留位、TDC强制值、内部位、工厂CHARGE_PUMP不开放编辑。\n'
                        'MEM_LOCK、SERIAL_NUMBER、MEM_CTRL 属于锁定/非易失存储操作，不属于运行参数。\n'
                        '本界面不写NVRAM。掉电/重新load会丢失配置；导出JSON可保存。\n'
                        'DSP 算法页部分参数只对linearize固件生效。\n'
                        'C_AVRG允许芯片全范围，但本板>256未验证，≥512曾出现明显低读。\n'
                        '修改参考/增益/模式后需重新验证pF标定；差分模式不能沿用普通通道解码。\n'
                        '禁用PG5 INTN、改触发源或关闭RUNBIT可能停止stream输出。\n'
                        '充电电阻：RCHG_SEL=1为180 kΩ；PRECHARGE_TIME=1023禁用预充电。\n'
                        '时序公式与约束见官方手册 §6.3、§7.2.3.2:\n'+DATASHEET+'\n\n')
        self.raw.configure(state='disabled')
        ttk.Label(self,textvariable=self.status,wraplength=990,padding=8).pack(fill='x')
        bar=ttk.Frame(self,padding=8); bar.pack(fill='x')
        self.buttons=[]
        for text,fn in [('读取当前值',self.refresh),('导出JSON',self.export),('导入JSON',self.load_file),('应用并读回',self.apply)]:
            b=ttk.Button(bar,text=text,command=fn); b.pack(side='left',padx=4); self.buttons.append(b)
        ttk.Button(bar,text='关闭',command=self.close).pack(side='right')
        self.protocol('WM_DELETE_WINDOW',self.close)
        self.refresh()

    def close(self):
        if self.pending: return
        self.app.settings_dialog = None
        self.destroy()

    def busy(self,on):
        self.pending=on
        for b in self.buttons: b.configure(state='disabled' if on else 'normal')

    def refresh(self):
        self.busy(True)
        self.status.set('正在读取64字节寄存器；没有写入或载入默认配置。')
        self.app._call('register_read',self.app.dev.read_registers)

    def result(self,name,result):
        self.busy(False)
        regs = result if name=='register_read' else result['registers']
        self.registers = regs
        for n,v in decode(regs).items(): self.vars[n].set(str(v))
        self.raw.configure(state='normal')
        if hasattr(self,'raw_start'): self.raw.delete(self.raw_start,'end')
        else: self.raw_start=self.raw.index('end-1c')
        self.raw.insert('end','\n'+'\n'.join('%02X: '%a+' '.join('%02X'%regs[i] for i in range(a,a+16)) for a in range(0,64,16)))
        self.raw.configure(state='disabled')
        self.status.set('已读回。RUNBIT=%d。'%decode(regs)['RUNBIT'] + ('配置已应用；采集保持暂停。' if name=='register_write' else '只修改需要调整的项。'))

    def error(self,message):
        self.busy(False)
        self.status.set('失败：'+message)

    def changes(self):
        if self.registers is None: raise ValueError('先读取当前配置')
        changes={n:int(v.get().strip(),0) for n,v in self.vars.items()}
        changes={n:v for n,v in changes.items() if v!=FIELDS[n].get(self.registers)}
        plan(self.registers,changes)
        return changes

    def apply(self):
        try: changes=self.changes()
        except ValueError as e:
            messagebox.showerror('参数无效',str(e),parent=self); return
        if not changes:
            self.status.set('没有改动。'); return
        self.status.set('正在停机、写入并读回：'+', '.join(changes))
        self.busy(True)
        expected={n:FIELDS[n].get(self.registers) for n in changes}
        self.app._call('register_write',lambda:self.app.dev.write_fields(changes,expected=expected))

    def export(self):
        if self.registers is None: return
        path=filedialog.asksaveasfilename(parent=self,defaultextension='.json',initialfile='pcap04-config.json')
        if path:
            with open(path,'w') as f: json.dump({'schema':1,'fields':decode(self.registers),'registers':self.registers},f,indent=2)
            self.status.set('已导出最近读回值（不含未应用编辑）：'+path)

    def load_file(self):
        if self.registers is None: return
        path=filedialog.askopenfilename(parent=self,filetypes=[('JSON','*.json')])
        if not path:return
        try:
            with open(path) as f: data=json.load(f)
            if data.get('schema')!=1:raise ValueError('Unknown schema')
            changes=data['fields']
            plan(self.registers,changes)
            for n,v in changes.items():self.vars[n].set(str(v))
        except (OSError,ValueError,KeyError,TypeError) as e:
            messagebox.showerror('导入失败',str(e),parent=self);return
        self.status.set('文件已载入编辑框，尚未写入硬件。点击应用并读回。')
