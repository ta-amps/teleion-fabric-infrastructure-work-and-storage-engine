"""Prepare example data or run one explicitly bounded optional worker training job."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .data import prepare


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    data = commands.add_parser("prepare")
    data.add_argument("--output", type=Path, required=True)
    train = commands.add_parser("train")
    train.add_argument("--dataset", type=Path, required=True)
    train.add_argument("--output", type=Path, required=True)
    train.add_argument("--recipe", type=Path, required=True)
    train.add_argument("--device", choices=["cpu", "cuda"])
    train.add_argument(
        "--allow-download",
        action="store_true",
        help="Fetch the pinned model only with existing authorized access; never accepts a license",
    )
    args = parser.parse_args(argv)
    if args.command == "prepare":
        manifest = prepare(args.output)
        print(json.dumps({"dataset_sha256": manifest["dataset_sha256"], "example_only": True}))
    else:
        from .peft_backend import recipe_from, run

        recipe = recipe_from(args.recipe)
        if args.device:
            recipe["device"] = args.device
        report = run(args.dataset, args.output, recipe, allow_download=args.allow_download)
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "device": report["hardware"]["device"],
                    "pretrained_weights_used": report["pretrained_weights_used"],
                    "adapter_sha256": report["adapter_sha256"],
                    "reload_equivalent": report["reload"]["equivalent"],
                }
            )
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
