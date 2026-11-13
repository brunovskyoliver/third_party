"""Same behavioral assertions run on LIVE and candidate; no new-API imports."""
from datetime import datetime
from unittest.mock import MagicMock, patch

from odoo.tests import TransactionCase, tagged
from .common import CaldavTestCommon
from .test_review_a import TestReviewA


@tagged('post_install', '-at_install', 'caldav_live_delta')
class TestCalDAVBehaviorDelta(TransactionCase, CaldavTestCommon):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with patch('caldav.DAVClient'):
            cls.user = cls._generate_user('live-delta', 'synthetic', 'synthetic', 'https://calendar.example.test/')
        cls.events = cls.env['calendar.event']

    def setUp(self):
        super().setUp()
        self.calendar = MagicMock()
        self.calendar.get_properties.return_value = {}
        client = MagicMock()
        client.calendar.return_value = self.calendar
        patcher = patch.object(type(self.user), '_get_caldav_client', return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)

    _component = TestReviewA._component
    _resource = TestReviewA._resource
    _poll = TestReviewA._poll
    _detached = TestReviewA._detached
    _ordinary = TestReviewA._ordinary
    _series_master = TestReviewA._series_master

    def test_six_detached_group_shapes_preserve_original_ids(self):
        for index, count in enumerate((35, 2, 2, 2, 4, 4)):
            with self.subTest(size=count, group=index):
                uid = 'live-delta-detached-%s' % index
                group = self._detached(uid, count, datetime(2022, 5, 1, 9))
                master = self._component(uid, start=group[0].start)
                master.add('rrule', {'freq': 'daily', 'count': count})
                imported = self.events._sync_event_from_ical(self._resource([master]).icalendar_instance, self.user)
                self.assertEqual(set(imported.ids), set(group.ids))
                self.assertEqual(len(group.exists().filtered('active')), count)

    def test_one_remote_delete_archives_instead_of_unlink(self):
        resources = self._ordinary(10)
        self._poll(resources)
        row = self.events.search([('caldav_uid', '=', 'ord-9')])
        self._poll(resources[:-1])
        self.assertTrue(row.exists(), 'Remote deletion physically unlinked the meeting')
        self.assertFalse(row.active)

    def test_mass_missing_resources_leave_every_row_visible(self):
        self._poll(self._ordinary(40))
        rows = self.events.search([('user_id', '=', self.user.id), ('caldav_uid', '!=', False)])
        outcome = self._poll([])
        self.assertIsNotNone(outcome)
        self.assertEqual(len(rows.exists().filtered('active')), 40)

    def test_partial_import_returns_failure_not_success(self):
        with patch.object(type(self.events), '_sync_event_from_ical', side_effect=ValueError('Synthetic invalid payload')):
            outcome = self._poll(self._ordinary(1))
        self.assertIsNotNone(outcome, 'Incomplete import reported success')

    def test_archived_occurrence_reappears_as_original_id(self):
        resource = self._resource([self._series_master('live-delta-restore', 4)])
        self._poll([resource])
        rows = self.events.search([('caldav_uid', '=', 'live-delta-restore')])
        rows.recurrence_id.base_event_id.with_context(caldav_no_sync=True, no_microsoft_sync=True).write({
            'active': False, 'recurrence_update': 'self_only',
        })
        self._poll([resource])
        self.assertEqual(len(rows.exists().filtered('active')), 4)

    def test_remote_loss_of_rrule_preserves_every_occurrence(self):
        master = self._series_master('live-delta-rule', 4)
        self._poll([self._resource([master])])
        rows = self.events.search([('caldav_uid', '=', 'live-delta-rule')])
        master.pop('rrule')
        # Stub only outbound adapters so LIVE's destructive break cannot be
        # accidentally rolled back by an unrelated mock transport exception.
        with patch.object(type(self.events), '_sync_unlink_to_caldav'), \
                patch.object(type(self.events), '_sync_write_to_caldav'):
            self._poll([self._resource([master])])
        self.assertEqual(len(rows.exists().filtered('active')), 4)
