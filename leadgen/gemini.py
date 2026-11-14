"""Tiny Gemini REST client (no SDK dependency, easy to mock)."""
import json
import logging
import re

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

URL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'


class GeminiError(Exception):
    """Gemini is unavailable, misconfigured, or returned something unusable."""


def is_configured():
    return bool(settings.GEMINI_API_KEY)


BUSY_CODES = {429, 500, 502, 503, 504}


def is_temporary(error):
    """True when Gemini itself failed (busy, timeout, model missing), not the content: worth retrying later."""
    text = str(error)
    return text.startswith(('Gemini returned', 'Gemini request failed', 'GEMINI_API_KEY')) or (
        'request failed' in text or any(f'returned {code}' in text for code in BUSY_CODES))


def models():
    fallbacks = [m.strip() for m in str(getattr(settings, 'GEMINI_FALLBACK_MODELS', '')).split(',') if m.strip()]
    return [settings.GEMINI_MODEL] + [m for m in fallbacks if m != settings.GEMINI_MODEL]


def _post(model, payload, timeout):
    try:
        response = requests.post(URL.format(model=model), headers={'x-goog-api-key': settings.GEMINI_API_KEY},
                                 json=payload, timeout=timeout)
    except requests.RequestException as exc:
        raise GeminiError(f'Gemini request failed: {exc}') from exc
    if response.status_code != 200:
        raise GeminiError(f'Gemini returned {response.status_code}: {response.text[:200]}')
    return response


def generate(prompt, json_output=False, temperature=0.4, timeout=None):
    """Return Gemini's text answer. With json_output=True, return parsed JSON.

    When the main model is overloaded (503) or times out, the fallback models are tried in turn.
    """
    if not settings.GEMINI_API_KEY:
        raise GeminiError('GEMINI_API_KEY is not set.')
    config = {'temperature': temperature, 'thinkingConfig': {'thinkingBudget': 0}}
    if json_output:
        config['responseMimeType'] = 'application/json'
    payload = {'contents': [{'parts': [{'text': prompt}]}], 'generationConfig': config}
    timeout = timeout or getattr(settings, 'GEMINI_TIMEOUT_SECONDS', 30)
    response, first_error = None, None
    for model in models():
        try:
            response = _post(model, payload, timeout)
            break
        except GeminiError as exc:
            first_error = first_error or exc
            logger.warning('Gemini model %s failed (%s); trying the next one', model, str(exc)[:80])
    if response is None:
        raise first_error
    try:
        text = response.json()['candidates'][0]['content']['parts'][0]['text'].strip()
    except (KeyError, IndexError, ValueError) as exc:
        raise GeminiError('Gemini returned no usable text.') from exc
    if not json_output:
        return text
    try:
        return json.loads(re.sub(r'^```(?:json)?|```$', '', text, flags=re.MULTILINE).strip())
    except ValueError as exc:
        raise GeminiError('Gemini did not return valid JSON.') from exc
