"""为i发电 · CHI TXT backup desktop companion."""
from pathlib import Path
import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from backup import BackupWriter, TECHNIQUES, read_recovery
from chi_memory import CHIReader, Memory, Waiting, Unsupported, list_chi_processes

VERSION = '0.1 验证版'
BASE = Path(sys.executable).resolve().parent if getattr(sys, 'frozen', False) else Path(__file__).resolve().parent.parent


class App:
    def __init__(self, root):
        self.root = root
        self.root.title('为i发电 · CHI 实时备份')
        self.root.geometry('900x690'); self.root.minsize(820, 640)
        self.events, self.stop = queue.Queue(), threading.Event()
        self.thread = None; self.processes = []; self.folder = None
        self.closing = False
        style = ttk.Style()
        style.theme_use('clam')
        style.configure('.', font=('Microsoft YaHei UI', 10), background='#f5f7fb')
        style.configure('TButton', padding=(12, 7))
        style.configure('Title.TLabel', font=('Microsoft YaHei UI', 24, 'bold'), foreground='#174a72')
        style.configure('Metric.TLabel', font=('Microsoft YaHei UI', 20, 'bold'), foreground='#174a72')
        style.configure('Treeview', rowheight=29, background='white', fieldbackground='white')
        self.root.configure(background='#f5f7fb')
        pane = ttk.Frame(root, padding=24); pane.pack(fill='both', expand=True)
        ttk.Label(pane, text='为i发电', style='Title.TLabel').pack(anchor='w')
        ttk.Label(pane, text=f'CHI760E · TXT 实时备份  |  {VERSION}').pack(anchor='w', pady=(2, 14))
        ttk.Label(pane, text='在 CHI 中设置、启动并查看曲线；这里负责保存。支持单通道 CA / i–t / CP。').pack(anchor='w')
        ttk.Label(pane, text='本版已做离线验证，首次正式使用前请完成短时实机比对。', foreground='#94601b').pack(anchor='w', pady=(3, 14))
        form = ttk.Frame(pane); form.pack(fill='x')
        form.columnconfigure(1, weight=1)
        ttk.Label(form, text='CHI 程序').grid(row=0, column=0, sticky='w', padx=(0, 12), pady=5)
        self.process = ttk.Combobox(form, state='readonly'); self.process.grid(row=0, column=1, sticky='ew')
        self.refresh_button = ttk.Button(form, text='刷新', command=self.refresh); self.refresh_button.grid(row=0, column=2, padx=(10, 0))
        ttk.Label(form, text='保存位置').grid(row=1, column=0, sticky='w', pady=8)
        self.destination = tk.StringVar(value=str(BASE / 'Data'))
        self.path_entry = ttk.Entry(form, textvariable=self.destination); self.path_entry.grid(row=1, column=1, sticky='ew')
        self.browse_button = ttk.Button(form, text='选择…', command=self.browse); self.browse_button.grid(row=1, column=2, padx=(10, 0))
        ttk.Label(form, text='TXT 保存间隔').grid(row=2, column=0, sticky='w', pady=5)
        interval_row = ttk.Frame(form); interval_row.grid(row=2, column=1, sticky='w')
        self.interval = tk.StringVar(value='1')
        self.interval_box = ttk.Spinbox(interval_row, from_=0.5, to=60, increment=0.5, textvariable=self.interval, width=8)
        self.interval_box.pack(side='left')
        ttk.Label(interval_row, text=' 秒   （CHI 采样间隔保持原设置）').pack(side='left')
        buttons = ttk.Frame(pane); buttons.pack(fill='x', pady=(16, 12))
        self.start_button = ttk.Button(buttons, text='连接并开始备份', command=self.start); self.start_button.pack(side='left')
        self.stop_button = ttk.Button(buttons, text='停止备份', command=self.stop_backup, state='disabled'); self.stop_button.pack(side='left', padx=8)
        ttk.Button(buttons, text='打开保存文件夹', command=self.open_folder).pack(side='left')
        self.convert_button = ttk.Button(buttons, text='恢复文件转 TXT', command=self.convert); self.convert_button.pack(side='right')
        self.status = tk.StringVar(value='请先打开 CHI760E，然后点击“刷新”。')
        self.status_label = ttk.Label(pane, textvariable=self.status, wraplength=820, foreground='#31536c')
        self.status_label.pack(anchor='w', fill='x', pady=(0, 10))
        metrics = ttk.Frame(pane); metrics.pack(fill='x')
        self.count, self.saved, self.latest = tk.StringVar(value='0 点'), tk.StringVar(value='—'), tk.StringVar(value='—')
        for col, (title, value) in enumerate((('已写入 TXT', self.count), ('最近写入', self.saved), ('最后采样时间', self.latest))):
            cell = ttk.Frame(metrics); cell.grid(row=0, column=col, sticky='w', padx=(0, 66))
            ttk.Label(cell, text=title).pack(anchor='w')
            ttk.Label(cell, textvariable=value, style='Metric.TLabel').pack(anchor='w')
        self.table = ttk.Treeview(pane, columns=('index', 'time', 'value'), show='headings', height=8)
        self.table.heading('index', text='序号'); self.table.heading('time', text='时间(s)'); self.table.heading('value', text='测量值')
        for col in ('index', 'time', 'value'):
            self.table.column(col, width=190, anchor='e')
        self.table.pack(fill='both', expand=True, pady=(12, 8))
        ttk.Label(pane, text='仅“已写入 TXT”的点已提交写盘。断电仍可能丢失最后一个保存间隔及硬盘缓存中的数据。', foreground='#66717c', wraplength=820).pack(anchor='w')
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(150, self.drain)
        self.refresh()

    def browse(self):
        folder = filedialog.askdirectory(initialdir=self.destination.get(), parent=self.root)
        if folder: self.destination.set(folder)

    def refresh(self):
        try:
            self.processes = list_chi_processes()
            self.process['values'] = [f'{name}  ·  PID {pid}' for pid, name in self.processes]
            if self.processes: self.process.current(0)
            else: self.process.set(''); self.status.set('未发现 CHI760E，请先打开原软件。')
        except Exception as error:
            self.status.set(str(error))

    def controls(self, active):
        self.start_button['state'] = 'disabled' if active else 'normal'
        self.stop_button['state'] = 'normal' if active else 'disabled'
        for widget in (self.refresh_button, self.browse_button, self.path_entry, self.interval_box, self.convert_button):
            widget['state'] = 'disabled' if active else 'normal'
        self.process['state'] = 'disabled' if active else 'readonly'

    def start(self):
        try:
            index = self.process.current()
            if index < 0 or index >= len(self.processes):
                raise ValueError('请打开并选择 CHI760E 程序。')
            interval = float(self.interval.get())
            if not 0.5 <= interval <= 60:
                raise ValueError('保存间隔请填写 0.5–60 秒。')
            target = Path(self.destination.get()).expanduser().resolve()
            target.mkdir(parents=True, exist_ok=True)
        except Exception as error:
            messagebox.showerror('未开始备份', str(error), parent=self.root); return
        self.stop.clear(); self.controls(True)
        self.status.set('正在校验 CHI 版本并连接…')
        self.thread = threading.Thread(target=self.worker, args=(self.processes[index][0], target, interval), daemon=True)
        self.thread.start()

    def worker(self, pid, target, interval):
        memory, writer = None, BackupWriter(target)
        try:
            memory = Memory(pid); reader = CHIReader(memory)
            last_flush = time.monotonic()
            last_new = time.monotonic()
            old_status = ''
            while not self.stop.is_set():
                if not memory.alive():
                    self.events.put(('status', 'CHI 已关闭；正在保存已读取的最后数据。')); break
                status = ''
                try:
                    snap = reader.snapshot()
                    previous_count = len(writer.points)
                    writer.accept(snap)
                    if len(writer.points) != previous_count:
                        last_new = time.monotonic()
                    status = ('本次测试已结束并保存，等待下一次测试。' if snap.complete
                              else '正在备份 ' + TECHNIQUES[snap.technique][0] + '，测试继续在 CHI 中进行。')
                    if snap.complete or time.monotonic() - last_flush >= interval:
                        if len(writer.points) > writer.flushed:
                            writer.flush(); self.events.put(('data', self.payload(writer)))
                        last_flush = time.monotonic()
                except Waiting as error:
                    status = str(error)
                except Unsupported as error:
                    status = '未读取：' + str(error)
                # Even if a new snapshot is temporarily unavailable, persist the
                # previously validated batch at its independent disk interval.
                if len(writer.points) > writer.flushed and time.monotonic() - last_flush >= interval:
                    writer.flush(); last_flush = time.monotonic()
                    self.events.put(('data', self.payload(writer)))
                if reader.was_running and time.monotonic() - last_new > 15:
                    status += '  已超过15秒没有新增点，请检查 CHI 状态。'
                if status != old_status:
                    self.events.put(('status', status)); old_status = status
                self.stop.wait(0.25)
        except Exception as error:
            self.events.put(('error', f'备份已停止：{error}'))
        finally:
            try:
                writer.close()
                if writer.directory: self.events.put(('data', self.payload(writer)))
            except Exception as error:
                self.events.put(('error', f'最后写盘失败：{error}。请保留当前文件夹。'))
            if memory: memory.close()
            self.events.put(('done', None))

    @staticmethod
    def payload(writer):
        return {'count': writer.flushed, 'saved': writer.last_saved, 'folder': str(writer.directory),
                'tech': writer.technique, 'tail': writer.points[max(0, writer.flushed-12):writer.flushed]}

    def drain(self):
        try:
            while True:
                kind, value = self.events.get_nowait()
                if kind in ('status', 'error'):
                    self.status.set(value)
                    self.status_label.configure(foreground='#b42318' if kind == 'error' else '#31536c')
                elif kind == 'data':
                    self.folder = Path(value['folder'])
                    self.count.set(f"{value['count']} 点"); self.saved.set(value['saved'])
                    if value['tail']: self.latest.set(f"{value['tail'][-1][0]:.6g} s")
                    self.table.heading('value', text=TECHNIQUES[value['tech']][1])
                    for item in self.table.get_children(): self.table.delete(item)
                    start = value['count'] - len(value['tail']) + 1
                    for i,(t,y) in enumerate(value['tail'], start):
                        self.table.insert('', 'end', values=(i, f'{t:.9g}', f'{y*TECHNIQUES[value["tech"]][3]:.9g}'))
                elif kind == 'done':
                    self.controls(False)
                    if self.closing: self.root.destroy(); return
        except queue.Empty:
            pass
        self.root.after(150, self.drain)

    def stop_backup(self):
        self.stop.set(); self.status.set('正在保存剩余数据；CHI 测试继续进行。')
        self.stop_button['state'] = 'disabled'

    def open_folder(self):
        target = self.folder or Path(self.destination.get())
        target.mkdir(parents=True, exist_ok=True)
        os.startfile(target)

    def convert(self):
        path = filedialog.askopenfilename(parent=self.root, title='选择已结束测试的 CHI 恢复文件', filetypes=[('CHI恢复文件', '*.tmp'), ('所有文件', '*.*')])
        if not path: return
        writer = BackupWriter(Path(self.destination.get()))
        try:
            snap = read_recovery(Path(path).read_bytes(), str(path))
            if not snap.points: raise ValueError('文件中尚无完整数据点。')
            writer.accept(snap); writer.close()
            self.events.put(('data', self.payload(writer)))
            self.status.set('恢复文件已转换，输出保存在独立文件夹。')
        except Exception as error:
            try: writer.close()
            except Exception: pass
            messagebox.showerror('转换未完成', str(error), parent=self.root)

    def close(self):
        if self.thread and self.thread.is_alive():
            self.closing = True; self.stop_backup()
        else:
            self.root.destroy()


def main():
    if '--self-test' in sys.argv:
        from self_check import run
        index = sys.argv.index('--self-test')
        folder = Path(sys.argv[index+1]) if len(sys.argv) > index+1 else BASE / 'SelfTest'
        run(folder, ui='--ui-check' in sys.argv); return
    root = tk.Tk(); App(root); root.mainloop()


if __name__ == '__main__':
    main()
