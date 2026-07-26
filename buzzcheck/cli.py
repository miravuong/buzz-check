from __future__ import annotations

import argparse
import http.server
import json
import os
import re
import secrets
import sys
import urllib.parse
import webbrowser
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from dotenv import load_dotenv

from buzzcheck.config import (
    CONFIG_PATH,
    TOKEN_PATH,
    ConfigMissing,
    Store,
    config_exists,
    load_refresh_token,
    load_store,
    save_refresh_token,
    save_store,
)
from buzzcheck.kroger import ClientCreds, KrogerAuthError, KrogerClient, KrogerError


# ---------- exit codes ----------

EXIT_ON_SALE = 0
EXIT_NOT_ON_SALE = 1
EXIT_ERROR = 2
EXIT_NO_MATCHES = 3


# ---------- BuzzBallz search config ----------

BUZZBALLZ_TERMS = ["BuzzBallz", "Buzz Ballz"]
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
    Query multiple term variants, merge, dedup by UPC. Kroger's /products
    requires filter.term or filter.productId — brand alone is rejected —
    so we rely on term queries and post-filter with is_buzzballz.
    """
    queries: list[dict] = [{"term": t} for t in BUZZBALLZ_TERMS]

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
    try:
        if argv and argv[0] in SUBCOMMANDS:
            cmd = argv[0]
            rest = argv[1:]
            if cmd == "stores":
                return run_stores(rest)
            if cmd == "setup":
                return run_setup(rest)
            if cmd == "auth":
                return run_auth(rest)
            if cmd == "selftest":
                return run_selftest(rest)
        return run_check(argv)
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
    except KeyboardInterrupt:
        print("\nbuzzcheck: interrupted.", file=sys.stderr)
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
    print(f"To pin one: `buzzcheck setup --zip {args.zip_code} --pick <N>`  (or `buzzcheck setup --location-id <ID>`).")
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
        return _run_check_inner(args)
    except (UsageError, ConfigMissing, KrogerAuthError, KrogerError) as e:
        if args.json_out:
            sys.stdout.write(json.dumps({"error": str(e)}) + "\n")
        raise


def _run_check_inner(args: argparse.Namespace) -> int:
    line_filter = parse_line_arg(args.line)

    if args.upc and not args.add:
        raise UsageError("--upc is only meaningful with --add.")
    if args.qty != 1 and not args.add:
        raise UsageError("--qty is only meaningful with --add.")
    if args.qty < 1:
        raise UsageError("--qty must be a positive integer.")
    if args.yes and not args.add:
        raise UsageError("--yes is only meaningful with --add.")

    creds = load_creds()

    if not config_exists():
        raise ConfigMissing(
            "no store pinned. Run `buzzcheck setup` (or `buzzcheck stores --zip <zip>` "
            "to browse first)."
        )

    store = load_store(args.store)

    with KrogerClient(creds) as client:
        variants = fetch_all_variants(client, store.location_id)

        filtered = [v for v in variants if v.line == line_filter] if line_filter else list(variants)

        if args.json_out:
            sys.stdout.write(render_json(variants, filtered, line_filter, store))
        else:
            sys.stdout.write(render_human(variants, filtered, line_filter, store, args.show_all))

        if not filtered:
            return EXIT_NO_MATCHES

        on_sale = [v for v in filtered if v.on_sale]

        if args.add:
            if not on_sale:
                return EXIT_NOT_ON_SALE
            return _add_flow(client, creds, store, on_sale, args)

        if on_sale:
            return EXIT_ON_SALE
        return EXIT_NOT_ON_SALE


# ---------- add-to-cart flow ----------


KROGER_BANNERS = [
    "Ralphs", "Fred Meyer", "King Soopers", "Smith's", "Fry's",
    "QFC", "Dillons", "Harris Teeter", "Food4Less",
]


def _add_flow(client: KrogerClient, creds: ClientCreds, store: Store,
              on_sale: list[Variant], args: argparse.Namespace) -> int:
    if args.upc:
        matches = [v for v in on_sale if v.upc == args.upc]
        if not matches:
            raise UsageError(
                f"--upc {args.upc} is not among the on-sale variants at {store.name or store.location_id}."
            )
        chosen = matches[0]
    elif len(on_sale) == 1:
        chosen = on_sale[0]
        if not args.yes:
            if not _confirm(f"Add {args.qty}x '{chosen.description}' ({chosen.size}) to cart?"):
                print("buzzcheck: cancelled.", file=sys.stderr)
                return EXIT_ON_SALE
    else:
        if args.yes:
            raise UsageError(
                f"{len(on_sale)} variants on sale; --yes only works with exactly one. "
                "Pass --upc <upc> to pick."
            )
        if args.json_out or not sys.stdin.isatty():
            raise UsageError(
                f"{len(on_sale)} variants on sale and no tty for a prompt. "
                "Pass --upc <upc> or --yes (if only one)."
            )
        chosen = _pick_variant(on_sale)
        if chosen is None:
            print("buzzcheck: cancelled.", file=sys.stderr)
            return EXIT_ON_SALE

    user_token = _get_user_access_token(client, creds)

    try:
        client.add_to_cart(
            user_access_token=user_token,
            upc=chosen.upc,
            quantity=args.qty,
            modality=args.modality,
        )
    except KrogerError as e:
        msg = str(e).lower()
        if "timeout" in msg or "timed out" in msg or "read" in msg and "error" in msg:
            print(
                "buzzcheck: cart add outcome is unknown (timeout / ambiguous failure). "
                "Check your Kroger cart before retrying — the item may already be in it.",
                file=sys.stderr,
            )
            return EXIT_ERROR
        raise

    print(f"Added {args.qty}x '{chosen.description}' to cart ({args.modality}).", file=sys.stderr)
    return EXIT_ON_SALE


def _confirm(prompt: str) -> bool:
    if not sys.stdin.isatty():
        return False  # never spend money on Enter-through-a-pipe
    try:
        answer = input(f"{prompt} [y/N] ").strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")


def _pick_variant(variants: list[Variant]) -> Optional[Variant]:
    print("On sale:", file=sys.stderr)
    for i, v in enumerate(variants, start=1):
        print(f"  {i}. {v.description} ({v.size})  ${v.promo:.2f} (was ${v.regular:.2f})", file=sys.stderr)
    try:
        raw = input(f"Select [1-{len(variants)}, q to cancel]: ").strip().lower()
    except EOFError:
        return None
    if raw in ("q", ""):
        return None
    try:
        idx = int(raw)
    except ValueError:
        return None
    if 1 <= idx <= len(variants):
        return variants[idx - 1]
    return None


# ---------- user OAuth (cart write) ----------


def _get_user_access_token(client: KrogerClient, creds: ClientCreds) -> str:
    refresh = load_refresh_token()
    if not refresh:
        raise UsageError(
            "No cart authorization on file. Run `buzzcheck auth` once to grant access."
        )
    try:
        body = client.refresh_user_token(refresh)
    except KrogerAuthError as e:
        raise KrogerAuthError(
            f"Refresh failed ({e}). Run `buzzcheck auth --force` to re-authorize."
        )
    new_refresh = body.get("refresh_token")
    if isinstance(new_refresh, str) and new_refresh and new_refresh != refresh:
        save_refresh_token(new_refresh)
    return body["access_token"]


class _OAuthCallbackHandler(http.server.BaseHTTPRequestHandler):
    captured: dict = {}

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        qs = urllib.parse.parse_qs(parsed.query)
        self.captured["code"] = (qs.get("code") or [None])[0]
        self.captured["state"] = (qs.get("state") or [None])[0]
        self.captured["error"] = (qs.get("error") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        body = (
            "<html><body style='font-family:sans-serif;padding:2em'>"
            "<h2>buzzcheck: authorization received.</h2>"
            "<p>You can close this tab.</p></body></html>"
        )
        self.wfile.write(body.encode())

    def log_message(self, *args, **kwargs):  # silence stderr access logs
        pass


def _run_oauth_flow(client: KrogerClient, redirect_uri: str) -> dict:
    parsed = urllib.parse.urlparse(redirect_uri)
    host = parsed.hostname or "localhost"
    port = parsed.port or 8000
    if parsed.path != "/callback":
        raise UsageError(
            f"KROGER_REDIRECT_URI must end in /callback (got {redirect_uri})."
        )

    state = secrets.token_urlsafe(16)
    _OAuthCallbackHandler.captured = {}
    auth_url = client.build_authorize_url(redirect_uri=redirect_uri, state=state)

    try:
        server = http.server.HTTPServer((host, port), _OAuthCallbackHandler)
    except OSError as e:
        raise UsageError(
            f"Cannot bind {host}:{port} for the OAuth callback ({e}). "
            "Close whatever is using that port, or update KROGER_REDIRECT_URI."
        )

    print("Opening browser for Kroger authorization...", file=sys.stderr)
    print(f"If it doesn't open, visit:\n  {auth_url}", file=sys.stderr)
    try:
        webbrowser.open(auth_url)
    except Exception:
        pass

    try:
        server.timeout = None
        while not _OAuthCallbackHandler.captured:
            server.handle_request()
    finally:
        server.server_close()

    captured = _OAuthCallbackHandler.captured
    if captured.get("error"):
        raise KrogerAuthError(f"Authorization failed: {captured['error']}")
    if captured.get("state") != state:
        raise KrogerAuthError("State mismatch in OAuth callback (possible CSRF).")
    if not captured.get("code"):
        raise KrogerAuthError("No authorization code received.")

    return client.exchange_auth_code(code=captured["code"], redirect_uri=redirect_uri)


# ---------- auth subcommand ----------


def run_auth(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="buzzcheck auth")
    parser.add_argument("--force", action="store_true", help="Re-run even if a refresh token exists.")
    args = parser.parse_args(argv)

    creds = load_creds()
    load_dotenv()
    redirect_uri = os.environ.get("KROGER_REDIRECT_URI", "http://localhost:8000/callback").strip()

    if load_refresh_token() and not args.force:
        print(
            "buzzcheck: cart authorization already exists. Use --force to re-run.",
            file=sys.stderr,
        )
        return EXIT_ON_SALE

    with KrogerClient(creds) as client:
        body = _run_oauth_flow(client, redirect_uri)

    refresh = body.get("refresh_token")
    if not isinstance(refresh, str) or not refresh:
        raise KrogerAuthError("No refresh token in exchange response.")
    save_refresh_token(refresh)
    print(f"Cart authorization saved to {TOKEN_PATH}.", file=sys.stderr)
    return EXIT_ON_SALE


# ---------- selftest subcommand ----------


def run_selftest(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="buzzcheck selftest",
        description="Diagnostic: confirm the connection works. Not a search.",
    )
    # Explicitly reject positional args so users can't sneak in a term.
    parser.parse_args(argv)

    creds = load_creds()
    if not config_exists():
        raise ConfigMissing(
            "no store pinned. Run `buzzcheck setup` first."
        )
    store = load_store()

    with KrogerClient(creds) as client:
        # Force a token fetch so we can report auth status separately.
        client._get_app_token()
        products = client.search_products(location_id=store.location_id, term="milk", limit=25)

    total = sum(len(p.get("items") or []) or 1 for p in products)
    priced = 0
    for p in products:
        for item in (p.get("items") or [{}]):
            price = item.get("price") or {}
            if price.get("regular") or price.get("promo"):
                priced += 1

    print(f"Store:    {store.name or store.location_id} ({store.location_id})", file=sys.stderr)
    print("Auth:     ok (client credentials)", file=sys.stderr)
    print(f'Query:    "milk" returned {total} items, {priced} with prices', file=sys.stderr)
    print("", file=sys.stderr)
    if priced > 0:
        print(
            "Connection is working. If `buzzcheck` finds nothing, this store "
            "does not stock BuzzBallz.",
            file=sys.stderr,
        )
        return EXIT_ON_SALE
    print(
        "No priced items returned. Something is off — check that the pinned "
        "location_id is a real store and the app has the Products API enabled.",
        file=sys.stderr,
    )
    return EXIT_ERROR


# ---------- setup subcommand ----------


def run_setup(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="buzzcheck setup")
    parser.add_argument("--zip", dest="zip_code", default=None, help="5-digit ZIP (prompted if absent).")
    parser.add_argument("--radius", type=int, default=10, help="Miles (default 10, max 100).")
    parser.add_argument("--chain", default=None, help="Filter to a chain, e.g. Ralphs.")
    parser.add_argument("--pick", type=int, default=None, help="Non-interactive: pick the Nth result.")
    parser.add_argument("--location-id", dest="location_id", default=None,
                        help="Skip search; use this ID directly (validated against the API).")
    parser.add_argument("--force", action="store_true", help="Overwrite existing config.")
    parser.add_argument("--name", default="primary", help="Config key for the store (default: primary).")
    args = parser.parse_args(argv)

    if args.radius < 1 or args.radius > 100:
        raise UsageError(f"--radius must be between 1 and 100 (got {args.radius}).")

    if config_exists() and not args.force:
        try:
            current = load_store()
            print("A store is already pinned:", file=sys.stderr)
            print(f"  {current.chain} — {current.name}", file=sys.stderr)
            print(f"  ID:      {current.location_id}", file=sys.stderr)
            print(f"  Address: {current.address}", file=sys.stderr)
            print("Re-run with --force to change.", file=sys.stderr)
            return EXIT_ERROR
        except ConfigMissing:
            pass  # malformed config; treat as overwrite

    creds = load_creds()

    with KrogerClient(creds) as client:
        if args.location_id:
            store = _setup_by_id(client, args.location_id)
        else:
            store = _setup_by_zip(client, args)

    save_store(store, name=args.name, make_default=True)
    print("", file=sys.stderr)
    print(f"Pinned: {store.chain} — {store.name}", file=sys.stderr)
    print(f"  ID:      {store.location_id}", file=sys.stderr)
    print(f"  Address: {store.address}", file=sys.stderr)
    print(f"  Config:  {CONFIG_PATH}", file=sys.stderr)
    return EXIT_ON_SALE


def _setup_by_id(client: KrogerClient, location_id: str) -> Store:
    if not re.fullmatch(r"\d+", location_id):
        raise UsageError(f"--location-id must be numeric (got '{location_id}').")
    data = client.get_location(location_id)
    if not data:
        raise UsageError(
            f"Location {location_id} not found. Run `buzzcheck stores --zip <zip>` to browse."
        )
    row = _store_row(data)
    return Store(
        location_id=row["location_id"] or location_id,
        chain=row["chain"],
        name=row["name"],
        address=row["address"],
        resolved_at=_now_iso(),
    )


def _setup_by_zip(client: KrogerClient, args: argparse.Namespace) -> Store:
    zip_code = args.zip_code
    if not zip_code:
        if not sys.stdin.isatty():
            raise UsageError("--zip is required in non-interactive contexts.")
        try:
            zip_code = input("ZIP code: ").strip()
        except EOFError:
            raise UsageError("no ZIP provided.")
    if not re.fullmatch(r"\d{5}", zip_code or ""):
        raise UsageError(f"invalid zip '{zip_code}' (must be 5 digits).")

    radius = args.radius
    stores = client.find_locations(zip_code=zip_code, radius=radius, chain=args.chain, limit=25)
    if not stores:
        radius = min(radius * 2, 100)
        print(f"buzzcheck: no stores within {args.radius} mi; retrying at {radius} mi...", file=sys.stderr)
        stores = client.find_locations(zip_code=zip_code, radius=radius, chain=args.chain, limit=25)

    if not stores:
        raise UsageError(
            f"No Kroger-family stores within {radius} miles of {zip_code}. "
            f"Kroger operates as {', '.join(KROGER_BANNERS)}. "
            "If none of these operate near you, this tool will not work."
        )

    rows = [_store_row(s) for s in stores]

    if args.pick is not None:
        if not (1 <= args.pick <= len(rows)):
            raise UsageError(
                f"--pick {args.pick} out of range (found {len(rows)} stores)."
            )
        chosen = rows[args.pick - 1]
    else:
        if not sys.stdin.isatty():
            raise UsageError(
                f"{len(rows)} stores found; pass --pick N in non-interactive contexts."
            )
        print(f"Kroger-family stores near {zip_code}:", file=sys.stderr)
        print("", file=sys.stderr)
        for i, r in enumerate(rows, start=1):
            dist = f"{r['distance']:.1f} mi" if r["distance"] is not None else ""
            print(f"  {i:>2}  {r['chain']:<16} {r['name']:<26} {r['address']}  {dist}".rstrip(),
                  file=sys.stderr)
        print("", file=sys.stderr)
        try:
            raw = input(f"Select a store [1-{len(rows)}, q to cancel]: ").strip().lower()
        except EOFError:
            raise UsageError("cancelled.")
        if raw in ("", "q"):
            raise UsageError("cancelled.")
        try:
            idx = int(raw)
        except ValueError:
            raise UsageError(f"invalid selection '{raw}'.")
        if not (1 <= idx <= len(rows)):
            raise UsageError(f"selection {idx} out of range.")
        chosen = rows[idx - 1]

    return Store(
        location_id=chosen["location_id"],
        chain=chosen["chain"],
        name=chosen["name"],
        address=chosen["address"],
        resolved_at=_now_iso(),
    )
