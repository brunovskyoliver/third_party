from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
import caldav
import logging
from urllib.parse import urlparse

_logger = logging.getLogger(__name__)


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
        if not option_selected and self._caldav_connection_fields.intersection(vals):
            self._refresh_caldav_calendar_options()
        return result

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
        """Compute whether CalDAV is enabled for each user by validating their credentials.
        We only check if we can connect to the server and access the principal, without
        fetching any events to avoid timeouts with large calendars."""
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
