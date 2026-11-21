from datetime import timedelta
from unittest.mock import MagicMock, patch

import requests
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.utils import timezone

from leads.models import Lead
from leads.services import LeadGenerator
from outreach import ai, audit, autopilot, branding, report
from outreach.models import LeadAudit, OutreachMessage
from outreach.tests.test_sequence import GOOD_BODY, GOOD_SUBJECT

GOOD_HTML = (
    '<html lang="en"><head><title>Summit Dental | Family dentist in Austin Texas</title>'
    '<meta name="description" content="Gentle family dentistry in Austin. Same-day appointments, evening hours, '
    'and clear prices before any treatment starts.">'
    '<meta name="viewport" content="width=device-width, initial-scale=1">'
    '<script type="application/ld+json">{}</script></head>'
    '<body><h1>Family dentist in Austin</h1><img src="a.jpg" alt="Our clinic"></body></html>')
BAD_HTML = '<html><head></head><body><img src="a.jpg"><img src="b.jpg"></body></html>'
GOOD_HEADERS = {'strict-transport-security': 'max-age=31536000', 'x-content-type-options': 'nosniff',
                'referrer-policy': 'no-referrer', 'x-frame-options': 'DENY', 'content-encoding': 'br'}


class FakeResponse:
    def __init__(self, headers=None, status=200):
        self.headers = headers or {}
        self.status_code = status
        self.encoding = 'utf-8'
        self.is_redirect = False


def run_audit(html, headers, url='https://shop.com/'):
    redirect = MagicMock(status_code=301, is_redirect=True, headers={'location': url})
    with patch('outreach.audit.fetch', return_value=(FakeResponse(headers), html.encode(), 300, url)), \
            patch('outreach.audit.tls_certificate', return_value=(90, '')), \
            patch('outreach.audit.requests.get', return_value=redirect):
        return audit.website_audit(url, 'Summit Dental')


def make_user(name='owner'):
    return get_user_model().objects.create_user(username=name, email=name + '@example.com', password='pw')


def make_lead(user, link='https://shop.com', email='info@shop.com', **extra):
    return Lead.objects.create(owner=user, email=email, source='google-maps', category='Dentists',
                               location='Austin, TX', country='US', link=link, is_verified=True, **extra)


class SiteKindTests(TestCase):
    def test_classification(self):
        self.assertEqual(audit.site_kind(''), 'none')
        self.assertEqual(audit.site_kind('https://www.facebook.com/shop'), 'social')
        self.assertEqual(audit.site_kind('https://joes-plumbing.wixsite.com/home'), 'free_builder')
        self.assertEqual(audit.site_kind('https://joesplumbing.com'), 'own')


class WebsiteChecksTests(TestCase):
    def test_a_bare_site_gets_evidence_backed_findings(self):
        data = run_audit(BAD_HTML, {})
        by_id = {c['id']: c for cat in data['categories'].values() for c in cat['checks']}
        for failing in ('strict-transport-security', 'title', 'description', 'viewport', 'framing'):
            self.assertEqual(by_id[failing]['status'], 'fail', failing)
        self.assertIn('2 of 2 images', by_id['alt']['evidence'])
        self.assertIn('curl -I', by_id['strict-transport-security']['verify'])
        self.assertGreaterEqual(data['counts']['fail'], 5)
        self.assertTrue(data['facts'])
        self.assertTrue(all(' ms' not in fact for fact in data['facts']))  # timing never becomes an email claim
        self.assertTrue(audit.has_findings(data))

    def test_a_well_built_site_has_nothing_to_offer(self):
        data = run_audit(GOOD_HTML, GOOD_HEADERS)
        self.assertEqual(data['counts']['fail'], 0, data['facts'])
        self.assertGreaterEqual(data['score'], 90)
        self.assertFalse(audit.has_findings(data))

    def test_plain_http_site_is_flagged(self):
        with patch('outreach.audit.fetch', return_value=(FakeResponse(GOOD_HEADERS), GOOD_HTML.encode(), 200,
                                                          'http://shop.com/')):
            data = audit.website_audit('http://shop.com', 'x')
        https = data['categories']['Security']['checks'][0]
        self.assertEqual((https['id'], https['status']), ('https', 'fail'))
        self.assertIn('Not secure', https['fact'])

    def test_free_subdomain_is_a_finding(self):
        data = run_audit(GOOD_HTML, GOOD_HEADERS, url='https://joe.wixsite.com/home')
        failed = [c['id'] for cat in data['categories'].values() for c in cat['checks'] if c['status'] == 'fail']
        self.assertIn('own-domain', failed)


class StoredAuditTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.lead = make_lead(self.user)

    def test_built_once_then_cached(self):
        with patch('outreach.audit.website_audit', return_value=run_audit(BAD_HTML, {})) as built:
            first = audit.get_audit(self.lead)
            second = audit.get_audit(self.lead)
        self.assertEqual(first, second)
        built.assert_called_once()
        self.assertEqual(LeadAudit.objects.get(lead=self.lead).kind, 'website')
        stale = LeadAudit.objects.get(lead=self.lead)
        stale.checked_at = timezone.now() - timedelta(days=30)
        stale.save()
        with patch('outreach.audit.website_audit', return_value=run_audit(GOOD_HTML, GOOD_HEADERS)) as again:
            audit.get_audit(self.lead)
        again.assert_called_once()

    def test_unreachable_site_means_no_claims(self):
        with patch('outreach.audit.website_audit', side_effect=requests.ConnectionError('down')):
            self.assertIsNone(audit.get_audit(self.lead))
        self.assertFalse(LeadAudit.objects.exists())

    def test_social_only_business_gets_a_presence_check(self):
        lead = make_lead(self.user, link='https://facebook.com/summit', email='b@summit.com')
        data = audit.get_audit(lead)
        self.assertEqual(data['kind'], 'presence')
        self.assertEqual(data['categories']['SEO']['checks'][0]['status'], 'fail')

    @override_settings(OUTREACH_REVIEW_BASE_URL='https://api.example.com', OUTREACH_PUBLIC_URL='')
    def test_links_need_https_and_a_real_audit(self):
        self.assertEqual(audit.existing_url(self.lead), '')
        with patch('outreach.audit.website_audit', return_value=run_audit(BAD_HTML, {})):
            audit.get_audit(self.lead)
        url = audit.existing_url(self.lead)
        self.assertTrue(url.startswith('https://api.example.com/api/outreach/audit/'))
        with override_settings(OUTREACH_REVIEW_BASE_URL='http://localhost:8000'):
            self.assertEqual(audit.existing_url(self.lead), '')
        with override_settings(OUTREACH_PUBLIC_URL='https://check.zubairhussain.xyz'):
            self.assertTrue(audit.existing_url(self.lead).startswith('https://check.zubairhussain.xyz/'))


@override_settings(OUTREACH_REVIEW_BASE_URL='https://api.example.com', OUTREACH_BOOKING_URL='https://book.example.com/')
class ReportPageTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.lead = make_lead(self.user)
        with patch('outreach.audit.website_audit', return_value=run_audit(BAD_HTML, {})):
            audit.get_audit(self.lead)
        self.path = audit.existing_url(self.lead).replace('https://api.example.com', '')

    def test_page_shows_evidence_packages_and_booking(self):
        response = self.client.get(self.path)
        self.assertEqual(response.status_code, 200)
        text = response.content.decode()
        for expected in ('Evidence', 'Check it yourself', 'Fix &amp; Secure', 'New Site', 'Care Plan',
                         'Book a 15-min call', 'https://book.example.com/', 'strict-transport-security'):
            self.assertIn(expected, text)
        self.assertIn('noindex', text)
        self.assertIn('fonts.googleapis.com', response.headers['Content-Security-Policy'])
        self.assertNotIn('script-src', response.headers['Content-Security-Policy'])

    @override_settings(OUTREACH_PACKAGES='[{"name":"Starter","tag":"t","items":["one"],"price":"USD 300"}]')
    def test_your_own_packages_and_prices_replace_the_defaults(self):
        text = self.client.get(self.path).content.decode()
        self.assertIn('Starter', text)
        self.assertIn('USD 300', text)
        self.assertNotIn('Care Plan', text)

    def test_bad_token_and_unreadable_site(self):
        self.assertEqual(self.client.get('/api/outreach/audit/forged/').status_code, 400)
        LeadAudit.objects.all().delete()
        with patch('outreach.audit.website_audit', side_effect=requests.ConnectionError('x')):
            self.assertEqual(self.client.get(self.path).status_code, 503)


class EmailAndDraftTests(TestCase):
    def setUp(self):
        self.user = make_user()
        self.lead = make_lead(self.user)

    def test_email_carries_the_report_link(self):
        text, html = branding.compose('s', 'Hello there', audit_url='https://x.example.com/audit/abc/')
        self.assertIn('https://x.example.com/audit/abc/', text)
        self.assertIn('See your free check', html)
        plain, no_link = branding.compose('s', 'Hello there')
        self.assertNotIn('free check', plain)
        self.assertNotIn('See your free check', no_link)

    @patch('outreach.ai.research_prompt_parts', return_value=('', '"Hi Summit Dental team,"'))
    def test_writer_gets_only_verified_facts_and_no_link_rule(self, _research):
        seen = []

        def fake_generate(prompt, **_kwargs):
            seen.append(prompt)
            return {'subject': GOOD_SUBJECT, 'body': GOOD_BODY}
        with patch('outreach.audit.website_audit', return_value=run_audit(BAD_HTML, {})), \
                patch('outreach.ai.gemini.generate', side_effect=fake_generate):
            subject, _body = ai.write_draft_text(self.lead)
        self.assertEqual(subject, GOOD_SUBJECT)
        self.assertIn('Public check', seen[0])
        self.assertIn('the home page has no title tag', seen[0])
        self.assertIn('Do not write any link', seen[0])

    @patch('outreach.ai.research_prompt_parts', return_value=('', '"Hi,"'))
    def test_a_site_that_passes_everything_is_not_worth_an_email(self, _research):
        with patch('outreach.audit.website_audit', return_value=run_audit(GOOD_HTML, GOOD_HEADERS)):
            with self.assertRaises(ai.SkipLead):
                ai.write_draft_text(self.lead)

    @patch('outreach.ai.research_prompt_parts', return_value=('', '"Hi,"'))
    def test_unreachable_site_still_gets_an_honest_email_without_claims(self, _research):
        seen = []

        def fake_generate(prompt, **_kwargs):
            seen.append(prompt)
            return {'subject': GOOD_SUBJECT, 'body': GOOD_BODY}
        with patch('outreach.audit.website_audit', side_effect=requests.ConnectionError('x')), \
                patch('outreach.ai.gemini.generate', side_effect=fake_generate):
            ai.write_draft_text(self.lead)
        self.assertNotIn('Public check of', seen[0])
        self.assertIn('Never claim you already ran a check', seen[0])


@override_settings(OUTREACH_AUTOPILOT_OWNER='owner')
class TargetingAndNoWebsiteTests(TestCase):
    def setUp(self):
        self.user = make_user()

    def test_free_builder_sites_are_drafted_first(self):
        make_lead(self.user, link='https://shop.com', email='info@shop.com')
        make_lead(self.user, link='https://joe.wixsite.com/home', email='joe@joesplumbing.com')
        order = [lead.link for lead in autopilot.draftable_leads(self.user, 5)]
        self.assertEqual(order[0], 'https://joe.wixsite.com/home')

    def test_no_website_businesses_are_kept_as_phone_only_leads(self):
        generator = LeadGenerator()
        generator.no_website = [
            {'title': 'Tacos Laguna', 'phone': '+1 713-555-0100', 'address': 'Houston, TX 77002', 'rating': 4.6,
             'reviews': 172, 'link': ''},
            {'title': 'No Phone Cafe', 'phone': '', 'address': 'Houston, TX', 'rating': 4, 'reviews': 3, 'link': ''}]
        save = generator._save_no_website  # pylint: disable=protected-access
        self.assertEqual(save(self.user, 'food trucks'), 1)
        self.assertEqual(save(self.user, 'food trucks'), 0)  # no duplicates
        lead = Lead.objects.get(email__isnull=True)
        self.assertEqual((lead.phone, lead.country), ('+1 713-555-0100', 'US'))
        self.assertFalse(autopilot.draftable_leads(self.user, 5))  # never emailed

    def test_weekly_report_lists_them_for_manual_contact(self):
        Lead.objects.create(owner=self.user, email=None, phone='+1 713-555-0100', source='google-maps',
                            location='Houston, TX', problem_statement='Local business: Tacos Laguna. No website.')
        text = report.build(report.collect(timezone.now() + timedelta(minutes=1)))[1]
        self.assertIn('NO website of their own', text)
        self.assertIn('Tacos Laguna | +1 713-555-0100', text)
        self.assertIn('Free site checks built this week', text)
        self.assertEqual(OutreachMessage.objects.count(), 0)
