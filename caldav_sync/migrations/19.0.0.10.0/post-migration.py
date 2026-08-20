"""Remove orphaned contacts created solely by historical CalDAV imports."""

import logging

from psycopg2 import sql


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Delete only calendar-created placeholder partners with no other use.

    The old importer created a partner whose name exactly matched its email
    for every unknown invitee.  A partner is a cleanup candidate only when it
    has that shape, is linked to a calendar event, is not a user/contact child,
    and has no foreign-key references outside the calendar attendee relation.
    This leaves any ambiguous or business-used partner untouched.
    """
    cr.execute(
        """
        CREATE TEMPORARY TABLE caldav_sync_partner_cleanup_candidate
        ON COMMIT DROP AS
        SELECT DISTINCT partner.id
          FROM res_partner AS partner
          JOIN calendar_event_res_partner_rel AS event_partner
            ON event_partner.res_partner_id = partner.id
         WHERE partner.email IS NOT NULL
           AND partner.name = partner.email
           AND partner.parent_id IS NULL
           AND NOT EXISTS (
               SELECT 1 FROM res_users AS users
                WHERE users.partner_id = partner.id
           )
        """
    )

    # Preserve a candidate whenever any non-calendar table references it.
    # Query PostgreSQL's FK catalog instead of maintaining a brittle list of
    # every Odoo module's business tables.
    cr.execute(
        """
        SELECT namespace.nspname, relation.relname, attribute.attname
          FROM pg_constraint AS fk
          JOIN pg_class AS relation ON relation.oid = fk.conrelid
          JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
          JOIN pg_attribute AS attribute
            ON attribute.attrelid = relation.oid
           AND attribute.attnum = fk.conkey[1]
         WHERE fk.contype = 'f'
           AND fk.confrelid = 'res_partner'::regclass
           AND array_length(fk.conkey, 1) = 1
           AND relation.relname NOT IN (
               'calendar_event_res_partner_rel',
               'calendar_attendee'
           )
        """
    )
    for schema_name, table_name, column_name in cr.fetchall():
        own_row_clause = sql.SQL("")
        if table_name == "res_partner":
            own_row_clause = sql.SQL(" AND referenced.id != candidate.id")
        cr.execute(
            sql.SQL(
                """
                DELETE FROM caldav_sync_partner_cleanup_candidate AS candidate
                 WHERE EXISTS (
                    SELECT 1
                      FROM {}.{} AS referenced
                     WHERE referenced.{} = candidate.id{}
                 )
                """
            ).format(
                sql.Identifier(schema_name),
                sql.Identifier(table_name),
                sql.Identifier(column_name),
                own_row_clause,
            )
        )

    cr.execute(
        """
        DELETE FROM calendar_attendee AS attendee
         USING caldav_sync_partner_cleanup_candidate AS candidate
         WHERE attendee.partner_id = candidate.id
        """
    )
    cr.execute(
        """
        DELETE FROM calendar_event_res_partner_rel AS event_partner
         USING caldav_sync_partner_cleanup_candidate AS candidate
         WHERE event_partner.res_partner_id = candidate.id
        """
    )
    cr.execute(
        """
        DELETE FROM res_partner AS partner
         USING caldav_sync_partner_cleanup_candidate AS candidate
         WHERE partner.id = candidate.id
        """
    )
    _logger.info("CalDAV removed %s unused calendar-created partners", cr.rowcount)
