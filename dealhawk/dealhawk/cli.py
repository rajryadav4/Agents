"""dealhawk — command line."""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path

from . import __version__
from .brain import Brain
from .config import ConfigError, load_config
from .engine import Engine
from .feedback import apply_label
from .web import serve_in_background
from .models import Deal, Offer
from .notify import ConsoleNotifier
from .scheduler import Scheduler
from .scoring import score_offer
from .sources import PRESETS, SOURCE_TYPES
from .store import Store

EXAMPLE = Path(__file__).with_name("config.example.yaml")


def _load(args):
    try:
        cfg = load_config(args.config)
    except ConfigError as e:
        sys.exit(f"config error: {e}")
    return cfg, Store(cfg.database)


def cmd_init(args) -> None:
    dest = Path(args.config)
    if dest.exists() and not args.force:
        sys.exit(f"{dest} already exists (use --force to overwrite)")
    shutil.copy(EXAMPLE, dest)
    print(f"wrote {dest}. Next:\n"
          "  1. install the ntfy app on your phone and subscribe to a long random topic\n"
          "  2. export NTFY_TOPIC=<that topic>\n"
          f"  3. edit the 'watches' in {dest} to say what you want\n"
          "  4. dealhawk test-notify   then   dealhawk run")


def _print_report(report) -> None:
    if report.suppressed:
        print(f"\n{len(report.suppressed)} matching deal(s) not sent:")
        for deal, why in report.suppressed[:15]:
            print(f"  [{deal.score.value:>3}] {deal.offer.title[:70]}  ({why})")


def cmd_once(args) -> None:
    cfg, store = _load(args)
    sources = [s for s in cfg.sources if not args.source or s.name in args.source]
    if not sources:
        sys.exit("no matching sources")
    notifiers = [ConsoleNotifier()] if args.dry_run else None
    engine = Engine(cfg, store, notifiers=notifiers)
    total = 0
    for s in sources:
        try:
            offers = s.fetch()
        except Exception as e:
            print(f"✗ {s.name}: {e}")
            continue
        report = engine.process(offers)
        total += len(report.alerted)
        print(f"✓ {s.name}: {report.offers} offers, {report.matched} matched, {len(report.alerted)} alerted")
        if args.verbose:
            for o in offers[:20]:
                sc = score_offer(o, [], cfg.scoring)
                price = f"${o.price:,.2f}" if o.price is not None else "—"
                print(f"    {price:>10}  {o.retailer[:12]:<12} {o.title[:70]}  (cold score {sc.value})")
        _print_report(report)
    print(f"\n{total} alert(s) sent.")


def cmd_run(args) -> None:
    cfg, store = _load(args)
    if not cfg.sources:
        sys.exit("no sources configured")
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    engine, tracker = _runtime(cfg, store)
    if args.port is not None:
        cfg.web.port = args.port
    if cfg.web.enabled:
        try:
            srv = serve_in_background(cfg.web, cfg.feedback, engine.brain, store, sources=cfg.sources,
                                      tracker=tracker, priceapi=_priceapi(cfg, store))
        except OSError as e:
            sys.exit(f"dashboard: {e.strerror or e}")
        print(f"🦅 dashboard: {_url(cfg.web, srv)}")
    Scheduler(engine, cfg.sources).run()


def _runtime(cfg, store):
    """Engine + link tracker; tracked links join the polling loop as one more source."""
    from .tracking import TrackedSource, Tracker
    engine = Engine(cfg, store)
    tracker = None
    if cfg.tracking.enabled:
        tracker = Tracker(store, engine, browser_fallback=cfg.tracking.browser_fallback)
        if not any(s.type == "tracked" for s in cfg.sources):
            cfg.sources.append(TrackedSource("tracked-links", tracker, interval=cfg.tracking.interval))
    return engine, tracker


def _priceapi(cfg, store):
    from .priceapi import PriceAPI
    return PriceAPI(store, scoring=cfg.scoring, browser_fallback=cfg.tracking.browser_fallback)


def _url(web, srv) -> str:
    host = "localhost" if web.host in ("127.0.0.1", "0.0.0.0", "::") else web.host
    return f"http://{host}:{srv.server_address[1]}"


def cmd_serve(args) -> None:
    """Dashboard only (no polling): browse an existing dealhawk.db, or preview with --demo."""
    import threading
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    if args.demo:
        from .config import Config
        from .demo import seed, seed_tracked, trickle
        cfg = Config(database=":memory:")
        store = Store(":memory:")
        seed(store)
        seed_tracked(store)
    else:
        cfg, store = _load(args)
    if args.host:
        cfg.web.host = args.host
    if cfg.web.host not in ("127.0.0.1", "localhost", "::1") and not cfg.web.password:
        sys.exit("serving on a non-local address needs web.password in the config")
    if args.port is not None:
        cfg.web.port = args.port
    engine, tracker = _runtime(cfg, store)
    from .web import make_server
    try:
        srv = make_server(cfg.web, cfg.feedback, engine.brain, store, demo=args.demo, sources=cfg.sources,
                          tracker=tracker, priceapi=_priceapi(cfg, store))
    except OSError as e:
        sys.exit(f"dashboard: {e.strerror or e}")
    print(f"🦅 dashboard: {_url(cfg.web, srv)}" + ("   (DEMO — sample data, not real deals)" if args.demo else ""))
    if args.demo:
        threading.Thread(target=trickle, args=(store,), daemon=True).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.shutdown()


def cmd_track(args) -> None:
    from .tracking import TrackError
    cfg, store = _load(args)
    engine, tracker = _runtime(cfg, store)
    if tracker is None:
        sys.exit("tracking is turned off in the config")
    if args.action == "add":
        if not args.url:
            sys.exit("usage: dealhawk track add <url> [--target PRICE]")
        try:
            t = tracker.add(args.url, "target" if args.target else "smart", args.target, check=False)
        except TrackError as e:
            sys.exit(str(e))
        how = f"alert at ≤ ${args.target:,.2f}" if args.target else "AI watch: lowest price / steal"
        print(f"tracking #{t['id']} ({how}) — checking now…")
        tracker.check(t["id"])
        t = store.get_tracked(t["id"])
        if t["status"] == "ok":
            print(f"  {t['title']}: ${t['last_price']:,.2f} at {t['retailer']}")
        else:
            print(f"  couldn't read the price yet: {t['error']}")
    elif args.action == "list":
        items = store.list_tracked()
        if not items:
            print("nothing tracked yet — dealhawk track add <url> [--target PRICE]")
        for t in items:
            price = f"${t['last_price']:,.2f}" if t["last_price"] else "—"
            low = f"low ${t['low_price']:,.2f}" if t["low_price"] else ""
            goal = f"target ${t['target_price']:,.2f}" if t["mode"] == "target" else "AI watch"
            state = "paused" if not t["active"] else ("⚠ " + (t["error"] or "")[:50] if t["status"] == "error" else "")
            print(f"#{t['id']:<4} {price:>10}  {low:<14} {goal:<18} {(t['title'] or t['url'])[:50]}  {state}")
    elif args.action == "rm":
        if not args.url or not args.url.isdigit():
            sys.exit("usage: dealhawk track rm <id>")
        print("removed" if store.delete_tracked(int(args.url)) else "no such item")
    elif args.action == "check":
        for t in store.list_tracked(active_only=True):
            tracker.check(t["id"])
        cmd_track(argparse.Namespace(**{**vars(args), "action": "list"}))


def cmd_price(args) -> None:
    from .sources.base import FetchError
    from .tracking import TrackError, explain
    cfg, store = _load(args)
    try:
        r = _priceapi(cfg, store).lookup(args.url, fresh=True)
    except TrackError as e:
        sys.exit(str(e))
    except FetchError as e:
        sys.exit(f"couldn't read the price: {explain(e, args.url)}")
    if args.json:
        import json
        print(json.dumps(r, indent=2, default=str))
        return
    h, sc = r["history"], r["score"]
    stock = {True: "in stock", False: "OUT OF STOCK", None: "stock unknown"}[r["in_stock"]]
    print(f"{r['title']}\n  ${r['price']:,.2f} at {r['retailer']} · {stock} · read via {r['method']}")
    if h["count"] > 1:
        print(f"  history: {h['count']} prices · low ${h['low']:,.2f} · high ${h['high']:,.2f}")
    print(f"  steal score {sc['value']}/100 ({sc['tier']}, {sc['basis']})")
    for reason in sc["reasons"]:
        print(f"   • {reason}")


def cmd_search(args) -> None:
    from .priceapi import DEFAULT_STORES
    cfg, store = _load(args)
    stores = args.stores.split(",") if args.stores else DEFAULT_STORES
    print(f"searching {', '.join(stores)} for “{args.query}”…")
    try:
        r = _priceapi(cfg, store).search(args.query, stores, limit=args.limit, fresh=True)
    except ValueError as e:
        sys.exit(str(e))
    if args.json:
        import json
        print(json.dumps(r, indent=2, default=str))
        return
    for name, st in r["stores"].items():
        print(f"  {'✓' if st['ok'] else '✗'} {name:<12} {st['count']} result(s)" +
              ("" if st["ok"] else f" — {st['error']}"))
    if not r["results"]:
        print("\nno prices found")
    for x in r["results"]:
        flag = " 🚨" if x["score"]["tier"] == "steal" else ""
        print(f"  ${x['price']:>10,.2f}  {x['retailer'][:12]:<12} {x['title'][:60]}{flag}\n               {x['url']}")


def cmd_webhooks(args) -> None:
    cfg, _ = _load(args)
    fb = cfg.feedback
    if not fb.active:
        sys.exit("set feedback.public_url and feedback.secret first")
    from .feedback import webhook_token
    base, t = fb.public_url.rstrip("/"), webhook_token(fb.secret)
    print(f"Twilio  → WhatsApp sandbox/sender 'When a message comes in' (POST):\n  {base}/wa/twilio?t={t}")
    print(f"Meta    → WhatsApp > Configuration > Webhook callback URL:\n  {base}/wa/cloud?t={t}")
    print(f"          Verify token: {t}   (subscribe to the 'messages' field)")


def cmd_feedback(args) -> None:
    cfg, store = _load(args)
    brain = Brain(store, cfg.scoring, cfg.learning, cfg.llm, judge=False)
    label = 1 if args.label in ("good", "yes", "y", "1", "+") else 0
    if apply_label(brain, store, args.deal_id, label):
        print(f"learned: deal {args.deal_id} is {'👍 good' if label else '👎 not for me'}")
    else:
        sys.exit(f"no deal {args.deal_id} with stored features (see `dealhawk deals`)")


def cmd_review(args) -> None:
    """Rate recent deals one by one — the fastest way to teach the model your taste."""
    cfg, store = _load(args)
    brain = Brain(store, cfg.scoring, cfg.learning, cfg.llm, judge=False)
    rows = store.unlabeled_deals(limit=args.limit)
    if not rows:
        print("nothing to review yet")
    for r in rows:
        price = f"${r['price']:,.2f}" if r["price"] is not None else "—"
        print(f"\n[{r['score']:>3} {r['tier']}] {price} at {r['retailer']}\n  {r['title']}\n  {r['url']}")
        ans = input("  good deal for you? [y]es / [n]o / [s]kip / [q]uit: ").strip().lower()
        if ans.startswith("q"):
            break
        if ans[:1] in ("y", "n"):
            apply_label(brain, store, r["id"], 1 if ans.startswith("y") else 0)
    print(f"\nmodel now trained on {brain.learner.n_labels:.1f} label-weight")


def cmd_brain(args) -> None:
    cfg, store = _load(args)
    brain = Brain(store, cfg.scoring, cfg.learning, cfg.llm, judge=False)
    if args.retrain:
        print(f"retrained on {brain.retrain()} labels")
    L = brain.learner
    st = store.stats()
    print(f"labels: you {st['labels_you']} · clicks {st['labels_clicks']} · crowd {st['labels_crowd']}")
    print(f"learned model influence: {L.confidence:.0%} (grows to 80% after ~{cfg.learning.warmup_labels:.0f} labels)")
    print(f"Claude judge: {'on (' + cfg.llm.model + ')' if cfg.llm.enabled else 'off'}")
    top = L.top_weights(15)
    if top:
        print("\nwhat it has learned (feature → weight; + means you like it):")
        for k, w in top:
            print(f"  {w:+6.2f}  {k}")


def _sample_deal() -> Deal:
    o = Offer("test", "Amazon", 'Apple MacBook Pro 14" M4 (TEST ALERT)', "https://www.amazon.com/", 1299.0)
    hist = [1999, 1999, 1899, 1999, 1799, 1949, 1999, 1699, 1999, 1899]
    return Deal(o, score_offer(o, hist), "test")


def cmd_test_notify(args) -> None:
    cfg, _ = _load(args)
    deal = _sample_deal()
    for n in cfg.notifiers:
        try:
            n.send(deal)
            print(f"✓ sent via {n.type}")
        except Exception as e:
            print(f"✗ {n.type}: {e}")


def cmd_deals(args) -> None:
    cfg, store = _load(args)
    since = time.time() - args.hours * 3600 if args.hours else None
    rows = store.recent_deals(limit=args.limit, min_score=args.min_score, since=since)
    if not rows:
        print("no deals logged yet — run `dealhawk run` and leave it going")
    for r in rows:
        when = datetime.fromtimestamp(r["ts"]).strftime("%m-%d %H:%M")
        bell = "🔔" if r["notified"] else "  "
        price = f"${r['price']:,.2f}" if r["price"] is not None else "—"
        flask = "🧪" if r.get("explore") else "  "
        print(f"{bell}{flask} #{r['id']:<5} {when} [{r['score']:>3} {r['tier']:<5}] {price:>10} "
              f"{r['retailer'][:12]:<12} {r['title'][:55]}")
        if args.verbose:
            print(f"      {r['url']}\n      {'; '.join(r['reasons'][:3])}")


def cmd_score(args) -> None:
    hist = [float(x) for x in args.history.split(",") if x.strip()] if args.history else []
    o = Offer("cli", "Retailer", args.title, "", args.price, reference_price=args.was)
    s = score_offer(o, hist)
    print(f"score {s.value}/100 → {s.tier} (basis: {s.basis}){'  ⚡ glitch suspect' if s.glitch_suspect else ''}")
    for r in s.reasons:
        print(f"  • {r}")


def cmd_sources(args) -> None:
    print("source types:", ", ".join(sorted(SOURCE_TYPES)))
    print("rss presets:")
    for k, v in PRESETS.items():
        print(f"  {k:<26} {v}")
    if Path(args.config).exists():
        cfg, store = _load(args)
        print("\nconfigured:")
        for s in cfg.sources:
            print(f"  {s!r}")
        print("\nstats:", store.stats())


def main(argv: list[str] | None = None) -> None:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default="dealhawk.yaml", help="config file (default: dealhawk.yaml)")
    common.add_argument("-v", "--verbose", action="store_true")
    p = argparse.ArgumentParser(prog="dealhawk", description="24/7 steal-deal hunter with phone alerts")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def cmd(name: str, help: str) -> argparse.ArgumentParser:
        return sub.add_parser(name, help=help, parents=[common])

    s = cmd("init", "write a starter dealhawk.yaml")
    s.add_argument("--force", action="store_true")
    s.set_defaults(fn=cmd_init)
    s = cmd("run", "run 24/7 (and serve the dashboard)")
    s.add_argument("--port", type=int, help="dashboard port (default from config: 8686)")
    s.set_defaults(fn=cmd_run)
    s = cmd("serve", "dashboard only (no polling) — over dealhawk.db, or --demo")
    s.add_argument("--demo", action="store_true", help="clearly-labelled sample data to preview the page")
    s.add_argument("--host", help="bind address (default from config: 127.0.0.1)")
    s.add_argument("--port", type=int, help="port (default from config: 8686)")
    s.set_defaults(fn=cmd_serve)
    s = cmd("once", "poll every source once")
    s.add_argument("--source", action="append", help="only this source (repeatable)")
    s.add_argument("--dry-run", action="store_true", help="print alerts here instead of sending them")
    s.set_defaults(fn=cmd_once)
    s = cmd("test-notify", "send a sample alert to every notifier")
    s.set_defaults(fn=cmd_test_notify)
    s = cmd("deals", "list recent deals")
    s.add_argument("--limit", type=int, default=30)
    s.add_argument("--min-score", type=int, default=0)
    s.add_argument("--hours", type=float, default=0, help="only the last N hours")
    s.set_defaults(fn=cmd_deals)
    s = cmd("score", "explain the steal score for a price")
    s.add_argument("price", type=float)
    s.add_argument("--history", help="comma-separated past prices, e.g. 100,100,95,60,100")
    s.add_argument("--was", type=float, help="claimed regular price")
    s.add_argument("--title", default="item")
    s.set_defaults(fn=cmd_score)
    s = cmd("sources", "list source types, presets and configured sources")
    s.set_defaults(fn=cmd_sources)
    s = cmd("feedback", "teach the model: was this deal good for you?")
    s.add_argument("deal_id", type=int)
    s.add_argument("label", choices=["good", "bad", "yes", "no", "y", "n", "1", "0", "+", "-"])
    s.set_defaults(fn=cmd_feedback)
    s = cmd("review", "rate recent deals one by one to train the model")
    s.add_argument("--limit", type=int, default=20)
    s.set_defaults(fn=cmd_review)
    s = cmd("track", "track a product link: add <url> [--target PRICE] | list | check | rm <id>")
    s.add_argument("action", choices=["add", "list", "check", "rm"])
    s.add_argument("url", nargs="?", help="product link (add) or id (rm)")
    s.add_argument("--target", type=float, help="alert when the price is at or below this; omit for AI watch")
    s.set_defaults(fn=cmd_track)
    s = cmd("price", "price API: live price + history of one product link")
    s.add_argument("url")
    s.add_argument("--json", action="store_true", help="print the raw API response")
    s.set_defaults(fn=cmd_price)
    s = cmd("search", "price API: compare one product across stores, cheapest first")
    s.add_argument("query")
    s.add_argument("--stores", help="comma-separated, e.g. walmart,bestbuy,newegg (default: 8 big stores)")
    s.add_argument("--limit", type=int, default=25)
    s.add_argument("--json", action="store_true", help="print the raw API response")
    s.set_defaults(fn=cmd_search)
    s = cmd("webhooks", "print the WhatsApp webhook URLs for Twilio / Meta")
    s.set_defaults(fn=cmd_webhooks)
    s = cmd("brain", "show what the model has learned")
    s.add_argument("--retrain", action="store_true", help="rebuild the model from all stored labels")
    s.set_defaults(fn=cmd_brain)

    args = p.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    main()
