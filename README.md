# Flipkart Stock Alert

Checks Flipkart product pages every 5 minutes and sends a Telegram message
when a product comes back in stock.

It runs in two places, so it keeps checking when the laptop is closed:

- **Mac:** launchd runs it every 5 minutes while the Mac is awake
  (`~/Library/LaunchAgents/com.rajat.flipkart-stock-alert.plist`).
  Telegram credentials come from `.env`. Log: `logs/alert.log`.
- **GitHub Actions:** cron-job.org starts the `Flipkart Stock Alert` workflow
  every 5 minutes through the GitHub API. Telegram credentials come from the
  repo secrets `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

Products are in `config.json` (any Flipkart URL that has `pid=` in it). After
editing it, commit and push so GitHub uses the same list.

Each place keeps its own state, so a restock can send two messages.

## How it decides

It reads two stock signals from the page for the exact product ID: the
schema.org `availability` value and Flipkart's own `availabilityStatus`.
If either says in stock, it sends one alert. It alerts again only after the
product sells out and comes back.

If it cannot read the page for 6 checks in a row (about 30 minutes), it sends
one warning so the alert never fails silently.

## Commands

Run one check now:

```bash
/usr/bin/python3 ~/flipkart-stock-alert/flipkart_alert.py
```

Send a Telegram test message:

```bash
/usr/bin/python3 ~/flipkart-stock-alert/flipkart_alert.py --test-telegram
```

Stop the alerts:

```bash
launchctl bootout gui/$(id -u)/com.rajat.flipkart-stock-alert
```

Start them again:

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.rajat.flipkart-stock-alert.plist
```

The Mac must be awake for checks to run. After sleep, launchd runs a check
as soon as the Mac wakes up.
