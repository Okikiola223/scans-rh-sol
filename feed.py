#!/usr/bin/env python3
"""Launch feed for Solana + Robinhood Chain with structural rug filters.

Solana: RugCheck new tokens + full report.
Robinhood: GeckoTerminal new pools + GoPlus when the token is indexed.

This hides kill switches (mint, freeze, honeypot, hidden owner). It does not
prove a token is safe. Devs still dump. Default prints PASSES only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

UA = "launch-feed/0.1 (+local)"
RUGCHECK_NEW = "https://api.rugcheck.xyz/v1/stats/new_tokens"
RUGCHECK_REPORT = "https://api.rugcheck.xyz/v1/tokens/{mint}/report"
GECKO_NEW = "https://api.geckoterminal.com/api/v2/networks/{net}/new_pools?include=base_token,quote_token"
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/4663?contract_addresses={addr}"

FAKE = {
    "SOL", "BTC", "ETH", "WETH", "USDC", "USDT", "USDG", "BONK", "JUP", "JITO",
    "TRUMP", "MELANIA", "DOGE", "PEPE", "WIF", "PENGU", "HYPE",
}

RH_SKIP = {
    "0x0bd7d308f8e1639fab988df18a8011f41eacad73",  # WETH
    "0x5fc5360d0400a0fd4f2af552add042d716f1d168",  # USDG
    "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "0x4783c67b63de2b358ac5951a7d41f47a38f3c046",  # official stock issuer
}

OFFICIAL_ISSUER = "0x4783c67b63de2b358ac5951a7d41f47a38f3c046"
SEEN_PATH = Path(__file__).resolve().parent / "seen.json"
SEEN_CAP = 8000


def load_seen() -> set[str]:
    if not SEEN_PATH.exists():
        return set()
    try:
        data = json.loads(SEEN_PATH.read_text())
        if isinstance(data, list):
            return set(str(x) for x in data)
    except Exception:
        return set()
    return set()


def save_seen(seen: set[str]) -> None:
    items = list(seen)
    if len(items) > SEEN_CAP:
        items = items[-SEEN_CAP:]
    SEEN_PATH.write_text(json.dumps(items))


def load_env() -> None:
    path = Path(__file__).resolve().parent / ".env"
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        k, v = k.strip(), v.strip().strip("'").strip('"')
        if k and k not in os.environ:
            os.environ[k] = v


def http_json(url: str, timeout: float = 20.0, data: bytes | None = None) -> Any:
    req = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": UA, "Accept": "application/json"},
        method="POST" if data is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def empty_auth(v: Any) -> bool:
    if v is None:
        return True
    s = str(v).strip()
    return s in ("", "null", "11111111111111111111111111111111", "None")


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def fake_ticker(sym: str) -> bool:
    s = (sym or "").strip().upper()
    return s in FAKE


# --- Solana -----------------------------------------------------------------


def sol_prefilter(row: dict) -> str | None:
    if not empty_auth(row.get("mintAuthority")):
        return "mint authority live"
    if not empty_auth(row.get("freezeAuthority")):
        return "freeze authority live"
    if fake_ticker(row.get("symbol") or ""):
        return f"ticker looks cloned ({row.get('symbol')})"
    return None


def market_addrs(report: dict) -> set[str]:
    out: set[str] = set()
    for m in report.get("markets") or []:
        for k in ("pubkey", "mintLP", "liquidityA", "liquidityB"):
            v = m.get(k)
            if v:
                out.add(v)
    known = report.get("knownAccounts") or {}
    if isinstance(known, dict):
        out.update(known.keys())
    return out


def insider_pct(report: dict) -> float:
    skip = market_addrs(report)
    total = 0.0
    for h in report.get("topHolders") or []:
        if (h.get("address") in skip) or (h.get("owner") in skip):
            continue
        total += fnum(h.get("pct"))
    return total


def sol_report_fail(report: dict, holders_min: int, insider_max: float) -> str | None:
    if report.get("rugged"):
        return "marked rugged"
    if not empty_auth(report.get("mintAuthority") or (report.get("token") or {}).get("mintAuthority")):
        return "mint authority live"
    if not empty_auth(report.get("freezeAuthority") or (report.get("token") or {}).get("freezeAuthority")):
        return "freeze authority live"
    for risk in report.get("risks") or []:
        lvl = str(risk.get("level") or "").lower()
        if lvl in {"danger", "critical"}:
            return f"danger: {risk.get('name') or risk.get('description')}"
    n = int(report.get("totalHolders") or 0)
    if n >= holders_min:
        pct = insider_pct(report)
        if pct >= insider_max:
            return f"non-LP top wallets {pct:.0f}%"
    return None


def pull_solana(limit: int, holders_min: int, insider_max: float, show_rejects: bool) -> list[dict]:
    print("Solana: RugCheck new tokens")
    try:
        rows = http_json(RUGCHECK_NEW)
    except Exception as e:
        print(f"  fetch failed: {e}")
        return []
    if not isinstance(rows, list):
        print("  unexpected payload")
        return []

    candidates = []
    for row in rows:
        why = sol_prefilter(row)
        if why:
            if show_rejects:
                print(f"  skip {row.get('symbol') or row.get('mint')}: {why}")
            continue
        candidates.append(row)
        if len(candidates) >= limit:
            break

    passed = []
    for i, row in enumerate(candidates):
        mint = row.get("mint")
        if i:
            time.sleep(1.15)
        try:
            report = http_json(RUGCHECK_REPORT.format(mint=mint))
        except Exception as e:
            if show_rejects:
                print(f"  skip {row.get('symbol')}: report failed ({e})")
            continue
        why = sol_report_fail(report, holders_min, insider_max)
        if why:
            if show_rejects:
                print(f"  skip {row.get('symbol') or mint}: {why}")
            continue
        passed.append(
            {
                "chain": "solana",
                "symbol": (report.get("tokenMeta") or {}).get("symbol") or row.get("symbol"),
                "name": (report.get("tokenMeta") or {}).get("name") or "",
                "token": mint,
                "launchpad": (
                    (report.get("launchpad") or {}).get("name")
                    if isinstance(report.get("launchpad"), dict)
                    else (report.get("launchpad") or report.get("deployPlatform") or "")
                ),
                "holders": report.get("totalHolders"),
                "liq": report.get("totalMarketLiquidity"),
                "note": "mint/freeze off",
                "url": f"https://rugcheck.xyz/tokens/{mint}",
                "dex": f"https://dexscreener.com/solana/{mint}",
            }
        )
    return passed


# --- Robinhood Chain --------------------------------------------------------


def gecko_new(net: str) -> tuple[list[dict], dict[str, dict]]:
    data = http_json(GECKO_NEW.format(net=net))
    tokens = {}
    for inc in data.get("included") or []:
        if inc.get("type") == "token":
            tokens[inc.get("id")] = inc.get("attributes") or {}
    return data.get("data") or [], tokens


def goplus(addr: str) -> dict:
    try:
        data = http_json(GOPLUS.format(addr=addr.lower()))
    except Exception:
        return {}
    result = data.get("result") or {}
    if not isinstance(result, dict):
        return {}
    return result.get(addr.lower()) or {}


def rh_goplus_fail(g: dict) -> str | None:
    if not g:
        return None  # not indexed yet; other filters still run
    def flag(key: str) -> bool:
        return str(g.get(key) or "0") in {"1", "true", "True"}

    if flag("is_honeypot"):
        return "honeypot"
    if flag("cannot_buy"):
        return "cannot buy"
    if flag("cannot_sell_all"):
        return "cannot sell all"
    if flag("is_mintable"):
        return "mintable"
    if flag("hidden_owner"):
        return "hidden owner"
    if flag("can_take_back_ownership"):
        return "owner can take it back"
    if flag("honeypot_with_same_creator"):
        return "same creator honeypot"
    buy = fnum(g.get("buy_tax"))
    sell = fnum(g.get("sell_tax"))
    if buy >= 10 or sell >= 10:
        return f"tax buy {buy}% sell {sell}%"
    owner_pct = fnum(g.get("owner_percent")) * (100.0 if fnum(g.get("owner_percent")) <= 1 else 1)
    # GoPlus owner_percent is often a fraction 0-1
    raw = g.get("owner_percent")
    op = fnum(raw)
    if raw not in (None, "") and op <= 1:
        op *= 100
    if op >= 30:
        return f"owner holds {op:.0f}%"
    creator = (g.get("creator_address") or "").lower()
    if creator == OFFICIAL_ISSUER:
        return "official stock issuer"
    return None


def rh_pool_fail(attrs: dict, tok: dict, min_liq: float) -> str | None:
    addr = (tok.get("address") or "").lower()
    if addr in RH_SKIP:
        return "infra / quote token"
    if fake_ticker(tok.get("symbol") or ""):
        return f"ticker looks cloned ({tok.get('symbol')})"
    liq = fnum(attrs.get("reserve_in_usd"))
    if min_liq > 0 and liq < min_liq:
        return f"liq ${liq:.0f} < ${min_liq:.0f}"
    tx = (attrs.get("transactions") or {}).get("h1") or {}
    buys = int(tx.get("buys") or 0)
    sells = int(tx.get("sells") or 0)
    # lots of buys and zero sells is the cheap honeypot signature, once there is flow
    if buys >= 8 and sells == 0:
        return "buys with zero sells"
    return None


def pull_robinhood(limit: int, min_liq: float, show_rejects: bool) -> list[dict]:
    print("Robinhood: GeckoTerminal new pools + GoPlus")
    try:
        pools, tokens = gecko_new("robinhood")
    except Exception as e:
        print(f"  fetch failed: {e}")
        return []

    seen: set[str] = set()
    passed = []
    for pool in pools:
        if len(passed) >= limit:
            break
        rel = ((pool.get("relationships") or {}).get("base_token") or {}).get("data") or {}
        tok = tokens.get(rel.get("id")) or {}
        addr = (tok.get("address") or "").lower()
        if not addr or addr in seen:
            continue
        seen.add(addr)
        attrs = pool.get("attributes") or {}
        why = rh_pool_fail(attrs, tok, min_liq)
        if why:
            if show_rejects:
                print(f"  skip {tok.get('symbol') or addr}: {why}")
            continue
        g = goplus(addr)
        why = rh_goplus_fail(g)
        if why:
            if show_rejects:
                print(f"  skip {tok.get('symbol') or addr}: {why}")
            continue
        scan = "goplus clean" if g else "goplus not indexed yet"
        dex_id = (((pool.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id") or ""
        passed.append(
            {
                "chain": "robinhood",
                "symbol": tok.get("symbol") or "",
                "name": tok.get("name") or "",
                "token": addr,
                "launchpad": dex_id,
                "holders": (g or {}).get("holder_count"),
                "liq": attrs.get("reserve_in_usd"),
                "note": scan,
                "url": f"https://robinhoodchain.blockscout.com/token/{addr}",
                "dex": f"https://dexscreener.com/robinhood/{addr}",
            }
        )
        time.sleep(0.25)
    return passed


# --- print ------------------------------------------------------------------


def fmt_row(r: dict) -> str:
    liq = r.get("liq")
    try:
        liq_s = f"${float(liq):,.0f}" if liq not in (None, "") else "-"
    except (TypeError, ValueError):
        liq_s = str(liq)
    holders = r.get("holders")
    hold_s = str(holders) if holders not in (None, "") else "-"
    pad = str(r.get("launchpad") or "")[:16]
    note = str(r.get("note") or "")[:28]
    return (
        f"{r['chain']:<10} {str(r.get('symbol') or '')[:12]:<12} "
        f"{hold_s:<6} {liq_s:<10} {pad:<16} {note:<28} {r.get('dex')}"
    )


def tg_text(r: dict) -> str:
    liq = r.get("liq")
    try:
        liq_s = f"${float(liq):,.0f}" if liq not in (None, "") else "-"
    except (TypeError, ValueError):
        liq_s = str(liq)
    pad = r.get("launchpad") or "-"
    return (
        f"{r.get('chain')}  {r.get('symbol') or '?'}\n"
        f"{r.get('note') or ''}\n"
        f"liq {liq_s}  holders {r.get('holders') or '-'}  {pad}\n"
        f"{r.get('dex')}\n"
        f"{r.get('token')}"
    )


def tg_send(token: str, chat_id: str, text: str) -> None:
    body = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    ).encode()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    http_json(url, data=body)


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("No passes. That is normal. Loosen with --loose or --show-rejects to see why.")
        return
    print()
    print(f"{'CHAIN':<10} {'SYMBOL':<12} {'HOLD':<6} {'LIQ':<10} {'PAD':<16} {'NOTE':<28} DEX")
    print("-" * 120)
    for r in rows:
        print(fmt_row(r))
        print(f"{'':10} {r.get('token')}")


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description="Solana + Robinhood launch feed with rug filters")
    ap.add_argument("--chain", choices=["sol", "rh", "both"], default="both")
    ap.add_argument("--limit", type=int, default=12, help="max reports per chain")
    ap.add_argument("--show-rejects", action="store_true")
    ap.add_argument("--loose", action="store_true", help="weaker holder/liq cuts")
    ap.add_argument("--watch", action="store_true", help="loop; send new passes to Telegram if .env is set")
    ap.add_argument("--interval", type=int, default=60, help="seconds between watches")
    ap.add_argument("--tg-test", action="store_true", help="send one test message to Telegram and exit")
    args = ap.parse_args()

    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID") or ""
    tg_ok = bool(tg_token and tg_chat)

    if args.tg_test:
        if not tg_ok:
            print("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID in launch-feed/.env")
            return 1
        tg_send(tg_token, tg_chat, "launch-feed test. if you see this, notifications work.")
        print("Test sent. Check Telegram.")
        return 0

    holders_min = 8 if args.loose else 15
    insider_max = 50.0 if args.loose else 35.0
    min_liq = 0.0 if args.loose else 200.0

    seen = load_seen()
    seeded = bool(seen)

    def once() -> None:
        nonlocal seen, seeded
        rows: list[dict] = []
        if args.chain in {"sol", "both"}:
            rows.extend(pull_solana(args.limit, holders_min, insider_max, args.show_rejects))
        if args.chain in {"rh", "both"}:
            rows.extend(pull_robinhood(args.limit, min_liq, args.show_rejects))
        fresh = [r for r in rows if r["token"] not in seen]
        for r in fresh:
            seen.add(r["token"])
        save_seen(seen)
        print_table(fresh if (args.watch or seeded) else rows)
        if not seeded:
            print(
                f"Seeded {len(seen)} tokens. Next new pass goes to Telegram."
                if tg_ok
                else f"Seeded {len(seen)} tokens."
            )
            seeded = True
            return
        if tg_ok:
            for r in fresh:
                try:
                    tg_send(tg_token, tg_chat, tg_text(r))
                    time.sleep(0.3)
                except Exception as e:
                    print(f"  telegram failed: {e}")
        print()
        print("Filters hide mint/freeze/honeypot/hidden-owner/high tax. They do not stop a dump.")

    once()
    while args.watch:
        time.sleep(max(30, args.interval))
        print("\n--- refresh ---\n")
        once()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        sys.exit(130)
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code}: {e.reason}", file=sys.stderr)
        sys.exit(1)
