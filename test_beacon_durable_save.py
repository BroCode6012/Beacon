"""Durable UI semantics: recorded requests survive slow analysis and failed retries."""
import threading
import unittest
from unittest.mock import Mock, patch

import engine_link
import beacon_save
from beacon_app import BeaconApp


class DurableSaveUI(unittest.TestCase):
    def app(self):
        app = BeaconApp.__new__(BeaconApp)
        app.client = Mock()
        app._durable_saves = True
        app._saved_draft = '@Example'
        app._saved_niche_id = 7
        app._saved_result = None
        app._saving_link = app._saved_refreshing = False
        app._saved_refreshed_at = 0
        app.saved_shown = 120
        app.section = 'Channels'
        app._render = Mock()
        app._invalidate = Mock()
        app.toast = Mock()
        app.run_bg = lambda work, done, message, **kw: done(work())
        return app

    def test_queue_never_runs_slow_legacy_analysis_in_browser(self):
        app = self.app()
        app.client.call.return_value = {'id': 19, 'status': 'queued'}
        with patch.object(beacon_save, 'save_channel', side_effect=AssertionError('slow work')):
            app._save_link()
        app.client.call.assert_called_once_with('enqueue_channel_save', link='@Example')
        self.assertEqual(app._saved_result['request_id'], 19)
        self.assertTrue(app._saved_result['queued'])
        self.assertEqual(app._saved_draft, '')
        self.assertFalse(app._saving_link)

    def test_unrecorded_request_keeps_link_and_releases_button(self):
        app = self.app()
        app.client.call.return_value = {'error': 'Temporarily unavailable'}
        app._save_link()
        self.assertEqual(app._saved_draft, '@Example')
        self.assertIn('error', app._saved_result)
        self.assertFalse(app._saving_link)

    def test_transport_diagnostics_are_replaced_with_simple_recovery_text(self):
        finder = Mock()
        finder.call.side_effect = RuntimeError("Finder call failed (400): Use a YouTube channel or video link.")
        with self.assertRaisesRegex(beacon_save.SaveError, '^Use a YouTube channel or video link\\.$'):
            beacon_save.queue_channel(finder, 'https://example.com')
        finder.call.side_effect = RuntimeError('private driver details')
        with self.assertRaisesRegex(beacon_save.SaveError, '^Could not record your request'):
            beacon_save.queue_channel(finder, '@Example')

    def test_refresh_queues_work_without_claiming_numbers_were_checked(self):
        app = self.app()
        app.client.call.return_value = {'queued_refresh': 3}
        with patch.object(beacon_save, 'refresh_saved', side_effect=AssertionError('slow work')):
            app._saved_refresh()
        app.client.call.assert_called_once_with('queue_saved_channel_refresh', niche_id=7)
        self.assertEqual(app._saved_refreshed_at, 0)
        self.assertFalse(app._saved_refreshing)

    def test_pending_and_failed_labels_preserve_the_distinction(self):
        self.assertEqual(beacon_save.request_status({'status': 'queued'})[0], 'Queued')
        self.assertEqual(beacon_save.request_status({'status': 'working'})[0], 'Finding the channel…')
        self.assertEqual(beacon_save.request_status({'status': 'working', 'saved_at': 'now'})[0],
                         'Saved — analysis pending')
        self.assertEqual(beacon_save.request_status({'status': 'failed', 'saved_at': 'now'})[0],
                         'Saved — analysis needs a retry')

    def test_background_redraw_reuses_the_typing_control(self):
        app = self.app()
        app._render_saved_with_field = Mock()
        app._render_saved()
        first = app._saved_link_field
        first.value = '@StillTyping'
        app._render_saved()
        self.assertIs(app._saved_link_field, first)
        self.assertEqual(first.value, '@StillTyping')

    def test_disconnected_session_drops_late_poll_result(self):
        app = self.app()
        app.cache, app._epoch, app._loading = {}, {}, set()
        app._session_stop = threading.Event()
        def disconnect(**kw):
            app._session_stop.set()
            return [{'id': 1, 'status': 'ready'}]
        app.client.call.side_effect = lambda *a, **kw: disconnect(**kw)
        workers = []
        def inline_thread(*, target, daemon):
            workers.append(target)
            return Mock(start=target)
        with patch('beacon_app.threading.Thread', side_effect=inline_thread):
            app._save_requests_tick()
        self.assertEqual(app.cache, {})
        self.assertEqual(app._loading, set())
        app._invalidate.assert_not_called()

    def test_retry_failure_is_visible_and_does_not_claim_queued(self):
        app = self.app()
        app.client.call.side_effect = RuntimeError('Unavailable')
        app._retry_save_request(5)
        self.assertIn('error', app._saved_result)
        self.assertNotIn('queued', app._saved_result)
