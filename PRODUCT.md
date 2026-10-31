# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users
A single owner who is new to trading, checking in briefly a few times a day while the bot trades on its own. They do not know trading jargon and should never need it to understand what is happening.

## Product Purpose
A personal automated trading bot for stocks (options later). The bot finds trades, sizes them, manages exits and enforces loss limits by itself. The UI exists to answer two questions at a glance: is it running (and if not, why), and am I making or losing money. Success is that the owner rarely needs to touch anything.

## Operating Context
- Paper trading through an IB Gateway broker connection; small account (under $100) in "growth mode".
- Live data arrives over one WebSocket; the dashboard is a React/Tailwind dark-theme app.
- Advanced tools (watchlist, backtesting, system health, strategy parameters) exist but are optional.

## Capabilities and Constraints
- UI-only changes: no backend or API changes in this effort. The flatten endpoint still requires `{"confirm": "FLATTEN"}`; the UI sends it silently.
- Dark theme stays.
- Plain-language copy; no unexplained trading terms.
- Undecided: first-run setup defaults (default strategies and symbols).

## Evidence on Hand
No testimonials, customers or benchmarks exist; none should be invented.

## Product Principles
- Automation first: the default state needs no input.
- Say what the bot is doing, and why it isn't, in a sentence.
- Money results (P&L, history) beat risk instrumentation on the main screens.
- Advanced controls stay available but out of the way.
- Safety actions (pause, sell everything) are always reachable and never buried.
