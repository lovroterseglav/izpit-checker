# izpit-alert

Scrapes available e-uprava driving-test appointment dates and emails each configured user when their matching results change.

The script fetches broad appointment data once, then filters it separately for every user in `config.json`.

## What It Does

- Requests e-uprava appointment pages with broad parameters:
  - `type=-`
  - `cat=-`
  - `izpitniCenter=-1`
  - `lokacija=-1`
  - `complete=false`
- Supports multiple users, each with their own filters.
- Filters by:
  - date range
  - city
  - category
  - appointment type: `CPP` or `DRIVING_TEST`
- Sends pretty summary emails with matching dates and cities.
- Stores per-user result hashes in one JSON file so unchanged users are not emailed again.

## Setup

Install dependencies:

```bash
python3 -m pip install -r requirements.txt
```

Set the Resend API key:

```bash
export RS_API_KEY="your-resend-api-key"
```

Run:

```bash
python3 main.py --config config.json
```

`--config` is the only CLI argument. Runtime behavior is configured through JSON.

## Config

Example:

```json
{
  "hash_file": "hash.json",
  "scrape": {
    "delay": 0.25,
    "timeout": 30,
    "verbose": false
  },
  "email": {
    "from": "Zamudko alerts <alert@example.com>",
    "subject": "Updates for driving dates"
  },
  "users": [
    {
      "name": "user-1",
      "email_to": ["user@example.com"],
      "date_from": "2026-05-18",
      "date_to": "2026-07-31",
      "city": ["JESENICE"],
      "category": ["B"],
      "type": "DRIVING_TEST"
    }
  ]
}
```

User fields:

- `name`: required stable user name, also used as the key in `hash.json`.
- `email_to`: required email address or list of addresses.
- `date_from`: optional inclusive start date in `YYYY-MM-DD`.
- `date_to`: required inclusive end date in `YYYY-MM-DD`.
- `city`: optional city or list of cities.
- `category`: optional category or list of categories, defaults to `["B"]`.
- `type`: optional appointment type, either `CPP` or `DRIVING_TEST`.

## Date Range And Page Requests

Each e-uprava page represents one week:

- `page=0` is the current partial week.
- `page=1` is the next week.
- later pages continue week by week.

You do not configure how many pages to request. The script calculates the required pages from each user's date window:

- If `date_from` is missing or already passed, it starts from `page=0`.
- It requests through that user's `date_to`.
- It fetches the sorted union of those per-user page ranges, skipping gaps between users' date windows.
- It filters the fetched appointments locally per user.

Date filtering is inclusive:

- appointments on `date_from` are included.
- appointments on `date_to` are included.

## Hashes

`hash_file` defaults to `hash.json`.

The file stores one hash per user:

```json
{
  "users": {
    "user-1": "sha1-hash"
  }
}
```

Each hash is calculated from that user's filtered summary payload. If one user's result changes, only that user is emailed and only that user's hash entry is updated.

## Scheduling

For cron, use an absolute path:

```cron
*/10 * * * * cd /path/to/izpit-alert && RS_API_KEY=your-key python3 main.py --config config.json
```

## Web interface for users

The private Slovenian web interface can add, edit, and delete entries in the
`users` section of `config.json`, and can manually run the normal appointment
check. A manual check sends notifications only when results changed and uses a
shared lock so it cannot overlap a cron-triggered check. It does not expose
scraper settings, email settings, hashes, or scheduling.

Install the dependencies and create a long random secret:

```bash
python3 -m pip install -r requirements.txt
export IZPIT_ALERT_WEB_SECRET="replace-with-a-long-random-value"
export RS_API_KEY="your-resend-api-key"
```

Run it directly with Python:

```bash
python3 web.py
```

The server listens on `127.0.0.1:8000`. To use another config file, set
`IZPIT_ALERT_CONFIG=/absolute/path/to/config.json`. To use another port, set
`PORT`.

For a production process manager, Gunicorn is also supported:

```bash
gunicorn --bind 127.0.0.1:8000 'web:create_app()'
```

Put nginx in front of the Python server and protect the location with Basic
Auth. A minimal location block is:

```nginx
location / {
    auth_basic "Izpit Alert";
    auth_basic_user_file /etc/nginx/.htpasswd;
    proxy_pass http://127.0.0.1:8000;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

Run the web tests with:

```bash
python3 -m unittest discover -s tests
```
