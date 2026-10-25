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
        import a7cr_reader
        import a7cr_operations
        report = {'references_valid': True, 'backup_modules_available': True, 'camera_accessed': False,
                  'installation_available': False, 'zero_brick_guarantee': False}
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
    root.title('A7CR FL2 / FL3 工具 — 備份開發版')
    root.geometry('850x760')
    root.minsize(800, 720)
    root.configure(bg='#101722')
    root.option_add('*Font', ('Microsoft JhengHei UI', 11))
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('A7.Horizontal.TProgressbar', troughcolor='#293546', background='#73d4bc', borderwidth=0)
    shell = tk.Frame(root, bg='#101722', padx=34, pady=28)
    shell.pack(fill='both', expand=True)
    tk.Label(shell, text='SONY A7CR  /  2.01', fg='#73d4bc', bg='#101722', font=('Segoe UI', 11, 'bold')).pack(anchor='w')
    tk.Label(shell, text='備份、安裝、還原。', fg='#f4f7fb', bg='#101722', font=('Microsoft JhengHei UI', 25, 'bold')).pack(anchor='w', pady=(12, 6))
    tk.Label(shell, text='先保存這台相機的資料，再處理後續安裝。', fg='#b8c5d6', bg='#101722').pack(anchor='w')
    card = tk.Frame(shell, bg='#1c2838', padx=22, pady=18)
    card.pack(fill='x', pady=(24, 0))
    tk.Label(card, text='目前開放：唯讀雙備份與自動準備', fg='#ffffff', bg='#1c2838', font=('Microsoft JhengHei UI', 13, 'bold')).pack(anchor='w')
    tk.Label(card, text='安裝與還原仍未開放：尚缺 Windows A7CR 的實機寫入與復原驗證。', fg='#f3cd83', bg='#1c2838', wraplength=650, justify='left').pack(anchor='w', pady=(8, 0))
    tk.Label(card, text='備份只讀取相機，不寫入韌體；Windows 連線仍待實機驗證。\n備份不是完整晶片映像，也不能保證故障後一定可救回。', fg='#bdcadb', bg='#1c2838', justify='left').pack(anchor='w', pady=(10, 0))
    status = tk.StringVar(value='請先使用「自動備份」。不需安裝 Python 或輸入指令。')
    tk.Label(shell, textvariable=status, fg='#f4f7fb', bg='#101722', wraplength=700, justify='left').pack(anchor='w', pady=(23, 10))
    progress = ttk.Progressbar(shell, style='A7.Horizontal.TProgressbar', mode='determinate', maximum=100)
    progress.pack(fill='x')
    device_controls = tk.Frame(shell, bg='#101722')
    device_controls.pack(fill='x', pady=(22, 0))
    identify = tk.Button(device_controls, text='唯讀連線檢查', relief='flat', padx=12, pady=12)
    identify.pack(side='left', padx=(0, 10))
    backup = tk.Button(device_controls, text='自動備份（唯讀）', bg='#73d4bc', fg='#10251f',
                       relief='flat', padx=18, pady=12, font=('Microsoft JhengHei UI', 12, 'bold'))
    backup.pack(side='left')
    tk.Button(device_controls, text='安裝 FL2 / FL3', state='disabled', padx=18, pady=12).pack(side='left', padx=10)
    tk.Button(device_controls, text='還原刷入前狀態', state='disabled', padx=18, pady=12).pack(side='left')
    controls = tk.Frame(shell, bg='#101722')
    controls.pack(fill='x', pady=(22, 0))
    events = queue.Queue()
    cancel = threading.Event()
    active = [False]
    active_action = ['prepare']
    close_pending = [False]
    last_folder = [data_root()]
    start = tk.Button(controls, text='準備安裝資料（離線）', bg='#73d4bc', fg='#10251f', activebackground='#9ae7d3', relief='flat', padx=20, pady=12, font=('Microsoft JhengHei UI', 12, 'bold'), cursor='hand2')
    start.pack(side='left')
    stop = tk.Button(controls, text='停止', state='disabled', padx=16, pady=12, relief='flat')
    stop.pack(side='left', padx=10)
    folder = tk.Button(controls, text='開啟資料夾', padx=16, pady=12, relief='flat')
    folder.pack(side='right')
    tk.Label(shell, text='非官方修改無法保證零變磚風險。本版不允許實機寫入。', fg='#92a4ba', bg='#101722', wraplength=700, justify='left').pack(anchor='w', pady=(22, 0))
    tk.Label(shell, text='A7CR Tools  ' + VERSION, fg='#60768f', bg='#101722', font=('Segoe UI', 9)).pack(anchor='w', pady=(12, 0))
    def work(action):
        try:
            if action == 'identify':
                from a7cr_easy import run_identify_worker
                run_identify_worker(data_root(), lambda *event: events.put(event))
            elif action == 'backup':
                from a7cr_easy import run_backup_worker
                run_backup_worker(data_root(), lambda *event: events.put(event), allow_driver_setup=True)
            else:
                prepare(data_root(), reference_root(), cancel, lambda *event: events.put(event))
        except Cancelled as exc:
            events.put(('cancelled', str(exc), 0))
        except BaseException as exc:
            events.put(('error', str(exc), 0))
        finally:
            events.put(('ended', '', 0))
    def begin(action='prepare'):
        if active[0]:
            return
        if action == 'backup' and not messagebox.askokcancel('開始唯讀備份',
                '請確認相機是 A7CR 2.01，USB 設為大量儲存並直接連接電腦。\n電池請先充足。這次不會刷機。\n\n按確定亦允許準備相機 054C:0E86／0336 的 Windows WinUSB 驅動，可能出現管理員提示。\n備份為同一磁碟雙副本，不是完整晶片救援映像。', parent=root):
            return
        cancel.clear(); active[0] = True; active_action[0] = action
        start.configure(state='disabled'); backup.configure(state='disabled'); identify.configure(state='disabled')
        stop.configure(state='normal' if action == 'prepare' else 'disabled')
        threading.Thread(target=work, args=(action,), daemon=True).start()
    def stop_work():
        cancel.set(); stop.configure(state='disabled'); status.set('正在停止，請稍候…')
    def open_folder():
        path = last_folder[0]
        if not path.exists():
            messagebox.showinfo('尚無資料', '開始準備後，工具會自動建立資料夾。', parent=root)
            return
        try:
            os.startfile(path)
        except OSError:
            messagebox.showinfo('Windows 無法開啟資料夾',
                '自動開啟被 Windows 拒絕。資料仍保存在：\n\n' + str(path), parent=root)
    identify.configure(command=lambda: begin('identify'))
    backup.configure(command=lambda: begin('backup'))
    start.configure(command=begin); stop.configure(command=stop_work); folder.configure(command=open_folder)
    def pump():
        try:
            while True:
                kind, text, fraction = events.get_nowait()
                if kind == 'workspace':
                    last_folder[0] = Path(text)
                elif kind == 'ended':
                    active[0] = False; start.configure(state='normal'); backup.configure(state='normal'); identify.configure(state='normal'); stop.configure(state='disabled')
                    if close_pending[0]:
                        root.destroy(); return
                else:
                    status.set(text); progress['value'] = fraction * 100
                    if kind == 'error':
                        messagebox.showerror('操作未完成', text, parent=root)
        except queue.Empty:
            pass
        root.after(120, pump)
    def close():
        if active[0] and active_action[0] in ('backup', 'identify'):
            close_pending[0] = True
            status.set('相機讀取中，結束後會自動關閉。請勿拔線或關機。')
            return
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
