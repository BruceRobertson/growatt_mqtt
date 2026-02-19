# Canadian Solar / Growatt Inverter Monitor

Reads data from a Canadian Solar or Growatt inverter via Modbus RTU (RS485) and uploads it to [PVOutput](https://pvoutput.org) and an MQTT broker.

Adapted from https://github.com/ArdescoConsulting/growattRS232 and https://github.com/jrbenito/canadianSolar-pvoutput

## Features

- Polls inverter input registers every 20 seconds for near-real-time data
- Uploads status to PVOutput every 5 minutes (aligned to clock boundaries)
- Publishes per-string and AC readings to an MQTT broker for integration with Home Assistant or similar
- Supports dual PV string monitoring (voltage, current, power per string)
- `--test` dry-run mode logs all data without sending
- Runs on a configurable schedule (default 05:00 - 21:00)

## Requirements

- Python 3
- A Growatt or Canadian Solar inverter connected via RS485/USB serial adapter

Install dependencies:

```
pip install -r requirements.txt
```

## Configuration

Copy `pvoutput.txt.rename` to `pvoutput.txt` and fill in your details:

| Key | Description |
|-----|-------------|
| `SYSTEMID` | Your PVOutput system ID |
| `APIKEY` | Your PVOutput API key |
| `TimeZone` | Timezone string (e.g. `America/Sao_Paulo`) |
| `INVERTERPORT` | Serial port for the inverter (e.g. `/dev/ttyUSB0`) |
| `MQTTUSER` | MQTT broker username |
| `MQTTPASS` | MQTT broker password |
| `MQTTBROKER` | MQTT broker hostname |
| `MQTTPORT` | MQTT broker port (e.g. `1883`) |
| `MQTTTOPIC` | MQTT base topic for published messages |
| `LOGLEVEL` | *(Optional)* Log verbosity: `DEBUG`, `INFO`, `WARNING` (default), `ERROR`, `CRITICAL` |
| `HA_DISCOVERY` | *(Optional)* Enable Home Assistant MQTT auto-discovery: `true` (default) or `false` |
| `HA_DISCOVERY_PREFIX` | *(Optional)* HA discovery topic prefix (default `homeassistant`) |

## Logging

Logs are written to both the console and `growatt.log` (daily rotation, 7 days retained). The default level is `WARNING`, which logs errors and warnings only. Set `LOGLEVEL=INFO` in `pvoutput.txt` to also see successful upload confirmations and scheduling messages.

## Usage

```
python src/growatt_mqtt.py
```

The script runs continuously, polling the inverter during the configured hours and sleeping overnight. Press `Ctrl+C` to exit.

### Dry-run / test mode

```
python src/growatt_mqtt.py --test
```

Reads the inverter but logs MQTT and PVOutput payloads at DEBUG level instead of sending them. Useful for verifying register mappings without affecting live systems.

## PVOutput Fields

| PVOutput field | Source |
|----------------|--------|
| v1 (Energy) | `wh_today` |
| v2 (Power) | `ac_power` |
| v5 (Temperature) | `temp` |
| v6 (Voltage) | `ac_volts` |
| v8 (Extended) | `pv_volts1` (DC voltage) |
| v9 (Extended) | `temp` (inverter temp) |
| v10 (Extended) | `wh_total` (lifetime energy) |
| v12 (Extended) | Efficiency % (`ac_power / pv_power * 100`) |

## Home Assistant Discovery

When `HA_DISCOVERY=true` (the default), the script publishes MQTT Discovery config payloads so Home Assistant automatically creates sensor entities grouped under a single **Growatt Solar Inverter** device. Discovery configs are published on every MQTT (re)connect, so HA picks them up after broker restarts too.

An availability topic (`<MQTTTOPIC>/availability`) is used with a Last Will and Testament (LWT) so sensors show as **unavailable** in HA when the script is not running.

To disable discovery, set `HA_DISCOVERY=false` in `pvoutput.txt`.

## MQTT Topics

All topics are published under the configured `MQTTTOPIC` prefix:

| Topic | Value |
|-------|-------|
| `<topic>/status` | Inverter status code |
| `<topic>/status_str` | Inverter status string |
| `<topic>/pv_power` | Total DC power from panels (W) |
| `<topic>/pv_volts1` | PV string 1 voltage (V) |
| `<topic>/pv_amps1` | PV string 1 current (A) |
| `<topic>/pv_power1` | PV string 1 power (W) |
| `<topic>/pv_volts2` | PV string 2 voltage (V) |
| `<topic>/pv_amps2` | PV string 2 current (A) |
| `<topic>/pv_power2` | PV string 2 power (W) |
| `<topic>/ac_power` | AC output power (W) |
| `<topic>/ac_volts` | AC output voltage (V) |
| `<topic>/ac_amps` | AC output current (A) |
| `<topic>/ac_frequency` | AC grid frequency (Hz) |
| `<topic>/wh_today` | Energy generated today (Wh) |
| `<topic>/wh_total` | Lifetime energy generated (Wh) |
| `<topic>/temp` | Inverter temperature (°C) |
| `<topic>/ipm_temp` | IPM module temperature (°C) |
| `<topic>/operation_hours` | Total operation hours |
| `<topic>/serial_no` | Inverter serial number |
| `<topic>/model_no` | Inverter model info |
