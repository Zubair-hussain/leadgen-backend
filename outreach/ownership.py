"""Proof that a personal-mailbox address (gmail.com and friends) really belongs to the business.

Small-business owners often publish their own Gmail on their site, and those reach the person who decides.
But a stray Gmail on a page can also belong to a web designer, a previous owner or a template. So a
freemail address is only treated as the owner's when ALL of this holds:

1. the business's own website (or free-builder page) publishes that exact address on its home page or a
   contact/about page (read now, not remembered from an earlier crawl);
2. Google Maps lists that same website for the business (research matched it and the site did not change);
3. the address is not an obvious placeholder or role-less template.

The proof is written to the lead so auto-send, ordering and the report can rely on it.
"""
import logging
from datetime import date

from . import audit, compliance, research

logger = logging.getLogger(__name__)
MARKER = 'Email confirmed on'
MAX_PAGES = 4


def is_freemail(lead):
    return bool(lead and lead.email and compliance.is_freemail(lead.email))


def is_confirmed(lead):
    return bool(lead and MARKER in (lead.problem_statement or ''))


def _published_on(lead):
    """The page of the lead's own site that shows the address, or ''."""
    from bs4 import BeautifulSoup
    from urllib.parse import urljoin, urlparse

    from leads.services import LeadGenerator

    generator = LeadGenerator()
    wanted = lead.email.strip().lower()
    html = generator._fetch_page(lead.link)  # pylint: disable=protected-access
    if wanted in generator._extract_emails_from_text(html):  # pylint: disable=protected-access
        return lead.link
    host = urlparse(lead.link).netloc
    pages = []
    if html:
        for link in BeautifulSoup(html, 'html.parser').find_all('a', href=True):
            text = f"{link['href']} {link.get_text(' ', strip=True)}".lower()
            target = urljoin(lead.link, link['href'])
            if (any(h in text for h in generator.CONTACT_LINK_HINTS) and urlparse(target).netloc == host
                    and target not in pages):
                pages.append(target)
    for page in pages[:MAX_PAGES - 1]:
        if wanted in generator._extract_emails_from_text(generator._fetch_page(page)):  # pylint: disable=protected-access
            return page
    return ''


def confirm(lead):
    """(True, where) when the proof holds, else (False, why not). Records the proof on the lead."""
    if is_confirmed(lead):
        return True, 'already confirmed'
    if audit.site_kind(lead.link) not in ('own', 'free_builder'):
        return False, 'there is no website of their own to check the address against'
    found = research.get_research(lead)
    if found is None or found.status != research.Status.OK:
        return False, 'Google Maps did not confirm this business'
    if found.data.get('website_changed'):
        return False, 'the website on Google Maps differs from the one the address came from'
    try:
        page = _published_on(lead)
    except Exception:  # pylint: disable=broad-except
        logger.exception('Ownership check failed for lead %s', lead.pk)
        return False, 'the website could not be read'
    if not page:
        return False, 'the address is not published on their own website'
    lead.problem_statement = f"{lead.problem_statement}\n{MARKER} {page} ({date.today().isoformat()})."
    lead.save(update_fields=['problem_statement'])
    return True, page
