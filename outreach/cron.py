"""Scheduler entry point for hosts without background workers (Vercel).

A GitHub Actions workflow (or any cron service) calls this every 5 minutes with
`Authorization: Bearer $CRON_SECRET`. Each call does what Celery Beat did: send at most one due
email, batch quiet drafts, read the inbox / queue follow-ups, and send the Sunday report.
The slower autopilot (lead hunting + Gemini drafts) has its own endpoint so it never delays sending.
"""
import hmac
import logging

from django.conf import settings
from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from . import compliance
from .autopilot import run_autopilot
from .batches import process_batches
from .followups import schedule_followups
from .report import weekly_report
from .services import process_queue

logger = logging.getLogger(__name__)


def _authorised(request):
    secret = settings.CRON_SECRET
    if not secret:
        return None
    header = request.headers.get('Authorization', '')
    return hmac.compare_digest(header.encode(), f'Bearer {secret}'.encode())


def _run(request, jobs):
    ok = _authorised(request)
    if ok is None:
        return JsonResponse({'error': 'CRON_SECRET is not configured.'}, status=503)
    if not ok:
        return JsonResponse({'error': 'Unauthorized.'}, status=401)
    results = {}
    for name, job in jobs:
        try:
            results[name] = str(job())[:200]
        except Exception:  # one failing job must not stop the others
            logger.exception('Cron job %s failed', name)
            results[name] = 'error'
    return JsonResponse(results)


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def tick(request):
    return _run(request, (('queue', process_queue), ('batches', process_batches),
                          ('followups', schedule_followups), ('report', weekly_report)))


def pipeline_status():
    """Counts only (no addresses or names): what is waiting, what was sent, and why nothing moves."""
    from django.db.models import Count
    from django.utils import timezone

    from leads.models import Lead

    from . import services
    from .models import OutreachBatch, OutreachMessage, SendThrottle

    def tally(queryset, field):
        return {row[field]: row['n'] for row in queryset.values(field).annotate(n=Count('pk'))}

    now = timezone.now()
    throttle = SendThrottle.objects.filter(key='gmail').first()
    return {
        'now': now.isoformat(),
        'leads': Lead.objects.count(),
        'messages': tally(OutreachMessage.objects.all(), 'status'),
        'batches': tally(OutreachBatch.objects.all(), 'status'),
        'oldest_pending_batch_auto_send_at': next(
            (b.auto_send_at.isoformat() for b in OutreachBatch.objects.filter(status='pending').order_by('created_at')[:1]
             if b.auto_send_at), None),
        'sent_today': services.sent_counts(now)['today'],
        'next_send_at': throttle.next_send_at.isoformat() if throttle else None,
        'open_send_regions': [r or 'any' for r in ('',) + compliance.REGIONS if services.in_send_window(now, r)],
        'dry_run': settings.OUTREACH_DRY_RUN,
        'auto_send': settings.OUTREACH_AUTO_SEND_ENABLED,
    }


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def status(request):
    ok = _authorised(request)
    if ok is None:
        return JsonResponse({'error': 'CRON_SECRET is not configured.'}, status=503)
    if not ok:
        return JsonResponse({'error': 'Unauthorized.'}, status=401)
    return JsonResponse({'pipeline': pipeline_status()})


@csrf_exempt
@require_http_methods(['GET', 'POST'])
def autopilot(request):
    # ?hunt=1 re-runs today's lead hunt now (same secret required), handy after changing targets.
    force = request.GET.get('hunt') == '1'
    return _run(request, (('autopilot', lambda: run_autopilot(force_hunt=force)),))
