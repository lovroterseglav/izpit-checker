#!/usr/bin/env python3
"""
Scrape e-uprava free driving-test appointment slots for multiple users.

Example:
  RS_API_KEY=... python main.py --config config.json
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import logging
import os
import re
import resend
import requests
import time
from bs4 import BeautifulSoup
from collections import defaultdict
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode, urlparse

BASE_URL = "https://e-uprava.gov.si/si/javne-evidence/prosti-termini-zemljevid/content/singleton.html"
DATE_FORMAT = "%d. %m. %Y"
DRIVING_TEST_TYPE = "Preverjanje znanja vožnje"
THEORY_TEST_TYPE = "Preverjanje znanja teorije"
CONFIG_TYPES = {
    "CPP": THEORY_TEST_TYPE,
    "DRIVING_TEST": DRIVING_TEST_TYPE,
}
RESEND_API_KEY_ENV = "RS_API_KEY"
DEFAULT_EMAIL_SUBJECT = "Prosti termini za vozniški izpit"
LEGACY_EMAIL_SUBJECT = "Updates for driving dates"
logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Appointment:
    date: str
    time: str
    city: str
    zone: str
    address: str
    categories: list[str]
    free_spots: int | None
    type: str

    @property
    def parsed_date(self) -> dt.date:
        return parse_slovenian_date(self.date)


@dataclass(frozen=True)
class ScrapeConfig:
    base_url: str
    delay: float
    timeout: float
    verbose: bool


@dataclass(frozen=True)
class EmailConfig:
    sender: str
    subject: str


@dataclass(frozen=True)
class UserConfig:
    name: str
    email_to: list[str]
    date_from: dt.date | None
    date_to: dt.date
    cities: list[str]
    categories: list[str]
    appointment_type: str | None


@dataclass(frozen=True)
class AppConfig:
    hash_file: Path
    scrape: ScrapeConfig
    email: EmailConfig
    users: list[UserConfig]


def normalize_ws(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip()


def parse_int(value: str) -> int | None:
    match = re.search(r"-?\d+", normalize_ws(value))
    return int(match.group(0)) if match else None


def parse_slovenian_date(value: str) -> dt.date:
    return dt.datetime.strptime(normalize_ws(value), DATE_FORMAT).date()


def parse_iso_date(value: Any, field_name: str) -> dt.date | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise ConfigError(f"{field_name} must be an ISO date string")
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise ConfigError(f"{field_name} must be an ISO date string, got {value!r}") from exc


def split_csv(values: Iterable[str]) -> list[str]:
    items: list[str] = []
    for value in values:
        items.extend(part.strip() for part in value.split(","))
    return [item for item in items if item]


def as_string_list(value: Any, field_name: str, *, default: list[str] | None = None) -> list[str]:
    if value is None:
        return list(default or [])
    if isinstance(value, str):
        return split_csv([value])
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return split_csv(value)
    raise ConfigError(f"{field_name} must be a string or a list of strings")


def split_categories(cell_text: str) -> list[str]:
    return split_csv([normalize_ws(cell_text)])


def fetch_html(session: Any, url: str, timeout: float) -> str:
    parsed = urlparse(url)
    headers = {
        "User-Agent": "Mozilla/5.0 (compatible; izpit-alert/2.0)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "sl,en;q=0.8",
        "Referer": f"{parsed.scheme}://{parsed.netloc}/",
    }
    try:
        response = session.get(url, headers=headers, timeout=timeout)
        response.raise_for_status()
    except requests.RequestException:
        logger.exception("Failed to fetch %s", url)
        raise
    return response.text


def find_td_by_datath(tr: Any, datath: str) -> Any | None:
    return tr.find("td", attrs={"data-th": datath})


def parse_page(html: str) -> list[Appointment]:
    soup = BeautifulSoup(html, "html.parser")
    results_div = soup.find(id="results")
    if not results_div:
        return []

    table = results_div.find("table")
    if not table:
        return []

    appointments: list[Appointment] = []
    current_date: str | None = None
    rows = table.find_all("tr", recursive=True)

    i = 0
    while i < len(rows):
        tr = rows[i]
        classes = tr.get("class") or []
        if "js_dicDetailsBtnRow" not in classes:
            i += 1
            continue

        date_td = tr.find("td", attrs={"data-th": "Datum"})
        if date_td:
            calendar = date_td.find("div", class_="calendarBox")
            if calendar and calendar.has_attr("aria-label"):
                current_date = normalize_ws(calendar["aria-label"])

        td_time = find_td_by_datath(tr, "Ura")
        time_str = normalize_ws(td_time.get_text(" ", strip=True)) if td_time else ""

        city = ""
        zone = ""
        td_location = find_td_by_datath(tr, "Tip / Lokacija")
        if td_location:
            city_div = td_location.find(class_="dicTitle1")
            zone_div = td_location.find(class_="dicDisclaimer")
            city = normalize_ws(city_div.get_text(" ", strip=True)) if city_div else ""
            zone = normalize_ws(zone_div.get_text(" ", strip=True)) if zone_div else ""

        td_cat = find_td_by_datath(tr, "Kategorije")
        categories = split_categories(td_cat.get_text(" ", strip=True)) if td_cat else []

        td_free = find_td_by_datath(tr, "Prosta mesta")
        free_spots = parse_int(td_free.get_text(" ", strip=True)) if td_free else None

        address = ""
        event_type = ""
        has_details = i + 1 < len(rows) and "js_dicDetails" in (rows[i + 1].get("class") or [])
        if has_details:
            details_tr = rows[i + 1]
            details_td = details_tr.find("td")
            address_div = details_tr.find(class_="dicTitle2")
            address = normalize_ws(address_div.get_text(" ", strip=True)) if address_div else ""

            if details_td:
                for selector in (".dicTitle2", ".dicDisclaimer", ".e2s_hrDic"):
                    for element in details_td.select(selector):
                        element.decompose()
                event_type = normalize_ws(details_td.get_text(" ", strip=True))

        if current_date and time_str and city:
            appointments.append(
                Appointment(
                    date=current_date,
                    time=time_str,
                    city=city,
                    zone=zone,
                    address=address,
                    categories=categories,
                    free_spots=free_spots,
                    type=event_type,
                )
            )

        i += 2 if has_details else 1

    return appointments


def build_url(page: int, *, base_url: str) -> str:
    query = {
        "lang": "si",
        "type": "-",
        "cat": "-",
        "izpitniCenter": "-1",
        "lokacija": "-1",
        "offset": "0",
        "sentinel_type": "ok",
        "sentinel_status": "ok",
        "is_ajax": "1",
        "complete": "false",
        "page": str(page),
    }
    return f"{base_url}?{urlencode(query)}"


def current_week_start(today: dt.date | None = None) -> dt.date:
    today = today or dt.date.today()
    return today - dt.timedelta(days=today.weekday())


def page_for_date(value: dt.date, *, today: dt.date | None = None) -> int:
    week_start = current_week_start(today)
    if value <= week_start:
        return 0
    delta_days = (value - week_start).days
    return delta_days // 7


def requested_pages(users: Iterable[UserConfig], *, today: dt.date | None = None) -> set[int]:
    week_start = current_week_start(today)
    user_list = list(users)
    if not user_list:
        raise ConfigError("users must be a non-empty list")

    pages: set[int] = set()
    for user in user_list:
        start_date = user.date_from if user.date_from and user.date_from > week_start else week_start
        start_page = page_for_date(start_date, today=today)
        end_page = page_for_date(user.date_to, today=today)
        pages.update(range(start_page, end_page + 1))

    return pages


def format_pages(pages: Iterable[int]) -> str:
    page_set = set(pages)
    if not page_set:
        return "none"
    return str(page_set)


def scrape(config: ScrapeConfig, pages: Iterable[int]) -> list[Appointment]:
    appointments: list[Appointment] = []

    with requests.Session() as session:
        for page in pages:
            url = build_url(page, base_url=config.base_url)
            if config.verbose:
                logger.info("GET %s", url)

            items = parse_page(fetch_html(session, url, config.timeout))
            appointments.extend(items)

            if config.delay > 0:
                time.sleep(config.delay)

    return appointments


def filter_appointments(items: Iterable[Appointment], user: UserConfig) -> list[Appointment]:
    cities = {city.upper() for city in user.cities}
    required_categories = set(user.categories)
    type_label = CONFIG_TYPES[user.appointment_type] if user.appointment_type else None

    filtered: list[Appointment] = []
    for item in items:
        item_categories = set(item.categories)
        if required_categories and not required_categories.intersection(item_categories):
            continue
        if cities and item.city.upper() not in cities:
            continue
        if type_label and type_label.casefold() not in item.type.casefold():
            continue
        if user.date_from and item.parsed_date < user.date_from:
            continue
        if item.parsed_date > user.date_to:
            continue
        filtered.append(item)

    return sorted(filtered, key=lambda item: (item.parsed_date, item.time, item.city, item.address))


def summarize_by_date(items: Iterable[Appointment], user: UserConfig) -> dict[str, Any]:
    cities_by_date: defaultdict[str, set[str]] = defaultdict(set)
    for item in items:
        cities_by_date[item.date].add(item.city)

    return {
        "date_from": user.date_from.isoformat() if user.date_from else None,
        "date_to": user.date_to.isoformat(),
        "result": {
            date: sorted(cities)
            for date, cities in sorted(
                cities_by_date.items(),
                key=lambda pair: parse_slovenian_date(pair[0]),
            )
        },
    }


def stable_hash(payload: Any) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha1(encoded).hexdigest()


def format_filters(user: UserConfig) -> list[str]:
    appointment_types = {
        "CPP": "teoretični izpit (CPP)",
        "DRIVING_TEST": "praktični izpit",
    }
    date_from = user.date_from.strftime(DATE_FORMAT) if user.date_from else "danes"
    return [
        f"od: {date_from}",
        f"do: {user.date_to.strftime(DATE_FORMAT)}",
        "kraji: " + (", ".join(user.cities) if user.cities else "vsi kraji"),
        "kategorije: " + (", ".join(user.categories) if user.categories else "vse kategorije"),
        "vrsta: " + appointment_types.get(user.appointment_type, "vse vrste"),
    ]


def format_pretty_summary(payload: dict[str, Any], user: UserConfig) -> str:
    result = payload.get("result", {})
    lines = [f"Prosti termini za vozniški izpit – {user.name}", ""]
    filters = format_filters(user)
    if filters:
        lines.append("Izbrani filtri: " + " | ".join(filters))
        lines.append("")
    if not result:
        lines.append("Trenutno ni najdenih prostih terminov, ki ustrezajo izbranim filtrom.")
        return "\n".join(lines)

    lines.append(f"Število datumov s prostimi termini: {len(result)}")
    for date, cities in result.items():
        lines.append(f"- {date}: {', '.join(cities)}")
    return "\n".join(lines)


def format_html_summary(payload: dict[str, Any], user: UserConfig) -> str:
    result = payload.get("result", {})
    filters = format_filters(user)
    filter_rows = "".join(
        f'<span style="display:inline-block;margin:0 6px 8px 0;padding:6px 10px;'
        f'border-radius:999px;background:#eef2ff;color:#344054;font-size:13px;">{escape(value)}</span>'
        for value in filters
    )

    if result:
        appointment_rows = "".join(
            '<tr>'
            f'<td style="padding:14px 16px;border-bottom:1px solid #e4e7ec;font-weight:700;'
            f'white-space:nowrap;color:#14213d;">{escape(date)}</td>'
            f'<td style="padding:14px 16px;border-bottom:1px solid #e4e7ec;color:#344054;">'
            f'{escape(", ".join(cities))}</td>'
            '</tr>'
            for date, cities in result.items()
        )
        results_html = (
            f'<p style="margin:0 0 12px;color:#344054;">Število datumov s prostimi termini: '
            f'<strong>{len(result)}</strong></p>'
            '<table role="presentation" width="100%" cellspacing="0" cellpadding="0" '
            'style="border:1px solid #e4e7ec;border-radius:12px;border-collapse:separate;'
            f'border-spacing:0;overflow:hidden;">{appointment_rows}</table>'
        )
    else:
        results_html = (
            '<div style="padding:18px;border-radius:12px;background:#f8fafc;border:1px solid #e4e7ec;">'
            '<p style="margin:0;color:#475467;">Trenutno ni najdenih prostih terminov, ki ustrezajo '
            'izbranim filtrom.</p></div>'
        )

    return f"""<!doctype html>
<html lang="sl">
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f5f7fb;font-family:Arial,sans-serif;color:#172033;">
  <div style="display:none;max-height:0;overflow:hidden;">Pregled prostih terminov za vozniški izpit.</div>
  <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="background:#f5f7fb;">
    <tr><td align="center" style="padding:32px 16px;">
      <table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:640px;">
        <tr><td style="padding:24px 28px;background:#14213d;border-radius:16px 16px 0 0;color:#fff;">
          <div style="font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#cbd5e1;">Izpit Alert</div>
          <h1 style="margin:8px 0 0;font-size:25px;line-height:1.25;">Prosti termini za vozniški izpit</h1>
        </td></tr>
        <tr><td style="padding:28px;background:#fff;border:1px solid #e4e7ec;border-top:0;border-radius:0 0 16px 16px;">
          <p style="margin:0 0 20px;font-size:16px;">Pozdravljeni, <strong>{escape(user.name)}</strong>!</p>
          <p style="margin:0 0 10px;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.05em;color:#667085;">Izbrani filtri</p>
          <div style="margin-bottom:20px;">{filter_rows}</div>
          {results_html}
          <p style="margin:24px 0 0;padding-top:18px;border-top:1px solid #e4e7ec;font-size:12px;line-height:1.5;color:#667085;">
            To je samodejno obvestilo storitve Izpit Alert.
          </p>
        </td></tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""


def require_env_var(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"environment variable {name} is required")
    return value


def send_email(
    text: str,
    user: UserConfig,
    email: EmailConfig,
    api_key: str,
    *,
    html: str | None = None,
) -> None:
    resend.api_key = api_key
    message: dict[str, Any] = {
        "from": email.sender,
        "to": user.email_to,
        "subject": email.subject,
        "text": text,
    }
    if html is not None:
        message["html"] = html
    try:
        result = resend.Emails.send(message)
    except Exception:
        logger.exception("Failed to send email for %s to %s", user.name, ", ".join(user.email_to))
        raise
    logger.info("Mail sent for %s: %s", user.name, result)


def read_hashes(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"users": {}}
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"hash file {path} is not valid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("users"), dict):
        raise ConfigError(f"hash file {path} must have shape {{\"users\": {{...}}}}")
    return data


def write_hashes(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")


class ConfigError(ValueError):
    pass


def load_json(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise ConfigError(f"config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config file {path} is not valid JSON") from exc
    if not isinstance(data, dict):
        raise ConfigError("config root must be a JSON object")
    return data


def require_object(value: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{field_name} must be an object")
    return value


def parse_non_negative_float(value: Any, field_name: str, default: float) -> float:
    if value is None:
        return default
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value < 0:
        raise ConfigError(f"{field_name} must be a non-negative number")
    return float(value)


def parse_bool(value: Any, field_name: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{field_name} must be true or false")
    return value


def parse_scrape_config(data: dict[str, Any]) -> ScrapeConfig:
    scrape = require_object(data.get("scrape", {}), "scrape")
    base_url = scrape.get("base_url", BASE_URL)
    if not isinstance(base_url, str) or not base_url:
        raise ConfigError("scrape.base_url must be a non-empty string")
    return ScrapeConfig(
        base_url=base_url,
        delay=parse_non_negative_float(scrape.get("delay"), "scrape.delay", 0.25),
        timeout=parse_non_negative_float(scrape.get("timeout"), "scrape.timeout", 30.0),
        verbose=parse_bool(scrape.get("verbose"), "scrape.verbose", False),
    )


def parse_email_config(data: dict[str, Any]) -> EmailConfig:
    email = require_object(data.get("email", {}), "email")
    sender = email.get("from", "Zamudko alerts <alert@zamudko.si>")
    subject = email.get("subject", DEFAULT_EMAIL_SUBJECT)
    if subject == LEGACY_EMAIL_SUBJECT:
        subject = DEFAULT_EMAIL_SUBJECT
    if not isinstance(sender, str) or not sender:
        raise ConfigError("email.from must be a non-empty string")
    if not isinstance(subject, str) or not subject:
        raise ConfigError("email.subject must be a non-empty string")
    return EmailConfig(sender=sender, subject=subject)


def parse_user(raw_user: Any, index: int) -> UserConfig:
    user = require_object(raw_user, f"users[{index}]")
    name = user.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError(f"users[{index}].name must be a non-empty string")

    appointment_type = user.get("type")
    if appointment_type is not None:
        if not isinstance(appointment_type, str):
            raise ConfigError(f"users[{index}].type must be CPP or DRIVING_TEST")
        appointment_type = appointment_type.upper()
        if appointment_type not in CONFIG_TYPES:
            raise ConfigError(f"users[{index}].type must be CPP or DRIVING_TEST")

    email_to = as_string_list(user.get("email_to"), f"users[{index}].email_to")
    if not email_to:
        raise ConfigError(f"users[{index}].email_to must contain at least one email address")
    date_from = parse_iso_date(user.get("date_from"), f"users[{index}].date_from")
    date_to = parse_iso_date(user.get("date_to"), f"users[{index}].date_to")
    if date_to is None:
        raise ConfigError(f"users[{index}].date_to is required")
    if date_from and date_from > date_to:
        raise ConfigError(f"users[{index}].date_from must be on or before date_to")

    return UserConfig(
        name=name.strip(),
        email_to=email_to,
        date_from=date_from,
        date_to=date_to,
        cities=[city.upper() for city in as_string_list(user.get("city"), f"users[{index}].city")],
        categories=as_string_list(user.get("category"), f"users[{index}].category", default=["B"]),
        appointment_type=appointment_type,
    )


def parse_config(path: Path) -> AppConfig:
    data = load_json(path)
    hash_file = data.get("hash_file", "hash.json")
    if not isinstance(hash_file, str) or not hash_file:
        raise ConfigError("hash_file must be a non-empty string")

    raw_users = data.get("users")
    if not isinstance(raw_users, list) or not raw_users:
        raise ConfigError("users must be a non-empty list")

    users = [parse_user(raw_user, index) for index, raw_user in enumerate(raw_users)]
    names = [user.name for user in users]
    if len(names) != len(set(names)):
        raise ConfigError("user names must be unique")

    return AppConfig(
        hash_file=Path(hash_file),
        scrape=parse_scrape_config(data),
        email=parse_email_config(data),
        users=users,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape e-uprava appointments and alert configured users.")
    parser.add_argument("--config", required=True, help="Path to the JSON config file.")
    return parser


def run(config: AppConfig) -> None:
    started = time.monotonic()
    api_key = require_env_var(RESEND_API_KEY_ENV)
    pages = requested_pages(config.users)
    if config.scrape.verbose:
        logger.info("Requesting pages %s.", format_pages(pages))
    appointments = scrape(config.scrape, pages)
    hashes = read_hashes(config.hash_file)
    user_hashes = hashes["users"]
    changed = False

    for user in config.users:
        items = filter_appointments(appointments, user)
        payload = summarize_by_date(items, user)
        new_hash = stable_hash(payload)
        if user_hashes.get(user.name) == new_hash:
            logger.info("No changes for %s.", user.name)
            continue

        text = format_pretty_summary(payload, user)
        html = format_html_summary(payload, user)
        logger.info("%s\n", text)
        send_email(text, user, config.email, api_key, html=html)
        user_hashes[user.name] = new_hash
        changed = True

    if changed:
        write_hashes(config.hash_file, hashes)

    if config.scrape.verbose:
        elapsed = time.monotonic() - started
        logger.info("Fetched %d appointments in %.2fs.", len(appointments), elapsed)


def run_user(config: AppConfig, user: UserConfig) -> None:
    """Send the current results for one user without reading or writing hashes."""
    started = time.monotonic()
    api_key = require_env_var(RESEND_API_KEY_ENV)
    pages = requested_pages([user])
    if config.scrape.verbose:
        logger.info("Requesting pages %s for %s.", format_pages(pages), user.name)

    appointments = scrape(config.scrape, pages)
    items = filter_appointments(appointments, user)
    payload = summarize_by_date(items, user)
    text = format_pretty_summary(payload, user)
    html = format_html_summary(payload, user)
    logger.info("%s\n", text)
    send_email(text, user, config.email, api_key, html=html)

    if config.scrape.verbose:
        elapsed = time.monotonic() - started
        logger.info("Fetched %d appointments for %s in %.2fs.", len(appointments), user.name, elapsed)


def run_user_config(config_path: Path, user_name: str) -> None:
    config = parse_config(config_path)
    user = next((candidate for candidate in config.users if candidate.name == user_name), None)
    if user is None:
        raise ConfigError(f"user not found: {user_name}")
    run_user(config, user)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    args = build_parser().parse_args()
    try:
        run(parse_config(Path(args.config)))
    except ConfigError as exc:
        logger.error("%s", exc)
        raise SystemExit(1) from exc
    except Exception:
        logger.exception("Unhandled error")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
