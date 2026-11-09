from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from web import create_app, users_revision


BASE_CONFIG = {
    "hash_file": "hash.json",
    "scrape": {"delay": 0.25, "timeout": 30, "verbose": True},
    "email": {"from": "Alert <alert@example.com>", "subject": "Updates"},
    "users": [
        {
            "name": "Ana",
            "email_to": ["ana@example.com"],
            "date_to": "2026-12-31",
            "city": ["JESENICE"],
            "category": ["B"],
            "type": "DRIVING_TEST",
            "future_field": "keep-me",
        },
        {
            "name": "Bine",
            "email_to": ["bine@example.com"],
            "date_from": "2026-11-10",
            "date_to": "2026-12-15",
            "city": ["KRANJ"],
            "category": ["B"],
            "type": "CPP",
        },
    ],
}


class WebAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.config_path = Path(self.temporary.name) / "config.json"
        self.hash_path = Path(self.temporary.name) / "hash.json"
        self.write_config(BASE_CONFIG)
        self.hash_path.write_text('{"users":{"Ana":"unchanged"}}\n', encoding="utf-8")
        self.app = create_app(
            self.config_path,
            {"TESTING": True, "SECRET_KEY": "test-secret", "WTF_CSRF_ENABLED": False},
        )
        self.client = self.app.test_client()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def write_config(self, data: dict) -> None:
        self.config_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def read_config(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def revision(self) -> str:
        return users_revision(self.read_config()["users"])

    def valid_form(self, **overrides):
        data = {
            "name": "Cilka",
            "email_to": "cilka@example.com, drugo@example.com",
            "date_from": "2026-11-11",
            "date_to": "2026-12-20",
            "cities": ["JESENICE", "KRANJ"],
            "categories": "B",
            "appointment_type": "DRIVING_TEST",
            "revision": self.revision(),
        }
        data.update(overrides)
        return data

    def test_lists_users(self) -> None:
        response = self.client.get("/users")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Ana", response.get_data(as_text=True))
        self.assertIn("Bine", response.get_data(as_text=True))

    def test_adds_user_and_preserves_other_config(self) -> None:
        response = self.client.post("/users/new", data=self.valid_form())
        self.assertEqual(response.status_code, 302)
        config = self.read_config()
        self.assertEqual(config["scrape"], BASE_CONFIG["scrape"])
        self.assertEqual(config["email"], BASE_CONFIG["email"])
        self.assertEqual(config["users"][-1]["name"], "Cilka")
        self.assertEqual(config["users"][-1]["city"], ["JESENICE", "KRANJ"])
        self.assertEqual(config["users"][-1]["category"], ["B"])

    def test_form_renders_searchable_locations_and_constrained_selects(self) -> None:
        response = self.client.get("/users/new")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('data-choice-search', body)
        self.assertIn('value="JESENICE"', body)
        self.assertIn('value="CPP"', body)
        self.assertIn('value="DRIVING_TEST"', body)
        self.assertNotIn("Vse vrste", body)
        for category in ("AM", "A1", "B", "CE", "G"):
            self.assertIn(f'value="{category}"', body)

    def test_edit_renames_user_preserves_unknown_field_and_hashes(self) -> None:
        hash_before = self.hash_path.read_bytes()
        response = self.client.post(
            "/users/0/edit",
            data=self.valid_form(name="Anja", email_to="anja@example.com"),
        )
        self.assertEqual(response.status_code, 302)
        user = self.read_config()["users"][0]
        self.assertEqual(user["name"], "Anja")
        self.assertEqual(user["future_field"], "keep-me")
        self.assertEqual(self.hash_path.read_bytes(), hash_before)

    def test_deletes_user_without_touching_hashes(self) -> None:
        hash_before = self.hash_path.read_bytes()
        response = self.client.post("/users/1/delete", data={"revision": self.revision()})
        self.assertEqual(response.status_code, 302)
        self.assertEqual([user["name"] for user in self.read_config()["users"]], ["Ana"])
        self.assertEqual(self.hash_path.read_bytes(), hash_before)

    def test_does_not_delete_last_user(self) -> None:
        config = self.read_config()
        config["users"] = config["users"][:1]
        self.write_config(config)
        response = self.client.post("/users/0/delete", data={"revision": self.revision()})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.read_config()["users"]), 1)

    def test_rejects_duplicate_name(self) -> None:
        response = self.client.post("/users/new", data=self.valid_form(name="Ana"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(self.read_config()["users"]), 2)

    def test_rejects_invalid_email_without_writing(self) -> None:
        before = self.config_path.read_bytes()
        response = self.client.post("/users/new", data=self.valid_form(email_to="not-an-email"))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_rejects_unknown_city_without_writing(self) -> None:
        before = self.config_path.read_bytes()
        response = self.client.post("/users/new", data=self.valid_form(cities=["NOT A LOCATION"]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_rejects_missing_or_invalid_exam_type(self) -> None:
        before = self.config_path.read_bytes()
        missing = self.client.post("/users/new", data=self.valid_form(appointment_type=""))
        invalid = self.client.post("/users/new", data=self.valid_form(appointment_type="TEST"))
        self.assertEqual(missing.status_code, 200)
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_rejects_invalid_category(self) -> None:
        before = self.config_path.read_bytes()
        response = self.client.post("/users/new", data=self.valid_form(categories="INVALID"))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_preserves_legacy_city_as_an_edit_option(self) -> None:
        config = self.read_config()
        config["users"][0]["city"] = ["LEGACY LOCATION"]
        self.write_config(config)
        response = self.client.get("/users/0/edit")
        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn('value="LEGACY LOCATION" checked', body)
        self.assertIn("LEGACY LOCATION (obstoječa vrednost)", body)

    def test_rejects_stale_form(self) -> None:
        old_revision = self.revision()
        config = self.read_config()
        config["users"][0]["city"] = ["LJUBLJANA"]
        self.write_config(config)
        response = self.client.post("/users/new", data=self.valid_form(revision=old_revision))
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.read_config()["users"]), 2)

    def test_csrf_is_enabled_by_default(self) -> None:
        app = create_app(self.config_path, {"TESTING": True, "SECRET_KEY": "test-secret"})
        response = app.test_client().post("/users/new", data={})
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
