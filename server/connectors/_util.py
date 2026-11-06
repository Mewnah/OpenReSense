"""Helpers shared by connector modules (the leading underscore keeps it out of connector auto-discovery)."""
from datetime import datetime

import httpx

from .base import Query


def check_times(q: Query) -> None:
    """Reject unparsable start/end up front: upstream APIs answer with 500s or cryptic errors."""
    for name in ("start", "end"):
        value = getattr(q, name)
        if value:
            try:
                datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"{name} must be an ISO 8601 date or time such as 2020-01-31 or "
                                 f"2020-01-31T12:00:00Z, got {value!r}") from None


def json_of(r: httpx.Response):
    """Response body as JSON; an HTML error page becomes a 502 (httpx error), not a ValueError (400)."""
    try:
        return r.json()
    except ValueError:
        raise httpx.DecodingError(f"{r.url.host} answered HTTP {r.status_code} with a non-JSON body", request=r.request) from None
