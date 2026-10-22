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
from .selection import ReviewSelection

BASE = Path(__file__).resolve().parent.parent


def generate_reports(config):
    """Save a normal review plan and its detailed reports."""
    plan = Planner(P4CLI(config["perforce"]), config).build()
    folder = (
        BASE
        / "reports"
        / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-" + plan["digest"][:8])
    )
    return {"plan": plan, "path": save_plan(plan, folder)}


def save_checklist_comparison(config):
    """Run the independent read-only comparison and save its complete report."""
    options = config.get("comparison", {})
    numbers = parse_changelists(options.get("changelists", []))
    report = compare_changelist(
        P4CLI(config["perforce"]), config, numbers, source=options.get("source", "auto")
    )
    folder = (
        BASE
        / "reports"
        / (datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-compare-" + numbers[0])
    )
    return save_comparison(report, folder)


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
        self.selection, self.review_plan = None, None
        self.file_choices, self.edit_choices, self.choice_widgets = {}, {}, []
        self.file_widgets = {}
        self.refreshing_selection = False
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
        self.compare_button = ttk.Button(
            self.settings,
            text="Check checklist differences",
            command=self.check_differences,
        )
        self.compare_button.grid(row=5, column=1, sticky="w", pady=(4, 4))
        ttk.Label(
            self.settings,
            text="Comma or space separated. Results are saved to disk.",
        ).grid(row=6, column=1, sticky="w", pady=(0, 3))
        ttk.Label(self.settings, text="Uses your existing p4 login ticket.").grid(
            row=7, column=1, sticky="w", pady=(3, 0)
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

        review_panes = ttk.Panedwindow(self.review, orient="horizontal")
        review_panes.pack(fill="both", expand=True)
        selection_pane = ttk.Frame(review_panes, width=340)
        diff_pane = ttk.Frame(review_panes)
        review_panes.add(selection_pane, weight=1)
        review_panes.add(diff_pane, weight=3)
        ttk.Label(selection_pane, text="Include in pending changelist", font=("Segoe UI", 11, "bold")).pack(anchor="w")
        ttk.Label(selection_pane, text="Select files, flags or sections. Connected syntax stays together.", wraplength=310).pack(anchor="w", pady=(4, 8))
        selection_toolbar = ttk.Frame(selection_pane)
        selection_toolbar.pack(fill="x")
        for label, value in (("Select all", True), ("Clear all", False)):
            button = ttk.Button(selection_toolbar, text=label, command=lambda value=value: self.select_all_edits(value), state="disabled")
            button.pack(side="left", padx=(0, 5))
            self.choice_widgets.append(button)
        self.selection_count = tk.StringVar(value="Generate a plan to choose edits.")
        ttk.Label(selection_pane, textvariable=self.selection_count, wraplength=310).pack(anchor="w", pady=8)
        choice_container = ttk.Frame(selection_pane)
        choice_container.pack(fill="both", expand=True)
        self.choice_canvas = tk.Canvas(choice_container, highlightthickness=0, width=320)
        choice_scroll = ttk.Scrollbar(choice_container, orient="vertical", command=self.choice_canvas.yview)
        self.choice_canvas.configure(yscrollcommand=choice_scroll.set)
        choice_scroll.pack(side="right", fill="y")
        self.choice_canvas.pack(side="left", fill="both", expand=True)
        self.choice_form = ttk.Frame(self.choice_canvas)
        choice_window = self.choice_canvas.create_window((0, 0), window=self.choice_form, anchor="nw")
        self.choice_form.bind("<Configure>", lambda _: self.choice_canvas.configure(scrollregion=self.choice_canvas.bbox("all")))
        self.choice_canvas.bind("<Configure>", lambda event: self.choice_canvas.itemconfigure(choice_window, width=event.width))
        self.summary_box = scrolledtext.ScrolledText(
            diff_pane, wrap="none", font=("Consolas", 10), state="disabled"
        )
        self.summary_box.pack(fill="both", expand=True)
        diff_scroll = ttk.Scrollbar(diff_pane, orient="horizontal", command=self.summary_box.xview)
        self.summary_box.configure(xscrollcommand=diff_scroll.set)
        diff_scroll.pack(fill="x")
        for tag, foreground, background in (
            ("diff_add", "#146c2e", "#e6f4ea"),
            ("diff_delete", "#b3261e", "#fce8e6"),
            ("diff_hunk", "#075985", "#e0f2fe"),
        ):
            self.summary_box.tag_configure(
                tag, foreground=foreground, background=background
            )
        self.approval_checkbox = ttk.Checkbutton(
            self.review,
            text="I approve the checked edits shown here and reviewed the saved report. BLOCKED items stay untouched.",
            variable=self.approved,
            command=self.update_apply,
        )
        self.approval_checkbox.pack(anchor="w", pady=(10, 5))
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
        self.compare_button.configure(state="disabled")
        self.apply_button.configure(state="disabled")
        self.set_selection_enabled(False)
        self.approval_checkbox.configure(state="disabled")
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
                self.compare_button.configure(state="normal")
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
            self.compare_button.configure(state="normal")
            self.status.set("Operation failed. See runtime.log for details.")
        finally:
            if not self.busy:
                self.set_selection_enabled(self.selection is not None and self.plan is not None)
                self.approval_checkbox.configure(state="normal")
            self.root.after(100, self.poll)

    def show_plan(self, result):
        selection = ReviewSelection(result["plan"])
        self.plan, self.plan_path = result["plan"], result["path"]
        self.selection = selection
        self.file_choices, self.edit_choices = {}, {}
        self.file_widgets = {}
        self.choice_widgets = self.choice_widgets[:2]
        for widget in self.choice_form.winfo_children():
            widget.destroy()
        for change in self.plan["changes"]:
            path = change["path"]
            frame = ttk.Frame(self.choice_form, padding=(2, 6))
            frame.pack(fill="x")
            variable = tk.IntVar(value=1)
            self.file_choices[path] = variable
            button = ttk.Checkbutton(frame, text=path.rsplit("/", 1)[-1], variable=variable, command=lambda path=path: self.toggle_file(path))
            button.pack(anchor="w")
            self.choice_widgets.append(button)
            self.file_widgets[path] = button
            ttk.Label(frame, text=path, wraplength=300, font=("Segoe UI", 8)).pack(anchor="w", padx=(20, 0))
            self.edit_choices[path] = {}
            for choice in selection.choices[path]:
                variable = tk.BooleanVar(value=True)
                self.edit_choices[path][choice.id] = variable
                row = ttk.Frame(frame)
                row.pack(fill="x", padx=(20, 0))
                button = ttk.Checkbutton(row, variable=variable)
                button.pack(side="left", anchor="n")
                # A separate wrapping label makes long statements readable and
                # remains clickable, just like the checkbox beside it.
                label = ttk.Label(row, text=choice.label, wraplength=260)
                label.pack(side="left", anchor="w")
                label.bind("<Button-1>", lambda _, button=button: button.invoke())
                self.choice_widgets.append(button)
                variable.trace_add("write", lambda *_: self.selection_changed())
        self.choice_canvas.yview_moveto(0)
        self.selection_changed()
        self.set_selection_enabled(not self.busy)
        self.tabs.select(self.review)
        self.status.set(f"Plan ready. Choose the edits to include. Reports saved in {self.plan_path.parent}.")

    def set_selection_enabled(self, enabled):
        for widget in self.choice_widgets:
            widget.state(["!disabled" if enabled else "disabled"])

    def selected_edits(self):
        return {path: {key for key, variable in choices.items() if variable.get()} for path, choices in self.edit_choices.items()}

    def toggle_file(self, path):
        if self.busy:
            return
        self.refreshing_selection = True
        try:
            for variable in self.edit_choices[path].values():
                variable.set(self.file_choices[path].get() == 1)
        finally:
            self.refreshing_selection = False
        self.selection_changed()

    def select_all_edits(self, selected):
        if self.busy or self.selection is None:
            return
        self.refreshing_selection = True
        try:
            for choices in self.edit_choices.values():
                for variable in choices.values():
                    variable.set(selected)
        finally:
            self.refreshing_selection = False
        self.selection_changed()

    def selection_changed(self):
        if self.refreshing_selection or self.selection is None:
            return
        selected = self.selected_edits()
        self.review_plan = self.selection.plan_for(selected)
        for path, choices in self.edit_choices.items():
            count = len(selected[path])
            self.file_choices[path].set(0 if not count else 1 if count == len(choices) else -1)
            self.file_widgets[path].configure(text=f"{path.rsplit('/', 1)[-1]} ({count}/{len(choices)})")
            self.file_widgets[path].state(["alternate" if 0 < count < len(choices) else "!alternate"])
        count = sum(len(ids) for ids in selected.values())
        total = sum(len(choices) for choices in self.edit_choices.values())
        self.selection_count.set(f"{len(self.review_plan['changes'])} of {len(selected)} files; {count} of {total} edits selected.")
        self.render_review()
        self.approved.set(False)
        self.update_apply()

    def render_review(self):
        position = self.summary_box.yview()[0]
        self.summary_box.configure(state="normal")
        self.summary_box.delete("1.0", "end")
        for line in compact_summary(self.review_plan).splitlines(keepends=True):
            self.summary_box.insert("end", line, review_line_tag(line) or ())
        self.summary_box.configure(state="disabled")
        self.summary_box.yview_moveto(position)

    def check_differences(self):
        if self.busy:
            return
        try:
            config = self.get_config()
            parse_changelists(config.get("comparison", {}).get("changelists", []))
            self.run(
                lambda: save_checklist_comparison(config),
                lambda output: self.status.set(
                    f"Checklist comparison saved to {output}"
                ),
                "Checking checklist differences and saving the report…",
            )
        except Exception as exc:
            messagebox.showerror("Checklist comparison", str(exc))

    def generate(self):
        if self.busy:
            return
        try:
            config = self.get_config()
            self.input_hash = digest(canonical(config))
            self.plan, self.review_plan, self.selection = None, None, None
            self.approved.set(False)
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
            and self.review_plan
            and self.review_plan["changes"]
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
        plan = copy.deepcopy(self.review_plan)
        folder = self.plan_path.parent / ("selected-" + plan["digest"][:12])
        journal = folder / ("execution-" + plan["digest"][:12] + ".json")

        def work():
            save_plan(plan, folder)
            return execute(
                P4CLI(plan["connection"]), plan, plan["digest"],
                acknowledge_reviews=True, acknowledge_blocked=True, journal_path=journal,
            )

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
            work,
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
