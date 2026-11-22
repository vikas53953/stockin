# Stockin

Stockin is a private, read-only tool that helps one investor decide whether to add to, trim, or exit a holding. It reads your holdings from your monthly CAS statement and your quotes from a broker API, and it checks each position against the thesis you wrote when you bought it.

> **Status: design target.** This README is written first, as the tutorial we are building toward. Commands below do not run yet. Each one lands in a small PR, and its section is marked as working when it does.

## Tutorial: your first review

This walkthrough takes you from an empty install to a review of every holding you own. It takes about fifteen minutes.

### 1. Install

> **Working.** `stockin doctor` runs today.

On your server, behind Tailscale:

```bash
uv tool install git+https://github.com/vikas53953/stockin
stockin doctor
```

Stockin keeps its data in `./data`, or wherever `STOCKIN_DATA` points.

`doctor` checks the database, the data folder, and each configured source, and prints what is missing.

### 2. Import your holdings

> **Working.** `stockin web`, `stockin import cas` and `stockin holdings` run today. A PDF's password is used once and never stored.

**Easiest: the local web app.** Run `stockin web`, then open http://127.0.0.1:8765. Pick your CAS PDF, type its password and press Import. You will see your holdings and the reconcile result. The app only answers on your own computer.

Or from the terminal:

Download your latest CAS from CAMS or KFintech (mutual funds) and NSDL or CDSL (demat, all brokers), then import each one:

```bash
stockin import cas ~/data/cas/2026-10-cams.pdf
stockin import cas ~/data/cas/2026-10-nsdl.pdf
```

Importing the same file twice changes nothing, and overlapping months are never double counted. You see one line per holding, grouped by account. `stockin holdings` shows them again at any time.

Demat statements list balances, not trades, so cost shows as `unknown` for demat holdings until a tradebook importer exists. Stockin never invents a cost.

### 3. Add what the CAS does not cover

```bash
stockin asset add epf  --balance 845000 --as-of 2026-10-31
stockin asset add ppf  --balance 312000 --as-of 2026-10-31
stockin asset add fd   --principal 200000 --rate 7.1 --start 2026-04-01 --months 12
stockin asset add gold --grams 20
```

### 4. Check it matches

> **Working.**

```bash
stockin reconcile
```

Every unit Stockin computes is compared with the CAS. Zero mismatches prints `OK`. Any mismatch prints the ISIN and both numbers, and exits with an error.

### 5. See your net worth

```bash
stockin networth
```

You see the total, the split by asset class, and the drift from your target mix.

### 6. Write a thesis

Pick one holding and record why you own it:

```bash
stockin thesis add INFY \
  --why "Margin recovery as deal wins convert" \
  --invalidate-below 1350 \
  --target 1900 \
  --max-weight 8 \
  --review 2027-01-15
```

### 7. Run your first review

```bash
stockin review
```

Each holding gets one verdict, with the reason beside it:

| Verdict | Means |
|---|---|
| hold | Inside every rule you set |
| add-eligible | Below your max weight and above your invalidation level |
| trim | Above your max weight or past your target |
| exit-check | Price below your invalidation level |
| review-due | Your review date has passed |

### 8. Get alerts and ask questions

- `stockin alerts check` keeps one alert each time a holding's verdict turns into add, trim or exit, worded exactly as the verdict. The same numbers never alert twice; `stockin alerts list` shows them all.
- Telegram sends those alerts to your phone.
- The dashboard at `http://stockin:8000` on your tailnet shows the same data in a browser.
- Claude can query your ledger through the read-only MCP server. Every number it quotes comes from a tool, not from the model.

## What Stockin will never do

- Place, change, or cancel an order.
- Send your data anywhere except the Telegram chat you configure.
- Store a broker password or a long-lived token.

## Development

This project uses [pstack](https://github.com/michael-denyer/pstack-claude). Opening the repository in Claude Code offers to install it from `.claude/settings.json`. Start work with `/pstack:poteto-mode`. Project rules are in [CLAUDE.md](CLAUDE.md).
