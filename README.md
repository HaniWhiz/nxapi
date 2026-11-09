# nxapi – Nexus interface counter poller

`nxapi.py` polls Cisco Nexus switches over **NX-API** and prints per-interface
counters, either as **JSON** or as **InfluxDB line protocol** (ready for Telegraf /
InfluxDB / Grafana).

For each switch it sends **one** NX-API JSON-RPC request containing a
`show interface ethernetX/Y` command per configured port, then extracts:

| Output field                              | NX-OS key (`show interface` JSON)      |
|-------------------------------------------|----------------------------------------|
| `discards_in` / `discards_out`            | `eth_indiscard` / `eth_outdiscard`     |
| `errors_in` / `errors_out`                | `eth_inerr` / `eth_outerr`             |
| `crc_errors`                              | `eth_crc`                              |
| `ucastPkts_in` / `ucastPkts_out`          | `eth_inucast` / `eth_outucast`         |
| `multicastPkts_in` / `multicastPkts_out`  | `eth_inmcast` / `eth_outmcast`         |
| `broadcastPkts_in` / `broadcastPkts_out`  | `eth_inbcast` / `eth_outbcast`         |
| `bytes_in` / `bytes_out`                  | `eth_inbytes` / `eth_outbytes`         |
| `packetRate_in` / `packetRate_out`        | `eth_inrate1_pkts` / `eth_outrate1_pkts` |

These are the same numbers you see in the CLI `show interface` output. To add a
counter, add a `(field, key)` pair to `FIELDS` in `nxapi.py` (`--debug` lists every key
your switch returns).

## Repository contents

| File                 | Purpose                                                       |
|----------------------|---------------------------------------------------------------|
| `nxapi.py`           | The poller                                                    |
| `api-hardenning.txt` | NX-OS config: read-only role, monitoring user, mgmt0 ACL      |
| `.gitignore`         | Python artifacts and `.env`                                   |

## 1. Switch configuration (NX-OS)

Enable NX-API (HTTPS on 443 by default):

```
N3K(config)# feature nxapi
```

Then apply [`api-hardenning.txt`](api-hardenning.txt). It does three things:

```
! 1. Read-only role: only 'show interface' is allowed
N3K(config)# role name nxapi-monN3k
N3K(config-role)# description NX-API counters poller - show interface only
N3K(config-role)# rule 1 permit command show interface
N3K(config-role)# rule 2 permit command show interface *

! 2. Monitoring user bound to that role (replace both placeholders)
N3K(config)# username <monitor-user> password <CHANGE-ME> role nxapi-monN3k

! 3. mgmt0 ACL: NX-API (443) only from the poller host (192.168.10.10 here)
N3K(config)# ip access-list NXAPI-ONLY
N3K(config-acl)# permit tcp 192.168.10.10/32 any eq 443
N3K(config-acl)# permit tcp any any eq 22
N3K(config-acl)# permit icmp any any
N3K(config-acl)# deny tcp any any eq 443
N3K(config-acl)# permit ip any any
N3K(config)# interface mgmt0
N3K(config-if)# ip access-group NXAPI-ONLY in
```

Change `192.168.10.10` to the address of the host that runs `nxapi.py`.
**Do not poll with the `admin` account.** If the poller's credentials leak, the
read-only role means they can only be used to run `show interface`.

Check the setup:

```
show feature | include nxapi
show nxapi
show role name nxapi-monN3k
show user-account <monitor-user>
show ip access-lists NXAPI-ONLY
```

## 2. Install

Python 3.8+ and `requests`:

```
python3 -m venv .venv && . .venv/bin/activate
pip install requests
```

## 3. Configure

### Switches and ports

Edit `SWITCHES` at the top of `nxapi.py`:

```python
SWITCHES = {
    'switch1': {
        'mgmt': '192.168.10.1',      # mgmt0 IP
        'name': 'SPINE-1',           # used as the 'switch' tag / field
        'interfaces': ['1/1', '1/2', '1/3']
    },
    'switch2': {
        'mgmt': '192.168.10.2',
        'name': 'SPINE-2',
        'interfaces': ['1/49', '1/50']
    }
}
```

### Credentials

Credentials are **not** stored in the script. They come from environment variables
or the command line:

| Variable     | CLI option   | Meaning                                         | Default          |
|--------------|--------------|-------------------------------------------------|------------------|
| `NXAPI_HOST` | `--host`     | mgmt address; overrides `SWITCHES` (only when `SWITCHES` has a single entry) | from `SWITCHES` |
| `NXAPI_USER` | `--user`     | NX-API user (with role `nxapi-monN3k`)          | `nxapi-mon`      |
| `NXAPI_PASS` | `--ask-pass` | Password. **Required.** `--ask-pass` prompts for it instead | –                |

There is no `--password` option on purpose, so the password never appears in
`ps` output or shell history. If no password is given, the script exits with code 2:

```
nxapi.py: error: no password given: set the NXAPI_PASS environment variable or use --ask-pass
```

Keep credentials in a `.env` file (already in `.gitignore`), readable only by the
poller account:

```
# .env  (chmod 600)
NXAPI_USER=nxapi-mon
NXAPI_PASS=<CHANGE-ME>
```

## 4. Usage

```
python3 nxapi.py -f {json,influxdb} [--debug] [--host HOST] [--user USER] [--ask-pass]
```

```
# Load credentials
set -a; . ./.env; set +a

# JSON
python3 nxapi.py -f json

# InfluxDB line protocol
python3 nxapi.py -f influxdb

# One-off check against another switch, prompt for the password
python3 nxapi.py -f json --host 192.168.10.2 --ask-pass

# Show every raw key the switch returns per interface (on stderr)
python3 nxapi.py -f json --debug
```

### JSON output

```json
{
  "time": "2026-11-09T11:12:20Z",
  "interfaces": [
    {
      "switch": "SPINE-1",
      "interface": "Ethernet1/1",
      "discards_in": "0",
      "discards_out": "0",
      "errors_in": "0",
      "errors_out": "0",
      "crc_errors": "0",
      "multicastPkts_in": "184213",
      "multicastPkts_out": "92311",
      "broadcastPkts_in": "12",
      "broadcastPkts_out": "4",
      "ucastPkts_in": "88123412",
      "ucastPkts_out": "90012233",
      "bytes_in": "61234567890",
      "bytes_out": "63456789012",
      "packetRate_in": "1520",
      "packetRate_out": "1611"
    }
  ]
}
```

Counter values are strings. The `time` value is the host's local time (the script
formats it with `time.strftime`), even though it ends in `Z`.

### InfluxDB output

One line per interface. Measurement `nexusinterface`, tags `switch` and
`interface`, and the timestamp in nanoseconds:

```
nexusinterface,switch=SPINE-1,interface=Ethernet1/1 discards_in=0,discards_out=0,errors_in=0,errors_out=0,crc_errors=0,multicastPkts_in=184213,multicastPkts_out=92311,broadcastPkts_in=12,broadcastPkts_out=4,ucastPkts_in=88123412,ucastPkts_out=90012233,bytes_in=61234567890,bytes_out=63456789012,packetRate_in=1520,packetRate_out=1611 1762686740000000000
```

Errors and warnings go to **stderr**, so stdout holds only data and can be piped
straight into a collector.

## 5. Scheduling

### Telegraf (`inputs.exec`)

```toml
[[inputs.exec]]
  commands    = ["/opt/nxapi/.venv/bin/python /opt/nxapi/nxapi.py -f influxdb"]
  interval    = "60s"
  timeout     = "30s"
  data_format = "influx"
  environment = ["NXAPI_USER=nxapi-mon", "NXAPI_PASS=${NXAPI_PASS}"]
```

Supply `NXAPI_PASS` to the Telegraf service from its environment file (for example
`/etc/default/telegraf`, mode 600), not from `telegraf.conf`.

### cron

```
* * * * * cd /opt/nxapi && set -a && . ./.env && set +a && .venv/bin/python nxapi.py -f json >> /var/log/nxapi.json 2>> /var/log/nxapi.err
```

## 6. Troubleshooting

| Symptom (stderr)                                        | Likely cause / check                                       |
|---------------------------------------------------------|------------------------------------------------------------|
| `Cannot poll ... Connection refused` / timeout          | `feature nxapi` off, wrong mgmt IP, or the mgmt0 ACL doesn't permit the poller IP on 443 (`show ip access-lists NXAPI-ONLY` hit counters) |
| `Cannot poll ... 401 Client Error: Unauthorized`        | Wrong user or password, or the user doesn't exist on that switch |
| `... (interface 1/x)` with a permission error           | The user's role doesn't permit the command (`show role name nxapi-monN3k`) |
| `no data for Ethernet1/x`                               | The port doesn't exist or is misspelled in `SWITCHES`          |
| `not returned by switch: crc_errors (eth_crc)`          | This platform or NX-OS version uses a different key. Run with `--debug` and adjust `FIELDS` |

## Security notes

- TLS certificate checks are **disabled** (`verify=False`) because switches usually
  use self-signed certificates. Restrict access with the mgmt0 ACL and keep the
  mgmt network isolated.
- The role only allows `show interface`. Don't widen it unless you need to.
- An earlier version of this repository had a hardcoded password in `nxapi.py`, and it is
  still in the git history. **Change that password on every switch that used it.**
