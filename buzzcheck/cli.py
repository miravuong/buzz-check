from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from dotenv import load_dotenv

from buzzcheck.config import ConfigMissing, Store, config_exists, load_store
from buzzcheck.kroger import ClientCreds, KrogerAuthError, KrogerClient, KrogerError


# ---------- exit codes ----------

EXIT_ON_SALE = 0
EXIT_NOT_ON_SALE = 1
EXIT_ERROR = 2
EXIT_NO_MATCHES = 3


# ---------- BuzzBallz search config ----------

BUZZBALLZ_TERMS = ["BuzzBallz", "Buzz Ballz"]
BUZZBALLZ_BRAND = "BuzzBallz"
BUZZBALLZ_NORM = "buzzballz"


# ---------- product-line classification ----------

LINE_CHILLERS = "chillers"
LINE_COCKTAILS = "cocktails"
LINE_BIGGIES = "biggies"
LINE_UNCATEGORIZED = "uncategorized"

LINE_ORDER = [LINE_CHILLERS, LINE_COCKTAILS, LINE_BIGGIES, LINE_UNCATEGORIZED]

LINE_DISPLAY = {
    LINE_CHILLERS: "Chillers",
    LINE_COCKTAILS: "Cocktails",
    LINE_BIGGIES: "Biggies",
    LINE_UNCATEGORIZED: "Uncategorized",
}

LINE_ALIASES = {
    "chillers": LINE_CHILLERS,
    "chiller": LINE_CHILLERS,
    "cocktails": LINE_COCKTAILS,
    "cocktail": LINE_COCKTAILS,
    "biggies": LINE_BIGGIES,
    "biggie": LINE_BIGGIES,
    "uncategorized": LINE_UNCATEGORIZED,
    "uncategorised": LINE_UNCATEGORIZED,
}


class UsageError(Exception):
    """User-supplied input is invalid; must exit 2 before any API call."""


# ---------- normalization + classification ----------

_NORM_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize(s: str) -> str:
    """Lowercase, strip whitespace/punctuation/underscores. `Buzz-Ballz` -> `buzzballz`."""
    return _NORM_RE.sub("", (s or "").lower())


def classify_line(description: str) -> str:
    n = normalize(description)
    if "chiller" in n:
        return LINE_CHILLERS
    if "biggie" in n:
        return LINE_BIGGIES
    if "cocktail" in n:
        return LINE_COCKTAILS
    return LINE_UNCATEGORIZED


def is_buzzballz(product: dict) -> bool:
    brand = normalize(product.get("brand") or "")
    desc = normalize(product.get("description") or "")
    return BUZZBALLZ_NORM in brand or BUZZBALLZ_NORM in desc


def parse_line_arg(raw: Optional[str]) -> Optional[str]:
    if raw is None:
        return None
    key = raw.strip().lower()
    if key not in LINE_ALIASES:
        valid = "chillers, cocktails, biggies, uncategorized"
        raise UsageError(
            f"Unknown line '{raw}'. Valid: {valid}. "
            "This tool only checks BuzzBallz; it is not a general product search."
        )
    return LINE_ALIASES[key]


# ---------- variant model ----------


@dataclass
class Variant:
    description: str
    upc: str
    size: str
    regular: float
    promo: float
    on_sale: bool
    percent_off: int
    price_available: bool
    line: str


def _price_view(price_obj: dict) -> tuple[float, float, bool]:
    if not price_obj:
        return 0.0, 0.0, False
    regular = float(price_obj.get("regular") or 0)
    promo = float(price_obj.get("promo") or 0)
    return regular, promo, True


def to_variants(product: dict) -> list[Variant]:
    desc = product.get("description") or ""
    line = classify_line(desc)
    out: list[Variant] = []
    items = product.get("items") or []
    for item in items:
        upc = str(item.get("upc") or product.get("upc") or product.get("productId") or "")
        if not upc:
            continue
        size = str(item.get("size") or "")
        regular, promo, available = _price_view(item.get("price") or {})
        on_sale = available and 0 < promo < regular
        percent_off = round((regular - promo) / regular * 100) if on_sale and regular > 0 else 0
        out.append(Variant(
            description=desc,
            upc=upc,
            size=size,
            regular=regular,
            promo=promo,
            on_sale=on_sale,
            percent_off=percent_off,
            price_available=available,
            line=line,
        ))
    if not items:
        upc = str(product.get("upc") or product.get("productId") or "")
        if upc:
            out.append(Variant(
                description=desc,
                upc=upc,
                size="",
                regular=0.0,
                promo=0.0,
                on_sale=False,
                percent_off=0,
                price_available=False,
                line=line,
            ))
    return out


# ---------- fetch pipeline ----------


def fetch_all_variants(client: KrogerClient, location_id: str) -> list[Variant]:
    """
    Query multiple term variants + brand filter, merge, dedup by UPC.
    Filter is post-fetch; LINE never reaches the API.
    """
    queries: list[dict] = [{"term": t} for t in BUZZBALLZ_TERMS]
    queries.append({"brand": BUZZBALLZ_BRAND})

    seen_upcs: set[str] = set()
    variants: list[Variant] = []
    for q in queries:
        products = client.search_products(location_id=location_id, **q)
        for product in products:
            if not is_buzzballz(product):
                continue
            for v in to_variants(product):
                if v.upc in seen_upcs:
                    continue
                seen_upcs.add(v.upc)
                variants.append(v)
    return variants


# ---------- formatting ----------


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _short_location(store: Store) -> str:
    if not store.address:
        return store.name or store.location_id
    city = ""
    parts = [p.strip() for p in store.address.split(",")]
    if len(parts) >= 2:
        city = parts[1]
    name = store.name or store.chain or "store"
    return f"{name} ({city})" if city else name


def _price_line(v: Variant) -> str:
    if not v.price_available:
        return "price unavailable"
    if v.on_sale:
        return f"${v.promo:.2f}  (was ${v.regular:.2f})  {v.percent_off}% off"
    return f"${v.regular:.2f}"


def render_human(
    variants: list[Variant],
    filtered: list[Variant],
    line_filter: Optional[str],
    store: Store,
    show_all: bool,
) -> str:
    lines: list[str] = []
    total = len(filtered)
    on_sale = [v for v in filtered if v.on_sale]
    label = "BuzzBallz" if not line_filter else f"BuzzBallz {LINE_DISPLAY[line_filter]}"
    loc = _short_location(store)

    if total == 0:
        lines.append(f"{label}: no matches at {loc}")
        lines.append("")
        lines.append("Run `buzzcheck selftest` to confirm the connection is working.")
        lines.append("If it passes, this store does not stock them.")
        return "\n".join(lines) + "\n"

    if on_sale:
        lines.append(f"{label}: {len(on_sale)} of {total} on sale at {loc}")
    else:
        lines.append(f"{label}: nothing on sale at {loc} ({total} checked)")
    lines.append("")

    to_show = filtered if (show_all or not on_sale) else on_sale
    if line_filter:
        for v in to_show:
            lines.extend(_render_variant_block(v))
    else:
        by_line: dict[str, list[Variant]] = {k: [] for k in LINE_ORDER}
        for v in to_show:
            by_line[v.line].append(v)
        first_group = True
        for key in LINE_ORDER:
            group = by_line[key]
            if not group:
                continue
            if not first_group:
                lines.append("")
            first_group = False
            lines.append(f"{LINE_DISPLAY[key]}")
            for v in group:
                lines.extend(_render_variant_block(v, indent="  "))

    if line_filter:
        hidden = len(variants) - total
        if hidden > 0:
            lines.append("")
            lines.append(
                f"{hidden} variant{'s' if hidden != 1 else ''} in other lines not shown. "
                "Run `buzzcheck` to see all."
            )

    if on_sale and not (show_all):
        lines.append("")
        lines.append("Run with --add to put it in your cart.")

    return "\n".join(lines) + "\n"


def _render_variant_block(v: Variant, indent: str = "  ") -> list[str]:
    header = f"{indent}{v.description}"
    if v.size:
        header += f"  {v.size}"
    return [header, f"{indent}{_price_line(v)}"]


def render_json(
    variants: list[Variant],
    filtered: list[Variant],
    line_filter: Optional[str],
    store: Store,
) -> str:
    payload = {
        "store": {
            "location_id": store.location_id,
            "chain": store.chain,
            "name": store.name,
            "address": store.address,
        },
        "line_filter": line_filter,
        "checked_at": _now_iso(),
        "match_count": len(filtered),
        "on_sale_count": sum(1 for v in filtered if v.on_sale),
        "total_matches_all_lines": len(variants),
        "variants": [asdict(v) for v in filtered],
    }
    return json.dumps(payload, indent=2) + "\n"


# ---------- argparse ----------


SUBCOMMANDS = {"setup", "stores", "auth", "selftest"}


def build_main_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="buzzcheck",
        description="Check whether BuzzBallz are on sale at a pinned Kroger-family store.",
    )
    p.add_argument(
        "line",
        nargs="?",
        default=None,
        help="Optional line filter: chillers, cocktails, biggies, uncategorized.",
    )
    p.add_argument("--add", action="store_true", help="Add to cart if on sale.")
    p.add_argument("--yes", action="store_true", help="Skip confirmation when exactly one on-sale variant.")
    p.add_argument("--upc", default=None, help="Add a specific UPC (must be on sale).")
    p.add_argument("--qty", type=int, default=1, help="Quantity for --add (default 1).")
    p.add_argument("--all", action="store_true", dest="show_all", help="Show every match, not just on-sale.")
    p.add_argument("--json", action="store_true", dest="json_out", help="Machine-readable output on stdout.")
    p.add_argument("--modality", default="PICKUP", choices=["PICKUP", "DELIVERY"], help="Fulfillment (default PICKUP).")
    p.add_argument("--store", default=None, help="Named store from config (default: primary).")
    return p


def build_stores_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="buzzcheck stores")
    p.add_argument("--zip", dest="zip_code", required=True, help="5-digit ZIP code.")
    p.add_argument("--radius", type=int, default=10, help="Miles (default 10, max 100).")
    p.add_argument("--chain", default=None, help="Filter to a chain, e.g. Ralphs.")
    p.add_argument("--json", action="store_true", dest="json_out")
    return p


# ---------- main dispatch ----------


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in SUBCOMMANDS:
        cmd = argv[0]
        rest = argv[1:]
        try:
            if cmd == "stores":
                return run_stores(rest)
            if cmd == "setup":
                return _not_implemented("setup", "run `buzzcheck stores --zip <zip>` and hand-write ~/.buzzcheck/config.json")
            if cmd == "auth":
                return _not_implemented("auth")
            if cmd == "selftest":
                return _not_implemented("selftest")
        except UsageError as e:
            print(f"buzzcheck: {e}", file=sys.stderr)
            return EXIT_ERROR
        except KrogerAuthError as e:
            print(f"buzzcheck: auth error: {e}", file=sys.stderr)
            return EXIT_ERROR
        except KrogerError as e:
            print(f"buzzcheck: API error: {e}", file=sys.stderr)
            return EXIT_ERROR
        except ConfigMissing as e:
            print(f"buzzcheck: {e}", file=sys.stderr)
            return EXIT_ERROR
    return run_check(argv)


def _not_implemented(cmd: str, hint: str = "") -> int:
    msg = f"buzzcheck {cmd}: not implemented in phase 1."
    if hint:
        msg += f" For now: {hint}"
    print(msg, file=sys.stderr)
    return EXIT_ERROR


# ---------- creds loading ----------


def load_creds() -> ClientCreds:
    load_dotenv()
    cid = os.environ.get("KROGER_CLIENT_ID", "").strip()
    csec = os.environ.get("KROGER_CLIENT_SECRET", "").strip()
    if not cid or not csec:
        raise UsageError(
            "KROGER_CLIENT_ID or KROGER_CLIENT_SECRET missing. "
            "Copy .env.example to .env and fill it in."
        )
    return ClientCreds(client_id=cid, client_secret=csec)


# ---------- stores subcommand ----------


def run_stores(argv: list[str]) -> int:
    parser = build_stores_parser()
    args = parser.parse_args(argv)
    if not re.fullmatch(r"\d{5}", args.zip_code):
        print(f"buzzcheck: invalid zip '{args.zip_code}' (must be 5 digits)", file=sys.stderr)
        return EXIT_ERROR
    if args.radius < 1 or args.radius > 100:
        print(f"buzzcheck: --radius must be between 1 and 100 (got {args.radius})", file=sys.stderr)
        return EXIT_ERROR

    creds = load_creds()
    with KrogerClient(creds) as client:
        stores = client.find_locations(
            zip_code=args.zip_code, radius=args.radius, chain=args.chain, limit=25
        )

    if args.json_out:
        rows = [_store_row(s) for s in stores]
        print(json.dumps({"zip": args.zip_code, "radius": args.radius, "chain": args.chain, "stores": rows}, indent=2))
        return EXIT_ON_SALE if stores else EXIT_NO_MATCHES

    if not stores:
        print(f"No Kroger-family stores within {args.radius} miles of {args.zip_code}"
              + (f" (chain={args.chain})" if args.chain else "") + ".", file=sys.stderr)
        return EXIT_NO_MATCHES

    print(f"Kroger-family stores near {args.zip_code}:")
    print()
    for i, s in enumerate(stores, start=1):
        row = _store_row(s)
        addr = row["address"]
        dist = f"{row['distance']:.1f} mi" if row["distance"] is not None else ""
        print(f"  {i:>2}  {row['chain']:<18} {row['name']:<28} {addr}  {dist}".rstrip())
    print()
    print("To pin one, note its ID and (for phase 1) hand-write it into ~/.buzzcheck/config.json:")
    print('  { "default_store": "primary", "stores": { "primary": { "location_id": "<ID>", "chain": "...", "name": "...", "address": "..." } } }')
    return EXIT_ON_SALE


def _store_row(s: dict) -> dict:
    addr = s.get("address") or {}
    address_str = ", ".join(
        p for p in [addr.get("addressLine1"), addr.get("city"), f'{addr.get("state","")} {addr.get("zipCode","")}'.strip()]
        if p
    )
    return {
        "location_id": s.get("locationId", ""),
        "chain": s.get("chain", "") or "",
        "name": s.get("name", "") or "",
        "address": address_str,
        "distance": s.get("distance"),
    }


# ---------- main check ----------


def run_check(argv: list[str]) -> int:
    parser = build_main_parser()
    args = parser.parse_args(argv)

    try:
        line_filter = parse_line_arg(args.line)
    except UsageError as e:
        print(f"buzzcheck: {e}", file=sys.stderr)
        return EXIT_ERROR

    if args.add:
        # --add is phase 2. Reject up front rather than fetching and then failing.
        print("buzzcheck: --add is not implemented in phase 1.", file=sys.stderr)
        return EXIT_ERROR
    if args.upc:
        print("buzzcheck: --upc is not implemented in phase 1 (used only with --add).", file=sys.stderr)
        return EXIT_ERROR
    if args.qty != 1:
        print("buzzcheck: --qty is not meaningful without --add (phase 2).", file=sys.stderr)
        return EXIT_ERROR

    try:
        creds = load_creds()
    except UsageError as e:
        _emit_error(str(e), json_out=args.json_out)
        return EXIT_ERROR

    if not config_exists():
        _emit_error(
            "no store pinned. Run `buzzcheck stores --zip <zip>` to find your store, "
            "then hand-write ~/.buzzcheck/config.json (see README/PRD). "
            "In phase 2, `buzzcheck setup` will do this for you.",
            json_out=args.json_out,
        )
        return EXIT_ERROR

    try:
        store = load_store(args.store)
    except ConfigMissing as e:
        _emit_error(str(e), json_out=args.json_out)
        return EXIT_ERROR

    try:
        with KrogerClient(creds) as client:
            variants = fetch_all_variants(client, store.location_id)
    except KrogerAuthError as e:
        _emit_error(f"auth error: {e}", json_out=args.json_out)
        return EXIT_ERROR
    except KrogerError as e:
        _emit_error(f"API error: {e}", json_out=args.json_out)
        return EXIT_ERROR

    if line_filter:
        filtered = [v for v in variants if v.line == line_filter]
    else:
        filtered = list(variants)

    if args.json_out:
        sys.stdout.write(render_json(variants, filtered, line_filter, store))
    else:
        sys.stdout.write(render_human(variants, filtered, line_filter, store, args.show_all))

    if not filtered:
        return EXIT_NO_MATCHES
    if any(v.on_sale for v in filtered):
        return EXIT_ON_SALE
    return EXIT_NOT_ON_SALE


def _emit_error(msg: str, *, json_out: bool) -> None:
    print(f"buzzcheck: {msg}", file=sys.stderr)
    if json_out:
        # keep stdout parseable
        sys.stdout.write(json.dumps({"error": msg}) + "\n")
