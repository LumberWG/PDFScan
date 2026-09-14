"""PDFScan GUI — Windows 图形界面：选择 PDF 或目录批量红头切分，实时进度条。

用法: python gui.py
说明: 切分在后台线程执行，OCR 用多进程池；停止按钮在当前文件内终止 OCR（缓存已
完成的页，下次自动续跑）。依赖 tkinter（Python 官方 Windows 安装包自带）。
"""
import os, re, glob, sys, queue, threading, contextlib
import multiprocessing as mp
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import fitz

from segment_redhead import segment, SegmentCancelled

DEFAULT_OUT = r"D:\Backup\RayChan\split_redhead"
PDF_RE = re.compile(r"\.pdf$", re.I)


def pdf_pages(path):
    try:
        doc = fitz.open(path)
        n = doc.page_count
        doc.close()
        return n
    except Exception:
        return -1


class QueueWriter:
    """把 segment 的 print 输出转发到 GUI 日志队列（worker 线程 -> UI 线程）。"""
    def __init__(self, q):
        self.q = q

    def write(self, s):
        s = s.rstrip()
        if s:
            self.q.put(("log", s))

    def flush(self):
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDFScan — 红头文档切分工具")
        self.geometry("900x660")
        self.q = queue.Queue()
        self.stop_event = threading.Event()
        self.worker = None
        self._build()
        self.after(100, self._poll)

    # ---------- 界面 ----------
    def _build(self):
        top = ttk.Frame(self, padding=8)
        top.pack(fill="x")
        ttk.Button(top, text="添加 PDF 文件…", command=self.add_files).pack(side="left")
        ttk.Button(top, text="添加目录（自动批量）…", command=self.add_dir).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="移除选中", command=self.remove_selected).pack(side="left", padx=(6, 0))
        ttk.Button(top, text="清空列表", command=self.clear_files).pack(side="left", padx=(6, 0))

        mid = ttk.Frame(self, padding=(8, 0))
        mid.pack(fill="x")
        ttk.Label(mid, text="输出目录:").pack(side="left")
        self.out_var = tk.StringVar(value=DEFAULT_OUT)
        ttk.Entry(mid, textvariable=self.out_var).pack(side="left", fill="x", expand=True, padx=4)
        ttk.Button(mid, text="浏览…", command=self.choose_out).pack(side="left")
        self.force_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(mid, text="强制重切已存在", variable=self.force_var).pack(side="left", padx=(10, 0))

        act = ttk.Frame(self, padding=8)
        act.pack(fill="x")
        self.start_btn = ttk.Button(act, text="开始处理", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(act, text="停止", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(6, 0))
        ttk.Button(act, text="打开输出目录", command=self.open_out).pack(side="left", padx=(6, 0))

        cols = ("name", "pages", "status", "docs")
        lst = ttk.Frame(self, padding=(8, 0))
        lst.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(lst, columns=cols, show="headings", selectmode="extended")
        self.tree.heading("name", text="PDF 文件")
        self.tree.heading("pages", text="页数")
        self.tree.heading("status", text="状态")
        self.tree.heading("docs", text="切出文档")
        self.tree.column("name", width=430)
        self.tree.column("pages", width=60, anchor="center")
        self.tree.column("status", width=110, anchor="center")
        self.tree.column("docs", width=80, anchor="center")
        vsb = ttk.Scrollbar(lst, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")

        prog = ttk.Frame(self, padding=8)
        prog.pack(fill="x")
        self.cur_var = tk.StringVar(value="当前文件：—")
        ttk.Label(prog, textvariable=self.cur_var).pack(anchor="w")
        row = ttk.Frame(prog)
        row.pack(fill="x", pady=(2, 6))
        self.page_bar = ttk.Progressbar(row, maximum=100)
        self.page_bar.pack(side="left", fill="x", expand=True)
        self.page_pct = ttk.Label(row, text="0%", width=6)
        self.page_pct.pack(side="left", padx=(4, 0))
        ttk.Label(prog, text="总体进度:").pack(anchor="w")
        row2 = ttk.Frame(prog)
        row2.pack(fill="x", pady=(2, 0))
        self.all_bar = ttk.Progressbar(row2, maximum=100)
        self.all_bar.pack(side="left", fill="x", expand=True)
        self.all_pct = ttk.Label(row2, text="0%", width=6)
        self.all_pct.pack(side="left", padx=(4, 0))

        logf = ttk.Frame(self, padding=(8, 0, 8, 8))
        logf.pack(fill="both", expand=True)
        ttk.Label(logf, text="日志:").pack(anchor="w")
        self.log = tk.Text(logf, height=9, wrap="word", state="disabled")
        lsb = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lsb.set)
        self.log.pack(side="left", fill="both", expand=True)
        lsb.pack(side="left", fill="y")

    def _log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    # ---------- 文件列表 ----------
    def _paths(self):
        return [self.tree.item(i, "values")[0] for i in self.tree.get_children()]

    def _insert_file(self, path):
        path = os.path.abspath(path)
        if path in self._paths():
            return
        n = pdf_pages(path)
        self.tree.insert("", "end", values=(path, n if n >= 0 else "?", "等待", ""))

    def add_files(self):
        if self.worker:
            return
        for p in filedialog.askopenfilenames(title="选择 PDF 文件", filetypes=[("PDF 文件", "*.pdf")]):
            self._insert_file(p)

    def add_dir(self):
        if self.worker:
            return
        d = filedialog.askdirectory(title="选择目录（处理其中所有 PDF）")
        if not d:
            return
        pdfs = sorted(glob.glob(os.path.join(d, "*.pdf")))
        if not pdfs:
            messagebox.showinfo("PDFScan", "该目录下没有 PDF 文件")
            return
        for p in pdfs:
            self._insert_file(p)
        self._log(f"[+] 从目录加入 {len(pdfs)} 个 PDF: {d}")

    def remove_selected(self):
        if self.worker:
            return
        for i in self.tree.selection():
            self.tree.delete(i)

    def clear_files(self):
        if self.worker:
            return
        for i in self.tree.get_children():
            self.tree.delete(i)

    def choose_out(self):
        d = filedialog.askdirectory(title="选择输出目录")
        if d:
            self.out_var.set(d)

    def open_out(self):
        d = self.out_var.get().strip()
        if os.path.isdir(d):
            os.startfile(d)
        else:
            messagebox.showinfo("PDFScan", "输出目录不存在，处理后会自动创建")

    # ---------- 处理 ----------
    def start(self):
        if self.worker:
            return
        items = self.tree.get_children()
        if not items:
            messagebox.showinfo("PDFScan", "请先添加 PDF 文件或目录")
            return
        out_dir = self.out_var.get().strip()
        if not out_dir:
            messagebox.showinfo("PDFScan", "请设置输出目录")
            return
        files = [self.tree.item(i, "values")[0] for i in items]
        force = bool(self.force_var.get())
        self.stop_event.clear()
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.all_bar["value"] = 0
        self.all_pct.configure(text="0%")
        self.worker = threading.Thread(target=self._run, args=(files, out_dir, force), daemon=True)
        self.worker.start()

    def stop(self):
        if self.worker:
            self.stop_event.set()
            self._log("[*] 正在停止…（当前文件的 OCR 会立即终止，已完成的页已缓存可续跑）")

    def _set_status(self, idx, status, docs=""):
        item = self.tree.get_children()[idx]
        vals = list(self.tree.item(item, "values"))
        vals[2], vals[3] = status, docs
        self.tree.item(item, values=vals)

    def _run(self, files, out_dir, force):
        n = len(files)
        try:
            for i, path in enumerate(files):
                if self.stop_event.is_set():
                    self.q.put(("log", "[*] 已停止"))
                    break
                base = PDF_RE.sub("", os.path.basename(path))
                man = os.path.join(out_dir, base, "manifest.csv")
                self.q.put(("status", i, "检查中", ""))
                if os.path.exists(man) and not force:
                    self.q.put(("status", i, "跳过(已存在)", ""))
                    self.q.put(("log", f"[SKIP] {base}（已存在，勾选“强制重切”可重切）"))
                    self.q.put(("overall", i + 1, n, 1.0))
                    continue
                self.q.put(("status", i, "处理中…", ""))
                self.q.put(("current", base))

                def cb(done, total):
                    self.q.put(("page", done, total))
                    self.q.put(("overall", i, n, done / total if total else 1.0))
                    return not self.stop_event.is_set()

                try:
                    with contextlib.redirect_stdout(QueueWriter(self.q)):
                        manifest = segment(path, out_dir, progress_cb=cb)
                    self.q.put(("status", i, "完成", f"{len(manifest)} 篇"))
                    self.q.put(("log", f"[OK] {base}: 切出 {len(manifest)} 篇"))
                except SegmentCancelled:
                    self.q.put(("status", i, "已取消", ""))
                    self.q.put(("log", f"[CANCEL] {base}"))
                    break
                except Exception as e:
                    self.q.put(("status", i, "失败", str(e)[:60]))
                    self.q.put(("log", f"[ERROR] {base}: {e}"))
                self.q.put(("overall", i + 1, n, 1.0))
        finally:
            self.q.put(("done",))

    # ---------- 队列轮询（线程安全地刷新 UI） ----------
    def _poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind = msg[0]
                if kind == "log":
                    self._log(msg[1])
                elif kind == "status":
                    self._set_status(msg[1], msg[2], msg[3])
                elif kind == "current":
                    self.cur_var.set(f"当前文件：{msg[1]}")
                elif kind == "page":
                    pct = msg[1] / msg[2] * 100 if msg[2] else 0
                    self.page_bar["value"] = pct
                    self.page_pct.configure(text=f"{pct:.0f}%  ({msg[1]}/{msg[2]})")
                elif kind == "overall":
                    i, n, frac = msg[1], msg[2], msg[3]
                    pct = (i + frac) / n * 100
                    self.all_bar["value"] = pct
                    self.all_pct.configure(text=f"{pct:.0f}%  ({i + (1 if frac >= 1 else 0)}/{n})")
                elif kind == "done":
                    self.worker = None
                    self.start_btn.configure(state="normal")
                    self.stop_btn.configure(state="disabled")
                    self.cur_var.set("当前文件：—")
                    self.page_bar["value"] = 0
                    self.page_pct.configure(text="0%")
                    self._log("[DONE] 全部处理完毕")
        except queue.Empty:
            pass
        self.after(100, self._poll)


def main():
    mp.freeze_support()   # Windows 打包/多进程必需
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
