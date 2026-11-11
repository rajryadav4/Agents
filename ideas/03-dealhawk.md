# DEALHAWK — 24/7 steal-deal hunter

**Pitch:** an agent that watches deal communities and the big retailers around the clock and
messages you on WhatsApp the moment something drops to a once-a-year price (or glitches) —
judged against that product's own price history, not the inflated "list price".

**Status:** Built — MVP in [`/dealhawk`](../dealhawk).

## Why
- Real steals (e.g. a $2,000 MacBook Pro at $1,300 for an hour on Halloween) are rare and short.
- Deal sites are noisy: "20% off" is everywhere and means nothing.
- By the time you hear about a glitch, it's fixed.

## What's in the MVP
- Steal Score from per-product price history (novelty vs usual sale low, robust σ-drop, depth).
- Sources: Slickdeals/Reddit feeds (every retailer), direct Walmart/Target/Amazon/+15 retailer page
  harvesting, Keepa (Amazon), Best Buy API, Shopify stores, watched URLs.
- Claude as judge for cold-start items and fake-deal detection; an online model that learns from
  👍/👎, clicks and crowd reactions; exploration alerts to learn faster.
- Live re-verification of suspected price glitches before alerting.
- WhatsApp (CallMeBot / Twilio / Meta Cloud API) with reply-to-train, plus ntfy/Telegram/Discord.
- Docker/systemd for 24/7, and a Claude Code cloud scheduled-task runner with git-persisted state.

## Next
- Cross-retailer price comparison for the same product (UPC/model-number matching).
- A web dashboard of live deals + price-history charts.
- Morning digest of deals held during quiet hours.
- Keepa product-history backfill so new watches start with years of history.
