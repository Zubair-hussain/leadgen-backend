"""Autopilot: find leads every day and draft emails for them, with nobody pressing buttons.

Each run does ONE small step, so it fits inside a 60-second serverless call:
  1. once a day, hunt for new leads for the next target in OUTREACH_AUTOPILOT_TARGETS (rotating);
  2. otherwise, ask Gemini to draft a couple of emails for verified leads that have none yet,
     until OUTREACH_AUTOPILOT_DRAFTS_PER_DAY is reached.

Nothing here sends email. Drafts join the normal review batch, so you get one review email and,
if OUTREACH_AUTO_SEND_ENABLED is on, only drafts that pass every check are sent after the wait.
"""
import logging
import time as clock
import zoneinfo
from datetime import datetime, time, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.text import slugify

from leadgen import gemini
from leads.models import Lead
from leads.regions import region_of

from . import compliance
from .models import OutreachMessage, SendThrottle

logger = logging.getLogger(__name__)

HUNT_KEY = 'autopilot-hunt'
DRAFT_BUDGET_SECONDS = 120


def targets():
    """[(category, location), ...] from "plumbers|Austin, TX; dentists|Leeds, UK"."""
    result = []
    for chunk in str(settings.OUTREACH_AUTOPILOT_TARGETS).split(';'):
        category, _, location = chunk.partition('|')
        if category.strip() and location.strip():
            result.append((category.strip(), location.strip()))
    return result


def owner():
    """The account that owns autopilot leads. Created on first run when OUTREACH_AUTOPILOT_OWNER is an email,
    with the same username Google sign-in uses (leads/authentication.py), so signing in later finds it."""
    name = settings.OUTREACH_AUTOPILOT_OWNER.strip()
    if not name:
        return None
    User = get_user_model()
    user = User.objects.filter(Q(username__iexact=name) | Q(email__iexact=name)).order_by('id').first()
    if user is None and '@' in name:
        user, _ = User.objects.get_or_create(username=slugify(name)[:140], defaults={'email': name})
        if user.has_usable_password():
            user.set_unusable_password()  # Google sign-in only; no password login for this account
            user.save(update_fields=['password'])
    return user


def target_for(now):
    options = targets()
    return options[now.date().toordinal() % len(options)] if options else None


def local_zone():
    try:
        return zoneinfo.ZoneInfo(settings.OUTREACH_AUTOPILOT_TIMEZONE)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        return zoneinfo.ZoneInfo('UTC')


def day_start(now):
    """Today's start time (e.g. 07:00 Pakistan time) as an aware datetime."""
    zone = local_zone()
    local = timezone.localtime(now, zone)
    return datetime.combine(local.date(), time(settings.OUTREACH_AUTOPILOT_HOUR), tzinfo=zone)


def _claim_hunt(now):
    """True once per day, at or after the start hour: the first caller moves the marker to tomorrow."""
    with transaction.atomic():
        marker, _ = SendThrottle.objects.select_for_update().get_or_create(
            key=HUNT_KEY, defaults={'next_send_at': now - timedelta(days=1)})
        if marker.next_send_at > now:
            return False
        marker.next_send_at = day_start(now) + timedelta(days=1)
        marker.save(update_fields=['next_send_at'])
    return True


def hunt(user, now):
    from leads.services import LeadGenerator

    category, location = target_for(now)
    saved = LeadGenerator().generate_leads(
        category, settings.OUTREACH_AUTOPILOT_PLATFORMS, target_location=location, owner=user)
    logger.info('Autopilot found %s new leads for %s in %s', len(saved), category, location)
    return f'hunted {len(saved)} new leads: {category} in {location}'


def draftable_leads(user, limit):
    """Verified leads with an email, never written to, in a region we may legally email."""
    blocked = compliance.blocked_countries()
    leads = (Lead.objects.filter(owner=user, is_verified=True)
             .exclude(email__isnull=True).exclude(email='')
             .exclude(outreach_messages__isnull=False)
             .order_by('-created_at')[:limit * 10])
    result = []
    for lead in leads:
        country = compliance.lead_country(lead)
        if not region_of(country) or country in blocked:
            continue
        if region_of(country) in ('UK', 'EU', 'AU') and compliance.is_freemail(lead.email):
            continue
        result.append(lead)
        if len(result) >= limit:
            break
    return result


def release_busy_parked(user):
    """Leads shelved only because Gemini was busy go back into the pool."""
    parked = OutreachMessage.objects.filter(owner=user, status=OutreachMessage.Status.CANCELLED,
                                            subject='(autopilot skipped)')
    ids = [m.pk for m in parked.only('pk', 'skip_reason')
           if gemini.is_temporary(m.skip_reason.removeprefix('Autopilot: '))]
    if ids:
        OutreachMessage.objects.filter(pk__in=ids).delete()
    return len(ids)


def drafts_today(user, now):
    start = day_start(now)
    return (OutreachMessage.objects.filter(owner=user, parent__isnull=True, created_at__gte=start)
            .exclude(status=OutreachMessage.Status.CANCELLED).count())


def run_autopilot(now=None, force_hunt=False):
    """One step of the daily routine. Returns a short status string for logs."""
    if not settings.OUTREACH_AUTOPILOT_ENABLED:
        return 'disabled'
    now = now or timezone.now()
    user = owner()
    if user is None:
        return 'owner-not-found (set OUTREACH_AUTOPILOT_OWNER)'
    if not targets():
        return 'no-targets (set OUTREACH_AUTOPILOT_TARGETS)'
    if now < day_start(now):
        return f'waiting-until-{settings.OUTREACH_AUTOPILOT_HOUR:02d}:00-{settings.OUTREACH_AUTOPILOT_TIMEZONE}'
    if force_hunt or _claim_hunt(now):
        return hunt(user, now)

    release_busy_parked(user)
    room = settings.OUTREACH_AUTOPILOT_DRAFTS_PER_DAY - drafts_today(user, now)
    if room <= 0:
        return 'daily-drafts-done'
    leads = draftable_leads(user, min(room, settings.OUTREACH_AUTOPILOT_DRAFTS_PER_RUN))
    if not leads:
        return 'no-new-leads-to-draft'
    from .ai import write_drafts

    created, skipped = [], []
    started = clock.monotonic()
    for lead in leads:
        if clock.monotonic() - started > DRAFT_BUDGET_SECONDS:
            break  # the rest wait for the next run, so the call never hits the 300 s limit
        made, missed = write_drafts(user, [lead])
        created += made
        skipped += missed
    by_id = {lead.pk: lead for lead in leads}
    for item in skipped:
        if gemini.is_temporary(item['reason']):
            continue  # Gemini was busy: the lead stays in the pool and is tried again next run
        # Park the lead with a cancelled record so it is not retried (and billed) every run.
        lead = by_id.get(item['lead_id'])
        if lead is not None:
            OutreachMessage.objects.create(
                owner=user, lead=lead, to_email=lead.email, subject='(autopilot skipped)', body='-',
                status=OutreachMessage.Status.CANCELLED, skip_reason=f"Autopilot: {item['reason']}"[:255])
    reasons = '; '.join(sorted({item['reason'][:80] for item in skipped}))
    for item in skipped:
        logger.warning('Autopilot skipped %s: %s', item['email'], item['reason'])
    return f'drafted {len(created)}, skipped {len(skipped)}' + (f' ({reasons})' if reasons else '')
