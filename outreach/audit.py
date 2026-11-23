"""A read-only, evidence-based check of a lead's public website, shown to them on a private report page.

What it does (and only this): one normal GET of the home page (following redirects, each hop checked against
private/internal addresses), one GET each of /robots.txt and /sitemap.xml, one plain-http request to see
whether it redirects to https, and one TLS handshake. That is what any visitor's browser or Google does.
No logins, no scanning, no probing for weaknesses.

Every finding carries the exact evidence it is based on (a header value, a tag, a measurement) and a way for
the owner to verify it. Nothing is invented: a check that cannot be measured is left out, not guessed.

A business with no website of its own (or only a social page) gets a presence check built from the Google
Maps facts already stored for it.
"""
import logging
import socket
import ssl
from datetime import datetime, timedelta
from urllib.parse import urljoin, urlparse

import requests
from django.conf import settings
from django.core import signing
from django.urls import reverse
from django.utils import timezone

from .models import LeadAudit

logger = logging.getLogger(__name__)

UA = 'LeadGen-SiteCheck/1.0 (read-only public page check)'
TIMEOUT = 8
MAX_BYTES = 400_000
MAX_HOPS = 4
SALT = 'outreach-audit'
TOKEN_DAYS = 90
CACHE_DAYS = 14

SOCIAL = ('facebook.com', 'instagram.com', 'yelp.com', 'tiktok.com', 'twitter.com', 'x.com', 'linkedin.com',
          'youtube.com', 'nextdoor.com')
FREE_BUILDERS = ('business.site', 'wixsite.com', 'godaddysites.com', 'weebly.com', 'square.site', 'carrd.co',
                 'linktr.ee', 'sites.google.com', 'webador.com', 'jimdosite.com', 'strikingly.com', 'webnode.com')
CATEGORIES = ('Security', 'SEO', 'Design and mobile', 'Speed')
POINTS = {'pass': 1.0, 'warn': 0.5, 'fail': 0.0}


# --- classification ------------------------------------------------------------------------

def _host(url):
    value = (url or '').strip().lower()
    return (urlparse(value if '//' in value else f'//{value}').hostname or '').removeprefix('www.')


def site_kind(url):
    """'none', 'social' (a profile, not a site), 'free_builder' (name.wixsite.com) or 'own'."""
    host = _host(url)
    if not host:
        return 'none'
    if any(host == d or host.endswith('.' + d) for d in SOCIAL):
        return 'social'
    if any(host == d or host.endswith('.' + d) for d in FREE_BUILDERS):
        return 'free_builder'
    return 'own'


host_of = _host


def _is_safe(url):
    from leads.services import LeadGenerator
    return LeadGenerator()._is_safe_public_url(url)  # pylint: disable=protected-access


# --- fetching ------------------------------------------------------------------------------

def fetch(url):
    """GET following redirects by hand so every hop is checked. Returns (response, body bytes, ms, final url)."""
    current = url
    for _ in range(MAX_HOPS + 1):
        if not _is_safe(current):
            raise ValueError('address is not a public website')
        response = requests.get(current, headers={'User-Agent': UA, 'Accept': 'text/html,*/*;q=0.8'},
                                timeout=TIMEOUT, allow_redirects=False, stream=True)
        if response.is_redirect:
            current = urljoin(current, response.headers.get('location', ''))
            response.close()
            continue
        body = b''
        for chunk in response.iter_content(32_768):
            body += chunk
            if len(body) >= MAX_BYTES:
                break
        response.close()
        # Time until the final server began answering: it excludes redirect hops and the download itself,
        # so a slow link on our side is not blamed on the site.
        return response, body, round(response.elapsed.total_seconds() * 1000), current
    raise ValueError('too many redirects')


def _status_of(url):
    """HTTP status of a plain GET, or 0 when unreachable (used for robots.txt and sitemap.xml)."""
    try:
        response, _, _, _ = fetch(url)
        return response.status_code
    except (requests.RequestException, ValueError, OSError):
        return 0


def tls_certificate(host):
    """(days until expiry, error text). Uses the normal trust store, like a browser."""
    try:
        context = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=TIMEOUT) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                cert = tls.getpeercert()
        expires = datetime.strptime(cert['notAfter'], '%b %d %H:%M:%S %Y %Z')
        return (expires - datetime.utcnow()).days, ''
    except ssl.SSLCertVerificationError as exc:
        return None, f'certificate not trusted ({exc.verify_message})'
    except (OSError, ssl.SSLError, KeyError, ValueError) as exc:
        return None, f'TLS handshake failed ({exc.__class__.__name__})'


# --- one finding ---------------------------------------------------------------------------

def finding(cid, category, title, status, evidence, why, fix, verify='', fact=''):
    """fact: one plain sentence the email writer may quote (only set for fail/warn)."""
    return {'id': cid, 'category': category, 'title': title, 'status': status, 'evidence': evidence,
            'why': why, 'fix': fix, 'verify': verify, 'fact': fact}


# --- website checks ------------------------------------------------------------------------

def security_checks(url, final_url, response):
    host = _host(final_url)
    headers = {k.lower(): v for k, v in response.headers.items()}
    checks = []
    https = final_url.startswith('https://')
    checks.append(finding(
        'https', 'Security', 'Site loads over HTTPS', 'pass' if https else 'fail',
        f'Final address: {final_url}',
        'Browsers label plain-http sites "Not secure", and Google ranks them lower.',
        'Install a free TLS certificate (Let\'s Encrypt) and redirect every page to https.',
        f'Open {url} and look at the padlock in the address bar.',
        '' if https else 'the site still loads over plain http, so browsers show "Not secure"'))
    if https:
        days, error = tls_certificate(host)
        if days is None:
            checks.append(finding(
                'tls', 'Security', 'Security certificate is valid', 'fail', f'TLS check on {host}: {error}',
                'Visitors see a full-page browser warning instead of your site.',
                'Reissue or renew the certificate.', f'openssl s_client -connect {host}:443',
                f'the security certificate has a problem ({error})'))
        else:
            status = 'pass' if days >= 21 else ('warn' if days >= 0 else 'fail')
            checks.append(finding(
                'tls', 'Security', 'Security certificate is valid and not about to expire', status,
                f'Certificate on {host} expires in {days} day(s).',
                'An expired certificate shows visitors a scary warning page and blocks the site.',
                'Turn on automatic renewal so it never lapses.', f'openssl s_client -connect {host}:443',
                '' if status == 'pass' else f'the security certificate expires in {days} days'))
        plain = f'http://{host}/'
        try:
            probe = requests.get(plain, headers={'User-Agent': UA}, timeout=TIMEOUT, allow_redirects=False)
            target = probe.headers.get('location', '')
            ok = probe.is_redirect and target.startswith('https://')
            checks.append(finding(
                'redirect', 'Security', 'Plain-http address redirects to https', 'pass' if ok else 'warn',
                f'GET {plain} answered {probe.status_code}' + (f' and redirected to {target}' if probe.is_redirect
                                                              else ' without redirecting to https'),
                'Anyone typing the short address would otherwise land on the unprotected version.',
                'Add a permanent 301 redirect from http to https.', f'curl -I {plain}',
                '' if ok else 'the plain-http address does not redirect to the secure version'))
        except (requests.RequestException, OSError):
            pass
    wanted = (
        ('strict-transport-security', 'HSTS header (tells browsers to always use https)',
         'Without it a visitor on public Wi-Fi can be silently downgraded to http.',
         'Send  Strict-Transport-Security: max-age=31536000; includeSubDomains',
         'the site does not send an HSTS header, so browsers are not told to always use the secure version'),
        ('x-content-type-options', 'X-Content-Type-Options header',
         'Stops browsers guessing file types, a trick used to run uploaded files as scripts.',
         'Send  X-Content-Type-Options: nosniff',
         'the site does not send the X-Content-Type-Options header'),
        ('referrer-policy', 'Referrer-Policy header',
         'Controls how much of your visitors\' browsing trail is leaked to other sites.',
         'Send  Referrer-Policy: strict-origin-when-cross-origin',
         'the site does not send a Referrer-Policy header'),
    )
    for name, title, why, fix, plain in wanted:
        if name == 'strict-transport-security' and not https:
            continue
        value = headers.get(name)
        checks.append(finding(
            name, 'Security', title, 'pass' if value else 'fail',
            f'{name}: {value}' if value else f'Response headers of {final_url} do not include "{name}".',
            why, fix, f'curl -I {final_url}', '' if value else plain))
    framing = headers.get('x-frame-options') or ('frame-ancestors' in headers.get('content-security-policy', ''))
    checks.append(finding(
        'framing', 'Security', 'Protection against being embedded in a hostile page (clickjacking)',
        'pass' if framing else 'fail',
        (f'x-frame-options: {headers["x-frame-options"]}' if headers.get('x-frame-options') else
         'content-security-policy has frame-ancestors') if framing else
        'Neither X-Frame-Options nor a CSP frame-ancestors rule is sent.',
        'Another site can load yours invisibly inside theirs and trick visitors into clicking.',
        'Send  X-Frame-Options: SAMEORIGIN  or a CSP with frame-ancestors.', f'curl -I {final_url}',
        '' if framing else 'the site has no protection against being embedded in a hostile page'))
    leaks = [f'{h}: {headers[h]}' for h in ('server', 'x-powered-by') if any(c.isdigit() for c in headers.get(h, ''))]
    if leaks:
        checks.append(finding(
            'version-leak', 'Security', 'Server does not announce its software version', 'warn',
            'Headers reveal versions: ' + '; '.join(leaks),
            'Attackers search for known flaws in the exact version you announce.',
            'Hide the version numbers in the server configuration.', f'curl -I {final_url}',
            'the server announces its software version, which helps attackers'))
    return checks


def seo_and_design_checks(url, final_url, soup, body_text_len):
    host = _host(final_url)
    checks = []
    title = (soup.title.string or '').strip() if soup.title and soup.title.string else ''
    status = 'pass' if 15 <= len(title) <= 65 else ('warn' if title else 'fail')
    checks.append(finding(
        'title', 'SEO', 'Page title is present and the right length', status,
        f'<title> = "{title[:90]}" ({len(title)} characters)' if title else 'The page has no <title> tag.',
        'The title is the clickable headline in Google results. Missing or cut-off titles lose clicks.',
        'Write a title of 30 to 60 characters with your service and town.', 'View page source and search for <title>.',
        '' if status == 'pass' else ('the home page has no title tag' if not title
                                     else f'the page title is {len(title)} characters, outside the 15 to 65 that fits')))
    meta = soup.find('meta', attrs={'name': lambda v: v and v.lower() == 'description'})
    desc = (meta.get('content') or '').strip() if meta else ''
    status = 'pass' if 70 <= len(desc) <= 170 else ('warn' if desc else 'fail')
    checks.append(finding(
        'description', 'SEO', 'Meta description is present', status,
        f'<meta name="description"> = "{desc[:110]}" ({len(desc)} characters)' if desc
        else 'No <meta name="description"> tag found.',
        'Google shows it under your title. Without it, Google picks random page text.',
        'Write a 120 to 160 character summary that makes people want to call.',
        'View page source and search for name="description".',
        '' if status == 'pass' else 'the home page has no meta description for Google to show'))
    h1s = soup.find_all('h1')
    checks.append(finding(
        'h1', 'SEO', 'Exactly one main heading (H1)', 'pass' if len(h1s) == 1 else 'warn',
        f'Found {len(h1s)} <h1> tag(s)' + (f': "{h1s[0].get_text(" ", strip=True)[:80]}"' if h1s else '.'),
        'The H1 tells Google and visitors what the page is about.',
        'Use one H1 that names the service and the town.', 'Inspect the page headline in your browser.',
        '' if len(h1s) == 1 else f'the home page has {len(h1s)} main headings instead of one'))
    robots_meta = soup.find('meta', attrs={'name': lambda v: v and v.lower() == 'robots'})
    robots_value = (robots_meta.get('content') or '').lower() if robots_meta else ''
    if 'noindex' in robots_value:
        checks.append(finding(
            'noindex', 'SEO', 'Page is allowed to appear in Google', 'fail',
            f'<meta name="robots" content="{robots_value}"> tells search engines to hide this page.',
            'Google is being told not to list the page, so customers cannot find you.',
            'Remove the noindex tag.', 'View page source and search for name="robots".',
            'a tag on the home page tells Google not to list it'))
    robots_status = _status_of(urljoin(final_url, '/robots.txt'))
    sitemap_status = _status_of(urljoin(final_url, '/sitemap.xml'))
    checks.append(finding(
        'sitemap', 'SEO', 'Sitemap helps Google find every page', 'pass' if sitemap_status == 200 else 'warn',
        f'GET /sitemap.xml answered {sitemap_status or "no response"}; GET /robots.txt answered '
        f'{robots_status or "no response"}.',
        'A sitemap speeds up indexing of new pages and changes.', 'Publish /sitemap.xml and reference it in robots.txt.',
        f'Open https://{host}/sitemap.xml',
        '' if sitemap_status == 200 else 'there is no sitemap.xml for Google to read'))
    has_schema = bool(soup.find('script', attrs={'type': 'application/ld+json'}))
    checks.append(finding(
        'schema', 'SEO', 'Business details marked up for Google (structured data)', 'pass' if has_schema else 'warn',
        'Found a JSON-LD block.' if has_schema else 'No application/ld+json structured data on the home page.',
        'LocalBusiness markup can unlock richer results with hours, rating and phone.',
        'Add LocalBusiness JSON-LD with name, address, phone and hours.',
        'View page source and search for ld+json.',
        '' if has_schema else 'the home page has no structured business data for Google'))
    viewport = soup.find('meta', attrs={'name': lambda v: v and v.lower() == 'viewport'})
    checks.append(finding(
        'viewport', 'Design and mobile', 'Page is built for phones (viewport tag)', 'pass' if viewport else 'fail',
        f'<meta name="viewport" content="{viewport.get("content", "")}">' if viewport
        else 'No <meta name="viewport"> tag: phones show a zoomed-out desktop page.',
        'Most local searches happen on a phone. Google ranks mobile-friendly pages first.',
        'Add the viewport tag and a responsive layout.', 'Open the site on a phone, or use browser DevTools > mobile view.',
        '' if viewport else 'the site is not set up for phones, where most local searches happen'))
    images = soup.find_all('img')
    missing = [i for i in images if not (i.get('alt') or '').strip()]
    if images:
        ratio = len(missing) / len(images)
        status = 'pass' if ratio == 0 else ('warn' if ratio < 0.5 else 'fail')
        checks.append(finding(
            'alt', 'Design and mobile', 'Images have descriptive alt text', status,
            f'{len(missing)} of {len(images)} images on the home page have no alt text.'
            + (f' First one: {missing[0].get("src", "")[:70]}' if missing else ''),
            'Alt text helps blind visitors and lets Google understand and rank your photos.',
            'Describe each image in a few words.', 'View page source and search for <img.',
            '' if status == 'pass' else f'{len(missing)} of {len(images)} images have no alt text'))
    lang = soup.html.get('lang') if soup.html else ''
    checks.append(finding(
        'lang', 'Design and mobile', 'Page language is declared', 'pass' if lang else 'warn',
        f'<html lang="{lang}">' if lang else 'The <html> tag has no lang attribute.',
        'Screen readers and translation tools rely on it.', 'Add lang="en" (or your language) to <html>.',
        'View page source, first lines.', '' if lang else 'the page does not declare its language'))
    mixed = [t.get('src') or t.get('href') for t in soup.find_all(['img', 'script', 'link', 'iframe'])
             if str(t.get('src') or t.get('href') or '').startswith('http://')]
    if final_url.startswith('https://') and mixed:
        checks.append(finding(
            'mixed', 'Security', 'No insecure (http) files loaded on the secure page', 'fail',
            f'{len(mixed)} file(s) are requested over plain http, e.g. {mixed[0][:80]}',
            'Browsers block or flag them, which breaks images and shows a "not fully secure" warning.',
            'Change those links to https.', 'Open DevTools > Console and look for "mixed content".',
            f'{len(mixed)} files on the secure page still load over plain http'))
    return checks


def speed_checks(response, body, elapsed_ms, final_url):
    checks = []
    status = 'pass' if elapsed_ms < 1000 else ('warn' if elapsed_ms < 2500 else 'fail')
    checks.append(finding(
        'ttfb', 'Speed', 'Server starts answering quickly', status,
        f'The server began answering after {elapsed_ms} ms (measured once from our server; your own speed may differ).',
        'Slow pages lose visitors, and speed is a Google ranking factor.',
        'Use faster hosting or a CDN, and cache the page.',
        f'curl -o /dev/null -s -w "%{{time_starttransfer}}" {final_url}'))
    size_kb = round(len(body) / 1024)
    status = 'pass' if size_kb <= 250 else ('warn' if size_kb <= 400 else 'fail')
    checks.append(finding(
        'weight', 'Speed', 'Home page HTML is lightweight', status,
        f'Home page HTML is {size_kb} KB' + (' (we stop reading at 400 KB).' if len(body) >= MAX_BYTES else '.'),
        'Heavy pages are slow on mobile data.', 'Remove unused code and plugins.', 'Check the page size in DevTools > Network.'))
    encoding = response.headers.get('content-encoding', '')
    if len(body) > 20_000:
        checks.append(finding(
            'compression', 'Speed', 'Text is compressed (gzip or brotli)', 'pass' if encoding else 'warn',
            f'content-encoding: {encoding}' if encoding else 'The response is sent uncompressed.',
            'Compression makes pages download several times faster.', 'Turn on gzip/brotli on the server.',
            f'curl -sI -H "Accept-Encoding: br,gzip" {final_url}'))
    return checks


# --- assembling the report -----------------------------------------------------------------

def _score(checks):
    return round(100 * sum(POINTS[c['status']] for c in checks) / len(checks)) if checks else 0


def summarise(checks, kind, url, business, extra=None):
    categories = {}
    for name in CATEGORIES:
        group = [c for c in checks if c['category'] == name]
        if group:
            categories[name] = {'score': _score(group), 'checks': group}
    counts = {s: sum(1 for c in checks if c['status'] == s) for s in ('fail', 'warn', 'pass')}
    worst = sorted((c for c in checks if c['fact']), key=lambda c: (c['status'] != 'fail', c['category']))
    data = {'kind': kind, 'url': url, 'business': business, 'checked_at': timezone.now().isoformat(),
            'score': _score(checks), 'counts': counts, 'categories': categories, 'reachable': True, 'error': '',
            'facts': [c['fact'] for c in worst[:3]]}
    data.update(extra or {})
    return data


def website_audit(url, business=''):
    """Run the live checks. Raises requests.RequestException/ValueError/OSError when the site cannot be read."""
    from bs4 import BeautifulSoup

    response, body, elapsed_ms, final_url = fetch(url if '//' in url else f'https://{url}')
    if response.status_code >= 400:
        raise ValueError(f'the site answered HTTP {response.status_code}')
    soup = BeautifulSoup(body.decode(response.encoding or 'utf-8', 'ignore'), 'html.parser')
    checks = (security_checks(url, final_url, response) + seo_and_design_checks(url, final_url, soup, len(body))
              + speed_checks(response, body, elapsed_ms, final_url))
    kind = site_kind(final_url)
    if kind == 'free_builder':
        checks.append(finding(
            'own-domain', 'SEO', 'Business has its own web address', 'fail',
            f'The site lives on a free subdomain: {_host(final_url)}',
            'A free subdomain looks temporary, is hard to remember, and builds Google rank for the builder, not for you.',
            'Register your own domain (about the price of a lunch per year) and point it at the site.',
            f'Look at the address bar: {_host(final_url)}',
            f'the site lives on a free subdomain ({_host(final_url)}) instead of the business\'s own domain'))
    return summarise(checks, 'website', final_url, business)


def presence_audit(lead, research=None):
    """No website of its own: check the online presence from the stored Google Maps facts."""
    data = (research.data if research else {}) or {}
    name = data.get('name') or ''
    kind = site_kind(lead.link)
    checks = [finding(
        'own-site', 'SEO', 'Business has a website of its own', 'fail',
        (f'Google Maps listing for "{name or "this business"}" shows '
         + ('no website.' if kind == 'none' else f'only a {kind.replace("_", " ")} page: {_host(lead.link)}')),
        'Customers who find you on Google or Maps cannot click through to see prices, photos or book. '
        'Competitors with a site get those clicks.',
        'A fast one-page site with your services, reviews, hours, map and a call button.',
        'Search the business name on Google Maps and look for the Website button.',
        'the Google listing has no website button, so interested customers have nowhere to click')]
    if data.get('rating') and data.get('reviews'):
        checks.append(finding(
            'reputation', 'SEO', 'Strong reviews are already earned', 'pass',
            f'Google shows {data["rating"]} stars from {data["reviews"]} reviews.',
            'Great reviews are the hardest part. A site turns them into bookings.', 'Show them on the site.',
            'Open the Google Maps listing.'))
    return summarise(checks, 'presence', lead.link or '', name)


# --- storage and links ---------------------------------------------------------------------

def get_audit(lead, force=False):
    """The stored audit, built on first use. Returns the data dict, or None when nothing could be measured."""
    from .models import LeadResearch

    stored = LeadAudit.objects.filter(lead=lead).first()
    if stored and not force and stored.checked_at > timezone.now() - timedelta(days=CACHE_DAYS):
        return stored.data or None
    research = LeadResearch.objects.filter(lead=lead, status='ok').first()
    name = ((research.data or {}).get('name') if research else '') or ''
    kind = site_kind(lead.link)
    try:
        if kind in ('own', 'free_builder'):
            data = website_audit(lead.link, name)
        else:
            data = presence_audit(lead, research)
    except (requests.RequestException, ValueError, OSError) as exc:
        logger.warning('Audit of %s could not read the site: %s', _host(lead.link), exc)
        return None  # never claim a problem we could not measure
    LeadAudit.objects.update_or_create(lead=lead, defaults={
        'kind': data['kind'], 'data': data, 'checked_at': timezone.now()})
    return data


def has_findings(data):
    return bool(data) and (data['counts']['fail'] + data['counts']['warn']) > 0


def existing_url(lead):
    """Report link for a lead that already has an audit with something to show; never fetches anything."""
    stored = LeadAudit.objects.filter(lead=lead).first()
    return url_for(lead) if stored and has_findings(stored.data) else ''


def make_token(lead):
    return signing.dumps({'lead': lead.pk}, salt=SALT)


def read_token(token):
    return signing.loads(token, salt=SALT, max_age=TOKEN_DAYS * 86400)['lead']


def url_for(lead):
    """Public https link to the report, or '' (no public https address configured)."""
    base = (settings.OUTREACH_PUBLIC_URL or settings.OUTREACH_REVIEW_BASE_URL).rstrip('/')
    if not base.startswith('https://'):
        return ''
    return f"{base}{reverse('outreach-audit', args=[make_token(lead)])}"


def prompt_facts(lead):
    """Lines for the email writer: only verified findings, plus whether a report link will be attached."""
    data = get_audit(lead)
    if not has_findings(data) or not data['facts']:
        return '', False
    when = data['checked_at'][:10]
    lines = [f"- Public check of {data['url'] or 'their listing'} on {when} found (exact, verified):"]
    lines += [f'    * {fact}' for fact in data['facts']]
    return '\n'.join(lines), True


def packages():
    """The offer shown on the report. Prices only appear if you set them in OUTREACH_PACKAGES (JSON)."""
    import json

    try:
        custom = json.loads(settings.OUTREACH_PACKAGES) if settings.OUTREACH_PACKAGES.strip() else None
    except ValueError:
        custom = None
    return custom or [
        {'name': 'Fix & Secure', 'tag': 'For an existing site',
         'items': ['Every red and amber item in this report fixed', 'HTTPS, security headers and certificate renewal',
                   'Title, description and structured data rewritten for Google', 'Re-check report to prove the fixes'],
         'price': ''},
        {'name': 'New Site', 'tag': 'For no site, or a free page',
         'items': ['Fast, mobile-first site on your own domain', 'Your reviews, hours, map and a one-tap call button',
                   'Local SEO basics and Google Business link-up', 'Free security check included'],
         'price': '', 'featured': True},
        {'name': 'Care Plan', 'tag': 'Keep it safe and found',
         'items': ['Monthly security and speed check with a short report', 'Updates, backups and uptime alerts',
                   'Ranking and review tracking', 'One small change a month included'],
         'price': ''},
    ]
