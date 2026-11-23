from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from leads.models import Lead
from outreach import ai, batches, report
from outreach.models import LeadAudit, LeadResearch, OutreachBatch, OutreachMessage
from outreach.tests.test_batches import SAFE, SETTINGS, make_draft


@override_settings(OUTREACH_REVIEW_EMAILS=False, **SETTINGS)
class QuietModeTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='u', password='pw')

    @patch('outreach.mailer.send_message', return_value=('n', 't'))
    @patch('outreach.ai.gemini.generate', return_value=SAFE)
    def test_no_review_or_auto_send_email_but_everything_still_works(self, _gen, send):
        make_draft(self.user, 'info@a.co.uk')
        make_draft(self.user, 'info@b.co.uk')
        batch = batches.create_batch(self.user)
        batch.refresh_from_db()
        self.assertIsNotNone(batch.notified_at)  # counts as notified: the Sunday report covers it
        OutreachBatch.objects.filter(pk=batch.pk).update(auto_send_at=timezone.now() - timedelta(minutes=1))
        self.assertEqual(len(batches.run_auto_sends()), 1)
        self.assertEqual(OutreachMessage.objects.filter(status='queued').count(), 2)
        send.assert_not_called()  # not one email to you


class ReviewerKnowsAboutTheCheckTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='u', password='pw')
        self.lead = Lead.objects.create(owner=self.user, email='info@shop.com', source='google-maps',
                                        category='Dentists', location='Austin, TX', link='https://shop.com',
                                        is_verified=True)
        self.message = OutreachMessage.objects.create(owner=self.user, lead=self.lead, to_email='info@shop.com',
                                                      subject='quick check', body='Hello')

    @override_settings(GEMINI_API_KEY='k')
    def test_prompt_carries_the_verified_findings(self):
        LeadAudit.objects.create(lead=self.lead, data={
            'counts': {'fail': 1, 'warn': 0, 'pass': 5}, 'checked_at': '2026-11-21T10:00:00+00:00',
            'url': 'https://shop.com/', 'facts': ['the site does not send a Referrer-Policy header']})
        seen = []

        def fake(prompt, **_kwargs):
            seen.append(prompt)
            return {'verdict': 'approve', 'confidence': 0.99, 'issues': []}
        with patch('outreach.ai.gemini.generate', side_effect=fake):
            self.assertEqual(ai.review_email(self.message), (True, ''))
        self.assertIn('verified_public_check', seen[0])
        self.assertIn('Referrer-Policy', seen[0])
        self.assertIn('REALLY', seen[0])

    @override_settings(GEMINI_API_KEY='k')
    def test_no_audit_means_no_exception_for_the_claim(self):
        seen = []

        def fake(prompt, **_kwargs):
            seen.append(prompt)
            return {'verdict': 'approve', 'confidence': 0.99, 'issues': []}
        with patch('outreach.ai.gemini.generate', side_effect=fake):
            ai.review_email(self.message)
        self.assertIn('EXCEPTION: when the recipient facts contain', seen[0])
        self.assertNotIn('"verified_public_check"', seen[0].split('Recipient facts:')[1])

    def test_greeting_drops_the_bracketed_part_of_a_business_name(self):
        LeadResearch.objects.create(lead=self.lead, status='ok', data={'name': 'Tacos Laguna (Food Truck)',
                                                                       'rating': 4.6, 'reviews': 172})
        _facts, greeting = ai.research_prompt_parts(self.lead)
        self.assertEqual(greeting, '"Hi Tacos Laguna team,"')


class SundayReportIsTheWholeStoryTests(TestCase):
    @override_settings(OUTREACH_REVIEW_BASE_URL='https://api.example.com')
    def test_lists_sent_held_and_ai_counts(self):
        user = get_user_model().objects.create_user(username='u', password='pw')
        now = timezone.now()
        lead = Lead.objects.create(owner=user, email='info@shop.com', source='google-maps',
                                   problem_statement='Local business: Duncan Realty. x')
        OutreachMessage.objects.create(owner=user, lead=lead, to_email='info@shop.com', subject='quick SEO check',
                                       body='b', status='sent', sent_at=now - timedelta(days=1))
        batch = OutreachBatch.objects.create(owner=user, status='held')
        OutreachMessage.objects.create(owner=user, to_email='hold@shop.com', subject='s', body='b', status='draft',
                                       batch=batch, hold_reason='Gemini was not sure (unsure): vague claim')
        text = report.build(report.collect(now + timedelta(minutes=1)))[1]
        for expected in ('Written by the AI this week: 2 first emails', 'Emails sent this week:',
                         'Duncan Realty | info@shop.com', 'Held back for your decision (1)',
                         'hold@shop.com: Gemini was not sure', '/api/outreach/batch-review/'):
            self.assertIn(expected, text)
