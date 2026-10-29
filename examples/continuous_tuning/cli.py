"""Command line surface: tick on demand, show status and the learning curve, build the pack.

    python -m examples.continuous_tuning.cli tick --root RUNS --base recipes/BASE.json
    python -m examples.continuous_tuning.cli curve --root RUNS --base recipes/BASE.json
    python -m examples.continuous_tuning.cli pack --output examples/continuous_tuning/pack
    python -m examples.continuous_tuning.cli export-tools

`tick` with `--backend hf` needs the optional worker environment (torch, transformers,
peft) and the base weights at `--base-path` or $TUNING_BASE_PATH. `--backend fixture`
runs the whole loop on CPU with a deterministic stand-in model.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).parent


def backend_factory(kind: str, base_noun: Path, base_path: Path | None):
    cache: dict[str, Any] = {}

    def make() -> Any:
        if "backend" not in cache:
            base = json.loads(base_noun.read_bytes())
            if kind == "fixture":
                from .fixture_backend import FixtureBackend
                from .tools import load_tools

                spec = json.loads((HERE / "learner_spec.json").read_bytes())
                cache["backend"] = FixtureBackend(load_tools(), spec["data_plan"])
            else:
                from .backend_hf import HFBackend

                path = base_path or Path(os.environ.get(base["source"]["path_env"], ""))
                if not path or not (path / "config.json").is_file():
                    raise ValueError(
                        f"base weights not found; set --base-path or ${base['source']['path_env']}"
                    )
                cache["backend"] = HFBackend(base, path)
        return cache["backend"]

    return make


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    commands = parser.add_subparsers(dest="command", required=True)
    tick = commands.add_parser("tick", help="evaluate the trigger and run at most one round")
    serve = commands.add_parser("serve-check", help="one base, several adapters attached by name")
    curve = commands.add_parser("curve", help="print or write the learning curve")
    for sub in (tick, serve, curve):
        sub.add_argument("--root", type=Path, required=True, help="private run directory")
        sub.add_argument("--base", type=Path, required=True, help="base-model noun JSON")
    for sub in (tick, serve):
        sub.add_argument("--base-path", type=Path)
        sub.add_argument("--backend", choices=["hf", "fixture"], default="hf")
        sub.add_argument("--max-verbs", type=int, default=64)
    serve.add_argument("--max-adapters", type=int, default=3)
    curve.add_argument("--output", type=Path)
    pack = commands.add_parser("pack", help="write the seed pack (deterministic, offline)")
    pack.add_argument("--output", type=Path, default=HERE / "pack")
    tools = commands.add_parser(
        "export-tools", help="refresh the tool snapshot from the served definitions"
    )
    tools.add_argument("--repo", type=Path, default=HERE.parents[1])
    args = parser.parse_args(argv)
    if args.command in ("tick", "serve-check"):
        from .loop import tick as run_tick

        factory = backend_factory(args.backend, args.base, args.base_path)
        extra = (
            {"stage": "serve_check", "params": {"max_adapters": args.max_adapters}}
            if args.command == "serve-check"
            else {}
        )
        result = run_tick(
            args.root,
            args.base,
            factory,
            max_verbs=args.max_verbs,
            log=lambda line: print(line, file=sys.stderr, flush=True),
            **extra,
        )
        print(
            json.dumps(
                {k: v for k, v in result.items() if k != "verbs"}
                | {
                    "verbs": [
                        {k: v[k] for k in ("stage", "step", "ok", "status", "seconds", "error")}
                        for v in result["verbs"]
                    ]
                },
                indent=1,
            )
        )
    elif args.command == "curve":
        from .report import learning_curve

        curve_value = learning_curve(args.root, args.base)
        text = json.dumps(curve_value, indent=1)
        if args.output:
            if args.output.exists():
                raise SystemExit(f"{args.output} exists; evidence is never overwritten")
            args.output.write_text(text + "\n")
        print(text)
    elif args.command == "pack":
        from .pack import write_pack

        print(json.dumps(write_pack(args.output), indent=1))
    else:
        from .tools import write_snapshot

        snapshot = write_snapshot(args.repo)
        print(
            json.dumps({"tools": len(snapshot["tools"]), "tools_sha256": snapshot["tools_sha256"]})
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
