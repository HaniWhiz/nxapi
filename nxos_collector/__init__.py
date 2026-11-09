"""NX-API 'show interface' collector, mounted inside an existing exporter at /probe."""

from .collector import (CONTENT_TYPE, Config, ProbeError, probe,  # noqa: F401
                        probe_from_query, wsgi_app)
