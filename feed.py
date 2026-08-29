#!/usr/bin/env python3
"""Launch feed: Ethereum, BNB, Robinhood.

GeckoTerminal new pools + GoPlus + DexScreener paid + $50k market cap.
Also pings Telegram when watched wallets buy a token that clears the same filter.

This does not auto-buy. A pass is not “safe.”
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

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
GECKO_NEW = "https://api.geckoterminal.com/api/v2/networks/{net}/new_pools?include=base_token,quote_token"
GOPLUS = "https://api.gopluslabs.io/api/v1/token_security/{cid}?contract_addresses={addr}"
DEX_ORDERS = "https://api.dexscreener.com/orders/v1/{chain}/{token}"
DEX_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/{token}"

DEFAULT_WATCH = [
    "0x5a52d4b820ae7f02880d270562950918acb14aa2",
    "0xdc0aa7c3205fe9c6b077522c5ca1acc4599af0d2",
]
MIN_MC = 50_000
OFFICIAL_ISSUER = "0x4783c67b63de2b358ac5951a7d41f47a38f3c046"

FAKE = {
    "BTC", "ETH", "WETH", "WBNB", "BNB", "USDC", "USDT", "USDG", "DAI", "BUSD",
    "WBTC", "STETH", "TRUMP", "DOGE", "PEPE", "SOL",
}

CHAINS = {
    "ethereum": {
        "gecko": "eth",
        "dex": "ethereum",
        "goplus": 1,
        "skip": {
            "0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2",  # WETH
            "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48",  # USDC
            "0xdac17f958d2ee523a2206206994597c13d831ec7",  # USDT
            "0x6b175474e89094c44da98b954eedeac495271d0f",  # DAI
            "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        },
        "explorer": "https://etherscan.io/token/{token}",
        "xfers": "https://eth.blockscout.com/api/v2/addresses/{addr}/token-transfers?type=ERC-20",
    },
    "bsc": {
        "gecko": "bsc",
        "dex": "bsc",
        "goplus": 56,
        "skip": {
            "0xbb4cdb9cbd36b01bd1cbaebf2de08d9173bc095c",  # WBNB
            "0x55d398326f99059ff775485246999027b3197955",  # USDT
            "0x8ac76a51cc950d9822d68b83fe1ad97b32cd580d",  # USDC
            "0xe9e7cea3dedca5984780bafc599bd69add087d56",  # BUSD
            "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
        },
        "explorer": "https://bscscan.com/token/{token}",
        "xfers": "https://bsc.blockscout.com/api/v2/addresses/{addr}/token-transfers?type=ERC-20",
    },
    "robinhood": {
        "gecko": "robinhood",
        "dex": "robinhood",
        "goplus": 4663,
        "skip": {
            "0x0bd7d308f8e1639fab988df18a8011f41eacad73",
            "0x5fc5360d0400a0fd4f2af552add042d716f1d168",
            "0xeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
            "0x4783c67b63de2b358ac5951a7d41f47a38f3c046",
        },
        "explorer": "https://robinhoodchain.blockscout.com/token/{token}",
        "xfers": "https://robinhoodchain.blockscout.com/api/v2/addresses/{addr}/token-transfers?type=ERC-20",
    },
}

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


def fnum(x: Any, default: float = 0.0) -> float:
    try:
        if x is None or x == "":
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def fake_ticker(sym: str) -> bool:
    return (sym or "").strip().upper() in FAKE


def dex_paid_mc(dex_chain: str, token: str, min_mc: float) -> tuple[str | None, dict]:
    info = {"paid": False, "mc": 0.0, "kind": ""}
    try:
        raw = http_json(DEX_ORDERS.format(chain=dex_chain, token=token))
    except Exception:
        raw = {}
    orders = raw if isinstance(raw, list) else (raw.get("orders") or [])
    boosts = [] if isinstance(raw, list) else (raw.get("boosts") or [])
    for o in orders:
        st = str(o.get("status") or "").lower()
        if st in {"cancelled", "rejected", "failed"}:
            continue
        if o.get("paymentTimestamp") or st in {"approved", "on-hold", "processing", "pending"}:
            info["paid"] = True
            info["kind"] = str(o.get("type") or "order")
            break
    if not info["paid"]:
        for b in boosts:
            if fnum(b.get("amount")) > 0 or fnum(b.get("totalAmount")) > 0:
                info["paid"] = True
                info["kind"] = "boost"
                break
    try:
        pairs = http_json(DEX_TOKEN.format(token=token)).get("pairs") or []
        same = [p for p in pairs if str(p.get("chainId") or "") == dex_chain]
        use = same or pairs
        mc = 0.0
        for p in use:
            mc = max(mc, fnum(p.get("marketCap")), fnum(p.get("fdv")))
        info["mc"] = mc
    except Exception:
        pass
    if not info["paid"]:
        return "dex not paid", info
    if info["mc"] < min_mc:
        return f"mc ${info['mc']:,.0f} < ${min_mc:,.0f}", info
    return None, info


def gecko_new(net: str) -> tuple[list[dict], dict[str, dict]]:
    data = http_json(GECKO_NEW.format(net=net))
    tokens = {}
    for inc in data.get("included") or []:
        if inc.get("type") == "token":
            tokens[inc.get("id")] = inc.get("attributes") or {}
    return data.get("data") or [], tokens


def goplus(cid: int, addr: str) -> dict:
    try:
        data = http_json(GOPLUS.format(cid=cid, addr=addr.lower()))
    except Exception:
        return {}
    result = data.get("result") or {}
    if not isinstance(result, dict):
        return {}
    return result.get(addr.lower()) or {}


def goplus_fail(g: dict, chain_key: str) -> str | None:
    if not g:
        return None
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
    buy, sell = fnum(g.get("buy_tax")), fnum(g.get("sell_tax"))
    if buy >= 10 or sell >= 10:
        return f"tax buy {buy}% sell {sell}%"
    raw = g.get("owner_percent")
    op = fnum(raw)
    if raw not in (None, "") and op <= 1:
        op *= 100
    if op >= 30:
        return f"owner holds {op:.0f}%"
    if chain_key == "robinhood":
        creator = (g.get("creator_address") or "").lower()
        if creator == OFFICIAL_ISSUER:
            return "official stock issuer"
    return None


def pool_fail(chain_key: str, attrs: dict, tok: dict, min_liq: float) -> str | None:
    cfg = CHAINS[chain_key]
    addr = (tok.get("address") or "").lower()
    if addr in cfg["skip"]:
        return "infra / quote token"
    if fake_ticker(tok.get("symbol") or ""):
        return f"ticker looks cloned ({tok.get('symbol')})"
    liq = fnum(attrs.get("reserve_in_usd"))
    if min_liq > 0 and liq < min_liq:
        return f"liq ${liq:.0f} < ${min_liq:.0f}"
    tx = (attrs.get("transactions") or {}).get("h1") or {}
    buys = int(tx.get("buys") or 0)
    sells = int(tx.get("sells") or 0)
    if buys >= 8 and sells == 0:
        return "buys with zero sells"
    return None


def pull_chain(chain_key: str, limit: int, min_liq: float, show_rejects: bool, min_mc: float) -> list[dict]:
    cfg = CHAINS[chain_key]
    print(f"{chain_key}: new pools")
    try:
        pools, tokens = gecko_new(cfg["gecko"])
    except Exception as e:
        print(f"  fetch failed: {e}")
        return []

    seen: set[str] = set()
    passed: list[dict] = []
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
        why = pool_fail(chain_key, attrs, tok, min_liq)
        if why:
            if show_rejects:
                print(f"  skip {tok.get('symbol') or addr}: {why}")
            continue
        g = goplus(cfg["goplus"], addr)
        why = goplus_fail(g, chain_key)
        if why:
            if show_rejects:
                print(f"  skip {tok.get('symbol') or addr}: {why}")
            continue
        why, dinfo = dex_paid_mc(cfg["dex"], addr, min_mc)
        if why:
            if show_rejects:
                print(f"  skip {tok.get('symbol') or addr}: {why}")
            continue
        dex_id = (((pool.get("relationships") or {}).get("dex") or {}).get("data") or {}).get("id") or ""
        passed.append(
            {
                "chain": chain_key,
                "symbol": tok.get("symbol") or "",
                "name": tok.get("name") or "",
                "token": addr,
                "launchpad": dex_id,
                "holders": (g or {}).get("holder_count"),
                "liq": attrs.get("reserve_in_usd"),
                "mc": dinfo["mc"],
                "note": f"paid {dinfo['kind']} mc ${dinfo['mc']:,.0f}",
                "url": cfg["explorer"].format(token=addr),
                "dex": f"https://dexscreener.com/{cfg['dex']}/{addr}",
            }
        )
        time.sleep(0.25)
    return passed


def watch_wallets(show_rejects: bool, min_mc: float, chain_keys: list[str]) -> list[dict]:
    raw = os.environ.get("WATCH_WALLETS") or ",".join(DEFAULT_WATCH)
    wallets = [w.strip().lower() for w in raw.split(",") if w.strip()]
    if not wallets:
        return []
    print(f"Wallets: {len(wallets)} on {', '.join(chain_keys)}")
    hits: list[dict] = []
    for chain_key in chain_keys:
        cfg = CHAINS[chain_key]
        for w in wallets:
            try:
                data = http_json(cfg["xfers"].format(addr=w))
            except Exception as e:
                print(f"  {chain_key} {w[:8]} fetch failed: {e}")
                continue
            items = data.get("items") or []
            seen_tok: set[str] = set()
            for it in items:
                to = ((it.get("to") or {}).get("hash") or "").lower()
                if to != w:
                    continue
                token = (
                    (it.get("token") or {}).get("address_hash")
                    or (it.get("token") or {}).get("address")
                    or ""
                ).lower()
                if not token or token in cfg["skip"] or token in seen_tok:
                    continue
                seen_tok.add(token)
                sym = (it.get("token") or {}).get("symbol") or "?"
                why, dinfo = dex_paid_mc(cfg["dex"], token, min_mc)
                if why:
                    if show_rejects:
                        print(f"  skip {sym} {chain_key} from {w[:8]}: {why}")
                    continue
                hits.append(
                    {
                        "chain": chain_key,
                        "symbol": sym,
                        "name": (it.get("token") or {}).get("name") or "",
                        "token": token,
                        "wallet": w,
                        "launchpad": "wallet",
                        "holders": "-",
                        "liq": None,
                        "mc": dinfo["mc"],
                        "note": f"{w[:8]}… bought · paid {dinfo['kind']}",
                        "url": cfg["explorer"].format(token=token),
                        "dex": f"https://dexscreener.com/{cfg['dex']}/{token}",
                    }
                )
                time.sleep(0.2)
            time.sleep(0.15)
    return hits


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
    mc = r.get("mc")
    try:
        mc_s = f"${float(mc):,.0f}" if mc not in (None, "") else "-"
    except (TypeError, ValueError):
        mc_s = "-"
    pad = r.get("launchpad") or "-"
    who = r.get("wallet")
    head = f"{r.get('chain')}  {r.get('symbol') or '?'}"
    if who:
        head = f"WALLET BUY  {who[:8]}…  {head}"
    return (
        f"{head}\n"
        f"{r.get('note') or ''}\n"
        f"mc {mc_s}  liq {liq_s}  {pad}\n"
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
        print("No passes. That is normal. Dex paid + $50k MC is a tight cut.")
        return
    print()
    print(f"{'CHAIN':<10} {'SYMBOL':<12} {'HOLD':<6} {'LIQ':<10} {'PAD':<16} {'NOTE':<28} DEX")
    print("-" * 120)
    for r in rows:
        print(fmt_row(r))
        print(f"{'':10} {r.get('token')}")


def resolve_chains(choice: str) -> list[str]:
    if choice == "all":
        return ["ethereum", "bsc", "robinhood"]
    if choice == "eth":
        return ["ethereum"]
    if choice == "bnb":
        return ["bsc"]
    if choice == "rh":
        return ["robinhood"]
    return [choice]


def main() -> int:
    load_env()
    ap = argparse.ArgumentParser(description="ETH + BNB + Robinhood launch feed")
    ap.add_argument("--chain", choices=["eth", "bnb", "rh", "all"], default="all")
    ap.add_argument("--limit", type=int, default=12, help="max reports per chain")
    ap.add_argument("--show-rejects", action="store_true")
    ap.add_argument("--loose", action="store_true", help="weaker liq cut (Dex paid + $50k still apply)")
    ap.add_argument("--watch", action="store_true")
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--tg-test", action="store_true")
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

    min_liq = 0.0 if args.loose else 200.0
    min_mc = MIN_MC
    chain_keys = resolve_chains(args.chain)

    seen = load_seen()
    seeded = bool(seen)

    def row_key(r: dict) -> str:
        if r.get("wallet"):
            return f"w:{r['chain']}:{r['wallet']}:{r['token']}"
        return f"{r['chain']}:{r['token']}"

    def once() -> None:
        nonlocal seen, seeded
        rows: list[dict] = []
        for ck in chain_keys:
            rows.extend(pull_chain(ck, args.limit, min_liq, args.show_rejects, min_mc))
        rows.extend(watch_wallets(args.show_rejects, min_mc, chain_keys))
        fresh = [r for r in rows if row_key(r) not in seen]
        for r in fresh:
            seen.add(row_key(r))
        save_seen(seen)
        print_table(fresh if (args.watch or seeded) else rows)
        if not seeded:
            print(
                f"Seeded {len(seen)} keys. Next new pass goes to Telegram."
                if tg_ok
                else f"Seeded {len(seen)} keys."
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
        print("Filter: DexScreener paid + market cap over $50k. Not a dump guarantee.")

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
