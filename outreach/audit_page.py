"""The private report page a lead opens from the email: /api/outreach/audit/<signed token>/."""
from django.conf import settings
from django.core import signing
from django.http import HttpResponse
from django.shortcuts import render
from django.utils import timezone
from django.views.decorators.http import require_GET

from leads.models import Lead

from . import audit, unsubscribe
from .models import OutreachMessage

# The report is read-only HTML: no scripts at all, but it may load the brand fonts from Google Fonts.
PAGE_CSP = ("default-src 'none'; style-src 'unsafe-inline' https://fonts.googleapis.com; "
            "font-src https://fonts.gstatic.com; img-src data:; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'")


def _gone(message, status):
    return HttpResponse(f'<!doctype html><meta name="robots" content="noindex"><title>Report</title>'
                        f'<body style="font-family:sans-serif;background:#0a0a0a;color:#F5F4F0;padding:48px 16px">'
                        f'<p style="max-width:460px;margin:0 auto">{message}</p>', status=status)


@require_GET
def audit_report(request, token):
    try:
        lead = Lead.objects.filter(pk=audit.read_token(token)).first()
    except (signing.BadSignature, KeyError, TypeError):
        return _gone('This link is not valid or has expired. Reply to my email and I will send a fresh one.', 400)
    if lead is None:
        return _gone('This report is no longer available.', 404)
    data = audit.get_audit(lead)
    if not data:
        return _gone('I could not read the website just now. Reply to my email and I will run the check again.', 503)

    categories = [{'name': name, **data['categories'][name]} for name in audit.CATEGORIES if name in data['categories']]
    score = data['score']
    last = OutreachMessage.objects.filter(lead=lead, status=OutreachMessage.Status.SENT).order_by('-sent_at').first()
    checked = data['checked_at'][:16].replace('T', ' ') + ' UTC'
    response = render(request, 'outreach/audit.html', {
        'audit': data, 'business': data.get('business') or '', 'host': audit.host_of(data.get('url') or lead.link),
        'category_list': categories, 'package_list': audit.packages(), 'checked': checked,
        'deg': round(score * 3.6), 'ring_color': '#4ade80' if score >= 80 else ('#fbbf24' if score >= 55 else '#f87171'),
        'booking_url': settings.OUTREACH_BOOKING_URL, 'portfolio_url': settings.OUTREACH_PORTFOLIO_URL,
        'title': settings.OUTREACH_SENDER_TITLE, 'sender_address': settings.OUTREACH_SENDER_ADDRESS,
        'unsubscribe_url': unsubscribe.url_for(last) if last else '',
        'now': timezone.now(),
    })
    response['Content-Security-Policy'] = PAGE_CSP
    response['Referrer-Policy'] = 'no-referrer'
    response['X-Robots-Tag'] = 'noindex, nofollow'
    return response
