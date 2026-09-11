"""Test-wide isolation.

The account store is a real SQLite file in the repo. Without this, running
the suite writes test accounts into the DEPLOYMENT's accounts.sqlite3 —
which also silently consumes the "first account on a fresh database becomes
admin" bootstrap, so a real install that had ever run its tests could end up
with no admin at all. Point every test run at a throwaway database instead.
"""
import os
import tempfile

# Must be set before app.api.auth is imported, since DB_PATH is read at
# import time.
_TMP_DB = os.path.join(tempfile.mkdtemp(prefix="ersa-test-accounts-"), "accounts.sqlite3")
os.environ.setdefault("ACCOUNTS_DB_PATH", _TMP_DB)
