"""Cisco Nexus interface-counter collector (NX-API 'show interface').

This module is meant to be mounted INSIDE an existing exporter, on its existing
HTTP server, as a multi-target endpoint:

    GET /probe?target=<switch-ip>

It has no third-party dependencies (Python standard library only), opens no
listening socket, starts no thread and keeps no state between scrapes.

Per scrape it sends exactly ONE NX-API JSON-RPC request:

    POST https://<target>:<port>/ins   method=cli   cmd="show interface"

and renders the result in the Prometheus text exposition format.

Configuration (environment variables / secret store):

    NXOS_PROBE_ENABLED     "false" disables the module (/probe -> 404). Default: true
    NXOS_USERNAME          NX-API user (custom role: 'show interface' only)
    NXOS_PASSWORD_FILE     file holding the password (secret-store mount); re-read
                           on every scrape so rotation needs no restart
    NXOS_PASSWORD          password, if no password file is used
    NXOS_ALLOWED_CIDRS     comma-separated CIDRs that targets must be inside.
                           Empty / unset = every target is refused.
    NXOS_PORT              NX-API HTTPS port. Default: 443
    NXOS_CA_FILE           CA bundle to verify switch certificates. Unset = no
                           verification (self-signed switch certificates)
    NXOS_TIMEOUT_SECONDS   per-request timeout. Default: 15 (keep it below the
                           scrape timeout, 20s)
    NXOS_EXCLUDE_REGEX     interfaces whose name matches are not exported
"""

import base64
import ipaddress
import json
import logging
import os
import re
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import parse_qs

log = logging.getLogger(__name__)

CONTENT_TYPE = "text/plain; version=0.0.4; charset=utf-8"
MAX_RESPONSE_BYTES = 64 * 1024 * 1024  # 'show interface' on a full chassis is a few MB

# show interface JSON key -> (metric suffix, help text)
COUNTERS: Sequence[Tuple[str, str, str]] = (
    ("eth_inucast",    "in_unicast_packets_total",   "Unicast packets received."),
    ("eth_outucast",   "out_unicast_packets_total",  "Unicast packets sent."),
    ("eth_inmcast",    "in_multicast_packets_total", "Multicast packets received."),
    ("eth_outmcast",   "out_multicast_packets_total", "Multicast packets sent."),
    ("eth_inbcast",    "in_broadcast_packets_total", "Broadcast packets received."),
    ("eth_outbcast",   "out_broadcast_packets_total", "Broadcast packets sent."),
    ("eth_inbytes",    "in_bytes_total",             "Bytes received."),
    ("eth_outbytes",   "out_bytes_total",            "Bytes sent."),
    ("eth_inerr",      "in_errors_total",            "Input errors."),
    ("eth_outerr",     "out_errors_total",           "Output errors."),
    ("eth_crc",        "crc_errors_total",           "CRC errors."),
    ("eth_indiscard",  "in_discards_total",          "Input discards."),
    ("eth_outdiscard", "out_discards_total",         "Output discards."),
)


class ProbeError(Exception):
    """The switch could not be polled (reported as nxos_up 0)."""


# --------------------------------------------------------------------------- config

def _env_bool(value: Optional[str], default: bool) -> bool:
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    username: str = ""
    password: str = ""
    password_file: str = ""
    allowed_networks: Tuple[Any, ...] = ()
    port: int = 443
    ca_file: str = ""
    timeout: float = 15.0
    exclude: Optional["re.Pattern[str]"] = None

    @classmethod
    def from_env(cls, env: Optional[Dict[str, str]] = None) -> "Config":
        env = os.environ if env is None else env
        nets = []
        for cidr in (env.get("NXOS_ALLOWED_CIDRS") or "").split(","):
            if cidr.strip():
                nets.append(ipaddress.ip_network(cidr.strip(), strict=False))
        exclude = env.get("NXOS_EXCLUDE_REGEX") or ""
        return cls(
            enabled=_env_bool(env.get("NXOS_PROBE_ENABLED"), True),
            username=env.get("NXOS_USERNAME", ""),
            password=env.get("NXOS_PASSWORD", ""),
            password_file=env.get("NXOS_PASSWORD_FILE", ""),
            allowed_networks=tuple(nets),
            port=int(env.get("NXOS_PORT") or 443),
            ca_file=env.get("NXOS_CA_FILE", ""),
            timeout=float(env.get("NXOS_TIMEOUT_SECONDS") or 15),
            exclude=re.compile(exclude) if exclude else None,
        )

    def get_password(self) -> str:
        if self.password_file:
            with open(self.password_file, encoding="utf-8") as f:
                return f.read().strip()
        return self.password

    def __repr__(self) -> str:  # never print the password
        return (f"Config(enabled={self.enabled}, username={self.username!r}, "
                f"allowed_networks={[str(n) for n in self.allowed_networks]}, "
                f"port={self.port}, ca_file={self.ca_file!r}, timeout={self.timeout})")


_config: Optional[Config] = None


def get_config() -> Config:
    """Config is read from the environment once, on first use."""
    global _config
    if _config is None:
        _config = Config.from_env()
        log.info("nxos collector: %r", _config)
    return _config


# --------------------------------------------------------------------------- target

def validate_target(target: str, cfg: Config) -> Tuple[int, str]:
    """Return (0, ip) if allowed, otherwise (http_status, reason).

    Only IP literals are accepted (no hostnames: DNS could point anywhere),
    and only inside NXOS_ALLOWED_CIDRS, so /probe cannot be used as a proxy.
    """
    if not target:
        return 400, "missing 'target' parameter"
    try:
        ip = ipaddress.ip_address(target.strip().strip("[]"))
    except ValueError:
        return 400, "target must be an IP address"
    if not any(ip.version == n.version and ip in n for n in cfg.allowed_networks):
        return 403, "target not in NXOS_ALLOWED_CIDRS"
    return 0, str(ip)


# --------------------------------------------------------------------------- NX-API

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a redirect could leave the allow-list
        return None


def _ssl_context(cfg: Config) -> ssl.SSLContext:
    if cfg.ca_file:
        ctx = ssl.create_default_context(cafile=cfg.ca_file)
        ctx.check_hostname = False  # targets are IPs; the CA still has to match
        return ctx
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def fetch_show_interface(ip: str, cfg: Config) -> Any:
    """One JSON-RPC request: 'show interface'. Returns the decoded JSON."""
    password = cfg.get_password()
    if not cfg.username or not password:
        raise ProbeError("NXOS_USERNAME / NXOS_PASSWORD(_FILE) not set")
    host = f"[{ip}]" if ":" in ip else ip
    payload = [{"jsonrpc": "2.0", "method": "cli",
                "params": {"cmd": "show interface", "version": 1}, "id": 1}]
    token = base64.b64encode(f"{cfg.username}:{password}".encode()).decode()
    req = urllib.request.Request(
        f"https://{host}:{cfg.port}/ins",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={"Content-Type": "application/json-rpc",
                 "Authorization": "Basic " + token},
    )
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),  # never go through HTTP(S)_PROXY
        urllib.request.HTTPSHandler(context=_ssl_context(cfg)),
        _NoRedirect(),
    )
    try:
        with opener.open(req, timeout=cfg.timeout) as resp:
            raw = resp.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as e:
        # NX-API sends JSON-RPC errors (e.g. RBAC denial) with HTTP 4xx/5xx
        detail = ""
        try:
            detail = _rpc_error(json.loads(e.read(65536)))
        except Exception:
            pass
        raise ProbeError(f"HTTP {e.code}{': ' + detail if detail else ''}") from None
    except (urllib.error.URLError, OSError) as e:
        raise ProbeError(str(getattr(e, "reason", e))) from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise ProbeError("response too large")
    try:
        return json.loads(raw)
    except ValueError:
        raise ProbeError("response is not JSON") from None


def _rpc_error(data: Any) -> str:
    for item in as_list(data):
        if isinstance(item, dict) and isinstance(item.get("error"), dict):
            err = item["error"]
            msg = err.get("message", "")
            d = (err.get("data") or {}).get("msg", "") if isinstance(err.get("data"), dict) else ""
            return f"{msg} {d}".strip()
    return ""


# --------------------------------------------------------------------------- parsing

def as_list(x: Any) -> List[Any]:
    """NX-OS returns a single object instead of a list when there is one row."""
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def to_number(v: Any) -> Optional[int]:
    """Counter values may be strings or numbers. None if unusable."""
    if isinstance(v, bool) or v is None:
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v)
    try:
        return int(str(v).strip())
    except ValueError:
        try:
            return int(float(str(v).strip()))
        except ValueError:
            return None


def extract_rows(data: Any) -> List[Dict[str, Any]]:
    """Return the ROW_interface entries of a JSON-RPC 'show interface' reply."""
    items = as_list(data)
    if not items:
        raise ProbeError("empty response")
    item = items[0]
    if not isinstance(item, dict):
        raise ProbeError("unexpected response format")
    if "error" in item:
        raise ProbeError(_rpc_error(item) or "NX-API error")
    body = (item.get("result") or {}).get("body")
    if not isinstance(body, dict) or "TABLE_interface" not in body:
        raise ProbeError("no TABLE_interface in response")
    table = body["TABLE_interface"]
    if isinstance(table, list):  # seen on some releases
        rows = [r for t in table if isinstance(t, dict) for r in as_list(t.get("ROW_interface"))]
    else:
        rows = as_list((table or {}).get("ROW_interface"))
    return [r for r in rows if isinstance(r, dict)]


def _up(v: Any) -> Optional[int]:
    if not isinstance(v, str):
        return None
    return 1 if v.strip().lower() == "up" else 0


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def render_metrics(rows: List[Dict[str, Any]], cfg: Config) -> Dict[str, List[Tuple[str, int]]]:
    """metric name -> [(interface, value)] for Ethernet* interfaces only."""
    out: Dict[str, List[Tuple[str, int]]] = {}
    for row in rows:
        name = row.get("interface")
        if not isinstance(name, str) or not name.startswith("Ethernet"):
            continue
        if cfg.exclude is not None and cfg.exclude.search(name):
            continue
        oper, admin = _up(row.get("state")), _up(row.get("admin_state"))
        if oper is not None:
            out.setdefault("nxos_interface_oper_up", []).append((name, oper))
        if admin is not None:
            out.setdefault("nxos_interface_admin_up", []).append((name, admin))
        for key, suffix, _ in COUNTERS:
            if key not in row:
                continue  # key not on this platform/release: skip silently
            val = to_number(row[key])
            if val is not None:
                out.setdefault("nxos_interface_" + suffix, []).append((name, val))
    return out


_HELP = {
    "nxos_interface_oper_up": ("gauge", "1 if the interface is operationally up."),
    "nxos_interface_admin_up": ("gauge", "1 if the interface is administratively up."),
    **{"nxos_interface_" + s: ("counter", h) for _, s, h in COUNTERS},
}


def format_exposition(up: int, duration: float,
                      metrics: Dict[str, List[Tuple[str, int]]]) -> str:
    lines = [
        "# HELP nxos_up 1 if the last NX-API poll of the switch succeeded.",
        "# TYPE nxos_up gauge",
        f"nxos_up {up}",
        "# HELP nxos_poll_duration_seconds Time taken by the NX-API poll.",
        "# TYPE nxos_poll_duration_seconds gauge",
        f"nxos_poll_duration_seconds {duration:.6f}",
    ]
    for name in _HELP:  # stable order, families kept together
        samples = metrics.get(name)
        if not samples:
            continue
        mtype, helptext = _HELP[name]
        lines.append(f"# HELP {name} {helptext}")
        lines.append(f"# TYPE {name} {mtype}")
        for iface, val in samples:
            lines.append(f'{name}{{interface="{_escape(iface)}"}} {val}')
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- entry point

Fetcher = Callable[[str, Config], Any]


def probe(target: str, cfg: Optional[Config] = None,
          fetch: Fetcher = fetch_show_interface) -> Tuple[int, str, bytes]:
    """Handle one /probe request. Returns (http_status, content_type, body).

    Bad/forbidden target -> 400/403 (scrape fails, 'up' == 0).
    Switch unreachable / NX-API error -> 200 with nxos_up 0, like blackbox_exporter.
    """
    cfg = cfg or get_config()
    if not cfg.enabled:
        return 404, "text/plain; charset=utf-8", b"nxos probe disabled\n"
    status, value = validate_target(target, cfg)
    if status:
        log.warning("nxos probe refused target=%r: %s", target, value)
        return status, "text/plain; charset=utf-8", (value + "\n").encode()

    start = time.monotonic()
    try:
        rows = extract_rows(fetch(value, cfg))
        metrics, up = render_metrics(rows, cfg), 1
    except ProbeError as e:
        log.warning("nxos probe target=%s failed: %s", value, e)
        metrics, up = {}, 0
    except Exception:  # never let a bad reply take the exporter's HTTP handler down
        log.exception("nxos probe target=%s: unexpected error", value)
        metrics, up = {}, 0
    duration = time.monotonic() - start
    return 200, CONTENT_TYPE, format_exposition(up, duration, metrics).encode()


def probe_from_query(query_string: str, cfg: Optional[Config] = None) -> Tuple[int, str, bytes]:
    """Same as probe(), taking the raw query string ('target=10.0.0.1')."""
    target = (parse_qs(query_string or "").get("target") or [""])[0]
    return probe(target, cfg)


def wsgi_app(environ, start_response):
    """WSGI callable for exporters built on a WSGI server / prometheus_client."""
    status, ctype, body = probe_from_query(environ.get("QUERY_STRING", ""))
    reason = {200: "OK", 400: "Bad Request", 403: "Forbidden", 404: "Not Found"}[status]
    start_response(f"{status} {reason}", [("Content-Type", ctype),
                                          ("Content-Length", str(len(body)))])
    return [body]
