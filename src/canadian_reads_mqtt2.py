# -*- coding: utf-8 -*-
import sys
import json
import argparse
import logging
import requests
import paho.mqtt.client as mqtt
from pathlib import Path
from paho.mqtt.client import CallbackAPIVersion

from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pytz import timezone
from time import sleep, time
from configobj import ConfigObj
from pymodbus.client.sync import ModbusSerialClient as ModbusClient

from const import (
    FAULTCODES,
    STATUSCODES,
    WARNINGCODES,
)

# Repo root is the parent of this file's directory (src/)
_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Command-line arguments
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser(description='Growatt inverter monitor')
_parser.add_argument('--test', action='store_true',
                     help='Test/dry-run mode: log MQTT and PVOutput data to terminal '
                          'at DEBUG level instead of sending it')
args = _parser.parse_args()

# Set up logging (console + daily rotating file)
logger = logging.getLogger('pvoutput')
logger.setLevel(logging.DEBUG if args.test else logging.WARNING)
_fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
_fh = TimedRotatingFileHandler(_ROOT / 'growatt.log', when='midnight', backupCount=7)
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_ch)

TEST_MODE = args.test
if TEST_MODE:
    logger.info('*** TEST MODE active – MQTT and PVOutput calls will be skipped ***')

def load_config(path=_ROOT / "pvoutput.txt"):
    """Read settings from config file. Exits with a clear message on error."""
    required_keys = [
        'SYSTEMID', 'APIKEY', 'TimeZone', 'INVERTERPORT',
        'MQTTUSER', 'MQTTPASS',
        'MQTTBROKER', 'MQTTPORT', 'MQTTTOPIC',
    ]
    try:
        config = ConfigObj(path, file_error=True)
    except IOError as e:
        logger.error("Could not read config file '%s': %s", path, e)
        sys.exit(1)

    missing = [k for k in required_keys if k not in config]
    if missing:
        logger.error("Missing config keys: %s", ', '.join(missing))
        sys.exit(1)

    return config


config = load_config()

# Override log level from config (optional, default WARNING)
# --test flag forces DEBUG and takes precedence over config
if TEST_MODE:
    logger.setLevel(logging.DEBUG)
elif 'LOGLEVEL' in config:
    _level = getattr(logging, config['LOGLEVEL'].upper(), None)
    if _level is not None:
        logger.setLevel(_level)
    else:
        logger.warning("Invalid LOGLEVEL '%s' in config, using WARNING", config['LOGLEVEL'])

SYSTEMID = config['SYSTEMID']
APIKEY = config['APIKEY']
LocalTZ = timezone(config['TimeZone'])

INVERTERPORT = config['INVERTERPORT']

# MQTT broker details
MQTTUSER = config['MQTTUSER']
MQTTPASS = config['MQTTPASS']
MQTTBROKER = config['MQTTBROKER']
MQTTPORT = config.as_int('MQTTPORT')
MQTTTOPIC = config['MQTTTOPIC']

# Home Assistant MQTT Discovery (optional, default enabled)
HA_DISCOVERY = config.get('HA_DISCOVERY', 'true').lower() in ('true', '1', 'yes')
HA_DISCOVERY_PREFIX = config.get('HA_DISCOVERY_PREFIX', 'homeassistant')

# Sensor definitions for MQTT publishing and HA discovery
# (object_id, name, unit, device_class, state_class, icon, entity_category)
SENSORS = [
    ("pv_power",    "PV Power",          "W",   "power",       "measurement",      "mdi:solar-power-variant", None),
    ("pv_volts1",   "PV1 Voltage",       "V",   "voltage",     "measurement",      "mdi:solar-panel",         None),
    ("pv_amps1",    "PV1 Current",       "A",   "current",     "measurement",      "mdi:current-dc",          None),
    ("pv_power1",   "PV1 Power",         "W",   "power",       "measurement",      "mdi:solar-panel",         None),
    ("pv_volts2",   "PV2 Voltage",       "V",   "voltage",     "measurement",      "mdi:solar-panel",         None),
    ("pv_amps2",    "PV2 Current",       "A",   "current",     "measurement",      "mdi:current-dc",          None),
    ("pv_power2",   "PV2 Power",         "W",   "power",       "measurement",      "mdi:solar-panel",         None),
    ("ac_power",    "AC Power",          "W",   "power",       "measurement",      "mdi:home-lightning-bolt",  None),
    ("ac_volts",    "AC Voltage",        "V",   "voltage",     "measurement",      "mdi:transmission-tower",   None),
    ("ac_amps",     "AC Current",        "A",   "current",     "measurement",      "mdi:current-ac",          None),
    ("ac_frequency","AC Frequency",      "Hz",  "frequency",   "measurement",      "mdi:sine-wave",           None),
    ("wh_today",    "Energy Today",      "Wh",  "energy",      "total_increasing",  "mdi:white-balance-sunny", None),
    ("wh_total",    "Energy Total",      "Wh",  "energy",      "total_increasing",  "mdi:lightning-bolt",      None),
    ("temp",        "Temperature",       "\u00b0C",  "temperature", "measurement",      "mdi:thermometer",         None),
    ("ipm_temp",    "IPM Temperature",   "\u00b0C",  "temperature", "measurement",      "mdi:thermometer-high",    None),
    ("operation_hours", "Operation Hours","h",   "duration",    "total_increasing",  "mdi:clock-outline",       None),
    ("status_str",      "Status",            None,  None,          None,               "mdi:solar-power",         "diagnostic"),
    ("serial_no",   "Serial Number",     None,  None,          None,               "mdi:identifier",          "diagnostic"),
    ("model_no",    "Model",             None,  None,          None,               "mdi:information-outline",  "diagnostic"),
]

# Local time with timezone
def localnow():
    return datetime.now(tz=LocalTZ)


class Inverter(object):

    def __init__(self, address, port):
        """Return a Inverter object with port set to *port* and
        values set to their initial state."""
        self._inv = ModbusClient(method='rtu', port=port, baudrate=9600, stopbits=1,
                                 parity='N', bytesize=8, timeout=1)
        self._unit = address

        # Inverter properties
        self.date = timezone('UTC').localize(datetime(1970, 1, 1, 0, 0, 0))
        self.status = -1
        self.pv_power_total = 0.0
        self.pv_power1 = 0.0
        self.pv_volts1 = 0.0
        self.pv_amps1 = 0.0
        self.pv_power2 = 0.0
        self.pv_volts2 = 0.0
        self.pv_amps2 = 0.0
        self.ac_volts = 0.0
        self.ac_power = 0.0
        self.ac_amps = 0.0
        self.ac_frequency = 0.0
        self.wh_today = 0
        self.wh_total = 0
        self.temp = 0.0
        self.ipm_temp = 0.0
        self.firmware = ''
        self.control_fw = ''
        self.model_no = ''
        self.serial_no = ''
        self.dtc = -1
        self.status_str = ''
        self.operation_hours = 0.0

    def connect(self):
        """Connect to the inverter. Returns True if successful."""
        return self._inv.connect()

    def close(self):
        """Close the connection to the inverter."""
        self._inv.close()

    def read_inputs(self):
        """Try read input properties from inverter, return true if succeed"""
        if not self._inv.connect():
            logger.error('Modbus: failed to connect to serial port %s', self._inv.port)
            return False

        # by default read first 45 registers (from 0 to 44)
        # they contain all basic information needed to report
        rr = self._inv.read_input_registers(0, 45, unit=self._unit)
        if not rr.isError() and len(rr.registers) >= 45:
            self.date = localnow()

            self.status = rr.registers[0]
            if self.status != -1:
                self.status_str = STATUSCODES[self.status]                        
            # my setup will never use high nibble but I will code it anyway
            self.pv_power_total = self._rsdf(rr.registers, 1)

            self.pv_volts1 = self._rssf(rr.registers, 3)
            self.pv_amps1 = self._rssf(rr.registers, 4)
            self.pv_power1 = self._rsdf(rr.registers, 5)

            self.pv_volts2 = self._rssf(rr.registers, 7)
            self.pv_amps2 = self._rssf(rr.registers, 8)
            self.pv_power2 = self._rsdf(rr.registers, 9)

            self.ac_power = self._rsdf(rr.registers, 11)
            self.ac_volts = self._rssf(rr.registers, 14)
            self.ac_amps = self._rssf(rr.registers, 15)
            self.ac_frequency = self._rssf(rr.registers, 13, 100)

            self.wh_today = self._rsdf(rr.registers, 26, 0.01)
            self.wh_total = self._rsdf(rr.registers, 28, 0.01)
            self.operation_hours = self._rsdf(rr.registers, 30, 7200)
            self.temp = self._rssf(rr.registers, 32)
            self.ipm_temp = self._rssf(rr.registers, 41)

            return True

        logger.warning('Modbus: input register read error (got %d registers, expected 45)',
                       len(rr.registers) if hasattr(rr, 'registers') else 0)
        self.status = -1
        self._inv.close()  # close on error to reset serial state
        return False

    @staticmethod
    def _decode_registers(registers, start, count):
        """Decode a sequence of 16-bit registers into a string (2 chars per register)."""
        return ''.join(
            chr(registers[i] >> 8) + chr(registers[i] & 0xFF)
            for i in range(start, start + count)
        )

    @staticmethod
    def _rssf(registers, index, scale=10):
        """Read and scale single to float."""
        return float(registers[index]) / scale

    @staticmethod
    def _rsdf(registers, index, scale=10):
        """Read and scale double to float."""
        return float((registers[index] << 16) + registers[index + 1]) / scale

    def version(self):
        """Read firmware version"""
        if not self._inv.connect():
            logger.error('Modbus: failed to connect to serial port %s', self._inv.port)
            return False

        # by default read first 45 holding registers (from 0 to 44)
        # they contain more than needed data
        rr = self._inv.read_holding_registers(0, 45, unit=self._unit)
        if not rr.isError() and len(rr.registers) >= 45:
            # returns G.1.8 on my unit
            self.firmware = self._decode_registers(rr.registers, 9, 3)

            # does not return any interesting thing on my model
            self.control_fw = self._decode_registers(rr.registers, 12, 3)

            # does match the label in the unit
            self.serial_no = self._decode_registers(rr.registers, 23, 5)

            # as per Growatt protocol
            mo = (rr.registers[28] << 16) + rr.registers[29]
            self.model_no = (
                'T' + str((mo & 0XF00000) >> 20) + ' Q' + str((mo & 0X0F0000) >> 16) +
                ' P' + str((mo & 0X00F000) >> 12) + ' U' + str((mo & 0X000F00) >> 8) +
                ' M' + str((mo & 0X0000F0) >> 4) + ' S' + str((mo & 0X00000F))
            )

            # 134 for my unit meaning single phase/single tracker inverter
            self.dtc = rr.registers[43]
            return True

        logger.warning('Modbus: holding register read error (got %d registers, expected 45)',
                       len(rr.registers) if hasattr(rr, 'registers') else 0)
        self.firmware = ''
        self.control_fw = ''
        self.model_no = ''
        self.serial_no = ''
        self.dtc = -1
        self._inv.close()  # close on error to reset serial state
        return False


class PVOutputAPI(object):

    _BASE_URL = "https://pvoutput.org/service/r2/"

    def __init__(self, API, system_id=None):
        self._API = API
        self._systemID = system_id
        self._wh_today_last = 0

    def add_status(self, payload, system_id=None):
        """Add live output data. Data should contain the parameters as described
        here: http://pvoutput.org/help.html#api-addstatus ."""
        sys_id = system_id if system_id is not None else self._systemID
        self.__call(self._BASE_URL + "addstatus.jsp", payload, sys_id)

    def add_output(self, payload, system_id=None):
        """Add end of day output information. Data should be a dictionary with
        parameters as described here: http://pvoutput.org/help.html#api-addoutput ."""
        sys_id = system_id if system_id is not None else self._systemID
        self.__call(self._BASE_URL + "addoutput.jsp", payload, sys_id)

    def __call(self, url, payload, system_id=None):
        headers = {
            'X-Pvoutput-Apikey': self._API,
            'X-Pvoutput-SystemId': system_id,
            'X-Rate-Limit': '1'
        }

        # Make three attempts
        for _ in range(3):
            try:
                r = requests.post(url, headers=headers, data=payload, timeout=10)
                reset = round(float(r.headers['X-Rate-Limit-Reset']) - time())
                if int(r.headers['X-Rate-Limit-Remaining']) < 10:
                    logger.warning("PVOutput: only %s requests left, reset after %s seconds",
                                   r.headers['X-Rate-Limit-Remaining'], reset)
                if r.status_code == 403:
                    logger.warning('PVOutput HTTP %d: %s', r.status_code, r.reason)
                    sleep(reset + 1)
                else:
                    r.raise_for_status()
                    break
            except requests.exceptions.HTTPError as errh:
                logger.error('PVOutput HTTP %d: %s', r.status_code, errh)
            except requests.exceptions.ConnectionError as errc:
                logger.error('PVOutput connection error: %s', errc)
            except requests.exceptions.Timeout as errt:
                logger.error('PVOutput timeout: %s', errt)
            except requests.exceptions.RequestException as err:
                logger.error('PVOutput request error: %s', err)

            sleep(5)
        else:
            logger.error('PVOutput API failed after 3 attempts')

    def send_status(self, date, energy_gen=None, power_gen=None, energy_imp=None,
                    power_imp=None, temp=None, vdc=None, cumulative=False, vac=None,
                    temp_inv=None, energy_life=None, comments=None, power_vdc=None,
                    system_id=None):
        # format status payload
        payload = {
            'd': date.strftime('%Y%m%d'),
            't': date.strftime('%H:%M'),
        }

        # Only report total energy if it has changed since last upload
        # this trick avoids avg power to zero with inverter that reports
        # generation in 100 watts increments (Growatt and Canadian solar)
        if ((energy_gen is not None) and (self._wh_today_last != energy_gen)):
            self._wh_today_last = int(energy_gen)
            payload['v1'] = int(energy_gen)

        if power_gen is not None:
            payload['v2'] = float(power_gen)
        if energy_imp is not None:
            payload['v3'] = int(energy_imp)
        if power_imp is not None:
            payload['v4'] = float(power_imp)
        if temp is not None:
            payload['v5'] = float(temp)
        if vdc is not None:
            payload['v8'] = float(vdc)
        if cumulative is True:
            payload['c1'] = 1
        else:
            payload['c1'] = 0
        if vac is not None:
            payload['v6'] = float(vac)
        if temp_inv is not None:
            payload['v9'] = float(temp_inv)
        if energy_life is not None:
            payload['v10'] = int(energy_life)
        if comments is not None:
            payload['m1'] = str(comments)[:30]
        # calculate efficiency
        if ((power_vdc is not None) and (power_vdc > 0) and (power_gen is not None)):
            payload['v12'] = float(power_gen) / float(power_vdc) * 100

        # Send status
        if TEST_MODE:
            logger.debug('PVOutput payload (not sent): %s', payload)
        else:
            self.add_status(payload, system_id)


def publish_ha_discovery(client, inv):
    """Publish HA MQTT Discovery configs for all sensors (retained)."""
    device = {
        "identifiers": [f"growatt_{inv.serial_no}"],
        "name": "Growatt Solar Inverter",
        "manufacturer": "Growatt",
        "model": inv.model_no,
        "sw_version": inv.firmware,
    }

    for obj_id, name, unit, dev_cls, state_cls, icon, ent_cat in SENSORS:
        config = {
            "name": name,
            "state_topic": f"{MQTTTOPIC}/{obj_id}",
            "unique_id": f"growatt_{inv.serial_no}_{obj_id}",
            "device": device,
            "availability_topic": f"{MQTTTOPIC}/availability",
            "icon": icon,
        }
        if unit:
            config["unit_of_measurement"] = unit
        if dev_cls:
            config["device_class"] = dev_cls
        if state_cls:
            config["state_class"] = state_cls
        if ent_cat:
            config["entity_category"] = ent_cat

        topic = f"{HA_DISCOVERY_PREFIX}/sensor/{inv.serial_no}/{obj_id}/config"
        client.publish(topic, json.dumps(config), retain=True)

    client.publish(f"{MQTTTOPIC}/availability", "online", retain=True)
    logger.info('HA discovery configs published (%d sensors)', len(SENSORS))


def main_loop():
    # init
    inv = Inverter(0x1, INVERTERPORT)
    inv.connect()
    inv.version()

    pvo = PVOutputAPI(APIKEY, SYSTEMID)

    # --- Persistent MQTT client ---
    mqtt_client = None
    if not TEST_MODE:
        def on_connect(client, userdata, flags, reason_code, properties):
            if reason_code == 0:
                logger.info('MQTT connected to %s:%d', MQTTBROKER, MQTTPORT)
                if HA_DISCOVERY:
                    publish_ha_discovery(client, inv)
            else:
                logger.error('MQTT connection failed (rc=%s)', reason_code)

        def on_disconnect(client, userdata, flags, reason_code, properties):
            if reason_code != 0:
                logger.warning('MQTT unexpected disconnect (rc=%s), will auto-reconnect', reason_code)

        mqtt_client = mqtt.Client(CallbackAPIVersion.VERSION2)
        mqtt_client.username_pw_set(MQTTUSER, MQTTPASS)
        mqtt_client.will_set(f"{MQTTTOPIC}/availability", "offline", retain=True)
        mqtt_client.on_connect = on_connect
        mqtt_client.on_disconnect = on_disconnect
        mqtt_client.connect(MQTTBROKER, MQTTPORT)
        mqtt_client.loop_start()
    else:
        # Log what discovery would do in test mode
        if HA_DISCOVERY:
            for obj_id, name, unit, dev_cls, state_cls, icon, ent_cat in SENSORS:
                topic = f"{HA_DISCOVERY_PREFIX}/sensor/{inv.serial_no}/{obj_id}/config"
                logger.debug('HA discovery (not sent): %s -> %s', topic, name)

    # start and stop monitoring (hour of the day)
    shStart = 5
    shStop = 21

    POLL_INTERVAL = 10  # seconds between inverter reads / MQTT publishes
    last_pvo_minute = -1  # guard against duplicate PVOutput uploads

    # Loop until end of universe
    try:
        while True:
            if shStart <= localnow().hour < shStop:
                # get readings from inverter, if success send to pvoutput
                inv.read_inputs()
                if inv.status != -1:

                    # Upload to PVOutput on 5-minute clock boundaries (once per slot)
                    now = localnow()
                    if now.minute % 5 == 0 and now.minute != last_pvo_minute:
                        pvo.send_status(date=inv.date, energy_gen=inv.wh_today,
                                        power_gen=inv.ac_power, vdc=inv.pv_volts1,
                                        vac=inv.ac_volts, temp_inv=inv.temp,
                                        energy_life=inv.wh_total,
                                        power_vdc=inv.pv_power_total)
                        last_pvo_minute = now.minute
                        logger.info('PVOutput updated successfully')

                    # Build state messages from inverter readings
                    state_values = {
                        'status': inv.status_str,
                        'pv_power': str(inv.pv_power_total),
                        'pv_volts1': str(inv.pv_volts1),
                        'pv_amps1': str(inv.pv_amps1),
                        'pv_power1': str(inv.pv_power1),
                        'pv_volts2': str(inv.pv_volts2),
                        'pv_amps2': str(inv.pv_amps2),
                        'pv_power2': str(inv.pv_power2),
                        'ac_power': str(inv.ac_power),
                        'ac_volts': str(inv.ac_volts),
                        'ac_amps': str(inv.ac_amps),
                        'ac_frequency': str(inv.ac_frequency),
                        'wh_today': str(inv.wh_today),
                        'wh_total': str(inv.wh_total),
                        'temp': str(inv.temp),
                        'ipm_temp': str(inv.ipm_temp),
                        'operation_hours': str(inv.operation_hours),
                        'serial_no': inv.serial_no,
                        'model_no': inv.model_no,
                    }

                    if TEST_MODE:
                        for key, val in state_values.items():
                            logger.debug('MQTT (not sent): %s/%s = %s', MQTTTOPIC, key, val)
                    else:
                        try:
                            for key, val in state_values.items():
                                mqtt_client.publish(f"{MQTTTOPIC}/{key}", val)
                            logger.info('MQTT published successfully')
                        except Exception as e:
                            logger.error('MQTT publish failed: %s', e)

                    sleep(POLL_INTERVAL)

                else:
                    # some error
                    sleep(POLL_INTERVAL)
            else:
                # it is too late or too early, let's sleep until next shift
                hour = localnow().hour
                minute = localnow().minute
                if 24 > hour >= shStop:
                    # before midnight
                    snooze = (((shStart - hour) + 24) * 60) - minute
                elif shStart > hour >= 0:
                    # after midnight
                    snooze = ((shStart - hour) * 60) - minute
                else:
                    snooze = 1  # fallback: recheck in 1 minute
                logger.info('Next shift starts in %d minutes', snooze)
                snooze = snooze * 60  # seconds
                sleep(snooze)
    finally:
        if mqtt_client is not None:
            mqtt_client.publish(f"{MQTTTOPIC}/availability", "offline", retain=True)
            mqtt_client.disconnect()
            mqtt_client.loop_stop()
            logger.info('MQTT disconnected cleanly')


if __name__ == '__main__':
    try:
        main_loop()
    except KeyboardInterrupt:
        print('\nExiting by user request.\n', file=sys.stderr)
        sys.exit(0)

