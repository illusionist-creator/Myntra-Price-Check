# Myntra Price Tracker → Telegram Alerts

Checks the price of Myntra products you configure and pings you on Telegram
when a price hits your target. Runs every 3 hours via GitHub Actions.

## What's tracked right now (`config.json`)
1. Armaf Women Signature Night EDP 100ml — alert at ≤ ₹1000
2. Bone Anthony Men Old Money EDP 50ml — alert at ≤ ₹600

Pincode `380006` is used to also fetch a delivery estimate for the alert
message. **Note:** Myntra prices don't vary by pincode — only the estimated
delivery date/serviceability does. The price check itself is unaffected.

## 1. Set up Telegram alerts (free, official API, no phone-number bot to chase)
1. Open Telegram, search for **@BotFather**, start a chat, send `/newbot`.
2. Follow the prompts (pick a name and a username ending in `bot`).
3. BotFather replies with your **bot token** — looks like
   `123456789:AAExampleTokenTextHere`. Save it.
4. Search for your new bot by its username and send it any message (e.g. `hi`)
   — this lets it message you back.
5. Get your **chat_id**: open this URL in a browser (replace `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   Look for `"chat":{"id":123456789, ...}` in the JSON response — that
   number is your `chat_id`.

That's it — no rotating numbers, no waitlists, no rate-limit cooldowns.

## 2. Local test run
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
playwright install --with-deps chromium

# either edit config.json directly with your bot_token/chat_id, or:
export TELEGRAM_BOT_TOKEN="123456789:AAExampleTokenTextHere"
export TELEGRAM_CHAT_ID="123456789"

python myntra_price_tracker.py
```
Check the console log. If price extraction fails for a product, a screenshot
+ HTML dump is saved to `debug/` so you can inspect what Myntra's page
actually looked like and adjust the selectors in `extract_price()`.

## 3. Deploy on GitHub Actions (runs every 3 hours automatically)
1. Push this folder to a **private** GitHub repo (keep it private — it
   contains your tracked products, not sensitive data itself, but no reason
   to make it public).
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**
   - `TELEGRAM_BOT_TOKEN` = your bot token from BotFather
   - `TELEGRAM_CHAT_ID` = your chat id from step 5 above
3. That's it — `.github/workflows/myntra_price_check.yml` runs on a
   `0 */3 * * *` cron schedule (every 3 hours) and also supports manual
   triggering via the "Run workflow" button in the Actions tab.
4. The workflow commits `state.json` back to the repo after each run so it
   remembers what it last alerted you about (avoids repeat pings for the
   same price).

## 4. Adding/removing products
Edit `config.json` — add another object to the `"products"` array with
`name`, `url`, and `target_price`.

## Alerting behavior
- You get a Telegram message the first time a price drops to/below your
  target.
- You will **not** get repeat alerts every 3 hours for the same price.
- You'll get alerted again if: the price drops further while still under
  target, or it rises back above target and later drops below it again.

## Known limitation
Myntra is a JavaScript-heavy single-page app and occasionally changes its
HTML structure/class names. The script tries several selector strategies
and falls back to a full-page text scan for "₹" amounts, but if Myntra
ships a redesign, price extraction may start failing. Check `debug/` for
screenshots when that happens — that's your signal to update the selectors.
