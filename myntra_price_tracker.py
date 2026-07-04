"""
myntra_price_tracker.py

Checks the current price of one or more Myntra product pages using a headless
browser (Myntra renders price via JS, so plain requests/BeautifulSoup won't
see it), optionally checks delivery serviceability for a given pincode, and
sends a Telegram alert when a product's price is at or below its configured
target.

NOTE ON PINCODE: Myntra prices are the same nationwide. The pincode is only
used to fetch a delivery-date / serviceability estimate, which is included
in the alert message for convenience -- it does NOT affect the price check.

State (last alerted price per product) is persisted to state.json so you
don't get repeat Telegram pings every run once a price has already triggered
an alert. You get alerted again only if the price changes while still being
at/under target (e.g. drops further), or rises above target and then drops
back down again.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeoutError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("myntra_tracker")

CONFIG_PATH = os.environ.get("MYNTRA_CONFIG", "config.json")
DEBUG_DIR = "debug"  # screenshots/html dumped here if price extraction fails


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def load_state(path):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(path, state):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def parse_price_from_text(text):
    """Extract the first plausible rupee amount from a blob of text."""
    matches = re.findall(r"₹\s?([\d,]+)", text)
    if not matches:
        return None
    # First match on a Myntra PDP is almost always the selling price;
    # subsequent ones are usually the struck-through MRP.
    cleaned = matches[0].replace(",", "")
    try:
        return int(cleaned)
    except ValueError:
        return None


def extract_price(page):
    """Try several selector strategies, falling back to a full-text regex scan."""
    selectors = [
        "span.pdp-price strong",
        ".pdp-price strong",
        "span.pdp-price",
        "[class*='pdp-price'] strong",
        "[class*='pdp-price']",
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0:
                txt = el.inner_text(timeout=3000)
                price = parse_price_from_text(txt)
                if price:
                    return price
        except PWTimeoutError:
            continue
        except Exception:
            continue

    # Fallback: scan the whole visible page text
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
        price = parse_price_from_text(body_text)
        if price:
            return price
    except Exception:
        pass

    return None


def check_pincode_delivery(page, pincode):
    """
    Best-effort delivery estimate lookup for the given pincode.
    Returns a short string describing the result, or None if the widget
    couldn't be found/used (page layout changes often on Myntra).
    """
    try:
        pin_input = page.locator(
            "input[placeholder*='pincode' i], input[id*='pincode' i]"
        ).first
        if pin_input.count() == 0:
            return None
        pin_input.click(timeout=3000)
        pin_input.fill(str(pincode))

        check_btn = page.locator(
            "button:has-text('Check'), div[class*='pincode'] button"
        ).first
        if check_btn.count() > 0:
            check_btn.click(timeout=3000)

        page.wait_for_timeout(2000)  # let the delivery estimate render

        delivery_text = page.locator(
            "[class*='delivery'], [class*='pincode']"
        ).first.inner_text(timeout=3000)
        return delivery_text.strip().replace("\n", " ")[:150]
    except Exception as e:
        log.debug(f"Pincode check failed (non-fatal): {e}")
        return None


def dump_debug(page, product_name):
    os.makedirs(DEBUG_DIR, exist_ok=True)
    safe_name = re.sub(r"[^a-zA-Z0-9]+", "_", product_name)[:50]
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    try:
        page.screenshot(path=f"{DEBUG_DIR}/{safe_name}_{ts}.png", full_page=True)
        with open(f"{DEBUG_DIR}/{safe_name}_{ts}.html", "w", encoding="utf-8") as f:
            f.write(page.content())
        log.warning(f"Saved debug screenshot/html for '{product_name}' to {DEBUG_DIR}/")
    except Exception as e:
        log.warning(f"Could not save debug artifacts: {e}")


def send_telegram_message(bot_token, chat_id, message):
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    resp = requests.post(
        url,
        data={"chat_id": chat_id, "text": message, "disable_web_page_preview": True},
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"Telegram request failed: {resp.status_code} {resp.text}")
    log.info("Telegram alert sent.")


def check_product(page, product, pincode):
    log.info(f"Checking: {product['name']}")
    page.goto(product["url"], wait_until="commit", timeout=45000)

    # Dismiss common popups (login modal / app-install nudge) if present
    for close_sel in ["button[class*='close']", "span[class*='close']"]:
        try:
            btn = page.locator(close_sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click(timeout=2000)
        except Exception:
            pass

    # Wait for the actual price element to show up rather than a fixed sleep
    try:
        page.wait_for_selector("[class*='pdp-price']", timeout=20000)
    except Exception:
        pass
    page.wait_for_timeout(1500)  # brief settle time for any late re-render

    price = extract_price(page)
    if price is None:
        log.error(f"Could not extract price for '{product['name']}'.")
        dump_debug(page, product["name"])
        return None, None

    delivery_info = check_pincode_delivery(page, pincode)
    return price, delivery_info


def main():
    config = load_config(CONFIG_PATH)
    state = load_state(config.get("state_file", "state.json"))
    pincode = config.get("pincode")
    notify_cfg = config["notify"]
    # Allow env vars to override config.json so real credentials never need
    # to be committed to the repo -- set these as GitHub Actions secrets.
    notify_cfg["bot_token"] = os.environ.get("TELEGRAM_BOT_TOKEN", notify_cfg.get("bot_token"))
    notify_cfg["chat_id"] = os.environ.get("TELEGRAM_CHAT_ID", notify_cfg.get("chat_id"))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            channel="chrome",
            args=["--disable-http2"],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 900},
        )
        page = context.new_page()

        for product in config["products"]:
            url = product["url"]
            target = product["target_price"]
            try:
                price, delivery_info = check_product(page, product, pincode)
            except Exception as e:
                log.error(f"Error checking '{product['name']}': {e}")
                continue

            if price is None:
                continue  # already logged + debug-dumped above

            log.info(f"  Current price: ₹{price} | Target: ₹{target}")

            prev = state.get(url, {})
            last_alerted_price = prev.get("last_alerted_price")

            should_alert = price <= target and price != last_alerted_price

            if should_alert:
                msg = (
                    f"🛍️ Price Alert: {product['name']}\n"
                    f"Now: ₹{price} (target ₹{target})\n"
                    f"{url}"
                )
                if delivery_info:
                    msg += f"\nDelivery to {pincode}: {delivery_info}"

                try:
                    send_telegram_message(
                        notify_cfg["bot_token"], notify_cfg["chat_id"], msg
                    )
                except Exception as e:
                    log.error(f"Failed to send Telegram alert: {e}")
                else:
                    prev["last_alerted_price"] = price
                    prev["last_alerted_at"] = datetime.now(timezone.utc).isoformat()
            else:
                log.info("  No alert needed (above target or already alerted at this price).")

            prev["last_seen_price"] = price
            prev["last_checked_at"] = datetime.now(timezone.utc).isoformat()
            state[url] = prev

            time.sleep(2)  # small pause between products, be polite to the site

        browser.close()

    save_state(config.get("state_file", "state.json"), state)
    log.info("Done.")


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        log.exception(f"Fatal error: {exc}")
        sys.exit(1)
