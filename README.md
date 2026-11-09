# nxapi

`nxapi.py` polls Cisco Nexus switches over NX-API, runs `show interface ethernetX/Y`
for the configured ports (one JSON-RPC request per switch) and prints the interface
counters (discards, errors, CRC, unicast/multicast/broadcast packets, bytes, packet
rate) either as JSON or as InfluxDB line protocol.

Switches and ports are defined in the `SWITCHES` dict at the top of `nxapi.py`.

## Required NX-OS configuration

```
feature nxapi
```

Then apply the config in [`api-hardenning.txt`](api-hardenning.txt):

- role `nxapi-monN3k` – read-only, only permits `show interface`
- a monitoring user bound to that role (replace the placeholders):
  `username <monitor-user> password <CHANGE-ME> role nxapi-monN3k`
- ACL `NXAPI-ONLY` on `mgmt0` – allows HTTPS (443) only from the poller host

Do not use the `admin` account for polling.

## Requirements

Python 3.8+ and `requests`:

```
pip install requests
```

## Environment variables

| Variable     | Meaning                                                      | Default     |
|--------------|--------------------------------------------------------------|-------------|
| `NXAPI_HOST` | Switch mgmt address (overrides the address in `SWITCHES`; only with a single switch entry) | from `SWITCHES` |
| `NXAPI_USER` | NX-API username (user with role `nxapi-monN3k`)               | `nxapi-mon` |
| `NXAPI_PASS` | NX-API password – **required** (or use `--ask-pass`)          | –           |

CLI overrides: `--host`, `--user`, `--ask-pass` (prompt for the password). There is
deliberately no `--password` option, so the password never shows up in `ps` output or
shell history. Keep credentials in a `.env` file (ignored by git) or your secret store.

## Usage

```
export NXAPI_USER=nxapi-mon
read -rs NXAPI_PASS && export NXAPI_PASS

# JSON
python3 nxapi.py -f json

# InfluxDB line protocol
python3 nxapi.py -f influxdb

# Different switch / user, prompt for password
python3 nxapi.py -f json --host 192.168.10.1 --user nxapi-mon --ask-pass

# Print all raw counter keys returned by the switch (to stderr)
python3 nxapi.py -f json --debug
```

Example InfluxDB output line:

```
nexusinterface,switch=SPINE-1,interface=Ethernet1/1 discards_in=0,...,packetRate_out=12 1731150000000000000
```

Note: the script uses `verify=False` (self-signed switch certificates).
