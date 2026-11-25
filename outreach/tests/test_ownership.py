from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from leads.models import Lead
from outreach import ai, autopilot, batches, ownership, playbook
from outreach.models import LeadResearch, OutreachMessage
from outreach.tests.test_batches import SAFE, SETTINGS

HOME = '<html><body><a href="/contact">Contact us</a></body></html>'
CONTACT = '<html><body>Call us or write to joe.plumbing@gmail.com</body></html>'


def pages(mapping):
    return lambda self, url: mapping.get(url, '')


class OwnershipProofTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='u', password='pw')
        self.lead = Lead.objects.create(owner=self.user, email='joe.plumbing@gmail.com', source='google-maps',
                                        category='Plumbers', location='Austin, TX', country='US',
                                        link='https://joesplumbing.com', is_verified=True,
                                        problem_statement='Local business: Joes Plumbing. x')
        LeadResearch.objects.create(lead=self.lead, status='ok', data={'name': 'Joes Plumbing', 'has_website': True})

    def test_address_published_on_their_contact_page_is_confirmed_and_remembered(self):
        site = {'https://joesplumbing.com': HOME, 'https://joesplumbing.com/contact': CONTACT}
        with patch('leads.services.LeadGenerator._fetch_page', pages(site)):
            self.assertEqual(ownership.confirm(self.lead), (True, 'https://joesplumbing.com/contact'))
        self.lead.refresh_from_db()
        self.assertTrue(ownership.is_confirmed(self.lead))
        self.assertEqual(ownership.confirm(self.lead), (True, 'already confirmed'))  # no second fetch needed

    def test_address_not_on_their_site_is_not_confirmed(self):
        with patch('leads.services.LeadGenerator._fetch_page', pages({'https://joesplumbing.com': HOME})):
            confirmed, why = ownership.confirm(self.lead)
        self.assertFalse(confirmed)
        self.assertIn('not published', why)
        self.assertFalse(ownership.is_confirmed(self.lead))

    def test_needs_a_real_site_and_a_google_maps_match(self):
        self.lead.link = 'https://facebook.com/joes'
        self.assertIn('no website of their own', ownership.confirm(self.lead)[1])
        self.lead.link = 'https://joesplumbing.com'
        LeadResearch.objects.filter(lead=self.lead).update(status='no_match')
        self.assertIn('Google Maps did not confirm', ownership.confirm(self.lead)[1])
        LeadResearch.objects.filter(lead=self.lead).update(status='ok', data={'website_changed': True})
        self.assertIn('differs', ownership.confirm(self.lead)[1])

    @patch('outreach.ai.research_prompt_parts', return_value=('', '"Hi,"'))
    def test_writer_skips_an_unproven_personal_address(self, _research):
        with patch('leads.services.LeadGenerator._fetch_page', pages({})):
            with self.assertRaises(ai.SkipLead) as caught:
                ai.write_draft_text(self.lead)
        self.assertIn('not confirmed', str(caught.exception))

    def test_confirmed_owners_are_drafted_before_everyone_else(self):
        Lead.objects.create(owner=self.user, email='info@bigshop.com', source='google-maps', country='US',
                            link='https://bigshop.com', is_verified=True)
        Lead.objects.create(owner=self.user, email='b@wixsite-owner.com', source='google-maps', country='US',
                            link='https://b.wixsite.com/x', is_verified=True)
        with override_settings(OUTREACH_AUTOPILOT_OWNER='u'):
            order = [lead.email for lead in autopilot.draftable_leads(self.user, 5)]
        self.assertEqual(order, ['joe.plumbing@gmail.com', 'b@wixsite-owner.com', 'info@bigshop.com'])

    def test_a_proven_owner_outranks_an_unproven_gmail_when_sending(self):
        other = Lead.objects.create(owner=self.user, email='x@gmail.com', source='google-maps', country='US',
                                    link='https://x.com', is_verified=True)
        self.lead.problem_statement += f"\n{ownership.MARKER} https://joesplumbing.com/contact (2026-11-25)."
        self.lead.save()
        proven = OutreachMessage.objects.create(owner=self.user, lead=self.lead, to_email=self.lead.email,
                                                subject='s', body='Hello there. Would you like it?')
        unproven = OutreachMessage.objects.create(owner=self.user, lead=other, to_email=other.email,
                                                  subject='s', body='Hello there. Would you like it?')
        self.assertGreater(playbook.priority(proven), playbook.priority(unproven))


@override_settings(**SETTINGS)
class AutoSendForOwnersTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(username='u', password='pw')

    def draft(self, email, confirmed):
        note = f'\n{ownership.MARKER} https://x.com/contact (2026-11-25).' if confirmed else ''
        lead = Lead.objects.create(owner=self.user, email=email, source='google-maps', category='Dentists',
                                   location='Austin', is_verified=True, problem_statement='Local business: X.' + note)
        return OutreachMessage.objects.create(
            owner=self.user, lead=lead, to_email=email, subject='Website help for your Austin practice',
            body=("Hi,\n\nYour clinic came up when I searched for dentists around Austin. I build websites "
                  "for local businesses and have a quick idea for yours.\n\nWould you like me to send it over?"
                  "\n\nRegards,\nZubair"))

    @patch('outreach.ai.gemini.generate', return_value=SAFE)
    def test_only_a_proven_owner_can_be_auto_sent(self, _gen):
        proven = self.draft('joe@gmail.com', True)
        unproven = self.draft('amy@gmail.com', False)
        self.assertEqual(batches.unsure_reason(proven), '')
        self.assertIn('not confirmed as the business owner', batches.unsure_reason(unproven))
