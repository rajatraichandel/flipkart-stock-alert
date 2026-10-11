#!/usr/bin/env python3
"""
Check Flipkart product pages and send a Telegram message when a watched product
comes back in stock.

Runs one check per invocation; launchd runs it every 5 minutes. Uses only the
Python standard library. Configure products in config.json and Telegram
credentials in .env.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional


APP_DIR = Path(__file__).resolve().parent
LOG_PATH = APP_DIR / "logs" / "alert.log"
MAX_LOG_BYTES = 1_000_000

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)

# schema.org availability values that mean "you can buy it now".
IN_STOCK_SCHEMA = {"InStock", "LimitedAvailability", "OnlineOnly", "InStoreOnly"}

BLOCK_MARKERS = ("are you a human", "captcha", "access denied")


@dataclass(frozen=True)
class Product:
    name: str
    url: str
    sku: str
    listing_id: str


@dataclass(frozen=True)
class StockStatus:
    available: Optional[bool]
    reason: str
    title: Optional[str] = None
    price: Optional[float] = None


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line)
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LOG_PATH.exists() and LOG_PATH.stat().st_size > MAX_LOG_BYTES:
        LOG_PATH.replace(LOG_PATH.with_suffix(".log.1"))
    with LOG_PATH.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def load_env_file(path: Path) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


def load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: dict[str, Any]) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    with tmp_path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
        f.write("\n")
    tmp_path.replace(path)


def configured_products(config: dict[str, Any]) -> list[Product]:
    products = []
    for item in config.get("products", []):
        if not item.get("enabled", True):
            continue
        query = urllib.parse.parse_qs(urllib.parse.urlparse(item["url"]).query)
        sku = item.get("sku") or (query.get("pid") or [""])[0]
        listing_id = item.get("listing_id") or (query.get("lid") or [""])[0]
        if not sku and not listing_id:
            raise SystemExit(f"Product URL needs a pid or lid parameter: {item['url']}")
        products.append(Product(item["name"], item["url"], sku, listing_id))
    return products


def fetch(url: str, timeout_seconds: int) -> tuple[int, str]:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-IN,en;q=0.9",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout_seconds, context=ssl.create_default_context()
        ) as response:
            body = response.read()
            encoding = response.headers.get_content_charset() or "utf-8"
            return response.status, body.decode(encoding, errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")


def json_ld_offer(html: str, sku: str) -> Optional[dict[str, Any]]:
    """Return the schema.org Product block for this SKU, if the page has one."""
    for match in re.finditer(
        r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S
    ):
        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if not isinstance(item, dict) or not isinstance(item.get("offers"), dict):
                continue
            if not sku or item.get("sku") == sku:
                return item
    return None


def listing_availability(html: str, listing_id: str) -> Optional[str]:
    """Return Flipkart's own availabilityStatus (e.g. IN_STOCK) for this listing."""
    if not listing_id:
        return None
    for match in re.finditer(re.escape(f'"listingId":"{listing_id}"'), html):
        window = html[match.end() : match.end() + 600]
        status = re.search(r'"availabilityStatus":"([A-Z_]+)"', window)
        if status:
            return status.group(1)
    return None


def parse_status(product: Product, html: str) -> StockStatus:
    product_ld = json_ld_offer(html, product.sku)
    listing_status = listing_availability(html, product.listing_id)

    title = None
    price = None
    schema_value = None
    if product_ld:
        title = product_ld.get("name")
        offers = product_ld["offers"]
        schema_value = str(offers.get("availability", "")).rsplit("/", 1)[-1] or None
        try:
            price = float(offers["price"])
        except (KeyError, TypeError, ValueError):
            pass

    if schema_value is None and listing_status is None:
        lowered = html.lower()
        if any(marker in lowered for marker in BLOCK_MARKERS):
            return StockStatus(None, "page looks like a bot check", title, price)
        return StockStatus(None, "no stock data found on page", title, price)

    reason = f"schema={schema_value}, listing={listing_status}"
    # Either signal saying "in stock" is enough. Missing a restock is worse
    # than one extra message.
    available = schema_value in IN_STOCK_SCHEMA or listing_status == "IN_STOCK"
    return StockStatus(available, reason, title, price)


def check_product(product: Product, timeout_seconds: int) -> StockStatus:
    last_error = ""
    for attempt in range(2):
        try:
            status_code, html = fetch(product.url, timeout_seconds)
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = f"network error: {exc}"
        else:
            if status_code == 200:
                return parse_status(product, html)
            last_error = f"HTTP {status_code}"
        if attempt == 0:
            time.sleep(5)
    return StockStatus(None, last_error)


def telegram_send(token: str, chat_id: str, message: str, timeout_seconds: int) -> None:
    payload = urllib.parse.urlencode(
        {"chat_id": chat_id, "text": message, "disable_web_page_preview": "false"}
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage", data=payload, method="POST"
    )
    with urllib.request.urlopen(
        request, timeout=timeout_seconds, context=ssl.create_default_context()
    ) as response:
        data = json.loads(response.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data}")


def run_checks(config: dict[str, Any], state_path: Path, token: str, chat_id: str) -> None:
    timeout_seconds = int(config.get("timeout_seconds", 20))
    failure_alert_after = int(config.get("failure_alert_after", 6))
    state = load_json(state_path, {"products": {}})
    product_state = state.setdefault("products", {})

    def send(message: str) -> bool:
        try:
            telegram_send(token, chat_id, message, timeout_seconds)
            return True
        except Exception as exc:
            log(f"[error] Could not send Telegram message: {exc}")
            return False

    for product in configured_products(config):
        key = product.sku or product.listing_id
        previous = product_state.get(key, {})
        status = check_product(product, timeout_seconds)
        now = int(time.time())

        entry = {
            "name": product.name,
            "url": product.url,
            "available": status.available,
            "reason": status.reason,
            "price": status.price,
            "last_checked_at": now,
            "notified_available": bool(previous.get("notified_available")),
            "consecutive_failures": int(previous.get("consecutive_failures", 0)),
            "failure_warning_sent": bool(previous.get("failure_warning_sent")),
        }

        status_text = {True: "IN STOCK", False: "out of stock", None: "unknown"}[status.available]
        price_text = f" Rs.{status.price:g}" if status.price else ""
        log(f"[check] {product.name}: {status_text}{price_text} ({status.reason})")

        if status.available is None:
            entry["consecutive_failures"] += 1
            if (
                entry["consecutive_failures"] >= failure_alert_after
                and not entry["failure_warning_sent"]
            ):
                minutes = entry["consecutive_failures"] * 5
                if send(
                    f"Flipkart alert problem: could not read stock for about {minutes} min.\n"
                    f"{product.name}\nLast error: {status.reason}\n{product.url}"
                ):
                    entry["failure_warning_sent"] = True
        else:
            if entry["failure_warning_sent"]:
                send(f"Flipkart alert is working again for: {product.name}")
            entry["consecutive_failures"] = 0
            entry["failure_warning_sent"] = False

        if status.available is True and not entry["notified_available"]:
            title = status.title or product.name
            if send(f"Back in stock on Flipkart: {title}{price_text}\n{product.url}"):
                entry["notified_available"] = True
                log(f"[sent] {product.name}")
        elif status.available is False:
            # Re-arm so the next restock sends a fresh alert.
            entry["notified_available"] = False

        product_state[key] = entry

    save_json(state_path, state)


def main() -> None:
    load_env_file(APP_DIR / ".env")

    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(APP_DIR / "config.json"))
    parser.add_argument("--state", default=str(APP_DIR / "state.json"))
    parser.add_argument(
        "--test-telegram", action="store_true", help="Send a test message and exit"
    )
    args = parser.parse_args()

    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env")

    config = load_json(Path(args.config), {})

    if args.test_telegram:
        names = ", ".join(p.name for p in configured_products(config))
        telegram_send(
            token,
            chat_id,
            f"Flipkart stock alert is set up. Checking every 5 minutes: {names}",
            20,
        )
        log("[test] Telegram test message sent")
        return

    run_checks(config, Path(args.state), token, chat_id)


if __name__ == "__main__":
    main()
