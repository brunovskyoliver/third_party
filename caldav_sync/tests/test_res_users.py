from odoo.tests import TransactionCase, tagged
from odoo.exceptions import ValidationError
from unittest.mock import patch, MagicMock
from .common import CaldavTestCommon
import caldav
import logging
from types import SimpleNamespace

_logger = logging.getLogger(__name__)


@tagged("post_install", "-at_install")
class TestUsers(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()

    def test_caldav_enabled_false_without_url(self):
        # Create user with no CalDAV credentials
        user = self._generate_user("test")
        self.assertFalse(user.is_caldav_enabled)

    def test_caldav_calendar_url_defaults_to_icloud_endpoint(self):
        user = self._generate_user("icloud-default")

        self.assertEqual(user.caldav_calendar_url, "https://caldav.icloud.com/")

    def test_caldav_enabled_false_without_credentials(self):
        """Test that is_caldav_enabled is False when any required field is missing."""
        # Test with missing URL - has username and password only
        user1 = self._generate_user(
            "test1", caldav_username="user1", caldav_password="pass1"
        )
        self.assertFalse(user1.is_caldav_enabled)

        # Test with missing username - has password and URL only
        user2 = self._generate_user(
            "test2", caldav_password="pass2", caldav_url="https://example.com"
        )
        self.assertFalse(user2.is_caldav_enabled)

        # Test with missing password - has username and URL only
        user3 = self._generate_user(
            "test3", caldav_username="user3", caldav_url="https://example.com"
        )
        self.assertFalse(user3.is_caldav_enabled)

    @patch("caldav.DAVClient")
    def test_caldav_enabled_success(self, MockDAVClient):
        """Test that is_caldav_enabled is True when connection succeeds."""
        # Create user with name 'test' and set CalDAV credentials
        user = self._generate_user(
            "test",
            caldav_username="user",
            caldav_password="pass",
            caldav_url="https://example.com/abc123",
        )

        # Mock successful connection
        mock_client = MockDAVClient.return_value
        mock_principal = MagicMock()
        mock_client.principal.return_value = mock_principal

        # Compute should succeed and set is_caldav_enabled to True
        user._compute_is_caldav_enabled()
        self.assertTrue(user.is_caldav_enabled)

    @patch("caldav.DAVClient")
    def test_caldav_enabled_connection_fails(self, MockDAVClient):
        """Test that is_caldav_enabled is False when connection fails."""
        user = self._generate_user(
            "test",
            caldav_username="user",
            caldav_password="pass",
            caldav_url="https://example.com/abc123",
        )

        # Mock failed connection
        mock_client = MockDAVClient.return_value
        mock_client.principal.side_effect = caldav.error.AuthorizationError(
            "Invalid credentials"
        )

        # Should handle the error gracefully and set is_caldav_enabled to False
        with self.assertLogs("odoo.addons.caldav_sync.models.res_users", "ERROR"):
            user._compute_is_caldav_enabled()
        self.assertFalse(user.is_caldav_enabled)

    def test_webcal_calendar_url_is_rejected(self):
        user = self._generate_user("webcal")

        with self.assertRaises(ValidationError):
            user.write({"caldav_calendar_url": "webcal://example.com/calendar.ics"})

    @patch("caldav.DAVClient")
    def test_saving_credentials_discovers_inline_calendar_options(self, mock_dav_client):
        user = self._generate_user("inline")
        mock_dav_client.return_value.principal.return_value.calendars.return_value = [
            SimpleNamespace(name="Práca", url="https://example.com/caldav/work/"),
            SimpleNamespace(name="Súkromný", url="https://example.com/caldav/private/"),
        ]

        user.write(
            {
                "caldav_calendar_url": "https://example.com/caldav/",
                "caldav_username": "inline-user",
                "caldav_password": "inline-password",
            }
        )

        options = self.env["caldav.calendar.option"].search(
            [("user_id", "=", user.id)], order="name"
        )
        self.assertEqual(options.mapped("name"), ["Práca", "Súkromný"])
        self.assertEqual(
            options.mapped("url"),
            [
                "https://example.com/caldav/work/",
                "https://example.com/caldav/private/",
            ],
        )

        user.write({"caldav_calendar_option_id": options[0].id})

        self.assertEqual(user.caldav_calendar_option_id, options[0])
        self.assertEqual(user.caldav_calendar_url, options[0].url)
