from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import logging

from .config import load
from .comparison import compare_changelist, comparison_summary, save_comparison
from .demo import demo_plan
from .executor import execute
from .perforce import P4CLI
from .planner import Planner, save_plan, summary


def main(argv=None):
    parser = argparse.ArgumentParser(description="SLSI Bluetooth delta: inspect, review, approve, apply")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("gui", help="Open the desktop tool")
    demo = commands.add_parser("demo", help="Create a synthetic offline plan; cannot apply")
    demo.add_argument("--out", default="reports/demo")
    plan = commands.add_parser("plan", help="Read-only Perforce inspection and diff")
    plan.add_argument("config")
    plan.add_argument("--catalog", help="Optional JSON rule catalog")
    plan.add_argument("--out", default="reports/latest")
    compare = commands.add_parser("compare", help="Read-only developer changelists versus blank-plan comparison")
    compare.add_argument("config")
    compare.add_argument("changelists", nargs="+", help="One or more changelist numbers (comma-separated numbers also accepted)")
    compare.add_argument("--source", choices=("auto", "submitted", "shelved", "workspace"), default="auto")
    compare.add_argument("--catalog", help="Optional JSON rule catalog")
    compare.add_argument("--out", default="reports/comparison")
    apply = commands.add_parser("apply", help="Apply an explicitly approved saved plan")
    apply.add_argument("plan")
    apply.add_argument("--approve", required=True, help="Full SHA-256 printed in the reviewed plan")
    apply.add_argument("--acknowledge-reviews", action="store_true", help="Confirm you reviewed every REVIEW item")
    apply.add_argument("--acknowledge-blocked", action="store_true", help="Apply planned changes while leaving BLOCKED checks untouched")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.StreamHandler(), logging.FileHandler(Path(__file__).resolve().parent.parent / "runtime.log", encoding="utf-8")])
    try:
        if args.command == "gui":
            from .gui import launch
            launch()
            return 0
        if args.command == "apply":
            source = Path(args.plan)
            saved = json.loads(source.read_text(encoding="utf-8"))
            result = execute(P4CLI(saved["connection"]), saved, args.approve,
                             acknowledge_reviews=args.acknowledge_reviews,
                             acknowledge_blocked=args.acknowledge_blocked,
                             journal_path=source.parent / ("execution-" + saved["digest"][:12] + ".json"))
            print(json.dumps(result, indent=2))
            return 0
        if args.command == "compare":
            config = load(args.config)
            catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8")) if args.catalog else None
            report = compare_changelist(P4CLI(config["perforce"]), config, " ".join(args.changelists),
                                       source=args.source, catalog=catalog)
            output = save_comparison(report, args.out)
            print(comparison_summary(report))
            print(f"Saved: {output.resolve()}")
            return 2 if report["incomplete"] else 0
        if args.command == "demo":
            result = demo_plan(args.out)
        else:
            config = load(args.config)
            catalog = json.loads(Path(args.catalog).read_text(encoding="utf-8")) if args.catalog else None
            result = Planner(P4CLI(config["perforce"]), config, catalog).build()
        output = save_plan(result, args.out)
        print(summary(result))
        print(f"Saved: {output.resolve()}")
        return 2 if any(c["status"] == "blocked" for c in result["checks"]) else 0
    except Exception as exc:
        logging.exception("Command failed")
        print(f"Error: {exc}", file=sys.stderr)
        return 1
