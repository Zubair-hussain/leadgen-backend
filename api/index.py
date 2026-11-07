"""Optional Vercel entry point: exposes the Django WSGI app (Vercel also detects leadgen/wsgi.py by itself)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'leadgen.settings')

from leadgen.wsgi import application  # noqa: E402

app = application
