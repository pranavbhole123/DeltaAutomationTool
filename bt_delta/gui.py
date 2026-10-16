"""Compact desktop front end. Reports stay on disk; Perforce work uses a worker."""

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
from .comparison import compare_changelist, parse_changelists, save_comparison
from .executor import execute
from .perforce import P4CLI
from .planner import Planner, canonical, compact_summary, digest, save_plan

BASE = Path(__file__).resolve().parent.parent


def generate_reports(config):
    """Save the plan and optional CL comparison without displaying report contents."""
    options = config.get("comparison", {})
    if not isinstance(options, dict):
        raise ValueError("comparison must be an object in the saved inputs")
    numbers = (
        parse_changelists(options["changelists"]) if options.get("changelists") else []
    )
    source = options.get("source", "auto")
    if source not in ("auto", "submitted", "shelved", "workspace"):
        raise ValueError("Unsupported changelist content source")
    p4 = P4CLI(config["perforce"])
    plan = Planner(p4, config).build()
    folder = (
        BASE
        / "reports"
        / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + plan["digest"][:8])
    )
    output = save_plan(plan, folder)
    report_error = False
    if numbers:
        try:
            comparison = compare_changelist(
                p4, config, numbers, source=source, preview_plan=plan
            )
            save_comparison(comparison, folder)
        except Exception as exc:
            # An optional report must not discard the successfully saved review plan.
            logging.exception("Saved changelist comparison failed")
            (folder / "comparison-error.txt").write_text(
                str(exc) + "\n", encoding="utf-8"
            )
            report_error = True
    return {"plan": plan, "path": output, "report_error": report_error}


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
        super().__init__(root, padding=16)
        self.pack(fill="both", expand=True)
        self.root, self.plan, self.plan_path = root, None, None
        self.busy = False
        self.events = queue.Queue()
        self.values = {}
        self.approved = tk.BooleanVar(value=False)
        self.status = tk.StringVar(
            value="Enter project and Perforce details, then generate a plan."
        )
        ttk.Label(self, text="Delta Automation", font=("Segoe UI", 20, "bold")).pack(
            anchor="w"
        )
        toolbar = ttk.Frame(self)
        toolbar.pack(fill="x", pady=(10, 12))
        for label, command in (
            ("Load inputs", self.load_config),
            ("Save inputs", self.save_config),
        ):
            ttk.Button(toolbar, text=label, command=command).pack(
                side="left", padx=(0, 8)
            )
        self.plan_button = ttk.Button(
            toolbar, text="Generate plan", command=self.generate
        )
        self.plan_button.pack(side="right")
        self.tabs = ttk.Notebook(self)
        self.tabs.pack(fill="both", expand=True)
        self.templates = ttk.Frame(self.tabs, padding=12)
        self.review = ttk.Frame(self.tabs, padding=12)
        self.tabs.add(self.templates, text="1. Inputs")
        self.tabs.add(self.review, text="2. Review & approve")

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
        form = ttk.Frame(canvas)
        window = canvas.create_window((0, 0), window=form, anchor="nw")
        form.bind(
            "<Configure>", lambda _: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind(
            "<Configure>", lambda event: canvas.itemconfigure(window, width=event.width)
        )

        self.settings = ttk.LabelFrame(form, text="Perforce", padding=10)
        self.settings.pack(fill="x", pady=(0, 10))
        self.settings.columnconfigure(1, weight=1)
        for i, (key, label) in enumerate(
            [
                ("perforce.port", "Server (P4PORT)"),
                ("perforce.user", "User"),
                ("perforce.client", "Writable workspace"),
                ("perforce.executable", "p4 executable"),
            ]
        ):
            self.entry(self.settings, key, label, i, width=48)
        self.changelist = tk.StringVar()
        self.changelist.trace_add("write", lambda *_: self.invalidate())
        ttk.Label(self.settings, text="Changelists (optional)").grid(
            row=4, column=0, sticky="w", padx=(0, 14), pady=4
        )
        self.changelist_entry = ttk.Entry(
            self.settings, textvariable=self.changelist, width=48
        )
        self.changelist_entry.grid(row=4, column=1, sticky="ew", pady=4)
        ttk.Label(
            self.settings,
            text="Comma or space separated. Comparison results are saved with the reports.",
        ).grid(row=5, column=1, sticky="w", pady=(0, 3))
        ttk.Label(self.settings, text="Uses your existing p4 login ticket.").grid(
            row=6, column=1, sticky="w", pady=(3, 0)
        )
        self.input_form = ttk.LabelFrame(form, text="Project", padding=10)
        self.input_form.pack(fill="x", pady=(0, 10))
        self.input_form.columnconfigure(1, weight=1)
        overrides_frame = ttk.Frame(form)
        overrides_frame.pack(fill="x", pady=(0, 6))
        ttk.Label(overrides_frame, text="Exact depot paths (optional JSON)").pack(
            anchor="w", pady=(0, 4)
        )
        self.overrides = scrolledtext.ScrolledText(
            overrides_frame, height=2, font=("Consolas", 10), wrap="none"
        )
        self.overrides.pack(fill="x")
        self.overrides.bind("<<Modified>>", self.modified)

        self.summary_box = scrolledtext.ScrolledText(
            self.review, wrap="none", font=("Consolas", 10), state="disabled"
        )
        self.summary_box.pack(fill="both", expand=True)
        for tag, foreground, background in (
            ("diff_add", "#146c2e", "#e6f4ea"),
            ("diff_delete", "#b3261e", "#fce8e6"),
            ("diff_hunk", "#075985", "#e0f2fe"),
        ):
            self.summary_box.tag_configure(
                tag, foreground=foreground, background=background
            )
        ttk.Checkbutton(
            self.review,
            text="I reviewed these edits and the saved report. BLOCKED items stay untouched.",
            variable=self.approved,
            command=self.update_apply,
        ).pack(anchor="w", pady=(10, 5))
        self.apply_button = ttk.Button(
            self.review,
            text="Apply to pending changelist",
            command=self.apply,
            state="disabled",
        )
        self.apply_button.pack(anchor="e")
        ttk.Label(self, textvariable=self.status, wraplength=1050).pack(
            anchor="w", pady=(10, 0)
        )
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
        if hasattr(self, "apply_button"):
            self.apply_button.configure(state="disabled")

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
            if old == path and "comparison" in config:
                fresh["comparison"] = config["comparison"]
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
        comparison = config.get("comparison", {})
        if not isinstance(comparison, dict):
            raise ValueError("comparison must be an object in the saved inputs")
        path = str(
            Path(config.get("rules_file") or DEFAULT_SPEC).expanduser().resolve()
        )
        spec = spec or load_spec(path)
        self.catalog, self.active_rules_path = spec, path
        self.rules_path.set(path)
        self.rules_title.set(spec["title"] + "  ·  schema 2")
        self.extra_config = copy.deepcopy(config)
        self.extra_config["rules_file"] = path
        numbers = comparison.get("changelists", "")
        self.changelist.set(
            ", ".join(str(n) for n in numbers)
            if isinstance(numbers, list)
            else str(numbers or "")
        )
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
        comparison = result.get("comparison", {}).copy()
        numbers = self.changelist.get().strip()
        if numbers:
            try:
                comparison["changelists"] = parse_changelists(numbers)
            except ValueError:
                if checked:
                    raise
                comparison["changelists"] = numbers
        else:
            comparison.pop("changelists", None)
        if comparison:
            result["comparison"] = comparison
        else:
            result.pop("comparison", None)
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
        self.apply_button.configure(state="disabled")
        self.status.set(message)

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
                self.busy = False
                self.plan_button.configure(state="normal")
                if error:
                    self.status.set("Operation failed. See runtime.log for details.")
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
            self.status.set("Operation failed. See runtime.log for details.")
        finally:
            self.root.after(100, self.poll)

    def show_plan(self, result):
        self.plan, self.plan_path = result["plan"], result["path"]
        self.summary_box.configure(state="normal")
        self.summary_box.delete("1.0", "end")
        for line in compact_summary(self.plan).splitlines(keepends=True):
            self.summary_box.insert("end", line, review_line_tag(line) or ())
        self.summary_box.configure(state="disabled")
        self.approved.set(False)
        self.tabs.select(self.review)
        suffix = (
            " Some optional reports failed; see the saved folder."
            if result["report_error"]
            else ""
        )
        self.status.set(
            f"Plan ready. Reports saved in {self.plan_path.parent}.{suffix}"
        )

    def generate(self):
        if self.busy:
            return
        try:
            config = self.get_config()
            self.input_hash = digest(canonical(config))
            self.run(
                lambda: generate_reports(config),
                self.show_plan,
                "Generating plan and saving reports…",
            )
        except Exception as exc:
            messagebox.showerror("Inputs", str(exc))

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
                messagebox.showwarning(
                    "Applied changes; blocked work remains",
                    f"{change_text}\n\n{len(blocked)} blocked check(s) were left untouched. Details are saved in plan.txt and the execution journal.",
                )
            else:
                messagebox.showinfo("Pending changelist", self.status.get())

        self.run(
            lambda: execute(
                P4CLI(plan["connection"]),
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
    root.geometry("1120x820")
    root.minsize(900, 680)
    style = ttk.Style(root)
    if "clam" in style.theme_names():
        style.theme_use("clam")
    style.configure("TLabel", font=("Segoe UI", 10))
    style.configure("TButton", font=("Segoe UI", 10), padding=6)
    App(root)
    root.mainloop()
