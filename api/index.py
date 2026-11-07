"""Vercel serverless entry point: exposes the Django WSGI app."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'leadgen.settings')

from leadgen.wsgi import application  # noqa: E402


def app(environ, start_response):
    # TEMPORARY diagnostic: /anything?__path_debug=1 shows what path Vercel hands to Django.
    if '__path_debug' in environ.get('QUERY_STRING', ''):
        keep = ('X-VERCEL', 'X-MATCHED', 'X-NOW', 'X-FORWARDED-HOST', 'X-REAL', 'X-ORIGINAL')
        headers = {k[5:].replace('_', '-').lower(): v for k, v in environ.items()
                   if k.startswith('HTTP_') and k[5:].replace('_', '-').startswith(keep)}
        body = json.dumps({
            'PATH_INFO': environ.get('PATH_INFO'), 'SCRIPT_NAME': environ.get('SCRIPT_NAME'),
            'REQUEST_URI': environ.get('REQUEST_URI'), 'RAW_URI': environ.get('RAW_URI'),
            'headers': headers,
        }).encode()
        start_response('200 OK', [('Content-Type', 'application/json'), ('Content-Length', str(len(body)))])
        return [body]
    return application(environ, start_response)
