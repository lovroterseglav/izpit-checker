#!/usr/bin/env python3
"""Private web interface for editing the users section of config.json."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable

from flask import Flask, abort, flash, redirect, render_template, request, url_for
from flask_wtf import CSRFProtect, FlaskForm
from wtforms import DateField, HiddenField, SelectField, StringField, SubmitField
from wtforms.fields.choices import SelectChoice
from wtforms.validators import DataRequired, Optional

from main import ConfigError, load_json, parse_user, split_csv

EMAIL_PATTERN = re.compile(r"^[^@\s,]+@[^@\s,]+\.[^@\s,]+$")


class StaleConfigError(RuntimeError):
    """Raised when a form was opened against an older config version."""


class UserForm(FlaskForm):
    name = StringField("Ime", validators=[DataRequired(message="Ime je obvezno.")])
    email_to = StringField(
        "E-poštni naslovi",
        validators=[DataRequired(message="Vnesite vsaj en e-poštni naslov.")],
    )
    date_from = DateField("Datum od", validators=[Optional()])
    date_to = DateField("Datum do", validators=[DataRequired(message="Datum do je obvezen.")])
    cities = StringField("Kraji", validators=[Optional()])
    categories = StringField("Kategorije", validators=[DataRequired(message="Vnesite vsaj eno kategorijo.")])
    appointment_type = SelectField(
        "Vrsta izpita",
        choices=[
            SelectChoice("", "Vse vrste"),
            SelectChoice("DRIVING_TEST", "Praktični izpit"),
            SelectChoice("CPP", "Teoretični izpit (CPP)"),
        ],
        validators=[Optional()],
    )
    revision = HiddenField()
    submit = SubmitField("Shrani uporabnika")


class DeleteForm(FlaskForm):
    revision = HiddenField()
    submit = SubmitField("Izbriši uporabnika")


def users_revision(users: list[dict[str, Any]]) -> str:
    encoded = json.dumps(users, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def config_users(data: dict[str, Any]) -> list[dict[str, Any]]:
    users = data.get("users")
    if not isinstance(users, list) or not users:
        raise ConfigError("users must be a non-empty list")
    if not all(isinstance(user, dict) for user in users):
        raise ConfigError("each users entry must be an object")
    return users


def email_values(value: str) -> list[str]:
    return split_csv([value])


def validate_emails(values: list[str]) -> None:
    invalid = [value for value in values if not EMAIL_PATTERN.fullmatch(value)]
    if invalid:
        raise ConfigError("Neveljaven e-poštni naslov: " + ", ".join(invalid))


def form_user(form: UserForm, existing: dict[str, Any] | None, index: int) -> dict[str, Any]:
    emails = email_values(form.email_to.data or "")
    validate_emails(emails)

    raw = dict(existing or {})
    raw.update(
        {
            "name": (form.name.data or "").strip(),
            "email_to": emails,
            "date_to": form.date_to.data.isoformat() if form.date_to.data else None,
            "city": split_csv([form.cities.data or ""]),
            "category": split_csv([form.categories.data or ""]),
        }
    )
    if form.date_from.data:
        raw["date_from"] = form.date_from.data.isoformat()
    else:
        raw.pop("date_from", None)
    if form.appointment_type.data:
        raw["type"] = form.appointment_type.data
    else:
        raw.pop("type", None)

    parsed = parse_user(raw, index)
    raw.update(
        {
            "name": parsed.name,
            "email_to": parsed.email_to,
            "date_to": parsed.date_to.isoformat(),
            "city": parsed.cities,
            "category": parsed.categories,
        }
    )
    return raw


def populate_form(form: UserForm, raw: dict[str, Any], index: int) -> None:
    user = parse_user(raw, index)
    form.name.data = user.name
    form.email_to.data = ", ".join(user.email_to)
    form.date_from.data = user.date_from
    form.date_to.data = user.date_to
    form.cities.data = ", ".join(user.cities)
    form.categories.data = ", ".join(user.categories)
    form.appointment_type.data = user.appointment_type or ""


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        if path.exists():
            os.chmod(temporary_path, stat.S_IMODE(path.stat().st_mode))
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise


def update_config(
    path: Path,
    expected_revision: str,
    mutate: Callable[[list[dict[str, Any]]], None],
) -> None:
    lock_path = path.parent / f".{path.name}.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        data = load_json(path)
        users = config_users(data)
        if users_revision(users) != expected_revision:
            raise StaleConfigError
        mutate(users)
        if not users:
            raise ConfigError("Vsaj en uporabnik mora ostati v konfiguraciji.")
        names = [parse_user(user, index).name for index, user in enumerate(users)]
        if len(names) != len(set(names)):
            raise ConfigError("Imena uporabnikov morajo biti enolična.")
        data["users"] = users
        write_json_atomic(path, data)


def create_app(config_path: str | Path | None = None, test_config: dict[str, Any] | None = None) -> Flask:
    app = Flask(__name__)
    default_path = Path(__file__).with_name("config.json")
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("IZPIT_ALERT_WEB_SECRET"),
        CONFIG_PATH=Path(config_path or os.environ.get("IZPIT_ALERT_CONFIG", default_path)).resolve(),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
    )
    if test_config:
        app.config.update(test_config)
    if not app.config.get("SECRET_KEY"):
        raise RuntimeError("IZPIT_ALERT_WEB_SECRET is required")

    CSRFProtect(app)

    def read() -> tuple[dict[str, Any], list[dict[str, Any]]]:
        data = load_json(app.config["CONFIG_PATH"])
        return data, config_users(data)

    @app.errorhandler(ConfigError)
    def config_error(error: ConfigError):
        return render_template("error.html", message=str(error)), 400

    @app.errorhandler(StaleConfigError)
    def stale_error(_error: StaleConfigError):
        return render_template(
            "error.html",
            message="Konfiguracija se je med urejanjem spremenila. Osvežite stran in poskusite znova.",
        ), 409

    @app.get("/")
    def index():
        return redirect(url_for("user_list"))

    @app.get("/users")
    def user_list():
        _data, users = read()
        parsed_users = [parse_user(raw, index) for index, raw in enumerate(users)]
        return render_template("users.html", users=parsed_users)

    @app.route("/users/new", methods=["GET", "POST"])
    def user_new():
        _data, users = read()
        form = UserForm()
        if request.method == "GET":
            form.categories.data = "B"
            form.appointment_type.data = "DRIVING_TEST"
            form.revision.data = users_revision(users)
        if form.validate_on_submit():
            candidate = form_user(form, None, len(users))

            def add(current: list[dict[str, Any]]) -> None:
                current.append(candidate)

            update_config(app.config["CONFIG_PATH"], form.revision.data, add)
            flash("Uporabnik je bil dodan.", "success")
            return redirect(url_for("user_list"))
        return render_template("user_form.html", form=form, title="Dodaj uporabnika", is_new=True)

    @app.route("/users/<int:user_index>/edit", methods=["GET", "POST"])
    def user_edit(user_index: int):
        _data, users = read()
        if user_index < 0 or user_index >= len(users):
            abort(404)
        form = UserForm()
        if request.method == "GET":
            populate_form(form, users[user_index], user_index)
            form.revision.data = users_revision(users)
        if form.validate_on_submit():
            candidate = form_user(form, users[user_index], user_index)

            def edit(current: list[dict[str, Any]]) -> None:
                if user_index >= len(current):
                    raise StaleConfigError
                current[user_index] = candidate

            update_config(app.config["CONFIG_PATH"], form.revision.data, edit)
            flash("Spremembe so bile shranjene.", "success")
            return redirect(url_for("user_list"))
        return render_template("user_form.html", form=form, title="Uredi uporabnika", is_new=False)

    @app.route("/users/<int:user_index>/delete", methods=["GET", "POST"])
    def user_delete(user_index: int):
        _data, users = read()
        if user_index < 0 or user_index >= len(users):
            abort(404)
        form = DeleteForm()
        if request.method == "GET":
            form.revision.data = users_revision(users)
        if form.validate_on_submit():

            def delete(current: list[dict[str, Any]]) -> None:
                if len(current) == 1:
                    raise ConfigError("Zadnjega uporabnika ni mogoče izbrisati.")
                if user_index >= len(current):
                    raise StaleConfigError
                current.pop(user_index)

            update_config(app.config["CONFIG_PATH"], form.revision.data, delete)
            flash("Uporabnik je bil izbrisan.", "success")
            return redirect(url_for("user_list"))
        user = parse_user(users[user_index], user_index)
        return render_template("user_delete.html", form=form, user=user)

    return app


if __name__ == "__main__":
    create_app().run(host="127.0.0.1", port=int(os.environ.get("PORT", "8000")))
