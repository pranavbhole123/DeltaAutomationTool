"""Small desktop front end. All Perforce work runs outside the UI thread."""
from __future__ import annotations

import copy
from datetime import datetime
import json
from pathlib import Path
import queue
import threading
import logging
import tkinter as tk
from tkinter import filedialog, messagebox, scrolledtext, ttk

from .config import parse_details, validate
from .comparison import compare_changelist, comparison_summary, parse_changelists, save_comparison
from .demo import demo_plan
from .executor import execute
from .perforce import P4CLI
from .planner import Planner, canonical, digest, preview_summary, save_plan, summary

BASE = Path(__file__).resolve().parent.parent


def review_line_tag(line):
    """Color only unified-diff content; keep ---/+++ file headers neutral."""
    if line.startswith("+") and not line.startswith("+++"):
        return "diff_add"
    if line.startswith("-") and not line.startswith("---"):
        return "diff_delete"
    if line.startswith("@@"):
        return "diff_hunk"
    return None


class App(ttk.Frame):
    def __init__(self, root):
        super().__init__(root, padding=18)
        self.pack(fill="both", expand=True)
        self.root, self.plan, self.plan_path = root, None, None
        self.busy = False
        self.events = queue.Queue()
        self.values = {}
        self.approved = tk.BooleanVar(value=False)
        self.jdm = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Enter project details, then generate a read-only plan.")
        ttk.Label(self, text="SLSI Bluetooth Delta", font=("Segoe UI", 20, "bold")).pack(anchor="w")
        ttk.Label(self, text="Template views → checklist checks → file diffs → your approval → pending changelist").pack(anchor="w", pady=(2, 12))
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 10))
        for label, command in (("Load inputs", self.load_config), ("Save inputs", self.save_config), ("Offline demo", self.demo)):
            ttk.Button(toolbar, text=label, command=command).pack(side="left", padx=(0, 8))
        self.plan_button = ttk.Button(toolbar, text="Generate plan (read-only)", command=self.generate)
        self.plan_button.pack(side="right")
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        self.templates = ttk.Frame(self.tabs, padding=14)
        self.settings = ttk.Frame(self.tabs, padding=14)
        self.review = ttk.Frame(self.tabs, padding=10)
        self.preview = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.templates, text="1. Templates")
        self.tabs.add(self.settings, text="2. Model & connection")
        self.tabs.add(self.review, text="3. Review & approve")
        self.tabs.add(self.preview, text="4. Empty-file preview")
        self.log_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.log_tab, text="5. Live log")
        self.comparison_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.comparison_tab, text="6. Changelist comparison")
        self.changelist = tk.StringVar()
        self.comparison_source = tk.StringVar(value="auto")
        self.comparison_status = tk.StringVar(value="Enter developer changelists to compare with the blank reference/checklist plan.")
        self.changelist.trace_add("write", lambda *_: self.invalidate_comparison())
        self.comparison_source.trace_add("write", lambda *_: self.invalidate_comparison())
        self.log_box = scrolledtext.ScrolledText(self.log_tab, wrap="word", font=("Consolas", 10), state="disabled")
        self.log_box.pack(fill="both", expand=True)
        self.templates.columnconfigure(1, weight=1)
        row = 0
        for role, title in (("current", "Current OS"), ("reference", "Reference OS")):
            ttk.Label(self.templates, text=title, font=("Segoe UI", 11, "bold")).grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 8))
            row += 1
            for field, label in (("system_template", "System template"), ("vendor_template", "Vendor template"), ("csc_path", "CSC depot path")):
                self.entry(self.templates, role + "." + field, label, row)
                row += 1
        ttk.Label(self.templates, text="Or paste C OS / Reference details in your original format:").grid(row=row, column=0, columnspan=2, sticky="w", pady=(12, 5))
        row += 1
        self.paste = scrolledtext.ScrolledText(self.templates, height=7, wrap="word", font=("Consolas", 10))
        self.paste.grid(row=row, column=0, columnspan=2, sticky="nsew")
        self.templates.rowconfigure(row, weight=1)
        row += 1
        ttk.Button(self.templates, text="Use pasted details", command=self.parse_paste).grid(row=row, column=1, sticky="e", pady=8)
        self.settings.columnconfigure(1, weight=1)
        labels = [("perforce.port", "Perforce server (P4PORT)"), ("perforce.user", "Perforce user"),
                  ("perforce.client", "Existing writable workspace"), ("perforce.executable", "p4 executable"),
                  ("model", "Model"), ("chipset", "Chipset (blank: infer reference)"), ("ap", "AP directory (blank: infer reference)"),
                  ("hcf_variant", "HCF model folder (blank: infer bluetooth.mk)"),
                  ("products", "TARGET_PRODUCT names (blank: infer selected HCF block)"),
                  ("firmware_sha256", "Approved firmware SHA-256 (optional)")]
        for i, (key, label) in enumerate(labels):
            self.entry(self.settings, key, label, i)
        ttk.Checkbutton(self.settings, text="JDM model: use /efs instead of /mnt/vendor/efs", variable=self.jdm, command=self.invalidate).grid(row=10, column=1, sticky="w", pady=5)
        ttk.Label(self.settings, text="Uses your existing p4 login ticket. Passwords are never requested or saved.").grid(row=11, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Label(self.settings, text='Optional exact depot overrides as JSON, e.g. {"current.system.floating_feature": "//depot/path/file.xml"}').grid(row=12, column=0, columnspan=2, sticky="w", pady=(8, 4))
        self.overrides = scrolledtext.ScrolledText(self.settings, height=5, font=("Consolas", 10))
        self.overrides.grid(row=13, column=0, columnspan=2, sticky="nsew")
        self.settings.rowconfigure(13, weight=1)
        self.overrides.bind("<<Modified>>", self.modified)
        self.summary_box = scrolledtext.ScrolledText(self.review, wrap="none", font=("Consolas", 10), state="disabled")
        self.summary_box.pack(fill="both", expand=True)
        self.summary_box.tag_configure("diff_add", foreground="#146c2e", background="#e6f4ea")
        self.summary_box.tag_configure("diff_delete", foreground="#b3261e", background="#fce8e6")
        self.summary_box.tag_configure("diff_hunk", foreground="#075985", background="#e0f2fe")
        ttk.Checkbutton(self.review, text="I reviewed the exact diffs and REVIEW items. I understand BLOCKED items will be left untouched.", variable=self.approved, command=self.update_apply).pack(anchor="w", pady=(10, 5))
        self.apply_button = ttk.Button(self.review, text="Apply reviewed changes to pending changelist", command=self.apply, state="disabled")
        self.apply_button.pack(anchor="e")
        ttk.Label(self, textvariable=self.status, wraplength=1050).pack(anchor="w", pady=(10, 0))
        self.preview_box = scrolledtext.ScrolledText(self.preview, wrap="none", font=("Consolas", 10), state="disabled")
        self.preview_box.pack(fill="both", expand=True)
        self.build_comparison_tab()
        self.set_config(json.loads((BASE / "examples" / "m36x.json").read_text(encoding="utf-8")))
        self.root.after(100, self.poll)

    def entry(self, parent, key, label, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 14), pady=4)
        variable = tk.StringVar()
        self.values[key] = variable
        variable.trace_add("write", lambda *_: self.invalidate())
        ttk.Entry(parent, textvariable=variable, width=76).grid(row=row, column=1, sticky="ew", pady=4)

    def modified(self, _event):
        if self.overrides.edit_modified():
            self.invalidate()
            self.overrides.edit_modified(False)

    def invalidate(self):
        self.approved.set(False)
        self.invalidate_comparison()
        if hasattr(self, "apply_button"):
            self.apply_button.configure(state="disabled")

    def invalidate_comparison(self):
        if getattr(self, "comparison_report", None):
            self.comparison_status.set("Inputs changed. Generate a fresh comparison; the displayed report uses the previous inputs.")

    def build_comparison_tab(self):
        inputs = ttk.Frame(self.comparison_tab)
        inputs.pack(fill="x")
        for column, (role, title) in enumerate((("current", "Current templates"), ("reference", "Reference templates"))):
            frame = ttk.LabelFrame(inputs, text=title, padding=8)
            frame.grid(row=0, column=column, sticky="nsew", padx=(0, 8) if column == 0 else 0)
            inputs.columnconfigure(column, weight=1)
            frame.columnconfigure(1, weight=1)
            for row, (field, label) in enumerate((("system_template", "System"), ("vendor_template", "Vendor"), ("csc_path", "CSC path"))):
                ttk.Label(frame, text=label).grid(row=row, column=0, sticky="w", padx=(0, 6), pady=3)
                ttk.Entry(frame, textvariable=self.values[role + "." + field]).grid(row=row, column=1, sticky="ew", pady=3)
        ttk.Label(self.comparison_tab, text="Template fields are shared with tab 1. Model, connection and path overrides come from tab 2.").pack(anchor="w", pady=(6, 4))
        controls = ttk.Frame(self.comparison_tab)
        controls.pack(fill="x", pady=6)
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="Developer changelists").grid(row=0, column=0, sticky="w")
        ttk.Entry(controls, textvariable=self.changelist, width=45).grid(row=0, column=1, columnspan=3, sticky="ew", padx=8)
        ttk.Label(controls, text="Content source").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Combobox(controls, textvariable=self.comparison_source, state="readonly", width=13,
                     values=("auto", "submitted", "shelved", "workspace")).grid(row=1, column=1, sticky="w", padx=8)
        self.compare_button = ttk.Button(controls, text="Generate blank plan & compare", command=self.compare)
        self.compare_button.grid(row=1, column=3, sticky="e", padx=8)
        ttk.Label(self.comparison_tab, text="Enter numbers separated by commas or spaces, e.g. 123456, 123457, 123458. Compares combined developer edits with blank-file content.", wraplength=1000).pack(anchor="w", pady=(0, 4))
        ttk.Label(self.comparison_tab, text="Auto uses submitted content, then a shelf, then local workspace content. Unshelved files require the developer's configured local workspace.", wraplength=1000).pack(anchor="w", pady=(0, 4))
        ttk.Label(self.comparison_tab, textvariable=self.comparison_status, wraplength=1000).pack(anchor="w", pady=(0, 6))
        self.comparison_box = scrolledtext.ScrolledText(self.comparison_tab, wrap="none", font=("Consolas", 10), state="disabled")
        self.comparison_box.pack(fill="both", expand=True)
        for tag, foreground, background in (("diff_add", "#146c2e", "#e6f4ea"), ("diff_delete", "#b3261e", "#fce8e6"), ("diff_hunk", "#075985", "#e0f2fe")):
            self.comparison_box.tag_configure(tag, foreground=foreground, background=background)

    def compare(self):
        if self.busy:
            return
        try:
            config = self.get_config()
            numbers, source = parse_changelists(self.changelist.get()), self.comparison_source.get()
            captured = digest(canonical({"config": config, "numbers": numbers, "source": source}))
            def complete(report):
                self.comparison_report = report
                folder = BASE / "reports" / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-compare-" + report["changelists"][0]["number"])
                output = save_comparison(report, folder)
                self.comparison_box.configure(state="normal")
                self.comparison_box.delete("1.0", "end")
                for line in comparison_summary(report).splitlines(keepends=True):
                    self.comparison_box.insert("end", line, review_line_tag(line) or ())
                self.comparison_box.configure(state="disabled")
                self.tabs.select(self.comparison_tab)
                self.comparison_status.set(f"{'Incomplete comparison' if report['incomplete'] else 'Comparison saved'}: {output}")
                try:
                    current = digest(canonical({"config": self.get_config(), "numbers": parse_changelists(self.changelist.get()),
                                                "source": self.comparison_source.get()}))
                except Exception:
                    current = None
                if current != captured:
                    self.invalidate_comparison()
                self.status.set(f"Read-only changelist comparison saved to {output}")
            self.run(lambda: compare_changelist(P4CLI(config["perforce"], progress=self.progress), config,
                                               numbers, source=source), complete,
                     "Generating blank-file content and comparing edits from the selected changelists.")
        except Exception as exc:
            messagebox.showerror("Changelist comparison", str(exc))

    def set_config(self, config):
        self.extra_config = copy.deepcopy(config)
        self.extra_config.pop("cp_template", None)
        for key, variable in self.values.items():
            if "." in key:
                section, field = key.split(".", 1)
                value = config.get(section, {}).get(field, "")
            else:
                value = config.get(key, "")
            variable.set(", ".join(value) if isinstance(value, list) else value)
        self.jdm.set(config.get("jdm", False))
        self.overrides.delete("1.0", "end")
        self.overrides.insert("1.0", json.dumps(config.get("paths", {}), indent=2))

    def get_config(self, *, checked=True):
        result = copy.deepcopy(getattr(self, "extra_config", {}))
        result.update({"perforce": {}, "current": {}, "reference": {}, "jdm": self.jdm.get()})
        for key, variable in self.values.items():
            value = variable.get().strip()
            if "." in key:
                section, field = key.split(".", 1)
                result[section][field] = value
            else:
                result[key] = value
        result["products"] = [x.strip() for x in result["products"].split(",") if x.strip()]
        result["paths"] = json.loads(self.overrides.get("1.0", "end").strip() or "{}")
        return validate(result) if checked else result

    def parse_paste(self):
        try:
            for key, value in parse_details(self.paste.get("1.0", "end")).items():
                if isinstance(value, dict):
                    for field, content in value.items():
                        self.values[key + "." + field].set(content)
                else:
                    self.values[key].set(value)
            self.status.set("Template details loaded. Check model and connection settings.")
        except Exception as exc:
            messagebox.showerror("Input", str(exc))

    def load_config(self):
        if self.busy:
            return
        filename = filedialog.askopenfilename(filetypes=[("JSON inputs", "*.json")], initialdir=str(BASE / "examples"))
        if filename:
            try:
                self.set_config(json.loads(Path(filename).read_text(encoding="utf-8-sig")))
            except Exception as exc:
                messagebox.showerror("Inputs", str(exc))

    def save_config(self):
        try:
            config = self.get_config(checked=False)
            filename = filedialog.asksaveasfilename(defaultextension=".json", filetypes=[("JSON inputs", "*.json")])
            if filename:
                Path(filename).write_text(json.dumps(config, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("Inputs", str(exc))

    def run(self, work, completed, message):
        if self.busy:
            return
        self.busy = True
        self.plan_button.configure(state="disabled")
        self.compare_button.configure(state="disabled")
        self.apply_button.configure(state="disabled")
        self.status.set(message)
        self.tabs.select(self.log_tab)
        def worker():
            try:
                self.events.put((completed, work(), None))
            except Exception as exc:
                logging.exception("Background operation failed")
                self.events.put((completed, None, str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        try:
            for _ in range(100):
                completed, result, error = self.events.get_nowait()
                if completed is None:
                    self.log_box.configure(state="normal")
                    self.log_box.insert("end", result + "\n")
                    self.log_box.see("end")
                    self.log_box.configure(state="disabled")
                    self.status.set(result)
                    continue
                self.busy = False
                self.plan_button.configure(state="normal")
                self.compare_button.configure(state="normal")
                if error:
                    self.progress("ERROR: " + error)
                    self.status.set(error)
                    messagebox.showerror("Bluetooth delta", error)
                else:
                    completed(result)
                self.update_apply()
        except queue.Empty:
            pass
        except Exception as exc:
            logging.exception("GUI result handling failed")
            self.busy = False
            self.plan_button.configure(state="normal")
            self.compare_button.configure(state="normal")
            self.status.set("ERROR: " + str(exc))
        finally:
            self.root.after(100, self.poll)

    def progress(self, message):
        self.events.put((None, datetime.now().strftime("%H:%M:%S") + " " + message, None))

    def show_plan(self, plan):
        self.plan = plan
        folder = BASE / "reports" / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + plan["digest"][:8])
        self.plan_path = save_plan(plan, folder)
        self.summary_box.configure(state="normal")
        self.summary_box.delete("1.0", "end")
        for line in summary(plan).splitlines(keepends=True):
            tag = review_line_tag(line)
            self.summary_box.insert("end", line, tag if tag else ())
        self.summary_box.configure(state="disabled")
        self.preview_box.configure(state="normal")
        self.preview_box.delete("1.0", "end")
        self.preview_box.insert("1.0", preview_summary(plan))
        self.preview_box.configure(state="disabled")
        self.approved.set(False)
        self.tabs.select(self.review)
        self.status.set(f"Plan saved to {self.plan_path}. Review diffs; blocked items will remain manual work.")

    def generate(self):
        try:
            config = self.get_config()
            self.input_hash = digest(canonical(config))
            self.run(lambda: Planner(P4CLI(config["perforce"], progress=self.progress), config).build(), self.show_plan,
                     "Reading template views and checklist files. No Perforce changes are being made.")
        except Exception as exc:
            messagebox.showerror("Inputs", str(exc))

    def demo(self):
        self.input_hash = None
        self.run(lambda: demo_plan(BASE / "reports" / "demo"), self.show_plan, "Building a synthetic offline demonstration.")

    def update_apply(self):
        allowed = not self.busy and self.plan and self.plan["mode"] == "live" and self.approved.get()
        try:
            allowed = allowed and digest(canonical(self.get_config())) == self.input_hash
        except Exception:
            allowed = False
        self.apply_button.configure(state="normal" if allowed else "disabled")

    def apply(self):
        self.update_apply()
        if str(self.apply_button["state"]) == "disabled":
            return
        plan = copy.deepcopy(self.plan)
        journal = self.plan_path.parent / ("execution-" + plan["digest"][:12] + ".json")
        def complete(result):
            self.approved.set(False)
            self.plan = None
            change_text = f"Pending changelist: {result['change']}." if result["change"] else "No changelist was needed."
            self.status.set(f"{result['status']}. {change_text} Journal: {journal if result['change'] else 'not created'}")
            blocked = result.get("blocked", [])
            if blocked:
                details = "\n\n".join(f"• {item['title']} ({item['source']})\n  {item['message']}" for item in blocked)
                messagebox.showwarning("Applied changes; blocked work remains",
                                       f"{change_text}\n\nThe following checks were not changed and still require manual work:\n\n{details}")
            else:
                messagebox.showinfo("Pending changelist", self.status.get())
        self.run(lambda: execute(P4CLI(plan["connection"], progress=self.progress), plan, plan["digest"], acknowledge_reviews=True,
                                 acknowledge_blocked=True, journal_path=journal),
                 complete, "Approval received. Checking revisions and workspace before applying exact changes.")


def launch():
    root = tk.Tk()
    root.title("SLSI Bluetooth Delta")
    root.geometry("1180x840")
    root.minsize(920, 720)
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("TLabel", font=("Segoe UI", 10))
    style.configure("TButton", font=("Segoe UI", 10), padding=6)
    App(root)
    root.mainloop()
