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

from .config import validate
from .spec import DEFAULT_SPEC, load_spec, get_value, set_value
from .comparison import (
    compare_changelist,
    comparison_summary,
    parse_changelists,
    save_comparison,
)
from .demo import demo_plan
from .executor import execute
from .perforce import P4CLI
from .planner import (
    Planner,
    canonical,
    compact_summary,
    digest,
    preview_summary,
    save_plan,
    summary,
)

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
        self.status = tk.StringVar(
            value="Enter project details, then generate a read-only plan."
        )
        ttk.Label(self, text="Delta Automation", font=("Segoe UI", 20, "bold")).pack(
            anchor="w"
        )
        ttk.Label(
            self,
            text="Template views → checklist checks → file diffs → your approval → pending changelist",
        ).pack(anchor="w", pady=(2, 12))
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(0, 10))
        for label, command in (
            ("Load inputs", self.load_config),
            ("Save inputs", self.save_config),
            ("Offline demo", self.demo),
        ):
            ttk.Button(toolbar, text=label, command=command).pack(
                side="left", padx=(0, 8)
            )
        self.plan_button = ttk.Button(
            toolbar, text="Generate plan (read-only)", command=self.generate
        )
        self.plan_button.pack(side="right")
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        self.templates = ttk.Frame(self.tabs, padding=14)
        self.settings = ttk.Frame(self.tabs, padding=14)
        self.review = ttk.Frame(self.tabs, padding=10)
        self.preview = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.templates, text="1. Project inputs")
        self.tabs.add(self.settings, text="2. Perforce")
        self.tabs.add(self.review, text="3. Review & approve")
        self.tabs.add(self.preview, text="4. Empty-file preview")
        self.log_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.log_tab, text="5. Live log")
        self.comparison_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.comparison_tab, text="6. Changelist comparison")
        self.comparison_results_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.comparison_results_tab, text="7. Comparison results")
        self.detail_tab = ttk.Frame(self.tabs, padding=10)
        self.tabs.add(self.detail_tab, text="8. Detailed report")
        self.detail_box = scrolledtext.ScrolledText(
            self.detail_tab, wrap="none", font=("Consolas", 10), state="disabled"
        )
        self.detail_box.pack(fill="both", expand=True)
        self.changelist = tk.StringVar()
        self.comparison_source = tk.StringVar(value="auto")
        self.comparison_status = tk.StringVar(
            value="Enter developer changelists to compare with tab 4 Empty-file preview."
        )
        self.changelist.trace_add("write", lambda *_: self.invalidate_comparison())
        self.comparison_source.trace_add(
            "write", lambda *_: self.invalidate_comparison()
        )
        self.log_box = scrolledtext.ScrolledText(
            self.log_tab, wrap="word", font=("Consolas", 10), state="disabled"
        )
        self.log_box.pack(fill="both", expand=True)
        self.rules_path = tk.StringVar(value=str(DEFAULT_SPEC))
        self.rules_title = tk.StringVar()
        chooser = ttk.Frame(self.templates)
        chooser.pack(fill="x", pady=(0, 8))
        ttk.Label(chooser, text="Rules file").pack(side="left", padx=(0, 8))
        self.rules_combo = ttk.Combobox(
            chooser,
            textvariable=self.rules_path,
            state="readonly",
            values=[
                str(p)
                for p in sorted((BASE / "checklist").glob("*.json"))
                if not p.name.endswith(".schema.json")
            ],
        )
        self.rules_combo.pack(side="left", fill="x", expand=True)
        self.rules_combo.bind("<<ComboboxSelected>>", lambda _: self.select_rules())
        ttk.Button(chooser, text="Browse…", command=self.choose_rules).pack(
            side="left", padx=6
        )
        ttk.Button(chooser, text="Reload", command=self.select_rules).pack(side="left")
        ttk.Label(self.templates, textvariable=self.rules_title).pack(
            anchor="w", pady=(0, 8)
        )
        container = ttk.Frame(self.templates)
        container.pack(fill="both", expand=True)
        canvas = tk.Canvas(container, highlightthickness=0)
        scroll = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        self.input_form = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=self.input_form, anchor="nw")
        self.input_form.bind(
            "<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind(
            "<Configure>", lambda event: canvas.itemconfigure(window, width=event.width)
        )
        self.input_form.columnconfigure(1, weight=1)
        ttk.Label(
            self.templates,
            text="Optional exact depot overrides: JSON object keyed by file IDs from the selected rules file.",
        ).pack(anchor="w", pady=(10, 4))
        self.overrides = scrolledtext.ScrolledText(
            self.templates, height=4, font=("Consolas", 10)
        )
        self.overrides.pack(fill="x")
        self.overrides.bind("<<Modified>>", self.modified)
        self.settings.columnconfigure(1, weight=1)
        labels = [
            ("perforce.port", "Perforce server (P4PORT)"),
            ("perforce.user", "Perforce user"),
            ("perforce.client", "Existing writable workspace"),
            ("perforce.executable", "p4 executable"),
        ]
        for i, (key, label) in enumerate(labels):
            self.entry(self.settings, key, label, i)
        ttk.Label(
            self.settings,
            text="Uses your existing p4 login ticket. Passwords are never requested or saved.",
        ).grid(row=4, column=0, columnspan=2, sticky="w", pady=5)
        self.summary_box = scrolledtext.ScrolledText(
            self.review, wrap="none", font=("Consolas", 10), state="disabled"
        )
        self.summary_box.pack(fill="both", expand=True)
        self.summary_box.tag_configure(
            "diff_add", foreground="#146c2e", background="#e6f4ea"
        )
        self.summary_box.tag_configure(
            "diff_delete", foreground="#b3261e", background="#fce8e6"
        )
        self.summary_box.tag_configure(
            "diff_hunk", foreground="#075985", background="#e0f2fe"
        )
        ttk.Checkbutton(
            self.review,
            text="I reviewed the proposed edits and REVIEW/BLOCKED reasons in Detailed report. BLOCKED items stay untouched.",
            variable=self.approved,
            command=self.update_apply,
        ).pack(anchor="w", pady=(10, 5))
        self.apply_button = ttk.Button(
            self.review,
            text="Apply reviewed changes to pending changelist",
            command=self.apply,
            state="disabled",
        )
        self.apply_button.pack(anchor="e")
        ttk.Label(self, textvariable=self.status, wraplength=1050).pack(
            anchor="w", pady=(10, 0)
        )
        self.preview_box = scrolledtext.ScrolledText(
            self.preview, wrap="none", font=("Consolas", 10), state="disabled"
        )
        self.preview_box.pack(fill="both", expand=True)
        self.build_comparison_tab()
        self.set_config(
            {"rules_file": str(DEFAULT_SPEC), "perforce": {"executable": "p4"}}
        )
        self.root.after(100, self.poll)

    def entry(self, parent, key, label, row, *, width=76):
        ttk.Label(parent, text=label).grid(
            row=row, column=0, sticky="w", padx=(0, 14), pady=4
        )
        variable = tk.StringVar()
        self.values[key] = variable
        variable.trace_add("write", lambda *_: self.invalidate())
        ttk.Entry(parent, textvariable=variable, width=width).grid(
            row=row, column=1, sticky="ew", pady=4
        )

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
            self.comparison_status.set(
                "Inputs changed. Generate a fresh comparison; the displayed report uses the previous inputs."
            )

    def build_comparison_tab(self):
        ttk.Label(self.comparison_tab, textvariable=self.rules_title).pack(
            anchor="w", pady=4
        )
        ttk.Label(
            self.comparison_tab,
            text="Uses the selected rules file and project inputs from tab 1, and the Perforce connection from tab 2.",
            wraplength=1000,
        ).pack(anchor="w", pady=4)
        controls = ttk.Frame(self.comparison_tab)
        controls.pack(fill="x", pady=6)
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="Developer changelists").grid(
            row=0, column=0, sticky="w"
        )
        ttk.Entry(controls, textvariable=self.changelist, width=45).grid(
            row=0, column=1, columnspan=3, sticky="ew", padx=8
        )
        ttk.Label(controls, text="Content source").grid(
            row=1, column=0, sticky="w", pady=6
        )
        ttk.Combobox(
            controls,
            textvariable=self.comparison_source,
            state="readonly",
            width=13,
            values=("auto", "submitted", "shelved", "workspace"),
        ).grid(row=1, column=1, sticky="w", padx=8)
        self.compare_button = ttk.Button(
            controls, text="Compare with Empty-file preview", command=self.compare
        )
        self.compare_button.grid(row=1, column=3, sticky="e", padx=8)
        ttk.Label(
            self.comparison_tab,
            text="Enter numbers separated by commas or spaces, e.g. 123456, 123457, 123458. Uses tab 4 previews, including files that already match. If no current preview exists, generates it and displays it in tab 4.",
            wraplength=1000,
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            self.comparison_tab,
            text="Each file shows tool suggestions, developer before/after edits, combined changelist changes and detailed mismatch reasons. Auto uses submitted content, then a shelf, then local workspace content. Unshelved files require the developer's configured local workspace.",
            wraplength=1000,
        ).pack(anchor="w", pady=(0, 4))
        ttk.Label(
            self.comparison_tab, textvariable=self.comparison_status, wraplength=1000
        ).pack(anchor="w", pady=(0, 6))
        ttk.Label(
            self.comparison_tab,
            text="Results open in tab 7. Suggestions absent from the selected edits are checked against the latest depot revision before being marked missing. Exclusions and presence checks follow the selected rules file.",
            wraplength=1000,
        ).pack(anchor="w", pady=6)
        ttk.Button(
            self.comparison_tab,
            text="View comparison results",
            command=lambda: self.tabs.select(self.comparison_results_tab),
        ).pack(anchor="w")
        ttk.Label(
            self.comparison_results_tab,
            textvariable=self.comparison_status,
            wraplength=1000,
        ).pack(anchor="w", pady=(0, 6))
        ttk.Button(
            self.comparison_results_tab,
            text="Back to comparison inputs",
            command=lambda: self.tabs.select(self.comparison_tab),
        ).pack(anchor="w", pady=(0, 6))
        horizontal = ttk.Scrollbar(self.comparison_results_tab, orient="horizontal")
        horizontal.pack(side="bottom", fill="x")
        self.comparison_box = scrolledtext.ScrolledText(
            self.comparison_results_tab,
            wrap="none",
            font=("Consolas", 10),
            state="disabled",
            xscrollcommand=horizontal.set,
        )
        horizontal.configure(command=self.comparison_box.xview)
        self.comparison_box.pack(fill="both", expand=True)
        for tag, foreground, background in (
            ("diff_add", "#146c2e", "#e6f4ea"),
            ("diff_delete", "#b3261e", "#fce8e6"),
            ("diff_hunk", "#075985", "#e0f2fe"),
        ):
            self.comparison_box.tag_configure(
                tag, foreground=foreground, background=background
            )

    def compare(self):
        if self.busy:
            return
        try:
            config = self.get_config()
            numbers, source = (
                parse_changelists(self.changelist.get()),
                self.comparison_source.get(),
            )
            preview_plan = copy.deepcopy(getattr(self, "preview_plan", None))
            if getattr(self, "preview_input_hash", None) != digest(canonical(config)):
                preview_plan = None
            captured = digest(
                canonical({"config": config, "numbers": numbers, "source": source})
            )

            def complete(report):
                self.comparison_report = report
                folder = (
                    BASE
                    / "reports"
                    / (
                        datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                        + "-compare-"
                        + report["changelists"][0]["number"]
                    )
                )
                output = save_comparison(report, folder)
                self.show_previews(report["plan"])
                self.preview_input_hash = digest(canonical(config))
                self.comparison_box.configure(state="normal")
                self.comparison_box.delete("1.0", "end")
                for line in comparison_summary(report).splitlines(keepends=True):
                    self.comparison_box.insert("end", line, review_line_tag(line) or ())
                self.comparison_box.configure(state="disabled")
                self.tabs.select(self.comparison_results_tab)
                self.comparison_status.set(
                    f"{'Incomplete comparison' if report['incomplete'] else 'Comparison saved'}: {output}"
                )
                try:
                    current = digest(
                        canonical(
                            {
                                "config": self.get_config(),
                                "numbers": parse_changelists(self.changelist.get()),
                                "source": self.comparison_source.get(),
                            }
                        )
                    )
                except Exception:
                    current = None
                if current != captured:
                    self.invalidate_comparison()
                self.status.set(f"Read-only changelist comparison saved to {output}")

            self.run(
                lambda: compare_changelist(
                    P4CLI(config["perforce"], progress=self.progress),
                    config,
                    numbers,
                    source=source,
                    preview_plan=preview_plan,
                ),
                complete,
                "Comparing tab 4 previews with selected edits and checking latest depot content for uncovered suggestions.",
            )
        except Exception as exc:
            messagebox.showerror("Changelist comparison", str(exc))

    def choose_rules(self):
        if self.busy:
            return
        path = filedialog.askopenfilename(
            title="Choose rule specification",
            initialdir=str(BASE / "checklist"),
            filetypes=[("JSON rules", "*.json")],
        )
        if path:
            self.rules_path.set(path)
            self.select_rules()

    def select_rules(self):
        old = getattr(self, "active_rules_path", str(DEFAULT_SPEC))
        if self.busy:
            self.rules_path.set(old)
            return
        self.rules_combo.configure(
            values=[
                str(p)
                for p in sorted((BASE / "checklist").glob("*.json"))
                if not p.name.endswith(".schema.json")
            ]
        )
        try:
            config = self.get_config(checked=False)
            path = str(Path(self.rules_path.get()).resolve())
            spec = load_spec(path)
            # Carry only inputs declared by the newly selected specification.
            fresh = {
                "rules_file": path,
                "perforce": config.get("perforce", {}),
                "paths": config.get("paths", {}) if old == path else {},
            }
            for key in spec["inputs"]:
                value = get_value(config, key)
                if value is not None:
                    set_value(fresh, key, value)
            self.set_config(fresh, spec=spec)
        except Exception as exc:
            self.rules_path.set(old)
            messagebox.showerror("Rules file", str(exc))

    def browse_input(self, key):
        path = filedialog.askopenfilename(
            title=self.catalog["inputs"][key].get("label", key)
        )
        if path:
            self.values[key].set(path)

    def set_config(self, config, *, spec=None):
        path = str(
            Path(config.get("rules_file") or DEFAULT_SPEC).expanduser().resolve()
        )
        spec = spec or load_spec(path)
        self.catalog, self.active_rules_path = spec, path
        self.rules_path.set(path)
        self.rules_title.set(spec["title"] + "  ·  schema 2")
        self.extra_config = copy.deepcopy(config)
        self.extra_config["rules_file"] = path
        for child in self.input_form.winfo_children():
            child.destroy()
        self.values = {
            key: value
            for key, value in self.values.items()
            if key.startswith("perforce.")
        }
        row = 0
        for key, field in spec["inputs"].items():
            if field.get("visible") is False:
                continue
            label = field.get("label", key) + (" *" if field.get("required") else "")
            kind = field["type"]
            if kind == "boolean":
                variable = tk.BooleanVar(value=False)
                self.values[key] = variable
                variable.trace_add("write", lambda *_: self.invalidate())
                ttk.Checkbutton(self.input_form, text=label, variable=variable).grid(
                    row=row, column=1, sticky="w", pady=4
                )
            elif kind == "choice":
                variable = tk.StringVar()
                self.values[key] = variable
                variable.trace_add("write", lambda *_: self.invalidate())
                ttk.Label(self.input_form, text=label).grid(
                    row=row, column=0, sticky="w", padx=(0, 14)
                )
                ttk.Combobox(
                    self.input_form,
                    textvariable=variable,
                    values=field["choices"],
                    state="readonly",
                ).grid(row=row, column=1, sticky="ew", pady=4)
            else:
                self.entry(self.input_form, key, label, row, width=48)
                if kind == "path":
                    ttk.Button(
                        self.input_form,
                        text="Browse…",
                        command=lambda key=key: self.browse_input(key),
                    ).grid(row=row, column=2, padx=4)
            row += 1
            if field.get("help"):
                ttk.Label(self.input_form, text=field["help"], wraplength=700).grid(
                    row=row, column=1, sticky="w", pady=(0, 5)
                )
                row += 1
        for key, variable in self.values.items():
            field = spec["inputs"].get(key, {})
            value = get_value(
                config,
                key,
                field.get("default", False if field.get("type") == "boolean" else ""),
            )
            variable.set(", ".join(value) if isinstance(value, list) else value)
        self.invalidate()
        self.preview_plan = None
        self.preview_input_hash = None
        self.overrides.delete("1.0", "end")
        self.overrides.insert("1.0", json.dumps(config.get("paths", {}), indent=2))

    def get_config(self, *, checked=True):
        result = copy.deepcopy(getattr(self, "extra_config", {}))
        result["rules_file"] = getattr(self, "active_rules_path", str(DEFAULT_SPEC))
        for key, variable in self.values.items():
            value = variable.get()
            if isinstance(value, str):
                value = value.strip()
            if self.catalog["inputs"].get(key, {}).get("type") == "list":
                value = [v for v in value.replace(",", " ").split() if v]
            set_value(result, key, value)
        result["paths"] = json.loads(self.overrides.get("1.0", "end").strip() or "{}")
        if checked:
            fresh_spec = load_spec(result["rules_file"])
            if fresh_spec != self.catalog:
                raise ValueError(
                    "Rules file changed on disk. Reload it in tab 1 and generate a fresh plan."
                )
            return validate(result, self.catalog)
        return result

    def load_config(self):
        if self.busy:
            return
        filename = filedialog.askopenfilename(
            filetypes=[("JSON inputs", "*.json")], initialdir=str(BASE / "examples")
        )
        if filename:
            try:
                config = json.loads(Path(filename).read_text(encoding="utf-8-sig"))
                if (
                    config.get("rules_file")
                    and not Path(config["rules_file"]).is_absolute()
                ):
                    config["rules_file"] = str(
                        (Path(filename).parent / config["rules_file"]).resolve()
                    )
                self.set_config(config)
            except Exception as exc:
                messagebox.showerror("Inputs", str(exc))

    def save_config(self):
        try:
            config = self.get_config(checked=False)
            filename = filedialog.asksaveasfilename(
                defaultextension=".json", filetypes=[("JSON inputs", "*.json")]
            )
            if filename:
                Path(filename).write_text(
                    json.dumps(config, indent=2), encoding="utf-8"
                )
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
                    messagebox.showerror("Delta Automation", error)
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
        self.events.put(
            (None, datetime.now().strftime("%H:%M:%S") + " " + message, None)
        )

    def show_plan(self, plan):
        self.plan = plan
        folder = (
            BASE
            / "reports"
            / (datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + plan["digest"][:8])
        )
        self.plan_path = save_plan(plan, folder)
        self.summary_box.configure(state="normal")
        self.summary_box.delete("1.0", "end")
        for line in compact_summary(plan).splitlines(keepends=True):
            tag = review_line_tag(line)
            self.summary_box.insert("end", line, tag if tag else ())
        self.summary_box.configure(state="disabled")
        self.detail_box.configure(state="normal")
        self.detail_box.delete("1.0", "end")
        self.detail_box.insert("1.0", summary(plan))
        self.detail_box.configure(state="disabled")
        self.show_previews(plan)
        self.preview_input_hash = self.input_hash
        self.approved.set(False)
        self.tabs.select(self.review)
        self.status.set(
            f"Plan saved to {self.plan_path}. Full reasons: Detailed report tab and plan.txt."
        )

    def show_previews(self, plan):
        self.preview_plan = copy.deepcopy(plan)
        self.preview_box.configure(state="normal")
        self.preview_box.delete("1.0", "end")
        self.preview_box.insert("1.0", preview_summary(plan))
        self.preview_box.configure(state="disabled")

    def generate(self):
        try:
            config = self.get_config()
            self.input_hash = digest(canonical(config))
            self.run(
                lambda: Planner(
                    P4CLI(config["perforce"], progress=self.progress), config
                ).build(),
                self.show_plan,
                "Reading template views and checklist files. No Perforce changes are being made.",
            )
        except Exception as exc:
            messagebox.showerror("Inputs", str(exc))

    def demo(self):
        self.input_hash = None
        self.run(
            lambda: demo_plan(BASE / "reports" / "demo"),
            self.show_plan,
            "Building a synthetic offline demonstration.",
        )

    def update_apply(self):
        allowed = (
            not self.busy
            and self.plan
            and self.plan["mode"] == "live"
            and self.approved.get()
        )
        try:
            allowed = (
                allowed and digest(canonical(self.get_config())) == self.input_hash
            )
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
            change_text = (
                f"Pending changelist: {result['change']}."
                if result["change"]
                else "No changelist was needed."
            )
            self.status.set(
                f"{result['status']}. {change_text} Journal: {journal if result['change'] else 'not created'}"
            )
            blocked = result.get("blocked", [])
            if blocked:
                details = "\n\n".join(
                    f"• {item['title']} ({item['source']})\n  {item['message']}"
                    for item in blocked
                )
                messagebox.showwarning(
                    "Applied changes; blocked work remains",
                    f"{change_text}\n\nThe following checks were not changed and still require manual work:\n\n{details}",
                )
            else:
                messagebox.showinfo("Pending changelist", self.status.get())

        self.run(
            lambda: execute(
                P4CLI(plan["connection"], progress=self.progress),
                plan,
                plan["digest"],
                acknowledge_reviews=True,
                acknowledge_blocked=True,
                journal_path=journal,
            ),
            complete,
            "Approval received. Checking revisions and workspace before applying exact changes.",
        )


def launch():
    root = tk.Tk()
    root.title("Delta Automation")
    root.geometry("1180x840")
    root.minsize(920, 720)
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("TLabel", font=("Segoe UI", 10))
    style.configure("TButton", font=("Segoe UI", 10), padding=6)
    App(root)
    root.mainloop()
