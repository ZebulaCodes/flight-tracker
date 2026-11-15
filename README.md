# Flight price tracker (SLC → LAS, June 23–26, 2027)

Checks Google Flights twice a day through SerpApi's free plan, keeps a price
history in `data/history.csv`, and pings your phone when the price drops.

## Cost: $0

- **SerpApi free plan:** 250 searches/month, no credit card. This tracker uses
  about 60/month (2 checks a day) plus 1 extra search when an alert fires.
  It also stops itself at 200/month (`monthly_request_cap`).
- **GitHub Actions:** free. Each run takes well under a minute.
- **ntfy.sh:** free phone notifications, no account.

## Setup (about 10 minutes)

1. **SerpApi key:** sign up at serpapi.com (free plan, no card). Copy your API
   key from the dashboard.
2. **Phone alerts:** install the **ntfy** app (iOS/Android). Tap **+** and
   subscribe to a topic name that's hard to guess, e.g.
   `vegas-flights-8k3q9z`. Anyone who knows the name can read the alerts, so
   don't use something obvious.
3. **GitHub repo:** create a new **private** repo and upload all these files,
   including the `.github/workflows/track.yml` folder. (Hidden folders can be
   skipped by drag-and-drop on some systems; if so, create the file through
   GitHub's **Add file → Create new file** and paste it in.)
4. **Secrets:** in the repo go to **Settings → Secrets and variables →
   Actions → New repository secret**, and add:
   - `SERPAPI_KEY` = your SerpApi key
   - `NTFY_TOPIC` = your topic name from step 2
5. **First run:** go to **Actions → Track flight price → Run workflow**. You
   should get a "Tracking started" notification with today's cheapest fare.
   After that it runs on its own twice a day.

## Settings (`config.json`)

| Setting | What it does |
|---|---|
| `adults` | Number of travelers. Prices are the total for everyone. Set to your real party size. |
| `stops` | `1` = nonstop only (default; SLC–LAS has plenty), `0` = any, `2` = up to 1 stop |
| `target_price` | e.g. `250` to get a high-priority alert at or below $250. `null` = off |
| `min_drop_to_alert` | Ignore new lows smaller than this many dollars (avoids $1 noise) |
| `monthly_request_cap` | Hard stop so you never exceed the free 250 |
| `fetch_return_details_on_alert` | Uses 1 extra search per alert to show the matching return flight |

## When you get alerted

- **Tracking started** – the first check, so you know it works.
- **New low** – cheapest price seen so far (by at least `min_drop_to_alert`).
- **At or below your target** – crossed your `target_price`.
- **Google now rates this price as LOW** – Google's own price insight flipped to "low".

Tapping the notification opens that exact search on Google Flights.

## Notes

- Southwest fares show on Google Flights, but booking happens on southwest.com.
  If an airline hasn't loaded its June 2027 schedule yet, its flights won't show
  until it does.
- To check a different trip later, just edit `config.json` and delete the files
  in `data/` so it starts a fresh history.
- Test notifications without using a search:
  `NTFY_TOPIC=your-topic python flight_tracker.py --test-alert`
