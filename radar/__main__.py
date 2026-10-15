"""new-term-radar: terms that are starting to surge, earliest signal first.

  python -m radar daily    [--config config.json] [--state state] [--out report.md] [--skip x,hf,...] [--new-sites] [--mail]
  python -m radar sources  [--config config.json] [--state state] [--out sources.md] [--mail]

Signals:
  ① x         names the accounts you follow said for the first time (AIsa)
  ① hf        models new to Hugging Face's trending top 50
  ② breakout  one new GitHub repo taking off on its own
  ③ sitemaps  pages other sites added since the last run
  ③ newsites  domains registered in the last 90 days that already rank (weekly; AIsa)
  ④ gsc       queries your own site got impressions for, for the first time (optional)
  ⑤ review    leads from 14 days ago checked in Google Trends; hit rate per signal (AIsa)
  usecases    the generic terms behind today's names, sized against a benchmark keyword (AIsa)

`sources` runs only ① (they change by the hour) and writes a report only when there is a
name or an error. `daily` runs everything; give it `--skip x,hf` when `sources` runs
apart, and it lists the ① leads of the last day from the database instead.

A small model scores every term: the name of a specific new thing, or a common word?
Names lead the report and are kept in state/radar.db for the review.
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import breakout, config, gsc, new_sites, notify, report, review, sitemaps, sources, store, usecases
from .i18n import t

SKIPPABLE = {"x", "hf", "breakout", "sitemaps", "newsites", "gsc", "review", "judge", "usecases"}


def run(fn, skip: bool = False) -> dict:
    """One signal failing still leaves a report: its error takes its place."""
    if skip:
        return {"skipped": True}
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001
        return {"error": f"{type(exc).__name__}: {' '.join(str(exc).split())[:160]}"}


def _finish(args, text: str, subject: str) -> None:
    if args.out:
        Path(args.out).write_text(text)
    print(text)
    if args.mail:
        print(notify.send(subject, text))


def cmd_sources(args, cfg: dict, state: Path) -> int:
    blocks = {"x": run(lambda: sources.x_new(cfg, state), "x" in args.skip),
              "hf": run(lambda: sources.hf_new(cfg, state), "hf" in args.skip)}
    lead, judged = report.leads(cfg, blocks)
    conn = store.connect(state)
    extra = {"judge": judged, "record": run(lambda: {"recorded": store.record(conn, report.to_record(cfg, lead))})}
    errors = any(b.get("error") for b in (*blocks.values(), *extra.values()))
    if report.named(cfg, lead) or errors:
        _finish(args, report.sources(cfg, lead, blocks, extra), f"New-term radar · sources {date.today()}")
    else:
        print(t("sources_quiet") + "; ".join(f"{k} {v.get('posts', v.get('models', t('dash')))}" for k, v in blocks.items()))
    return 0


def cmd_daily(args, cfg: dict, state: Path) -> int:
    weekly = args.new_sites or date.today().weekday() == cfg["new_sites"]["weekday"]
    blocks = {
        "x": run(lambda: sources.x_new(cfg, state), "x" in args.skip),
        "hf": run(lambda: sources.hf_new(cfg, state), "hf" in args.skip),
        "breakout": run(lambda: breakout.breakouts(cfg, state), "breakout" in args.skip),
        "sitemap": run(lambda: sitemaps.diff(cfg, state), "sitemaps" in args.skip),
        "newsite": run(lambda: new_sites.new_sites(cfg, state), "newsites" in args.skip or not weekly),
    }
    found = run(lambda: gsc.gsc_new(cfg), "gsc" in args.skip)
    lead, judged = report.leads(cfg, blocks)
    conn = store.connect(state)
    extra = {"judge": judged, "record": run(lambda: {"recorded": store.record(conn, report.to_record(cfg, lead))})}
    # With ① run apart, the daily report lists what that job kept since yesterday.
    extra["recent_sources"] = run(lambda: {"rows": store.recent_sources(conn)}, not {"x", "hf"} <= args.skip)
    # The names to read for their generic terms: things, not words that surged (usecases.names_to_read).
    names = usecases.names_to_read(report.to_record(cfg, lead), extra["recent_sources"].get("rows", []))
    repos = {r["term"]: r["repo"] for r in lead if r.get("repo")}
    extra["usecases"] = run(lambda: usecases.run(cfg, state, list(dict.fromkeys(names)), repos),
                            "usecases" in args.skip or not cfg["usecases"]["per_day"])
    run(lambda: store.record(conn, report.usecase_leads(extra["usecases"])))
    rev = run(lambda: review.review(cfg, conn), "review" in args.skip)
    summ = run(lambda: store.summary(conn))
    text = report.daily(cfg, lead, blocks, found, rev, summ, extra)
    if args.out:
        Path(args.out).with_suffix(".json").write_text(json.dumps(
            {"leads": lead, **blocks, "gsc": found, "review": rev, "summary": summ, "keywords": extra["usecases"]}, ensure_ascii=False, indent=1, default=str))
    _finish(args, text, f"New-term radar {date.today()}")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python -m radar", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["daily", "sources"])
    p.add_argument("--config", default="config.json")
    p.add_argument("--state", default="state", help="directory for radar.db and the snapshots")
    p.add_argument("--out", help="write the report (Markdown) here; `daily` also writes a .json beside it")
    p.add_argument("--skip", default="", help="comma-separated: " + ",".join(sorted(SKIPPABLE)))
    p.add_argument("--new-sites", action="store_true", help="run the weekly new-site signal today")
    p.add_argument("--mail", action="store_true", help="push the report out, mail and/or webhook (see radar/notify.py)")
    args = p.parse_args(argv)
    args.skip = {s for s in args.skip.split(",") if s}
    unknown = args.skip - SKIPPABLE
    if unknown:
        p.error(f"--skip: unknown {sorted(unknown)}")
    cfg = config.load(Path(args.config))
    if "judge" in args.skip:
        cfg["judge"]["model"] = ""
    state = Path(args.state)
    state.mkdir(parents=True, exist_ok=True)
    return cmd_sources(args, cfg, state) if args.command == "sources" else cmd_daily(args, cfg, state)


if __name__ == "__main__":
    sys.exit(main())
