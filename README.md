# buzzcheck

Check whether BuzzBallz are on sale at your Kroger store from the terminal, and stage them in your cart if they are.

The product is fixed. The store is yours to pick.

Works with any Kroger-family store: Ralphs, Fred Meyer, King Soopers, Smith's, Fry's, QFC, Dillons, Harris Teeter, Food4Less. You pick your store once during setup.

Stops at the cart. It cannot check out, by design.

```console
$ buzzcheck
BuzzBallz: 1 of 3 on sale at Ralphs Fresh Fare (Huntington Beach)

  BuzzBallz Chillers Watermelon Smash  200 ml
  $2.79  (was $3.49)  20% off

Run with --add to put it in your cart.

$ buzzcheck --add
Add 1x BuzzBallz Chillers Watermelon Smash to cart? [y/N]: y
Added. Review your cart at ralphs.com before checkout.
```

Finds nothing? `buzzcheck selftest` tells you whether the connection is
broken or your store just does not carry them.

## Requirements

- Python 3.10+
- A Kroger account, the one you actually shop with
- A free developer app at [developer.kroger.com](https://developer.kroger.com)

## Setup

### 1. Register a Kroger app

At [developer.kroger.com](https://developer.kroger.com), create an application.

| Setting | Value |
|---|---|
| Environment | **Production** |
| API products | Cart, Locations, Products |
| Redirect URI | `http://localhost:8000/callback` |

Two things people get wrong here:

**Choose Production, not Certification.** Kroger user accounts do not exist in the certification environment, so the Cart API cannot work there. Production means real data and your real cart, not that you are publishing anything.

**Set the Redirect URI.** The form marks it optional. It is not. Cart authorization fails without it, and the error does not mention it.

Save your Client ID and Client Secret.

### 2. Install

```bash
git clone https://github.com/YOURNAME/buzzcheck
cd buzzcheck
python -m venv .venv && source .venv/bin/activate
pip install -e .
```

### 3. Add credentials

```bash
cp .env.example .env
```

Then fill it in:

```
KROGER_CLIENT_ID=your_client_id
KROGER_CLIENT_SECRET=your_client_secret
KROGER_REDIRECT_URI=http://localhost:8000/callback
```

`.env` is gitignored. Keep it that way.

### 4. Pick your store

Kroger store IDs are internal and cannot be looked up outside their API, so `setup` finds yours for you:

```console
$ buzzcheck setup
Zip code: 92649

Kroger-family stores near 92649:

  1  Ralphs Fresh Fare   5241 Warner Ave, Huntington Beach, CA 92649    0.8 mi
  2  Ralphs              9840 Adams Ave, Huntington Beach, CA 92646     2.3 mi
  3  Food4Less           16600 Beach Blvd, Westminster, CA 92683        3.1 mi

Select a store [1-3, q to cancel]: 1

Saved: Ralphs Fresh Fare (70100123)
       5241 Warner Ave, Huntington Beach, CA 92649
```

Read that confirmation. If it is not the store you meant, run `buzzcheck setup --force` and pick again.

Other ways in:

```bash
buzzcheck stores --zip 92649                 # just look, save nothing
buzzcheck stores --zip 92649 --chain Ralphs  # filter by banner
buzzcheck setup --zip 92649 --pick 1         # non-interactive
buzzcheck setup --location-id 70100123       # if you already know it
```

If your local store is not called Kroger, that is normal. Search anyway and look for your banner in the results. If nothing comes back within 20 miles, Kroger may not operate in your area.

### 5. Authorize cart access

Only needed if you plan to use `--add`. Searching works without it.

```bash
buzzcheck auth
```

Opens your browser to Kroger's login and permission screen. You are granting access to your own registered app, not a third party. The refresh token is saved to `~/.buzzcheck/token.json` at mode `0600` and reused, so you should not see this again.

## Usage

```
buzzcheck [LINE] [OPTIONS]
```

| Command | What it does |
|---|---|
| `buzzcheck` | Check every BuzzBallz line at your store |
| `buzzcheck chillers` | Only Chillers |
| `buzzcheck cocktails` | Only the original Cocktails |
| `buzzcheck biggies` | Only the 1.75 L Biggies |
| `buzzcheck selftest` | Verify your connection works |
| `buzzcheck --all` | Show every variant, not just on-sale ones |
| `buzzcheck --add` | Prompt to add, if anything is on sale |
| `buzzcheck --add --qty 2` | Add two |
| `buzzcheck --add --yes` | Skip the prompt. Only when exactly one item is on sale. |
| `buzzcheck --add --upc 0008532100013` | Add a specific item |
| `buzzcheck --json` | Machine-readable output |
| `buzzcheck --modality DELIVERY` | Override fulfillment. Default is pickup. |

`LINE` is one of `chillers`, `cocktails`, `biggies`, or `uncategorized`. It filters what you see, not what gets fetched, so the output always tells you how many variants in other lines were hidden. It is not a search box: `buzzcheck milk` is a usage error.

`--yes` refuses to act when two or more variants are on sale, and tells you to pass `--upc` instead. Picking one for you would be guessing, and a wrong order is harder to notice than an error message.

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Something is on sale |
| `1` | Nothing is on sale |
| `2` | Error: no config, auth failure, network, bad input |
| `3` | No matches for that term |

Which makes it composable:

```bash
# Desktop notification when it goes on sale
buzzcheck && osascript -e 'display notification "BuzzBallz on sale"'

# Check every morning at 9
0 9 * * * /path/to/.venv/bin/buzzcheck || true

# Pull just the discounted items
buzzcheck --json | jq '.variants[] | select(.on_sale)'
```

Note that `1` means "worked fine, nothing on sale," not failure. Only `2` is an actual error.

## Where things live

| Path | Contains | Commit it? |
|---|---|---|
| `.env` | Client ID and secret | **Never** |
| `~/.buzzcheck/config.json` | Your store and default term | Outside the repo |
| `~/.buzzcheck/token.json` | OAuth refresh token, mode `0600` | Outside the repo |

Config and tokens live in your home directory rather than the project, so cloning this repo never inherits someone else's store and `git add -A` cannot publish your credentials.

## Two behaviors worth understanding

**Cart adds are not idempotent.** Kroger's `/cart/add` is additive, not a set operation. Two calls at quantity 1 leave quantity 2 in your cart. There is no retry logic anywhere near that request for this reason. If a call times out, check your cart before running it again.

**Prices require a store.** Kroger's products endpoint returns pricing only when a location ID is attached. Without one it still returns HTTP 200, just with no price data, so everything looks like it is not on sale. If results come back with prices missing, that is the cause.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `command not found: buzzcheck` | Not installed or venv inactive | `source .venv/bin/activate`, re-run `pip install -e .` |
| Exit `2`, "run buzzcheck setup" | No config yet | Run `buzzcheck setup` |
| Zero stores found | Wrong zip, or Kroger absent | Widen `--radius`, or check whether any banner operates near you |
| Results have no prices | Location ID not reaching the request | Confirm `~/.buzzcheck/config.json` has a `location_id` |
| Everything says not on sale | Same as above, or genuinely no promos | Check an item you know is discounted |
| No matches for BuzzBallz | Store does not stock them, or setup issue | Run `buzzcheck selftest` to tell which |
| Browser flow shows an error page | Redirect URI mismatch | Must match the Kroger app registration exactly, port included |
| `403 missing required scopes` | Wrong scope for that call | Search needs `product.compact`, cart needs `cart.basic:write` |
| `401 unauthorized` | Token expired | Should refresh automatically. If not, `buzzcheck auth --force` |
| Cart says unauthorized | Never authorized | Run `buzzcheck auth` |

A 401 and a 403 mean different things here. 401 is an expired token, 403 is asking for the wrong permission. They have different fixes.

## What this does not do

- **Check out.** Cart is the boundary. You review and pay on Kroger's own site.
- **Watch prices.** Single-shot only. Use cron, which is what the exit codes are for.
- **Search for anything else.** BuzzBallz only. The positional argument picks a product line from a fixed list, it is not a search box.
- **Work with non-Kroger retailers.** Kroger banners only.

## Notes

Alcohol is generally pickup-only and availability varies by state and store, which is why `--modality` defaults to `PICKUP`. Stores verify ID at handoff regardless, so adding to your cart stages an order rather than completing a purchase.

Kroger rate limits per endpoint rather than per operation, so operations sharing an endpoint draw from the same budget.

Unofficial client. Not affiliated with, endorsed by, or sponsored by The Kroger Co.

MIT licensed.
