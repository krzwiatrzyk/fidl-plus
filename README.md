# Fidl Plus

[![CI](https://github.com/KamKubicki/fidl-plus/actions/workflows/ci.yml/badge.svg)](https://github.com/KamKubicki/fidl-plus/actions/workflows/ci.yml)
[![Docker image](https://github.com/KamKubicki/fidl-plus/actions/workflows/docker.yml/badge.svg)](https://github.com/KamKubicki/fidl-plus/actions/workflows/docker.yml)

A self-hosted web app for browsing your Lidl Plus receipts. It pulls data
through the Lidl Plus mobile API and shows it in the browser.

The app's interface is in Polish; this README gives the original button and
page names in **bold** so you can find them.

## Features

- **Multiple accounts** – e.g. yours and your spouse's; view the data together or per account (filter in the navigation bar)
- **Login status** – whether each account's tokens work, when they were last refreshed and when it last synced
- **Dashboard** – spending stats, favourite store, recent purchases, spending per account
- **Receipts** – every purchase, filterable by store, date and account
- **Receipt details** – the full product list with prices and discounts
- **Products** – every product you have bought, with search, sorting and price ranges
- **Schedule** – daily receipt download and token refresh without clicking,
  with run history and a run-now button
- **Price history** – a chart of a product's price over time
- **Name normalisation** – the same product sold under different names is merged by barcode

## Screenshots

| Dashboard | Insights |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Insights](docs/screenshots/insights.png) |

| Top products | Price history |
|---|---|
| ![Top products](docs/screenshots/top_products_v2.png) | ![Chart](docs/screenshots/product_chart.png) |

| Receipts | Receipt details |
|---|---|
| ![Receipts](docs/screenshots/receipts.png) | ![Details](docs/screenshots/receipt_detail.png) |

## Running with Docker (recommended)

There is nothing to build and no Chrome to install. A ready-made image is
published to the GitHub Container Registry:

```
ghcr.io/kamkubicki/fidl-plus:latest
```

The image is **multi-arch**: the same tag contains `linux/amd64` (Synology,
regular PCs, servers) and `linux/arm64` (Apple Silicon, Raspberry Pi) builds,
and Docker picks the right one for you.

### Start in two commands

```bash
mkdir fidl-plus && cd fidl-plus
curl -O https://raw.githubusercontent.com/KamKubicki/fidl-plus/master/docker-compose.yml
docker compose up -d
```

The first pull takes a while: the image is about 2 GB because it ships
Chromium.

### Logging in

1. Open `http://localhost:8000/login`
2. Click **Otwórz przeglądarkę i zaloguj się** (open the browser and log in)
3. A second tab opens with a view of the browser running inside the
   container (noVNC, port 6080). Log in to Lidl Plus there as usual.
4. The token is captured automatically and stored in `./data/fidl.db`
5. Go back to `http://localhost:8000` and click **Odśwież dane** (refresh
   data). From then on the scheduler downloads new receipts on its own.

Add another account (e.g. your spouse's) via **Konta** (accounts) →
**Dodaj konto** (add account). Logging in to an account that is already on
the list updates it instead of creating a duplicate. Each account's state is
shown in the navigation bar (green / yellow / red dot) and on the **Konta**
page.

You log in by hand, in a visible window, because Lidl may ask for an SMS
code or a captcha, which a fully automated login would fail on.

If Lidl blocks logins from your server's IP address, the `/login` page has a
**Method 2**: run the login once on a computer with Chrome
(`python3 browser_login.py`) and upload the resulting `lidl_tokens.json`
through the form.

### Updating

```bash
docker compose pull && docker compose up -d
```

### Building from source

```bash
git clone https://github.com/KamKubicki/fidl-plus.git
cd fidl-plus
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

---

## Synology NAS step by step

### Option 1: Container Manager (DSM 7.2 or newer)

1. **File Station** → create a folder for the data, e.g. `docker/fidl-plus`
2. **Container Manager** → **Project** → **Create**
3. Project name: `fidl-plus`, path: the folder from step 1
4. Source: **Create docker-compose.yml** and paste:

```yaml
services:
  fidl-plus:
    image: ghcr.io/kamkubicki/fidl-plus:latest
    container_name: fidl-plus
    restart: unless-stopped
    ports:
      - "8000:8000"
      - "6080:6080"
    volumes:
      - /volume1/docker/fidl-plus/data:/data
    environment:
      TZ: Europe/Warsaw
      PUID: "1026"     # see below
      PGID: "100"
    shm_size: "1gb"
```

5. Click **Next** → **Done**. The first start takes a few minutes (a 2 GB
   download).
6. Open `http://NAS-IP:8000/login` and log in as described above

### Option 2: Portainer

**Stacks** → **Add stack** → **Web editor**, paste the same YAML as above and
click **Deploy the stack**.

To update: **Stacks** → `fidl-plus` → **Update** with **Re-pull image**
ticked.

### Option 3: SSH

```bash
mkdir -p /volume1/docker/fidl-plus && cd /volume1/docker/fidl-plus
curl -O https://raw.githubusercontent.com/KamKubicki/fidl-plus/master/docker-compose.yml
docker compose up -d
```

### Finding the right PUID and PGID

Synology gives users unusual ids (often `1026`, `1027`). The container uses
`1000:1000` by default. If your folder belongs to someone else, set the
right values; you can check them over SSH:

```bash
id your_user_name
# uid=1026(kamil) gid=100(users)
```

You do not need to fix permissions by hand: the container starts as root
only to set the owner of `/data`, then drops to `PUID:PGID`. The app and
Chromium never run as root.

### Troubleshooting

| Symptom | Cause |
|---|---|
| The container keeps restarting | `shm_size: "1gb"` is missing – Chromium will not start with the default 64 MB |
| `ERROR: /data is not writable` | Wrong `PUID`/`PGID` – check with `id` over SSH |
| The app works but noVNC does not | Port 6080 is not forwarded |
| The noVNC window is blank | Reload the tab; Chromium takes a few seconds to start |

Logs: **Container Manager** → `fidl-plus` → **Log**, or
`docker logs fidl-plus`.

---

## Configuration

| Variable | Default | Description |
|---|---|---|
| `DATA_DIR` | `/data` | directory for the database (accounts, tokens, receipts) |
| `PUID` / `PGID` | `1000` | owner of the files in `/data` |
| `NOVNC_PORT` | `6080` | port of the browser preview |
| `NOVNC_URL` | – | full noVNC address when running behind a reverse proxy |
| `LOGIN_TIMEOUT` | `180` | how many seconds to wait for the login |
| `TZ` | – | time zone, e.g. `Europe/Warsaw`; the scheduler runs in this zone |
| `SYNC_CRON` | `0 6 * * *` | when to download new receipts (cron syntax) |
| `TOKEN_REFRESH_CRON` | `0 */6 * * *` | when to refresh the tokens of all accounts (cron syntax) |
| `SCHEDULER_ENABLED` | `1` | `0` turns the scheduler off; jobs can then only be run by hand |
| `RADAR_ENABLED` | `0` | `1` turns on the [fastapi-radar](https://github.com/doganarif/fastapi-radar) dashboard at `/__radar` |

The scheduler runs inside the app process, so uvicorn must run with **a
single worker** (as the image does). Several workers would mean several
schedulers downloading the same receipts at once.

To change the app port, edit `docker-compose.yml`:

```yaml
ports:
  - "8080:8000"
```

### fastapi-radar

With `RADAR_ENABLED=1` a **Radar** link appears in the navigation bar: a list
of requests with timings, the SQL queries sent to the database and exceptions
with stack traces. Data is kept in `DATA_DIR/radar.duckdb` for 24 hours. It
is off by default because it stores request and response bodies, has no
login of its own and can replay a recorded request. `/login*` paths and SQL
query parameters are never recorded, so tokens do not end up in the
dashboard.

> **Security note.** The app has no authentication and noVNC runs without a
> password. Anyone who can reach port 8000 sees all your receipts; anyone who
> can reach port 6080 controls a browser inside your network. Keep both ports
> on your LAN and do not forward them on your router.

---

## Local install (without Docker)

### Requirements

- Python 3.11+
- Google Chrome (macOS) or Chromium (Linux) – for the login

### Install

```bash
git clone https://github.com/KamKubicki/fidl-plus.git
cd fidl-plus

python3 -m venv .venv
source .venv/bin/activate

pip install -r requirements.txt
```

On macOS the default Google Chrome path is used. On other systems, or if
Chrome lives somewhere unusual, point `CHROME_BINARY` at the binary:

```bash
export CHROME_BINARY=/usr/bin/chromium
```

## Usage

### 1. Log in to Lidl Plus

```bash
python3 browser_login.py
```

A Chrome window opens with the Lidl Plus login form. Log in by hand; the
token is captured automatically and saved to `data/lidl_tokens.json`.

> The access token is valid for about an hour. The refresh token renews it
> without logging in again; the scheduler does that every few hours, so an
> account should not quietly log itself out after 30 days.

Without arguments the script writes `lidl_tokens.json`, which you can upload
on the `/login` page (Method 2). With `--db` it writes the tokens straight
into the app's database (`--account-id N` for a specific account).

### 2. Start the app

```bash
python3 app.py
```

The app opens automatically at `http://localhost:8000`.

### 3. Download the receipts

Go to `/sync` or click **Odśwież dane** in the navigation bar. The first
download of all receipts can take a few minutes; after that the scheduler
fetches only the new ones every day.

---

## Project layout

```
├── app.py                  # FastAPI server – all endpoints
├── db.py                   # SQLite: accounts, receipts, job history
├── services.py             # Token refresh and per-account sync
├── scheduler.py            # APScheduler: recurring jobs
├── lidl_api.py             # Lidl Plus mobile API client (OAuth2 PKCE)
├── browser_login.py        # Login through a real Chrome/Chromium
├── receipt_parser.py       # HTML receipt parser + name normalisation
├── Dockerfile              # Chromium + Xvfb + noVNC + the app
├── docker-compose.yml      # ready-made image from GHCR
├── docker-compose.dev.yml  # overlay for building locally
├── docker-entrypoint.sh    # Starts Xvfb / x11vnc / noVNC / uvicorn
├── lidl-callback-handler   # Handler for the com.lidlplus.app:// scheme
├── templates/              # Jinja2 templates
│   ├── base.html
│   ├── index.html
│   ├── receipts.html
│   ├── receipt_detail.html
│   ├── receipt_print.html
│   ├── products.html
│   ├── product.html
│   ├── accounts.html
│   ├── jobs.html
│   ├── login.html
│   └── sync.html
├── requirements.txt
├── data/                   # fidl.db (accounts, tokens, receipts), radar.duckdb
│                           # (not in the repository)
└── docs/
    └── screenshots/
```

## How the login works

Lidl Plus uses **OAuth2 PKCE** with `client_id=LidlPlusNativeClient`. The
login form is protected by **reCAPTCHA Enterprise v3**, which blocks
automated HTTP requests.

The workaround: `browser_login.py` starts a real browser through Playwright,
which gets a good reCAPTCHA score. After the login Lidl redirects to the deep
link `com.lidlplus.app://callback?code=...`, which is captured three ways:

1. **CDP** (`Network.requestWillBeSent`) – also sees 302 redirects to the
   custom scheme, which Playwright does not always report,
2. Playwright events – `request`, `framenavigated`, `popup`,
3. an **`xdg-open` handler** – the system hands the URL to the
   `lidl-callback-handler` script, which writes it to a file (Linux/Docker).

The code is then exchanged for tokens.

## Prices and discounts

Receipts carry two prices:

- **base price** – `currentUnitPrice`, the regular shelf price,
- **promotional price** – the amount actually paid, after Lidl Plus coupons.

Stats, charts and rankings use the promotional prices.

> `totalAmount` returned by the API is already **net** of discounts;
> `totalDiscount` is only used to show how much you saved.

## Data

Accounts, tokens, receipts and job history live in the SQLite database
`data/fidl.db` (the directory is set with `DATA_DIR`). The directory is not
committed to the repository (`.gitignore`).

> **Upgrading from an older version:** on the first start, existing
> `lidl_tokens.json` and `receipts.json` files (including the old name
> `wszystkie_paragony_szczegoly.json`) are imported as "Konto 1" (account 1)
> and renamed with a `.migrated` suffix. Nothing is deleted.

Syncing only ever **adds** receipts: an API error during a download never
removes anything already in the database.

The API returns receipts in two formats:
- **JSON** (`itemsLine`) – newer receipts, with a full product structure
- **HTML** (`htmlPrintedReceipt`) – older receipts; `receipt_parser.py`
  parses them into the same format, including the discount lines

## Development

```bash
pip install -r requirements-dev.txt

pytest          # tests
ruff check .    # lint
ruff check --fix .
```

The tests only use synthetic receipts (`tests/conftest.py`); real data never
goes into the repository. They cover the logic that breaks most quietly:
parsing discounts from HTML, attaching a coupon to the right line, goods sold
by weight, and template rendering.

CI (GitHub Actions) runs lint and tests on Python 3.11 and 3.12.

### Releases

After every merge to `master`, Release Drafter updates a draft release. The
version (semver) comes from the PR titles, which follow Conventional Commits:
`feat:` bumps the minor version, `fix:` and everything else bump the patch
version, and `!` (e.g. `feat!:`) bumps the major version. The `major` /
`minor` labels on a PR force a bump, and `skip-changelog` leaves a PR out of
the notes.

Publishing the draft (**Releases** → the draft → **Publish release**) builds
the `linux/amd64` + `linux/arm64` image and pushes it to GHCR tagged `1.4.2`,
`1.4`, `1` and `latest` (releases marked as pre-release do not get
`latest`). Merging to `master` alone no longer publishes an image.

## Tech stack

- **FastAPI** + **Uvicorn** – backend
- **Jinja2** – HTML templates
- **HTMX** – dynamic UI without writing JS
- **Chart.js** – price charts
- **Playwright** – login through Chrome/Chromium
- **BeautifulSoup4** – parsing HTML receipts
- **Xvfb + noVNC** – browser window preview in Docker
