# CarePulse — New Reality Christian Center (Ado-Ekiti)

Church Evangelism & Outreach WhatsApp Drip Engine built for **New Reality Christian Center** in **Ado-Ekiti, Ekiti State, Nigeria** (scaling to 5,000+ prospects) with a **Hybrid Dual-Mode Architecture** that keeps Meta WhatsApp API costs near **$0.00**.

## Architecture Highlights

1. **Mode A — Automated Meta Cloud API (`WA_CLOUD_API_ENABLED=True`)**
   - Direct Meta Graph API (`v22.0+`) client using persistent `requests.Session` connection pooling (`pool_connections=20`, `pool_maxsize=50`).
   - **Zero-Marketing-Template Cost Guard:**
     - Inside 24h Customer Service Window (`last_inbound_at < 24h`) -> **$0.00** Free-Form Interactive Buttons.
     - Outside 24h window on Step 1 -> **$0.0067** Utility Confirmation Template (`outreach_request_confirmation_v1`).
     - **22-Hour Window Chaining:** Upon Quick Reply / inbound reply, sends Step 1 pastoral welcome immediately (`$0.00`) and schedules Step 2 for **+22 hours** inside the same free window.
     - **Silent Contact Cutoff:** Outside 24h window on Step 2 or Step 3 -> transitions to `MANUAL_FALLBACK` (`$0.00`) instead of spending on cold templates.
2. **Mode B — Day-1 `wa.me` Phone Fallback & Field Handshake (`WA_CLOUD_API_ENABLED=False`)**
   - **15-Second Field Capture (`/`)** + **Instant Field QR Handshake (`/field/handshake/<id>/`)** so prospects can scan and open a `$0.00` inbound conversation on the spot.
   - **Coordinator Dashboard (`/dashboard/`)** with HTMX zero-reload **1-Tap `wa.me` Queue**, **Replied Inbox & HTML5 Voice Note Audio Player**, and **Sunday RSVP Bus Pickup Export (`/dashboard/export-rsvps/`)**.
   - **Public 1-Tap RSVP & Prayer Card (`/r/<rsvp_token>/`)** syncing attendance and prayer updates back to Django.
   - **Bulk CSV Importer (`/import/`)** normalizing Nigerian numbers (`0803...`, `803...`, `234...`, `+234...`) to E.164 and staggering 5,000+ sends across hourly buckets with randomized jitter.

---

## Free Public Hosting Options (For Church Member Testing)

### Option 1: Render (1-Click Free Blueprint — Recommended for Quick Public URL)
1. Push the `carepulse/` folder to a GitHub repository.
2. Go to [render.com](https://render.com) -> **New +** -> **Blueprint** -> connect your GitHub repo.
3. Render automatically reads `render.yaml`, provisions a free PostgreSQL database and web service, runs `./build.sh`, and gives you a public HTTPS URL like `https://carepulse-new-reality-christian.onrender.com`.
4. *(Tip)* Update `CHURCH_WHATSAPP_NUMBER` in Render's Environment tab to New Reality Christian's actual outreach WhatsApp number.

### Option 2: PythonAnywhere (Always-Free Beginner Tier, No Credit Card, No Sleep)
1. Create a free account at [pythonanywhere.com](https://www.pythonanywhere.com).
2. Upload `carepulse/` (or clone from GitHub) into `/home/<username>/carepulse`.
3. In a Bash console:
   ```bash
   cd ~/carepulse
   python3.12 -m venv .venv
   source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env
   python manage.py migrate
   python manage.py collectstatic --noinput
   ```
4. Under the **Web** tab -> **Add a new web app** -> **Manual configuration (Python 3.12)** -> point WSGI file to `config.wsgi.application` and set Virtualenv to `/home/<username>/carepulse/.venv`.
5. Your church members can test publicly at `https://<username>.pythonanywhere.com`.

### Option 3: Docker Compose (Full Stack with Celery + Postgres + Redis)
```bash
cd carepulse
cp .env.example .env
docker compose up --build
```
