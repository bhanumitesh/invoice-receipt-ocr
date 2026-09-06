# A conftest.py at the repo root (rather than only inside tests/) makes
# pytest treat this directory as the rootdir and add it to sys.path, so
# `import utils` / `import batch_processor` work in test files without each
# one needing its own sys.path hack — this app's modules live flat at the
# repo root, not in an installable package.
#
# config.py requires these at import time (via config._require()) — dummy
# values are fine for tests, since nothing here makes a real Anthropic,
# Resend, or Supabase call.
import os

os.environ.setdefault("ANTHROPIC_API_KEY", "test-anthropic-key")
os.environ.setdefault("RESEND_API_KEY", "test-resend-key")
os.environ.setdefault("RESEND_SENDER", "test@example.com")
os.environ.setdefault("ADMIN_EMAIL", "admin@example.com")
os.environ.setdefault("SUPABASE_URL", "https://test.supabase.co")
os.environ.setdefault("SUPABASE_KEY", "test-supabase-key")
