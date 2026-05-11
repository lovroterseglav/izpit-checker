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
    filters: list[str] = []
    if user.date_from:
        filters.append(f"from {user.date_from.isoformat()}")
    filters.append(f"to {user.date_to.isoformat()}")
    if user.cities:
        filters.append("cities: " + ", ".join(user.cities))
    if user.categories:
        filters.append("categories: " + ", ".join(user.categories))
    if user.appointment_type:
        filters.append(f"type: {user.appointment_type}")
    return filters


def format_pretty_summary(payload: dict[str, Any], user: UserConfig) -> str:
    result = payload.get("result", {})
    lines = [f"Appointment alert for {user.name}", ""]
    filters = format_filters(user)
    if filters:
        lines.append("Filters: " + " | ".join(filters))
        lines.append("")
    if not result:
        lines.append("No matching appointments found.")
        return "\n".join(lines)

    lines.append(f"Found matching appointments on {len(result)} date(s):")
    for date, cities in result.items():
        lines.append(f"- {date}: {', '.join(cities)}")
    return "\n".join(lines)


def require_env_var(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise ConfigError(f"environment variable {name} is required")
    return value


def send_email(text: str, user: UserConfig, email: EmailConfig, api_key: str) -> None:
    resend.api_key = api_key
    try:
        result = resend.Emails.send(
            {
                "from": email.sender,
                "to": user.email_to,
                "subject": email.subject,
                "text": text,
            }
        )
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
    subject = email.get("subject", "Updates for driving dates")
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
        logger.info("%s\n", text)
        send_email(text, user, config.email, api_key)
        user_hashes[user.name] = new_hash
        changed = True

    if changed:
        write_hashes(config.hash_file, hashes)

    if config.scrape.verbose:
        elapsed = time.monotonic() - started
        logger.info("Fetched %d appointments in %.2fs.", len(appointments), elapsed)


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
