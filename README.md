# Canadian Solar / Growatt Inverter Monitor

Reads data from a Canadian Solar or Growatt inverter via Modbus RTU (RS485) and uploads it to [PVOutput](https://pvoutput.org) and an MQTT broker.

## Features

- Polls inverter input registers every 60 seconds for live power/energy data
- Uploads status to PVOutput every 5 minutes
- Publishes readings to an MQTT broker for integration with Home Assistant or similar
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

## Usage

```
python canadian_reads_mqtt2.py
```

The script runs continuously, polling the inverter during the configured hours and sleeping overnight. Press `Ctrl+C` to exit.

## MQTT Topics

All topics are published under the configured `MQTTTOPIC` prefix:

| Topic | Value |
|-------|-------|
| `<topic>/status` | Inverter status code |
| `<topic>/pv_power` | DC power from panels (W) |
| `<topic>/pv_volts` | DC voltage from panels (V) |
| `<topic>/ac_power` | AC output power (W) |
| `<topic>/ac_volts` | AC output voltage (V) |
| `<topic>/wh_today` | Energy generated today (Wh) |
| `<topic>/wh_total` | Lifetime energy generated (Wh) |
| `<topic>/temp` | Inverter temperature (C) |
| `<topic>/serial_no` | Inverter serial number |
| `<topic>/model_no` | Inverter model info |
