"""Windows GUI for the verified offline preparation workflow."""
from __future__ import annotations
import json
import os
from pathlib import Path
import queue
import sys
import threading


def main():
    if len(sys.argv) > 1 and sys.argv[1] == '--research-worker':
        from a7cr_easy import research_worker
        return research_worker(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == '--self-test':
        from a7cr_easy import reference_root
        import a7cr_research
        a7cr_research.verify_references(reference_root())
        report = {'references_valid': True, 'camera_accessed': False, 'installation_available': False}
        Path(sys.argv[2]).write_text(json.dumps(report, indent=2), encoding='utf-8')
        return 0
    if len(sys.argv) > 1 and sys.argv[1] == '--prepare-local':
        from a7cr_easy import data_root, prepare, reference_root
        result = prepare(data_root(), reference_root(), threading.Event(), lambda *args: None)
        Path(sys.argv[2]).write_text(json.dumps(result, indent=2), encoding='utf-8')
        return 0
    import tkinter as tk
    from tkinter import ttk, messagebox
    from a7cr_easy import Cancelled, VERSION, data_root, prepare, reference_root
    root = tk.Tk()
    root.title('A7CR FL2 / FL3 準備工具')
    root.geometry('790x660')
    root.minsize(730, 640)
    root.configure(bg='#101722')
    root.option_add('*Font', ('Microsoft JhengHei UI', 11))
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('A7.Horizontal.TProgressbar', troughcolor='#293546', background='#73d4bc', borderwidth=0)
    shell = tk.Frame(root, bg='#101722', padx=34, pady=28)
    shell.pack(fill='both', expand=True)
    tk.Label(shell, text='SONY A7CR  /  2.01', fg='#73d4bc', bg='#101722', font=('Segoe UI', 11, 'bold')).pack(anchor='w')
    tk.Label(shell, text='FL2 / FL3，少一點折騰。', fg='#f4f7fb', bg='#101722', font=('Microsoft JhengHei UI', 25, 'bold')).pack(anchor='w', pady=(12, 6))
    tk.Label(shell, text='按一下開始，下載、解包、建構與驗證都由工具處理。', fg='#b8c5d6', bg='#101722').pack(anchor='w')
    card = tk.Frame(shell, bg='#1c2838', padx=22, pady=18)
    card.pack(fill='x', pady=(24, 0))
    tk.Label(card, text='目前版本：自動準備與離線驗證', fg='#ffffff', bg='#1c2838', font=('Microsoft JhengHei UI', 13, 'bold')).pack(anchor='w')
    tk.Label(card, text='相機安裝／還原後端尚未完成。本工具不會改動相機。', fg='#f3cd83', bg='#1c2838', wraplength=650, justify='left').pack(anchor='w', pady=(8, 0))
    tk.Label(card, text='第一次約下載 2.3 GB，請保留至少 7 GB 可用空間。\n過程中不需要連接相機，也不用安裝 Python 或 Git。', fg='#bdcadb', bg='#1c2838', justify='left').pack(anchor='w', pady=(10, 0))
    status = tk.StringVar(value='準備好了。按下方按鈕即可開始。')
    tk.Label(shell, textvariable=status, fg='#f4f7fb', bg='#101722', wraplength=700, justify='left').pack(anchor='w', pady=(23, 10))
    progress = ttk.Progressbar(shell, style='A7.Horizontal.TProgressbar', mode='determinate', maximum=100)
    progress.pack(fill='x')
    controls = tk.Frame(shell, bg='#101722')
    controls.pack(fill='x', pady=(22, 0))
    events = queue.Queue()
    cancel = threading.Event()
    active = [False]
    last_folder = [data_root()]
    start = tk.Button(controls, text='一鍵準備（不寫入相機）', bg='#73d4bc', fg='#10251f', activebackground='#9ae7d3', relief='flat', padx=20, pady=12, font=('Microsoft JhengHei UI', 12, 'bold'), cursor='hand2')
    start.pack(side='left')
    stop = tk.Button(controls, text='停止', state='disabled', padx=16, pady=12, relief='flat')
    stop.pack(side='left', padx=10)
    folder = tk.Button(controls, text='開啟資料夾', padx=16, pady=12, relief='flat')
    folder.pack(side='right')
    tk.Label(shell, text='準備完成 ≠ 已安裝。安裝與恢復原狀功能仍待完成實機驗證。', fg='#92a4ba', bg='#101722', wraplength=700, justify='left').pack(anchor='w', pady=(22, 0))
    tk.Label(shell, text='A7CR Tools  ' + VERSION, fg='#60768f', bg='#101722', font=('Segoe UI', 9)).pack(anchor='w', pady=(12, 0))
    def work():
        try:
            prepare(data_root(), reference_root(), cancel, lambda *event: events.put(event))
        except Cancelled as exc:
            events.put(('cancelled', str(exc), 0))
        except BaseException as exc:
            events.put(('error', str(exc), 0))
        finally:
            events.put(('ended', '', 0))
    def begin():
        if active[0]:
            return
        cancel.clear(); active[0] = True
        start.configure(state='disabled'); stop.configure(state='normal')
        threading.Thread(target=work, daemon=True).start()
    def stop_work():
        cancel.set(); stop.configure(state='disabled'); status.set('正在停止，請稍候…')
    def open_folder():
        path = last_folder[0]
        if not path.exists():
            messagebox.showinfo('尚無資料', '開始準備後，工具會自動建立資料夾。', parent=root)
            return
        os.startfile(path)
    start.configure(command=begin); stop.configure(command=stop_work); folder.configure(command=open_folder)
    def pump():
        try:
            while True:
                kind, text, fraction = events.get_nowait()
                if kind == 'workspace':
                    last_folder[0] = Path(text)
                elif kind == 'ended':
                    active[0] = False; start.configure(state='normal'); stop.configure(state='disabled')
                else:
                    status.set(text); progress['value'] = fraction * 100
                    if kind == 'error':
                        messagebox.showerror('準備未完成', text, parent=root)
        except queue.Empty:
            pass
        root.after(120, pump)
    def close():
        if active[0]:
            stop_work()
            root.after(200, close_when_stopped)
        else:
            root.destroy()
    def close_when_stopped():
        if active[0]:
            root.after(200, close_when_stopped)
        else:
            root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    root.after(120, pump)
    if '--ui-smoke-test' in sys.argv:
        root.after(1200, root.destroy)
    root.mainloop()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
