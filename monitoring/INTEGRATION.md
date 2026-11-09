# Integrating the Nexus collector into the existing exporter

`nxos_collector/` is a **module**, not a service. It opens no port, starts no thread,
spawns no process and keeps no state. It only adds a `/probe?target=<switch-ip>`
handler to the HTTP server the existing exporter already runs.

- Python standard library only (`urllib`, `ssl`, `json`, `ipaddress`, `re`). No `pip install`,
  so it builds and runs offline.
- One NX-API request per switch per scrape: `POST https://<switch>:<port>/ins`, JSON-RPC
  `cli`, `show interface`. It does not use the DME (`dbgIfIn`/`dbgIfOut`), so values match the
  CLI and follow `clear counters`.
- It ignores `HTTP(S)_PROXY` and does not follow redirects, so the allow-list can't be
  bypassed.

## 1. Copy the module

Copy `nxos_collector/` into the exporter's source tree (next to its other collectors) and
package it with the exporter the way the existing collectors are packaged.

## 2. Mount `/probe` on the existing HTTP server

> **The exporter's real code is not in this repository, so the diff below is a template,
> not the final diff.** Apply the hunk that matches how the exporter serves `/metrics`.
> If the exporter is written in **Go**, this module must be ported to Go (stdlib
> `net/http`, `crypto/tls`, `encoding/json`, `net/netip`) and not run as Python.

The entry points are:

| Function                              | Use when the exporter uses                             |
|---------------------------------------|--------------------------------------------------------|
| `nxos_collector.wsgi_app`             | a WSGI server / `prometheus_client.make_wsgi_app()`    |
| `nxos_collector.probe_from_query(qs)` | anything else: returns `(status, content_type, body)`  |

### a) Exporter built on `prometheus_client` + WSGI

```diff
 from prometheus_client import make_wsgi_app
+import nxos_collector

 metrics_app = make_wsgi_app(registry)

 def app(environ, start_response):
+    if environ.get("PATH_INFO") == "/probe":
+        return nxos_collector.wsgi_app(environ, start_response)
     return metrics_app(environ, start_response)
```

If the exporter uses `prometheus_client.start_http_server()`, there is no routing hook.
It has to switch to `make_wsgi_app()` on the same port (same process, same port, no new
service) before `/probe` can be added.

### b) Exporter built on `http.server`

```diff
+import nxos_collector
+from urllib.parse import urlsplit

 class Handler(BaseHTTPRequestHandler):
     def do_GET(self):
+        url = urlsplit(self.path)
+        if url.path == "/probe":
+            status, ctype, body = nxos_collector.probe_from_query(url.query)
+            self.send_response(status)
+            self.send_header("Content-Type", ctype)
+            self.send_header("Content-Length", str(len(body)))
+            self.end_headers()
+            self.wfile.write(body)
+            return
         ...existing /metrics handling...
```

**The server must handle requests concurrently** (`ThreadingHTTPServer`, a threaded WSGI
server, or similar). A probe can block for up to `NXOS_TIMEOUT_SECONDS` (15s). On a
single-threaded server, one slow switch would delay `/metrics` and every other probe.

## 3. Configuration (exporter's environment / secret store)

| Variable               | Default | Meaning                                                             |
|------------------------|---------|---------------------------------------------------------------------|
| `NXOS_PROBE_ENABLED`   | `true`  | `false` turns the module off: `/probe` returns 404               |
| `NXOS_USERNAME`        | –       | NX-API user bound to the `show interface`-only role                 |
| `NXOS_PASSWORD_FILE`   | –       | Path to the password from the secret store (re-read every scrape, so rotation needs no restart) |
| `NXOS_PASSWORD`        | –       | Password, if no file is used                                        |
| `NXOS_ALLOWED_CIDRS`   | *(empty)* | Comma-separated CIDRs. **Empty = every target refused (403).** Only IP literals are accepted, not hostnames |
| `NXOS_PORT`            | `443`   | NX-API HTTPS port (`nxapi https port ...` on the switch)            |
| `NXOS_CA_FILE`         | –       | CA bundle for switch certificates. Unset = no verification          |
| `NXOS_TIMEOUT_SECONDS` | `15`    | Per-request timeout. Keep below the scrape timeout (20s)           |
| `NXOS_EXCLUDE_REGEX`   | –       | Interfaces to skip, e.g. `^Ethernet1/(4[0-8])$`                     |

Example systemd drop-in for the existing exporter unit (no secrets in it):

```ini
# /etc/systemd/system/<existing-exporter>.service.d/nxos.conf
[Service]
Environment=NXOS_USERNAME=nxapi-mon
Environment=NXOS_PASSWORD_FILE=/run/secrets/nxapi-mon
Environment=NXOS_ALLOWED_CIDRS=192.168.10.0/24
```

Switch side: the NX-OS role, user and mgmt0 ACL from the main README. The ACL must permit
the **exporter host** on the NX-API port.

## 4. Behaviour

| Situation                                   | HTTP | Result                                     |
|---------------------------------------------|------|--------------------------------------------|
| Poll OK                                     | 200  | `nxos_up 1` + interface metrics            |
| Switch unreachable / timeout / 401 / RBAC / bad JSON | 200 | `nxos_up 0`, reason in the exporter log |
| `target` missing or not an IP               | 400  | scrape `up == 0`                           |
| `target` outside `NXOS_ALLOWED_CIDRS`       | 403  | scrape `up == 0`                           |
| `NXOS_PROBE_ENABLED=false`                  | 404  | scrape `up == 0`                           |

Metrics (labels: `interface` only; `switch`/`dc` are added by the scrape job):

| Metric                                          | Type    | `show interface` key |
|-------------------------------------------------|---------|----------------------|
| `nxos_up`                                       | gauge   | –                    |
| `nxos_poll_duration_seconds`                    | gauge   | –                    |
| `nxos_interface_oper_up`                        | gauge   | `state`              |
| `nxos_interface_admin_up`                       | gauge   | `admin_state`        |
| `nxos_interface_in_unicast_packets_total`       | counter | `eth_inucast`        |
| `nxos_interface_out_unicast_packets_total`      | counter | `eth_outucast`       |
| `nxos_interface_in_multicast_packets_total`     | counter | `eth_inmcast`        |
| `nxos_interface_out_multicast_packets_total`    | counter | `eth_outmcast`       |
| `nxos_interface_in_broadcast_packets_total`     | counter | `eth_inbcast`        |
| `nxos_interface_out_broadcast_packets_total`    | counter | `eth_outbcast`       |
| `nxos_interface_in_bytes_total`                 | counter | `eth_inbytes`        |
| `nxos_interface_out_bytes_total`                | counter | `eth_outbytes`       |
| `nxos_interface_in_errors_total`                | counter | `eth_inerr`          |
| `nxos_interface_out_errors_total`               | counter | `eth_outerr`         |
| `nxos_interface_crc_errors_total`               | counter | `eth_crc`            |
| `nxos_interface_in_discards_total`              | counter | `eth_indiscard`      |
| `nxos_interface_out_discards_total`             | counter | `eth_outdiscard`     |

Only `Ethernet*` interfaces are exported. If a key is missing on a platform or release,
that metric is skipped for that port.

## 5. Tests

```
python3 -m unittest discover -s tests -v
```

The tests use recorded `show interface` replies in `tests/fixtures/` (single row returned as an
object, multi-row list with numbers and strings, missing keys, non-Ethernet rows, RBAC error)
and need no network.

## 6. Rollback (disable without redeploying anything else)

Pick the lightest option. None of them needs a new build or touches other collectors.

1. **Stop polling, exporter untouched (seconds).** Remove or comment out the
   `nxos_interfaces` job in the vmagent scrape config (or empty the
   `/etc/vmagent/targets/nxos_*.yml` files, which file_sd picks up without a reload), then
   `kill -HUP <vmagent-pid>` or `curl -X POST http://<vmagent>:8429/-/reload`.
   No more NX-API requests are sent. Keeping the code in the exporter costs nothing,
   because it only runs when `/probe` is called.
2. **Turn the module off in the exporter.** Set `NXOS_PROBE_ENABLED=false` (or remove
   `NXOS_ALLOWED_CIDRS`, which refuses every target) and restart the exporter. This is a
   config change, not a redeploy. `/probe` then returns 404 (or 403), and `/metrics` and the
   other collectors carry on as before.
3. **Switch side (if the exporter can't be changed).** Remove the monitoring user, or
   remove the exporter host from the mgmt0 ACL. Polls fail with `nxos_up 0` and nothing
   else is affected.

Also silence or disable the `nxos_interfaces` vmalert group, so the rollback doesn't
trigger `NxosScrapeFailed` / `NxosPollFailed`.
