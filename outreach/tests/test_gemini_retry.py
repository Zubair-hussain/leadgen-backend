from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from leadgen import gemini
from outreach import batches
from outreach.models import OutreachBatch, OutreachMessage
from outreach.tests.test_batches import SAFE, SETTINGS, make_draft

DOWN = gemini.GeminiError('Gemini returned 503: This model is currently experiencing high demand.')


@override_settings(**SETTINGS)
class GeminiOutageRetryTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='u', password='pw')

    def held_batch(self, *emails):
        for email in emails:
            make_draft(self.user, email)
        with patch('outreach.mailer.send_message', return_value=('n', 't')):
            batch = batches.create_batch(self.user)
        OutreachBatch.objects.filter(pk=batch.pk).update(auto_send_at=timezone.now() - timedelta(minutes=1))
        with patch('outreach.mailer.send_message', return_value=('n', 't')) as send, \
                patch('outreach.ai.gemini.generate', side_effect=DOWN):
            batches.run_auto_sends()
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'held')
        self.assertTrue(all(m.hold_reason.startswith(batches.GEMINI_DOWN) for m in batch.messages.all()))
        self.assertEqual(send.call_count, 1)  # the first outcome is reported to you
        return batch

    def age(self, batch, minutes):
        OutreachBatch.objects.filter(pk=batch.pk).update(decided_at=timezone.now() - timedelta(minutes=minutes))

    @patch('outreach.mailer.send_message', return_value=('n', 't'))
    def test_held_batch_is_retried_and_sent_once_gemini_is_back(self, send):
        batch = self.held_batch('info@a.co.uk', 'info@b.co.uk')
        self.age(batch, 30)
        with patch('outreach.ai.gemini.generate', return_value=SAFE):
            self.assertEqual(len(batches.run_auto_sends()), 1)
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'auto_approved')
        self.assertEqual(OutreachMessage.objects.filter(status='queued').count(), 2)
        self.assertIn('2 queued, 0 held back', send.call_args.args[1])

    @patch('outreach.mailer.send_message', return_value=('n', 't'))
    def test_not_retried_within_twenty_minutes(self, send):
        batch = self.held_batch('info@a.co.uk')
        self.age(batch, 5)
        with patch('outreach.ai.gemini.generate', return_value=SAFE):
            self.assertEqual(batches.run_auto_sends(), [])
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'held')
        send.assert_not_called()

    @patch('outreach.mailer.send_message', return_value=('n', 't'))
    def test_a_retry_that_fails_again_does_not_email_you_again(self, send):
        batch = self.held_batch('info@a.co.uk')
        self.age(batch, 30)
        with patch('outreach.ai.gemini.generate', side_effect=DOWN):
            batches.run_auto_sends()
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'held')
        send.assert_not_called()
        self.age(batch, 30)  # and it can try again later
        with patch('outreach.ai.gemini.generate', return_value=SAFE):
            self.assertEqual(len(batches.run_auto_sends()), 1)

    @patch('outreach.mailer.send_message', return_value=('n', 't'))
    def test_batches_held_for_a_real_reason_are_left_alone(self, send):
        make_draft(self.user, 'someone@outlook.com')
        batch = batches.create_batch(self.user)
        OutreachBatch.objects.filter(pk=batch.pk).update(auto_send_at=timezone.now() - timedelta(minutes=1))
        with patch('outreach.ai.gemini.generate', return_value=SAFE):
            batches.run_auto_sends()
        self.age(batch, 60)
        send.reset_mock()
        with patch('outreach.ai.gemini.generate', return_value=SAFE):
            self.assertEqual(batches.run_auto_sends(), [])
        batch.refresh_from_db()
        self.assertEqual(batch.status, 'held')
        send.assert_not_called()

    def test_very_old_held_batches_are_not_revived(self):
        batch = self.held_batch('info@a.co.uk')
        self.age(batch, 60 * 24 * 5)
        self.assertEqual(batches.release_gemini_holds(), [])


@override_settings(GEMINI_API_KEY='key-one', GEMINI_API_KEYS=['key-two', 'key-one'], GEMINI_MODEL='main',
                   GEMINI_FALLBACK_MODELS='backup')
class GeminiKeyRotationTests(TestCase):
    OK = {'candidates': [{'content': {'parts': [{'text': 'hi'}]}}]}

    def reply(self, status, text=''):
        from unittest.mock import MagicMock
        return MagicMock(status_code=status, text=text, json=lambda: self.OK)

    def test_keys_are_deduplicated_and_ordered(self):
        self.assertEqual(gemini.api_keys(), ['key-one', 'key-two'])

    @patch('leadgen.gemini.requests.post')
    def test_quota_error_switches_to_the_next_key_on_the_same_model(self, post):
        post.side_effect = [self.reply(429, 'quota'), self.reply(200)]
        self.assertEqual(gemini.generate('x'), 'hi')
        first, second = post.call_args_list
        self.assertEqual((first.kwargs['headers']['x-goog-api-key'], second.kwargs['headers']['x-goog-api-key']),
                         ('key-one', 'key-two'))
        self.assertTrue(first.args[0].endswith('main:generateContent') and second.args[0].endswith('main:generateContent'))

    @patch('leadgen.gemini.requests.post')
    def test_overload_goes_to_the_next_model_not_the_next_key(self, post):
        post.side_effect = [self.reply(503, 'busy'), self.reply(200)]
        gemini.generate('x')
        self.assertTrue(post.call_args_list[1].args[0].endswith('backup:generateContent'))
        self.assertEqual(post.call_args_list[1].kwargs['headers']['x-goog-api-key'], 'key-one')
