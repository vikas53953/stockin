"""Stockin ledger domain: synthesized sketch ("facts in, everything else derived").

Shape in one paragraph
----------------------
The database stores only FACTS that somebody outside the program asserted:
a statement said a transaction happened, a statement said a balance was N
units, a statement said "I list every transaction in this window", the owner
said "my EPF is X" or "my thesis on INFY is Y". Every fact has a
content-derived id, so storing is set-union and import is idempotent by
construction. Nothing derived is ever stored: no holdings table, no lots
table, no synthetic "opening balance" rows. The whole fact set is loaded
into one immutable in-memory `Book`, and holdings, lots, reconcile, net
worth and review verdicts are pure functions of (Book, PriceBook, date).

Module map (src/stockin/...)                       who may import it
----------------------------------------------------------------------------
money.py        Value, Money, Qty, QtyDelta, Price, Bps   everyone
facts.py        Fact union, Instrument, Account    everyone
book.py         Book, Timeline, Holding, Lot,      everyone (pure)
                reconcile()
prices.py       PriceBook, Quote, QuoteSource port everyone (pure + 1 port)
networth.py     networth()                         everyone (pure)
review.py       review(), Verdict, due_alerts()    everyone (pure)
store.py        Ledger (read), LedgerWriter        app only
sources/cas.py  casparser dict -> Statement        app only
app.py          Desk (read), Clerk (write)         surfaces: web/mcp/alerts get
                                                   Desk, cli gets Clerk
cli.py          thin typer shell                   nobody

import-linter contracts (CI): store and sources are imported only by app;
Clerk only by cli; pure modules never import sqlite3, casparser, httpx,
or datetime.now.

Dependency direction: money <- facts <- book <- {networth, review};
store and sources depend on facts only. The pure modules never import
sqlite3, casparser, httpx or the clock. `today` and prices are arguments.

Data flow for every read surface (CLI, dashboard, MCP, Telegram):

    Desk.open(cfg).review(today) / .networth(today) / .reconcile()
        (inside: Ledger.book() + Desk.prices(book) -> pure function)
        -> frozen pydantic result -> JSON
"""
