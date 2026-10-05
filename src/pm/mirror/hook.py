"""Refresh the Supabase mirror after the app writes to the database.

`@mirrored` wraps a function that takes a db_path keyword (the job and approval
functions). After it returns -- or raises -- the mirror is refreshed in the
background if, and only if, it is switched on (PM_SUPABASE_MIRROR=1). Off by
default, it does nothing at all.
"""

from __future__ import annotations

import functools

from pm.mirror.supabase_mirror import mirror_if_enabled
from pm.storage.db import DEFAULT_DB_PATH


def mirrored(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        finally:
            mirror_if_enabled(kwargs.get("db_path", DEFAULT_DB_PATH))

    return wrapper
