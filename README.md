# P1BatteryController

Open-source reference implementation for controlling a home battery using smart-meter/P1 net-flow data and dynamic electricity prices.

The public repository is generated from an explicit allowlist. Local configuration, databases, credentials, IP addresses, device identifiers and other private Home Automation files are intentionally not exported.

## Scope

- economic battery planning;
- P1/net-flow driven surplus charging;
- Marstek/Venus control logic as the first battery adapter;
- no inverter API required for the control concept.

## Safety

This software can control electrical equipment. Review configuration, limits and local installation requirements before enabling writes. Use simulation/read-only validation first.

## Automated export

This repository is updated from a private production tree by an allowlist-based export pipeline. A failed test, privacy scan or Git check blocks publication.
