# Grafana panel queries (VictoriaMetrics datasource, Prometheus type)

Dashboard variables:

| Variable     | Query                                                                 |
|--------------|-----------------------------------------------------------------------|
| `$dc`        | `label_values(nxos_up{job="nxos_interfaces"}, dc)`                    |
| `$switch`    | `label_values(nxos_up{job="nxos_interfaces", dc=~"$dc"}, switch)`     |
| `$interface` | `label_values(nxos_interface_oper_up{switch=~"$switch"}, interface)`  |

Use `$__rate_interval` (it is at least 4× the 60s scrape interval). Always use `rate()` / `increase()`
on the `_total` series, never the raw value: after `clear counters` (or a reload) the counter
drops and `rate()`/`increase()` treat that drop as a reset instead of a negative spike.

## Throughput (bits/s), time series, unit `bps`

```promql
# In  (legend: {{switch}} {{interface}} in)
rate(nxos_interface_in_bytes_total{switch=~"$switch", interface=~"$interface"}[$__rate_interval]) * 8
# Out (negative-Y via override "Transform: negative-Y", or multiply by -8)
rate(nxos_interface_out_bytes_total{switch=~"$switch", interface=~"$interface"}[$__rate_interval]) * 8
```

## Packets/s by type, time series, unit `pps`

```promql
rate(nxos_interface_in_unicast_packets_total{switch=~"$switch", interface=~"$interface"}[$__rate_interval])
rate(nxos_interface_in_multicast_packets_total{switch=~"$switch", interface=~"$interface"}[$__rate_interval])
rate(nxos_interface_in_broadcast_packets_total{switch=~"$switch", interface=~"$interface"}[$__rate_interval])
# same with out_*
```

## Errors and discards in the selected time range, table / bar gauge

```promql
increase(nxos_interface_crc_errors_total{switch=~"$switch"}[$__range])  > 0
increase(nxos_interface_in_errors_total{switch=~"$switch"}[$__range])   > 0
increase(nxos_interface_out_errors_total{switch=~"$switch"}[$__range])  > 0
increase(nxos_interface_in_discards_total{switch=~"$switch"}[$__range]) > 0
increase(nxos_interface_out_discards_total{switch=~"$switch"}[$__range]) > 0
```

Instant query, format "Table", "Merge" transform, so you get one row per interface with only the
ports that had errors.

## Error rate over time, time series, unit `/s`

```promql
rate(nxos_interface_crc_errors_total{switch=~"$switch", interface=~"$interface"}[$__rate_interval])
rate(nxos_interface_in_errors_total{switch=~"$switch", interface=~"$interface"}[$__rate_interval])
```

## Top 10 busiest ports (bits/s), bar gauge

```promql
topk(10,
  rate(nxos_interface_in_bytes_total{dc=~"$dc"}[$__rate_interval]) * 8
  + rate(nxos_interface_out_bytes_total{dc=~"$dc"}[$__rate_interval]) * 8
)
```

## Port status, state timeline / table

```promql
nxos_interface_oper_up{switch=~"$switch"}                  # value mapping 1=up (green) 0=down (red)
nxos_interface_admin_up{switch=~"$switch"} == 1
  and nxos_interface_oper_up{switch=~"$switch"} == 0       # admin-up / oper-down ports
```

## Collector health, stat panels

```promql
nxos_up{switch=~"$switch"}                                  # 1 = poll OK
max_over_time(nxos_poll_duration_seconds{switch=~"$switch"}[$__range])   # should stay < 15s
up{job="nxos_interfaces", switch=~"$switch"}                # 0 = scrape refused/failed
```
