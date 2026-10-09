"""phone-desk: give your agents a phone, with one rule: nothing dials until a human 👍s it.

    phone-desk propose --who .. --to .. --goal .. [--ask ..] [--fallback ..] [--fact ..]... [--hold|--dry-run]
    phone-desk surface <id>          send a held draft's bubble to the owner
    phone-desk approve <id>          terminal approver: decide at a real terminal
    phone-desk tick                  run every minute (cron/launchd/systemd): dial, poll, report
    phone-desk status [<id>]
    phone-desk linetest              dial NIST's public time line (+1 303-499-7111); no approval needed
    phone-desk rehearse <id>         text rehearsal (needs the optional `anthropic` package) or --print
    phone-desk webhook               one LoopMessage webhook JSON on stdin (Authorization via --auth)
    phone-desk serve                 a tiny local webhook receiver for LoopMessage
    phone-desk external contract     the text to paste to your external agent
    phone-desk external ingest       one message from the external agent on stdin
    phone-desk external research <id> [--host example.com]   print a research job for it
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Optional

from . import brief as B
from . import external
from .config import ConfigError, load
from .desk import Desk


def _desk() -> Desk:
    return Desk(load())


def _anthropic_llm():
    """A Claude-backed llm(system, messages) for text rehearsal, if the optional
    `anthropic` package is installed. Model: REHEARSAL_MODEL (default claude-opus-5)."""
    try:
        import anthropic
    except ImportError:
        raise SystemExit("text rehearsal needs `pip install anthropic` (or use --print and any chat model)")
    client = anthropic.Anthropic()
    model = os.environ.get("REHEARSAL_MODEL", "claude-opus-5")

    def llm(system: str, messages: list) -> str:
        msgs, last = [], None
        for m in messages:                       # merge same-role runs; the API wants alternation
            if last == m["role"]:
                msgs[-1]["content"] += "\n" + m["content"]
            else:
                msgs.append(dict(m))
            last = m["role"]
        if msgs and msgs[-1]["role"] == "assistant":
            msgs.append({"role": "user", "content": "(continue)"})
        resp = client.messages.create(model=model, max_tokens=1024, system=system, messages=msgs)
        if resp.stop_reason == "refusal":
            return "[end call]"
        return "".join(b.text for b in resp.content if b.type == "text")
    return llm


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="phone-desk", description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("propose", help="put a call request in front of the owner")
    p.add_argument("--who", required=True)
    p.add_argument("--to", required=True, help="US number, any format")
    p.add_argument("--goal", required=True)
    p.add_argument("--ask", default="")
    p.add_argument("--fallback", default="")
    p.add_argument("--fact", action="append", default=[], help="a fact it may share; repeat. Nothing else is shared.")
    p.add_argument("--voicemail", default="", help="message to leave on voicemail (default: leave none)")
    p.add_argument("--max-minutes", type=int, default=B.DEFAULT_MINUTES)
    p.add_argument("--origin", default="agent", choices=["owner", "agent"])
    p.add_argument("--hold", action="store_true", help="record as a draft; `surface <id>` sends it later")
    p.add_argument("--dry-run", action="store_true", help="print the bubble; record and send nothing")

    sf = sub.add_parser("surface", help="send a held draft's bubble to the owner")
    sf.add_argument("iid")
    a = sub.add_parser("approve", help="terminal approver: decide on a request at a real terminal")
    a.add_argument("iid")
    sub.add_parser("tick", help="dial approved calls in the window, poll calls, report outcomes")
    s = sub.add_parser("status")
    s.add_argument("iid", nargs="?")
    sub.add_parser("linetest", help="dial NIST's public time line and verify the whole pipe")
    r = sub.add_parser("rehearse", help="play the call out in text against a simulated front desk")
    r.add_argument("iid")
    r.add_argument("--persona", help="how the simulated desk should push back")
    r.add_argument("--print", dest="print_only", action="store_true",
                   help="print the caller and desk prompts for any chat model instead of running")
    w = sub.add_parser("webhook", help="process one LoopMessage webhook JSON from stdin")
    w.add_argument("--auth", default=os.environ.get("WEBHOOK_AUTHORIZATION", ""),
                   help="the Authorization header value the webhook arrived with")
    sv = sub.add_parser("serve", help="run a minimal local LoopMessage webhook receiver")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8787)

    ex = sub.add_parser("external", help="the external-agent side")
    exs = ex.add_subparsers(dest="excmd", required=True)
    exs.add_parser("contract", help="print the text to paste to your external agent")
    ei = exs.add_parser("ingest", help="one message from the external agent, on stdin")
    ei.add_argument("--agent", help="its name (default EXTERNAL_AGENT_NAME)")
    er = exs.add_parser("research", help="print a pre-call research job for the external agent")
    er.add_argument("iid")
    er.add_argument("--host", action="append", help="a host it may visit; repeat")

    args = ap.parse_args(argv)
    try:
        desk = _desk()
    except ConfigError as exc:
        print(f"config: {exc}", file=sys.stderr)
        return 2

    if args.cmd == "propose":
        try:
            b = B.Brief(who=args.who, to=args.to, goal=args.goal, ask=args.ask, fallback=args.fallback,
                        facts=args.fact, max_minutes=args.max_minutes, voicemail=args.voicemail, origin=args.origin)
        except B.BriefError as exc:
            print(f"refused: {exc}")
            return 2
        item = desk.propose(b, hold=args.hold, dry_run=args.dry_run)
        print(item["bubble"]["text"] if args.dry_run else json.dumps({"id": item["id"], "status": item["status"]}))
        return 0
    if args.cmd == "surface":
        item = desk.surface(args.iid)
        print(json.dumps({"id": item["id"], "status": item["status"]}))
        return 0
    if args.cmd == "approve":
        print(desk.approve_terminal(args.iid))
        return 0
    if args.cmd == "tick":
        print(json.dumps(desk.tick()))
        return 0
    if args.cmd == "linetest":
        res = desk.linetest()
        print(json.dumps(res, indent=1))
        return 0 if res["ok"] else 1
    if args.cmd == "rehearse":
        it = desk.get(args.iid)
        if not it:
            print(f"no call request {args.iid}")
            return 2
        b = B.Brief.from_dict(it["brief"])
        if args.print_only:
            print("=== CALLER (the voice agent's exact instructions) ===\n"
                  + B.system_prompt(b, desk.cfg.owner_name) + Desk.TEXT_SIM_NOTE)
            print("\n=== FRONT DESK (play this in a second chat) ===\n" + B.sim_prompt(b, args.persona))
            return 0
        res = desk.rehearse_text(args.iid, _anthropic_llm(), persona=args.persona)
        print(res["text"] + "\n\n" + res["transcript"])
        return 0
    if args.cmd == "webhook":
        try:
            payload = json.loads(sys.stdin.read())
        except ValueError:
            print(json.dumps({"ok": False, "reason": "malformed JSON"}))
            return 2
        print(json.dumps(desk.handle_webhook(payload, {"Authorization": args.auth})))
        return 0
    if args.cmd == "serve":
        from .server import serve
        serve(desk, args.host, args.port)
        return 0
    if args.cmd == "external":
        if args.excmd == "contract":
            print(external.contract(desk.cfg.owner_name, cap=desk.cfg.external_daily_cap))
        elif args.excmd == "ingest":
            print(json.dumps(external.ingest(desk, sys.stdin.read(),
                                             agent=args.agent or desk.cfg.external_agent_name)))
        else:
            print(external.research_job(desk, args.iid, hosts=args.host))
        return 0
    # status
    data = desk.store.load_desk()
    if args.iid:
        print(json.dumps(data["items"].get(args.iid), indent=1, ensure_ascii=False, default=str))
        return 0
    for iid, it in sorted(data["items"].items(), key=lambda kv: kv[1].get("created_at", "")):
        b = it["brief"]
        tag = f" ({b.get('requested_by')} asked)" if b.get("origin") == "external" else ""
        flag = " ⚠️research-flagged" if (it.get("research") or {}).get("flagged") else ""
        print(f"{iid} {it.get('status'):9} {it.get('created_at', '')[:16]} {b['who']} {B.pretty(b['to'])}{tag}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
