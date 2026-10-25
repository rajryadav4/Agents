"""WARDEN command-line interface.

    warden serve [--port 8000] [--host 0.0.0.0]   start the control plane + dashboard
    warden demo                                    run every scenario in the terminal
    warden redteam [--ablation]                    run the red-team campaign
    warden trust                                   print the Trust Score report
    warden lint <policy.yaml>                      lint a policy file
    warden compile "<english sentence>"            compile English into a policy rule
    warden blast <agent_id>                        show an agent's blast radius
    warden train [--n 8000]                        (re)train the injection classifier
    warden verify                                  verify the demo audit ledger
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

G, R, Y, B, DIM, X = "\033[32m", "\033[31m", "\033[33m", "\033[36m", "\033[2m", "\033[0m"


def _c(ok: bool, text: str) -> str:
    return f"{G if ok else R}{text}{X}"


def cmd_serve(a: argparse.Namespace) -> int:
    import uvicorn

    from warden.server.app import create_app

    app = create_app()
    print(f"{B}WARDEN{X} control plane → http://{a.host}:{a.port}   (dashboard at /, API docs at /docs)")
    uvicorn.run(app, host=a.host, port=a.port, log_level="warning")
    return 0


def cmd_demo(a: argparse.Namespace) -> int:
    from warden.demo import scenarios
    from warden.demo.world import build_world

    world = build_world()
    passed = 0
    for res in scenarios.run_all(world, pace=0.0):
        passed += res.passed
        print(f"\n{_c(res.passed, '●')} {B}{res.title}{X} {DIM}({res.duration_ms:.0f} ms){X}")
        print(f"  {DIM}{res.story}{X}")
        for s in res.steps:
            if s.status == "info":
                print(f"    {DIM}· {s.actor}: {s.action}{X}")
            else:
                color = G if s.status == "executed" else Y if "pending" in s.status else R
                print(f"    {color}{s.status:>14}{X} {s.actor} — {s.action}")
    print(f"\n{_c(passed == len(scenarios.SCENARIOS), f'{passed}/{len(scenarios.SCENARIOS)} scenarios passed')}")
    print(f"ledger intact: {_c(world.warden.ledger.verify()['ok'], str(world.warden.ledger.verify()['ok']))}")
    return 0 if passed == len(scenarios.SCENARIOS) else 1


def cmd_redteam(a: argparse.Namespace) -> int:
    if a.ablation:
        from warden.redteam.ablation import run_ablation

        rep = run_ablation()
        print(f"{B}Ablation study{X} — which layer matters?\n")
        print(f"  {'configuration':34s} {'blocked':>9}  {'weighted':>9}")
        for row in rep["rows"]:
            print(f"  {row['config']:34s} {row['blocked']:>3}/{row['attacks']:<4}  {row['weighted_block_rate']*100:>7.1f}%"
                  + (f"  {DIM}missed: {', '.join(row['missed'][:4])}{'…' if len(row['missed'])>4 else ''}{X}" if row["missed"] else ""))
        return 0
    from warden.redteam.attacks import run_campaign

    rep = run_campaign()
    headline = _c(rep["blocked"] == rep["attacks"], f"{rep['blocked']}/{rep['attacks']} blocked")
    print(f"{B}Red-team campaign{X}: {headline}"
          f"  (severity-weighted {rep['weighted_block_rate']*100:.1f}%)  {DIM}{rep['duration_ms']:.0f} ms{X}\n")
    for o in rep["outcomes"]:
        print(f"  {_c(o['blocked'], 'OK' if o['blocked'] else 'XX')} {o['name']:38s} {DIM}{o['category']:<12} "
              f"{', '.join(o['stopped_by'][:3])}{X}")
    for cat, c in rep["categories"].items():
        print(f"  {DIM}{cat:14s} {c['blocked']}/{c['total']}{X}")
    return 0 if rep["blocked"] == rep["attacks"] else 1


def cmd_trust(a: argparse.Namespace) -> int:
    from warden import trust
    from warden.demo.world import build_world

    world = build_world()
    from warden.demo import scenarios

    scenarios.run_all(world)
    rep = trust.compute(world.warden)
    print(f"{B}WARDEN Trust Score{X}: {G}{rep['score']}/100  grade {rep['grade']}{X}\n")
    for k, v in rep["breakdown"].items():
        bar = "█" * int(v["score"] / 5) + "·" * (20 - int(v["score"] / 5))
        print(f"  {v['label']:18s} {bar} {v['score']:>5.1f}  {DIM}(weight {int(v['weight']*100)}%){X}")
    print(f"\n  {DIM}attacks blocked {rep['evidence']['attacks_blocked']}/{rep['evidence']['attacks_tested']}, "
          f"ledger intact={rep['evidence']['ledger_intact']}, gaps={rep['evidence']['sensitive_gaps'] or 'none'}{X}")
    return 0


def cmd_lint(a: argparse.Namespace) -> int:
    from warden.policy.engine import Policy
    from warden.policy.lint import lint, summary

    try:
        pol = Policy.load(a.file)
    except Exception as exc:  # noqa: BLE001
        print(_c(False, f"could not parse policy: {exc}"))
        return 2
    issues = lint(pol)
    for i in issues:
        col = {"error": R, "warning": Y, "info": DIM}[i["level"]]
        print(f"  {col}{i['level']:8s}{X} {i['code']:18s} {i.get('rule_id') or '-':28s} {i['message']}")
    s = summary(issues)
    errs = _c(s["error"] == 0, f"{s['error']} errors")
    print(f"\n{errs}, {s['warning']} warnings, {s['info']} info ({len(pol.rules)} rules)")
    return 1 if s["error"] else 0


def cmd_compile(a: argparse.Namespace) -> int:
    import yaml

    from warden.policy import nl

    c = nl.compile_rule(a.text, org_domain="acme.com")
    print(f"{B}Understood{X} (confidence {c.confidence:.0%}):")
    for u in c.understood:
        print(f"  {G}✓{X} {u}")
    for warn in c.warnings:
        print(f"  {Y}!{X} {warn}")
    if c.rule:
        print(f"\n{B}Generated rule:{X}\n{DIM}" + yaml.safe_dump(c.rule, sort_keys=False).rstrip() + X)
    else:
        print(_c(False, "\ncould not generate a rule from that sentence"))
        return 1
    return 0


def cmd_blast(a: argparse.Namespace) -> int:
    from warden.demo.world import build_world
    from warden.simulate import blast_radius

    world = build_world()
    try:
        b = blast_radius(world.warden, a.agent, world.tokens.get(a.agent))
    except KeyError:
        print(_c(False, f"unknown agent {a.agent}; try: " + ", ".join(x.id for x in world.warden.agents.all())))
        return 2
    print(f"{B}Blast radius for {a.agent}{X} ({b['role']}, {b['status']})\n  {b['assessment']}\n")
    print(f"  {G}reachable unattended ({b['reachable_count']}){X}: " + ", ".join(t["tool"] for t in b["reachable"]) or "none")
    print(f"  {Y}needs approval{X}: " + (", ".join(t["tool"] for t in b["needs_approval"]) or "none"))
    print(f"  {R}blocked{X}: " + (", ".join(t["tool"] for t in b["blocked"]) or "none"))
    return 0


def cmd_train(a: argparse.Namespace) -> int:
    from warden.detect.classifier import DEFAULT_MODEL_PATH, train_default

    print("training injection classifier…")
    clf, m = train_default(n=a.n)
    clf.save(DEFAULT_MODEL_PATH)
    print(f"{_c(True, 'done')}: {json.dumps(m)}\n  saved → {DEFAULT_MODEL_PATH}")
    return 0


def cmd_verify(a: argparse.Namespace) -> int:
    from warden.demo import scenarios
    from warden.demo.world import build_world

    world = build_world()
    scenarios.run_all(world)
    v = world.warden.ledger.verify()
    label = "intact" if v["ok"] else f"BROKEN at #{v['broken_at']}: {v['reason']}"
    print(f"ledger: {_c(v['ok'], label)} ({v['count']} records)")
    return 0 if v["ok"] else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="warden", description="Control plane & firewall for AI agents.")
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="start the control plane + dashboard")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    s.set_defaults(fn=cmd_serve)

    sub.add_parser("demo", help="run all scenarios in the terminal").set_defaults(fn=cmd_demo)

    rt = sub.add_parser("redteam", help="run the red-team campaign")
    rt.add_argument("--ablation", action="store_true", help="run the layer ablation study")
    rt.set_defaults(fn=cmd_redteam)

    sub.add_parser("trust", help="print the Trust Score").set_defaults(fn=cmd_trust)

    ln = sub.add_parser("lint", help="lint a policy file")
    ln.add_argument("file")
    ln.set_defaults(fn=cmd_lint)

    co = sub.add_parser("compile", help="compile English into a policy rule")
    co.add_argument("text")
    co.set_defaults(fn=cmd_compile)

    bl = sub.add_parser("blast", help="show an agent's blast radius")
    bl.add_argument("agent")
    bl.set_defaults(fn=cmd_blast)

    tr = sub.add_parser("train", help="(re)train the injection classifier")
    tr.add_argument("--n", type=int, default=8000)
    tr.set_defaults(fn=cmd_train)

    sub.add_parser("verify", help="verify the demo audit ledger").set_defaults(fn=cmd_verify)

    args = p.parse_args(argv)
    if not getattr(args, "fn", None):
        p.print_help()
        return 0
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
