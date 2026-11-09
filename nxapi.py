import argparse
import textwrap
import requests
import json
import urllib3
import time
import sys

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Username and password
USERNAME = 'admin'
PASSWORD = 'Cisc0123'

# Switches and interfaces to poll
SWITCHES = {
    'switch1': {
        'mgmt': '192.168.10.1',
        'name': 'SPINE-1',
        'interfaces': ['1/1', '1/2', '1/3']
    }
}

# Output field -> key in the 'show interface' JSON
# (same numbers you see in the CLI 'show interface' output)
FIELDS = [
    ('discards_in',       'eth_indiscard'),
    ('discards_out',      'eth_outdiscard'),
    ('errors_in',         'eth_inerr'),
    ('errors_out',        'eth_outerr'),
    ('crc_errors',        'eth_crc'),
    ('multicastPkts_in',  'eth_inmcast'),
    ('multicastPkts_out', 'eth_outmcast'),
    ('broadcastPkts_in',  'eth_inbcast'),
    ('broadcastPkts_out', 'eth_outbcast'),
    ('ucastPkts_in',      'eth_inucast'),
    ('ucastPkts_out',     'eth_outucast'),
    ('bytes_in',          'eth_inbytes'),
    ('bytes_out',         'eth_outbytes'),
    ('packetRate_in',     'eth_inrate1_pkts'),
    ('packetRate_out',    'eth_outrate1_pkts'),
]


def as_list(x):
    # NX-OS returns a dict instead of a list when there is only one row
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def show_interfaces(ip_addr, ports):
    """Run 'show interface ethernetX/Y' for all ports in ONE NX-API request."""
    payload = [{"jsonrpc": "2.0", "method": "cli",
                "params": {"cmd": "show interface ethernet" + p, "version": 1}, "id": n}
               for n, p in enumerate(ports, 1)]
    r = requests.post("https://" + ip_addr + "/ins", json=payload,
                      auth=(USERNAME, PASSWORD), verify=False, timeout=15,
                      headers={"Content-Type": "application/json-rpc"})
    r.raise_for_status()
    rows = {}
    for item in as_list(r.json()):
        if "error" in item:
            print(f"{ip_addr}: {item['error'].get('message')} "
                  f"(interface {ports[item.get('id', 1) - 1]})", file=sys.stderr)
            continue
        body = (item.get("result") or {}).get("body") or {}
        for row in as_list(body.get("TABLE_interface", {}).get("ROW_interface")):
            rows[row.get("interface")] = row
    return rows


if __name__ == '__main__':
    parser = argparse.ArgumentParser(
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent('''\
            Cisco Nexus get interface counters via NX-API ('show interface')
            --------------------------------
            Examples for usage:
            python3 a.py -f json
            python3 a.py -f influxdb
            python3 a.py -f json --debug   (print all raw keys of each interface)
       '''))
    parser.add_argument('-f', required=True, dest='arg_format', choices=['json', 'influxdb'],
                        help='Output format')
    parser.add_argument('--debug', action='store_true',
                        help='Also print every raw counter key the switch returns')
    args = parser.parse_args()

    unixtime = time.time_ns()
    interface_stats = {'time': time.strftime('%Y-%m-%dT%H:%M:%SZ'), 'interfaces': []}

    for sw in SWITCHES.values():
        try:
            rows = show_interfaces(sw['mgmt'], sw['interfaces'])
        except (requests.RequestException, ValueError) as e:
            print(f"Cannot poll {sw['name']} ({sw['mgmt']}): {e}", file=sys.stderr)
            continue

        for port in sw['interfaces']:
            name = "Ethernet" + port
            row = rows.get(name)
            if row is None:
                print(f"{sw['name']}: no data for {name}", file=sys.stderr)
                continue
            if args.debug:
                print(f"--- raw keys for {name} ---", file=sys.stderr)
                print(json.dumps(row, indent=2), file=sys.stderr)

            values, missing = {}, []
            for field, key in FIELDS:
                if key in row:
                    values[field] = str(row[key])
                else:
                    missing.append(f"{field} ({key})")
            if missing:
                print(f"{sw['name']} {name}: not returned by switch: {', '.join(missing)}",
                      file=sys.stderr)

            if args.arg_format == 'json':
                interface_stats['interfaces'].append(
                    {'switch': sw['name'], 'interface': name, **values})
            elif values:
                fields_txt = ",".join(f"{k}={v}" for k, v in values.items())
                print(f"nexusinterface,switch={sw['name']},interface={name} "
                      f"{fields_txt} {unixtime}")

    if args.arg_format == 'json':
        print(json.dumps(interface_stats, indent=2))
