"""`tessera`: drive the in-process store on a SQLite file from a shell.

Several workers (people, Muse Code, Claude Code) can share one database file, because SQLite
serializes writers. That is how the build loop runs on Tessera before the HTTP service exists.
Output is JSON on stdout; errors are JSON on stderr with exit code 3 (stale lease), 4 (not found)
or 1 (anything else).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from tessera._version import __version__
from tessera.errors import TesseraError
from tessera.models import ChildSpec, QueueConfig, TaskSpec
from tessera.store import Store

EXIT_CODES = {409: 3, 404: 4}


def _json_arg(text: str) -> Any:
    """Parse JSON given inline or as @path/to/file.json."""
    raw = Path(text[1:]).read_text(encoding="utf-8") if text.startswith("@") else text
    return json.loads(raw)


def _emit(value: Any) -> None:
    if isinstance(value, BaseModel):
        print(value.model_dump_json(indent=2))
    elif isinstance(value, list) and value and isinstance(value[0], BaseModel):
        print(json.dumps([v.model_dump(mode="json") for v in value], indent=2))
    elif isinstance(value, dict) and value and isinstance(next(iter(value.values())), BaseModel):
        print(json.dumps({k: v.model_dump(mode="json") for k, v in value.items()}, indent=2))
    else:
        print(json.dumps(value, indent=2))


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tessera", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--db",
        default=os.environ.get("TESSERA_DB", ".tessera/tessera.db"),
        help="SQLite file (default $TESSERA_DB or .tessera/tessera.db)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the database")
    d = sub.add_parser("define", help="create or change a queue")
    d.add_argument("queue")
    d.add_argument("--lease", type=int, default=600, help="max seconds one lease may be held")
    d.add_argument("--heartbeat", type=int, default=60, help="seconds between heartbeats")
    d.add_argument("--misses", type=int, default=3, help="missed heartbeats before a lapse")
    d.add_argument("--max-attempts", type=int, default=3)
    d.add_argument("--backoff-base", type=float, default=30.0)
    d.add_argument("--backoff-cap", type=float, default=900.0)

    e = sub.add_parser("enqueue", help="add a task")
    e.add_argument("queue")
    e.add_argument("--brief", required=True, help="JSON object, or @file.json")
    e.add_argument("--id", dest="task_id")
    e.add_argument("--kind", default="task")
    e.add_argument(
        "--effect",
        default="write-internal",
        choices=["read", "write-internal", "write-repo", "public"],
    )
    e.add_argument("--priority", type=int, default=50)
    e.add_argument(
        "--depends-on",
        action="append",
        default=[],
        dest="depends_on",
        help="a task that must be done, dead or cancelled first (repeatable)",
    )

    c = sub.add_parser("claim", help="lease the next ready task (prints null when none)")
    c.add_argument("queue")
    c.add_argument("--worker", required=True)
    c.add_argument("--kind", action="append", dest="kinds")
    c.add_argument("--effect", action="append", dest="effects")

    h = sub.add_parser("heartbeat", help="extend a lease")
    h.add_argument("task_id")
    h.add_argument("--token", type=int, required=True)

    s = sub.add_parser("submit", help="store a result and end the lease")
    s.add_argument("task_id")
    s.add_argument("--token", type=int, required=True)
    s.add_argument("--output", required=True, help="JSON object, or @file.json")
    s.add_argument("--idempotency-key", required=True)
    s.add_argument("--fail", action="store_true", help="report a failed attempt")
    s.add_argument("--error")

    sp = sub.add_parser("split", help="add child tasks; the parent waits for them")
    sp.add_argument("task_id")
    sp.add_argument("--token", type=int, required=True)
    sp.add_argument("--children", required=True, help="JSON list of child specs, or @file.json")

    pk = sub.add_parser("park", help="end the lease without a failure and wait for input")
    pk.add_argument("task_id")
    pk.add_argument("--token", type=int, required=True)
    pk.add_argument("--reason", required=True)
    pk.add_argument("--resume-within", type=float, help="seconds before it comes back by itself")

    rs = sub.add_parser("resume", help="give a parked task its input; it becomes ready again")
    rs.add_argument("task_id")
    rs.add_argument("--input", required=True, help="JSON object, or @file.json")

    ca = sub.add_parser("cancel", help="cancel a task and its unfinished children")
    ca.add_argument("task_id")
    ca.add_argument("--reason", default="")

    for name, text in (
        ("reap", "return lapsed leases to their queues"),
        ("counts", "print the counts record"),
        ("health", "print health"),
        ("queues", "list queue definitions"),
    ):
        sub.add_parser(name, help=text)
    ev = sub.add_parser("events", help="print the event log")
    ev.add_argument("--after", type=int, default=0)
    ev.add_argument("--limit", type=int, default=1000)
    for name in ("task", "results"):
        sub.add_parser(name, help=f"print one task's {name}").add_argument("task_id")
    sv = sub.add_parser("serve", help="run the HTTP service, MCP door and agent pages")
    sv.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 only inside a container")
    sv.add_argument("--port", type=int, default=8080)

    keys = sub.add_parser("keys", help="issue, list or revoke worker keys").add_subparsers(
        dest="keys_command", required=True
    )
    issue = keys.add_parser("issue", help="create a key; it is printed once and never stored")
    issue.add_argument("--label", required=True, help="who holds it, for example muse-web-01")
    issue.add_argument(
        "--queue",
        action="append",
        required=True,
        dest="queues",
        help="queue name or glob (repeatable), for example 'research.*'",
    )
    issue.add_argument("--kind", action="append", dest="kinds", help="task kinds (default: any)")
    issue.add_argument(
        "--effect",
        action="append",
        dest="effects",
        choices=["read", "write-internal", "write-repo"],
        help="effect classes (default: all but public)",
    )
    issue.add_argument("--scope", action="append", dest="scopes", choices=["admin", "store"])
    issue.add_argument("--days", type=float, default=14.0)
    keys.add_parser("list", help="list keys (never their secrets)")
    revoke = keys.add_parser("revoke", help="revoke a key at once")
    revoke.add_argument("key_id")
    sub.add_parser("version", help="print the version")
    return p


def _run(store: Store, args: argparse.Namespace) -> Any:
    cmd = args.command
    if cmd == "init":
        return {"db": store.path, "ok": True}
    if cmd == "define":
        return store.define_queue(
            QueueConfig(
                name=args.queue,
                lease_seconds=args.lease,
                heartbeat_seconds=args.heartbeat,
                miss_limit=args.misses,
                max_attempts=args.max_attempts,
                backoff_base_seconds=args.backoff_base,
                backoff_cap_seconds=args.backoff_cap,
            )
        )
    if cmd == "enqueue":
        return {
            "task_id": store.enqueue(
                TaskSpec(
                    queue=args.queue,
                    brief=_json_arg(args.brief),
                    task_id=args.task_id,
                    kind=args.kind,
                    effect_class=args.effect,
                    priority=args.priority,
                    depends_on=args.depends_on,
                )
            )
        }
    if cmd == "claim":
        return store.claim(args.queue, args.worker, kinds=args.kinds, effect_classes=args.effects)
    if cmd == "heartbeat":
        return store.heartbeat(args.task_id, args.token)
    if cmd == "submit":
        return store.submit(
            args.task_id,
            args.token,
            _json_arg(args.output),
            idempotency_key=args.idempotency_key,
            ok=not args.fail,
            error=args.error,
        )
    if cmd == "split":
        children = [ChildSpec.model_validate(c) for c in _json_arg(args.children)]
        return {"children": store.split(args.task_id, args.token, children)}
    if cmd == "park":
        return store.park(
            args.task_id, args.token, reason=args.reason, resume_within=args.resume_within
        )
    if cmd == "resume":
        return store.resume(args.task_id, _json_arg(args.input))
    if cmd == "cancel":
        return store.cancel(args.task_id, args.reason)
    if cmd == "reap":
        return {"reaped": store.reap()}
    if cmd == "counts":
        return store.counts()
    if cmd == "health":
        return store.health()
    if cmd == "queues":
        return store.queues()
    if cmd == "events":
        return store.events(after=args.after, limit=args.limit)
    if cmd == "task":
        return store.task(args.task_id)
    if cmd == "results":
        return store.results(args.task_id)
    if cmd == "keys":
        return _keys(store, args)
    raise SystemExit(f"unknown command {cmd}")


def _keys(store: Store, args: argparse.Namespace) -> Any:
    if args.keys_command == "issue":
        secret, grant = store.issue_key(
            args.label,
            queues=args.queues,
            kinds=args.kinds or ["*"],
            effect_classes=args.effects or ["read", "write-internal", "write-repo"],
            scopes=args.scopes or [],
            days=args.days,
        )
        print(
            f"key for {grant.label} ({grant.key_id}), shown once; store it somewhere safe:",
            file=sys.stderr,
        )
        return {"key_id": grant.key_id, "key": secret, "expires_at": grant.expires_at}
    if args.keys_command == "list":
        return store.list_keys()
    return store.revoke_key(args.key_id)


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from tessera.service import ServiceSettings, create_app

    settings = ServiceSettings.from_env()
    settings = ServiceSettings(**{**settings.__dict__, "db_path": args.db})
    uvicorn.run(
        create_app(Store(args.db), settings),
        host=args.host,
        port=args.port,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_level="info",
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "version":
        print(__version__)
        return 0
    if args.command == "serve":
        return _serve(args)
    store = Store(args.db)
    try:
        _emit(_run(store, args))
        return 0
    except TesseraError as exc:
        print(json.dumps({"error": exc.code, "detail": exc.detail}), file=sys.stderr)
        return EXIT_CODES.get(exc.status, 1)
    finally:
        store.close()


if __name__ == "__main__":
    sys.exit(main())
