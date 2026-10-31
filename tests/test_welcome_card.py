"""Image welcome cards (2.19): drawing, upload checks, storage and routes.

The bot's half (attaching the card to an announcement) is in
`tests/test_announcements.py`, beside the rest of that cog's tests.
"""

import base64
import io
import os
import stat
import tempfile
import time
import unittest

from PIL import Image

from core import database
from core import settings_cache
from core import welcome_card

GUILD_ID = 1420070400000000051


def image_bytes(size=(1600, 900), fmt="PNG", colour=(200, 80, 40)):
    output = io.BytesIO()
    Image.new("RGB", size, colour).save(output, format=fmt)
    return output.getvalue()


def decoded(png: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(png))
    image.load()
    return image


class DrawingTests(unittest.TestCase):
    def test_a_card_is_a_card_sized_png_with_or_without_uploads(self):
        for background, avatar in ((None, None), (welcome_card.normalise_background(
                image_bytes()), image_bytes((256, 256)))):
            with self.subTest(uploaded=background is not None):
                card = decoded(welcome_card.render_card(background, avatar, "tester",
                                                        "Welcome to Test Guild!"))
                self.assertEqual(("PNG", welcome_card.CARD_SIZE), (card.format, card.size))

    def test_glyphs_the_font_lacks_are_dropped_and_spaces_kept_single(self):
        font = welcome_card._font(40)
        self.assertEqual("Árvíztűrő Tükör", welcome_card.drawable("Árvíztűrő 🎮 Tükör", font))

    def test_a_name_too_long_for_any_size_is_cut_with_an_ellipsis(self):
        card = Image.new("RGB", welcome_card.CARD_SIZE)
        from PIL import ImageDraw
        font, shown = welcome_card._fit(ImageDraw.Draw(card), "W" * 400, 64, 32, 500)
        self.assertTrue(shown.endswith("…"))
        self.assertLessEqual(ImageDraw.Draw(card).textlength(shown, font=font), 500)

    def test_a_broken_avatar_draws_the_initial_instead(self):
        card = decoded(welcome_card.render_card(None, b"not an image", "tester", "x"))
        self.assertEqual(welcome_card.CARD_SIZE, card.size)


class UploadTests(unittest.TestCase):
    def test_an_accepted_image_is_re_encoded_at_the_cards_size(self):
        for fmt in ("PNG", "JPEG", "WEBP", "GIF"):
            with self.subTest(fmt=fmt):
                stored = decoded(welcome_card.normalise_background(image_bytes(fmt=fmt)))
                self.assertEqual(("PNG", welcome_card.CARD_SIZE), (stored.format, stored.size))

    def test_what_is_kept_is_redrawn_pixels_not_the_upload(self):
        """Trailing bytes after a valid image do not survive the re-encode."""
        upload = image_bytes() + b"<script>alert(1)</script>"
        self.assertNotIn(b"<script>", welcome_card.normalise_background(upload))

    def test_refusals_name_their_reason(self):
        bomb = io.BytesIO()
        # Tiny on disk, enormous when decoded: refused from the header alone.
        Image.new("1", (5000, 5000)).save(bomb, format="PNG")
        for data, reason in (
            (b"not an image at all", "card_image_unreadable"),
            (image_bytes((100, 100), fmt="BMP"), "card_image_unsupported"),
            (b"x" * (welcome_card.MAX_UPLOAD_BYTES + 1), "card_image_too_large"),
            (bomb.getvalue(), "card_image_too_large"),
        ):
            with self.subTest(reason=reason):
                with self.assertRaises(welcome_card.CardImageError) as caught:
                    welcome_card.normalise_background(data)
                self.assertEqual(reason, caught.exception.reason)


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "economy.db")

    def tearDown(self):
        database.DB_PATH = self.original_path
        self.temp_dir.cleanup()

    def test_a_background_lives_beside_the_database_and_is_not_world_readable(self):
        png = welcome_card.normalise_background(image_bytes())
        welcome_card.save_background(GUILD_ID, "join", png)
        path = welcome_card.background_path(GUILD_ID, "join")
        self.assertEqual(os.path.join(self.temp_dir.name, "cards"), str(path.parent))
        self.assertEqual(png, welcome_card.load_background(GUILD_ID, "join"))
        self.assertEqual(0, stat.S_IMODE(path.stat().st_mode) & 0o007)
        self.assertIsNone(welcome_card.load_background(GUILD_ID, "leave"))
        self.assertTrue(welcome_card.delete_background(GUILD_ID, "join"))
        self.assertFalse(welcome_card.delete_background(GUILD_ID, "join"))

    def test_the_returning_card_falls_back_to_the_join_background(self):
        join = welcome_card.normalise_background(image_bytes(colour=(10, 20, 30)))
        rejoin = welcome_card.normalise_background(image_bytes(colour=(200, 0, 0)))
        self.assertIsNone(welcome_card.load_background(GUILD_ID, "rejoin"))
        welcome_card.save_background(GUILD_ID, "join", join)
        self.assertEqual(join, welcome_card.load_background(GUILD_ID, "rejoin"))
        welcome_card.save_background(GUILD_ID, "rejoin", rejoin)
        self.assertEqual(rejoin, welcome_card.load_background(GUILD_ID, "rejoin"))
        self.assertEqual(join, welcome_card.load_background(GUILD_ID, "join"))

    def test_only_a_card_event_has_a_path(self):
        with self.assertRaises(ValueError):
            welcome_card.background_path(GUILD_ID, "ban")


class CardRouteTests(unittest.TestCase):
    def setUp(self):
        import dashboard_api
        self.api = dashboard_api
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_path = database.DB_PATH
        database.DB_PATH = os.path.join(self.temp_dir.name, "dashboard.db")
        database.initialize_database()
        database.register_guild(GUILD_ID, "Test Guild")
        settings_cache.invalidate()
        dashboard_api.app.config.update(TESTING=True)
        self.client = dashboard_api.app.test_client()
        self.original_admin_id = dashboard_api.ADMIN_ID
        dashboard_api.ADMIN_ID = "42"
        dashboard_api._rate_limit_events.clear()
        with self.client.session_transaction() as session:
            session.update({
                "logged_in": True, "user_id": "42",
                "display": {"username": "tester", "avatar": None},
                "csrf_token": "csrf-token", "server_session_id": "server-session",
                "authorized_guild_ids": [str(GUILD_ID)],
                "authenticated_at": time.time(),
            })
        self.headers = {"X-CSRF-Token": "csrf-token"}
        self.url = f"/api/guilds/{GUILD_ID}/announcements/join/card-background"

    def tearDown(self):
        self.api.ADMIN_ID = self.original_admin_id
        database.DB_PATH = self.original_path
        settings_cache.invalidate()
        self.temp_dir.cleanup()

    def upload(self, data):
        return self.client.post(self.url, headers=self.headers,
                                json={"image": base64.b64encode(data).decode()})

    def test_upload_status_and_remove(self):
        self.assertFalse(self.client.get(self.url).get_json()["data"]["exists"])
        self.assertEqual(200, self.upload(image_bytes()).status_code)
        self.assertTrue(self.client.get(self.url).get_json()["data"]["exists"])
        self.assertEqual(welcome_card.CARD_SIZE,
                         decoded(welcome_card.load_background(GUILD_ID, "join")).size)
        self.assertEqual(200, self.client.delete(self.url, headers=self.headers).status_code)
        self.assertIsNone(welcome_card.load_background(GUILD_ID, "join"))
        actions = [entry["action"] for entry in database.get_settings_audit(GUILD_ID)]
        self.assertEqual(["card_background.delete", "card_background.update"], actions)

    def test_a_refused_upload_explains_itself_and_stores_nothing(self):
        for body in ({"image": "%%% not base64"}, {"image": 5},
                     {"image": base64.b64encode(b"plain text").decode()}):
            with self.subTest(body=str(body)[:30]):
                response = self.client.post(self.url, headers=self.headers, json=body)
                self.assertEqual(400, response.status_code)
                self.assertNotIn("[", response.get_json()["message"])
        self.assertIsNone(welcome_card.load_background(GUILD_ID, "join"))

    def test_only_card_events_and_only_with_the_csrf_token(self):
        kick = f"/api/guilds/{GUILD_ID}/announcements/kick/card-background"
        self.assertEqual(400, self.client.get(kick).status_code)
        response = self.client.post(self.url, json={"image": ""})
        self.assertEqual(403, response.status_code)

    def test_the_preview_is_the_card_drawn_from_the_saved_settings(self):
        response = self.client.get(f"/api/guilds/{GUILD_ID}/announcements/join/card-preview")
        self.assertEqual(200, response.status_code)
        self.assertEqual("image/png", response.mimetype)
        self.assertEqual("no-store", response.headers["Cache-Control"])
        self.assertEqual(welcome_card.CARD_SIZE, decoded(response.data).size)

    def test_another_guild_cannot_reach_it(self):
        other = f"/api/guilds/{GUILD_ID + 1}/announcements/join/card-preview"
        self.assertIn(self.client.get(other).status_code, (401, 403))


if __name__ == "__main__":
    unittest.main()
