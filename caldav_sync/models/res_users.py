from datetime import timedelta
import json

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
import caldav
import logging
from requests.exceptions import RequestException
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)

# Failed accounts wait 5, 10, 20 ... minutes before the next attempt, at most 6 h.
CALDAV_RETRY_BASE_SECONDS = 300
CALDAV_RETRY_MAX_SECONDS = 6 * 3600
# A healthy account refreshes its last-success time at most this often, so the
# shared system-parameter cache is not invalidated by every two-minute poll.
CALDAV_SUCCESS_REFRESH_SECONDS = 3600


class ResUsers(models.Model):
    _inherit = "res.users"

    caldav_calendar_url = fields.Char(
        string="CalDAV Calendar URL", default="https://caldav.icloud.com/"
    )
    caldav_username = fields.Char(string="CalDAV Username")
    # Write-only, like base's `password`: the value lives in the
    # res_users.caldav_password column, which the ORM never reads, so no
    # read, export, search or copy can return it. Only the owner (or a
    # superuser job) can set it; see _set_caldav_password.
    caldav_password = fields.Char(
        string="CalDAV Password",
        compute="_compute_caldav_password",
        inverse="_set_caldav_password",
        copy=False,
        help="Uložené heslo sa z bezpečnostných dôvodov nezobrazuje. "
        "Nové heslo zadajte len vtedy, keď ho chcete zmeniť.",
    )
    caldav_calendar_option_id = fields.Many2one(
        "caldav.calendar.option", string="CalDAV Calendar"
    )
    # Configured with credentials the server accepted. An unreachable server
    # keeps the account enabled; the poll job records reachability in the
    # per-account health record (see _caldav_health).
    is_caldav_enabled = fields.Boolean(compute="_compute_is_caldav_enabled", store=True)

    _caldav_connection_fields = {
        "caldav_calendar_url",
        "caldav_username",
        "caldav_password",
    }

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + self._caldav_preference_fields

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + self._caldav_preference_fields

    _caldav_preference_fields = [
        "caldav_calendar_url",
        "caldav_username",
        "caldav_password",
        "caldav_calendar_option_id",
    ]

    def init(self):
        super().init()
        self.env.cr.execute(
            "ALTER TABLE res_users ADD COLUMN IF NOT EXISTS caldav_password varchar"
        )

    def _compute_caldav_password(self):
        for user in self:
            user.caldav_password = ""

    def _set_caldav_password(self):
        for user in self:
            password = user.caldav_password or None
            if not (self.env.su or user.id == self.env.uid) and (
                password or not self.env.user._is_system()
            ):
                raise AccessError(_("Only the user can set their own CalDAV password."))
            self.env.cr.execute(
                "UPDATE res_users SET caldav_password = %s WHERE id = %s",
                (password, user.id),
            )
        # The cache holds the plain value until the end of the request.
        self.invalidate_recordset(["caldav_password"])
        # The SQL write is invisible to the ORM; on create the stored flag was
        # already computed without the password, so compute it again.
        self.env.add_to_compute(self._fields["is_caldav_enabled"], self)

    def _get_caldav_password(self):
        self.ensure_one()
        self.env.cr.execute(
            "SELECT caldav_password FROM res_users WHERE id = %s", (self.id,)
        )
        row = self.env.cr.fetchone()
        return row[0] if row else None

    def _sync_microsoft_calendar(self):
        """Run one Microsoft sync at a time and avoid CalDAV fan-out.

        The Microsoft and CalDAV bridges can both be enabled for a user. Without
        this guard, a Microsoft sync writes events while the CalDAV override
        synchronously mirrors those writes, and concurrent browser sync requests
        can lock the same calendar event until the reverse proxy times out.
        """
        self.ensure_one()
        self.env.cr.execute(
            "SELECT pg_try_advisory_xact_lock(hashtext(%s), %s)",
            ("caldav_sync.microsoft_calendar", self.id),
        )
        if not self.env.cr.fetchone()[0]:
            _logger.info(
                "Skipping concurrent Microsoft calendar sync for user ID %s",
                self.id,
            )
            return False

        # Cron callers may provide a superuser environment; Microsoft import
        # semantics must follow this record's account, not env.uid.
        sync_env = self.env(user=self.id, su=True)
        sync_user = self.with_env(sync_env).with_context(
            caldav_no_sync=True,
            calendar_sync_source="microsoft",
        )
        return super(ResUsers, sync_user)._sync_microsoft_calendar()

    @api.constrains("caldav_calendar_url")
    def _check_caldav_calendar_url(self):
        for user in self:
            if user.caldav_calendar_url and urlparse(user.caldav_calendar_url).scheme.lower() == "webcal":
                raise ValidationError(_(
                    "A webcal URL is a read-only subscription. Use the HTTPS CalDAV endpoint, then choose a calendar."
                ))

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        users._refresh_caldav_calendar_options()
        return users

    def write(self, vals):
        vals = dict(vals)
        option_selected = "caldav_calendar_option_id" in vals
        if option_selected and vals["caldav_calendar_option_id"]:
            option = self.env["caldav.calendar.option"].sudo().browse(
                vals["caldav_calendar_option_id"]
            ).exists()
            if not option or any(option.user_id != user for user in self):
                raise ValidationError(_("Choose a calendar discovered for this user."))
            vals["caldav_calendar_url"] = option.url
        elif "caldav_calendar_url" in vals:
            vals["caldav_calendar_option_id"] = False

        result = super().write(vals)
        if self._caldav_connection_fields.intersection(vals) or option_selected:
            # New connection settings deserve an immediate attempt.
            self._caldav_reset_health()
        if not option_selected and self._caldav_connection_fields.intersection(vals):
            self._refresh_caldav_calendar_options()
        return result

    # Per-account health lives in a system parameter (settings administrators
    # only) as content-free JSON: timestamps, the exception class name and a
    # counter, never a message, URL or credential. Keeping it out of res_users
    # needs no schema change on the loaded release copy.
    def _caldav_health_key(self):
        return "caldav_sync.health.%s" % self.id

    def _caldav_health(self):
        self.ensure_one()
        text = self.env["ir.config_parameter"].sudo().get_param(
            self._caldav_health_key(), ""
        )
        try:
            health = json.loads(text) if text else {}
        except (TypeError, ValueError):
            health = {}
        return health if isinstance(health, dict) else {}

    def _caldav_set_health(self, health):
        self.ensure_one()
        self.env["ir.config_parameter"].sudo().set_param(
            self._caldav_health_key(),
            json.dumps(health, sort_keys=True, separators=(",", ":")) if health else False,
        )

    def _caldav_reset_health(self):
        for user in self:
            health = user._caldav_health()
            if health.get("streak") or health.get("next_retry"):
                user._caldav_set_health({
                    key: value for key, value in health.items() if key == "last_success"
                })

    def _caldav_record_poll(self, error=None, backoff=True):
        """Store the outcome of one poll attempt; called by the poll job.

        Only connection failures (backoff=True) delay the next attempt."""
        now = fields.Datetime.now()
        stamp = fields.Datetime.to_string(now)
        for user in self:
            health = user._caldav_health()
            if error is None:
                last = fields.Datetime.to_datetime(health.get("last_success"))
                if (
                    not health.get("streak") and not health.get("failure_class")
                    and last and (now - last).total_seconds() < CALDAV_SUCCESS_REFRESH_SECONDS
                ):
                    continue
                user._caldav_set_health({"last_success": stamp})
                continue
            streak = int(health.get("streak") or 0) + 1
            health.update({
                "last_failure": stamp,
                "failure_class": type(error).__name__[:64],
                "streak": streak,
            })
            health.pop("next_retry", None)
            if backoff:
                delay = min(
                    CALDAV_RETRY_BASE_SECONDS * 2 ** min(streak - 1, 16),
                    CALDAV_RETRY_MAX_SECONDS,
                )
                health["next_retry"] = fields.Datetime.to_string(
                    now + timedelta(seconds=delay)
                )
            user._caldav_set_health(health)

    def _caldav_retry_due(self, now=None):
        next_retry = fields.Datetime.to_datetime(self._caldav_health().get("next_retry"))
        return not next_retry or next_retry <= (now or fields.Datetime.now())

    @api.onchange("caldav_calendar_option_id")
    def _onchange_caldav_calendar_option_id(self):
        if self.caldav_calendar_option_id:
            self.caldav_calendar_url = self.caldav_calendar_option_id.url

    def _refresh_caldav_calendar_options(self):
        option_model = self.env["caldav.calendar.option"].sudo()
        for user in self:
            existing_options = option_model.search([("user_id", "=", user.id)])
            if not all(
                (
                    user.caldav_calendar_url,
                    user.caldav_username,
                    user._get_caldav_password(),
                )
            ):
                existing_options.unlink()
                continue

            try:
                calendars = user._get_caldav_client().principal().calendars()
                values = [
                    {
                        "user_id": user.id,
                        "name": calendar.name or _("Calendar"),
                        "url": str(calendar.url),
                    }
                    for calendar in calendars
                ]
            except Exception:
                existing_options.unlink()
                _logger.warning(
                    "Unable to discover CalDAV calendars for user ID %s", user.id
                )
                continue

            with self.env.cr.savepoint():
                existing_options.unlink()
                option_model.create(values)

    @api.depends("caldav_username", "caldav_password", "caldav_calendar_url")
    def _compute_is_caldav_enabled(self):
        """CalDAV is enabled when the account is configured and the server
        accepts it. We only access the principal, without fetching events.

        An unreachable server (timeout, DNS, refused connection) does not
        disable the account: this value is stored, so a transient failure
        would otherwise drop the account from polling until its settings
        change. The poll job records and backs off such failures instead."""
        for rec in self:
            # If any required field is empty, CalDAV is disabled
            if not (
                rec.caldav_username
                and rec._get_caldav_password()
                and rec.caldav_calendar_url
            ):
                rec.is_caldav_enabled = False
                continue
            try:
                client = rec._get_caldav_client()
                # Just try to access the principal, which is a lightweight operation
                client.principal()
                rec.is_caldav_enabled = True
            except RequestException as error:
                rec.is_caldav_enabled = True
                _logger.warning(
                    "CalDAV server unreachable while validating user ID %s (%s); "
                    "the account stays enabled and the poll job will retry",
                    rec.id, type(error).__name__,
                )
            except Exception:
                rec.is_caldav_enabled = False
                _logger.error("Failed to validate CalDAV credentials for user ID %s", rec.id)

    def _get_caldav_client(self):
        self.ensure_one()
        return caldav.DAVClient(
            url=self.caldav_calendar_url,
            username=self.caldav_username,
            password=self._get_caldav_password(),
            timeout=30,
        )

    def _get_caldav_events(self):
        self.ensure_one()
        client = self._get_caldav_client()
        try:
            calendar = client.calendar(url=self.caldav_calendar_url)
            events = calendar.events()
            self.is_caldav_enabled = True
        except Exception:
            self.is_caldav_enabled = False
            _logger.error("Failed to fetch CalDAV events for user ID %s", self.id)
            try:
                principal = client.principal()
                msg = (
                    f"Failed to connect to the calendar, but successfully connected "
                    f"to the server at {client.url}. You may need to select another "
                    f"calendar URL from those below.\n\n"
                )
                for calendar in principal.calendars():
                    msg += f"{calendar.name}: {calendar.url}\n"
                raise UserError(msg)
            except Exception:
                _logger.error("Failed to inspect CalDAV calendars for user ID %s", self.id)
                return
        return events
