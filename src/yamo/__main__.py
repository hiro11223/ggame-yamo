"""CLI: python -m yamo run / check-tags"""

from __future__ import annotations

import argparse
import logging
import sys

from .config import load_config
from .http import HttpClient


def _client(cfg) -> HttpClient:
    return HttpClient(cfg.user_agent, cfg.fetch.retry, cfg.fetch.timeout_sec)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="yamo", description="実況ネタ探索サイト ジェネレータ")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--log-level", default="INFO")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="収集してページ用データを出力する")
    r.add_argument("--state-dir", default="state")
    r.add_argument("--out", default="site")
    r.add_argument("--max-new", type=int, default=None, help="作品ごとの取得（公式サイトURL など）の上限（config を上書き）")
    r.add_argument("--sources", default=None, help="カンマ区切り。例: steam")

    sub.add_parser("check-tags", help="config のタグが実在するか検証する")

    args = p.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )
    cfg = load_config(args.config)

    if args.cmd == "check-tags":
        from .pipeline import fetch_steam_tag_ids

        warnings: list[str] = []
        fetch_steam_tag_ids(_client(cfg), cfg, warnings)
        for w in warnings:
            print("WARN:", w)
        print("OK" if not warnings else f"{len(warnings)}件の警告")
        return 1 if warnings else 0

    from .pipeline import run

    sources = set(args.sources.split(",")) if args.sources else None
    result = run(cfg, _client(cfg), args.state_dir, args.out, max_new=args.max_new, sources=sources)
    for s in result.statuses:
        print(f"[{s.name}] {s.status} count={s.count} {'; '.join(s.notes + s.errors)}")
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
