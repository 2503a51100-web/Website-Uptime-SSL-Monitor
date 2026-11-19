# Website Uptime and SSL Monitor

Product 2 of the startup concept. It checks whether a website is up, measures
its response time, reads its SSL certificate expiry, saves every check in
SQLite, and calculates uptime statistics from the saved records.

Independent of Product 1 (DNS Resolution Checker). The two can be merged later.

## Technologies

Flask, SQLite, Python Requests, Python `socket` and `ssl`, HTML/CSS/JavaScript, Chart.js (loaded from a CDN, so the graph needs internet).

## Project structure

```
app.py            Flask app and API routes
monitor.py        URL validation + availability check (no Flask/SQLite inside)
ssl_checker.py    SSL certificate check (standard library only)
database.py       All SQLite code
templates/index.html
static/css/style.css
static/js/script.js
tests/test_app.py Automated tests (no internet needed)
```

`monitor.check_website(url)` and `ssl_checker.check_ssl(host, port)` return plain
dicts and know nothing about Flask or SQLite, so they can become the core of an
AWS Lambda function later. Only `database.py` would need replacing (e.g. DynamoDB).

## Run on Windows (VS Code terminal)

Requires Python 3.10 or newer. Open the project folder in VS Code, open a terminal (Ctrl+`), then:

PowerShell:
```
python -m venv venv
venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

If PowerShell blocks the activate script, run this once and try again:
`Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`
(or use Command Prompt: `venv\Scripts\activate.bat`).

Open http://127.0.0.1:5000 in your browser. The database file `monitor.db` is created automatically on first start.

Run the automated tests:
```
python -m unittest discover -s tests -v
```

## How a check works

1. The URL is validated; `https://` is added if missing.
2. DNS lookup (socket) so unknown domains give a clear message.
3. HTTP GET with Requests (5 s connect timeout, 10 s read timeout, redirects followed). Response time is measured from sending the request until the response headers arrive (includes DNS, connect, TLS handshake, redirects).
4. **UP** = status code below 400. 4xx/5xx, timeouts, refused connections, DNS failures and SSL errors = **DOWN**.
5. For HTTPS sites the certificate is fetched and validated: Valid, Expiring Soon (30 days or less), Expired, Invalid (self-signed, wrong hostname, untrusted issuer), or Unavailable. HTTP-only sites show Not Applicable.

Uptime % = successful checks / total checks x 100, from the database.
Failed checks with no response (timeout, refused, DNS) are not plotted on the graph, since they have no response time.

## API

| Method | Route | Purpose |
|---|---|---|
| POST | `/api/check` | Body `{"url": "google.com"}`. 201 with result, 400 if URL invalid |
| GET | `/api/history` | Saved checks, newest first. Optional `?limit=` and `?url=` |
| GET | `/api/history/<id>` | One record, 404 if missing |
| DELETE | `/api/history/<id>` | Delete one record, 404 if missing |
| GET | `/api/stats` | Total, successful, failed, uptime %. Optional `?url=` |
| GET | `/api/sites` | Distinct checked URLs (history filter) |

## Deploying later

- Set `FLASK_DEBUG=0` and run behind a proper server (gunicorn, etc.).
- Set `BLOCK_PRIVATE_TARGETS=1` so the public service cannot be used to probe localhost or internal networks. (It checks the address you enter; it does not re-check redirect targets.)
- Optional timeouts: `MONITOR_CONNECT_TIMEOUT`, `MONITOR_READ_TIMEOUT`.
