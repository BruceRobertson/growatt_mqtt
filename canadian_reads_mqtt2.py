# -*- coding: utf-8 -*-
import sys
import argparse
import logging
import requests
import paho.mqtt.publish as publish

from datetime import datetime
from logging.handlers import TimedRotatingFileHandler
from pytz import timezone
from time import sleep, time
from configobj import ConfigObj
from pymodbus.client.sync import ModbusSerialClient as ModbusClient

# ---------------------------------------------------------------------------
# Command-line arguments
# ---------------------------------------------------------------------------
_parser = argparse.ArgumentParser(description='Canadian Solar inverter monitor')
_parser.add_argument('--test', action='store_true',
                     help='Test/dry-run mode: log MQTT and PVOutput data to terminal '
                          'at DEBUG level instead of sending it')
args = _parser.parse_args()

# Set up logging (console + daily rotating file)
logger = logging.getLogger('pvoutput')
logger.setLevel(logging.DEBUG if args.test else logging.WARNING)
_fmt = logging.Formatter('%(asctime)s %(levelname)s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
_fh = TimedRotatingFileHandler('canadianSolar.log', when='midnight', backupCount=7)
_fh.setFormatter(_fmt)
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
logger.addHandler(_fh)
logger.addHandler(_ch)

TEST_MODE = args.test
if TEST_MODE:
    logger.info('*** TEST MODE active – MQTT and PVOutput calls will be skipped ***')

def load_config(path="pvoutput.txt"):
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
        self.pv_power = 0.0
        self.pv_volts = 0.0
        self.ac_volts = 0.0
        self.ac_power = 0.0
        self.wh_today = 0
        self.wh_total = 0
        self.temp = 0.0
        self.firmware = ''
        self.control_fw = ''
        self.model_no = ''
        self.serial_no = ''
        self.dtc = -1
        self.cmo_str = ''

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
                self.cmo_str = 'Status: '+str(self.status)
            # my setup will never use high nibble but I will code it anyway
            self.pv_power = float((rr.registers[1] << 16)+rr.registers[2])/10
            self.pv_volts = float(rr.registers[3])/10
            self.ac_power = float((rr.registers[11] << 16)+rr.registers[12])/10
            self.ac_volts = float(rr.registers[14])/10
            self.wh_today = float((rr.registers[26] << 16)+rr.registers[27])*100
            self.wh_total = float((rr.registers[28] << 16)+rr.registers[29])*100
            self.temp = float(rr.registers[32])/10
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
            payload['v6'] = float(vdc)
        if cumulative is True:
            payload['c1'] = 1
        else:
            payload['c1'] = 0
#        if vac is not None:
#            payload['v8'] = float(vac)
#        if temp_inv is not None:
#            payload['v9'] = float(temp_inv)
        if energy_life is not None:
            payload['v10'] = int(energy_life)
        if comments is not None:
            payload['m1'] = str(comments)[:30]
        # calculate efficiency
        if ((power_vdc is not None) and (power_vdc > 0) and (power_gen is not None)):
            payload['v12'] = float(power_gen) / float(power_vdc)

        # Send status
        if TEST_MODE:
            logger.debug('PVOutput payload (not sent): %s', payload)
        else:
            self.add_status(payload, system_id)


def main_loop():
    # init
    inv = Inverter(0x1, INVERTERPORT)
    inv.connect()
    inv.version()

    pvo = PVOutputAPI(APIKEY, SYSTEMID)

    # start and stop monitoring (hour of the day)
    shStart = 5
    shStop = 21

    POLL_INTERVAL = 10  # seconds between inverter reads / MQTT publishes
    last_pvo_minute = -1  # guard against duplicate PVOutput uploads

    # Loop until end of universe
    while True:
        if shStart <= localnow().hour < shStop:
            # get readings from inverter, if success send to pvoutput
            inv.read_inputs()
            if inv.status != -1:

                # Upload to PVOutput on 5-minute clock boundaries (once per slot)
                now = localnow()
                if now.minute % 5 == 0 and now.minute != last_pvo_minute:
                    pvo.send_status(date=inv.date, energy_gen=inv.wh_today,
                                    power_gen=inv.ac_power, vdc=inv.pv_volts,
                                    vac=inv.ac_volts, temp_inv=inv.temp,
                                    energy_life=inv.wh_total,
                                    power_vdc=inv.pv_power)
                    last_pvo_minute = now.minute
                    logger.info('PVOutput updated successfully')

                msgs = [
                    { 'topic': f"{MQTTTOPIC}/status", 'payload': str(inv.status) },
                    { 'topic': f"{MQTTTOPIC}/pv_power", 'payload': str(inv.pv_power) },
                    { 'topic': f"{MQTTTOPIC}/pv_volts", 'payload': str(inv.pv_volts) },
                    { 'topic': f"{MQTTTOPIC}/ac_power", 'payload': str(inv.ac_power) },
                    { 'topic': f"{MQTTTOPIC}/ac_volts", 'payload': str(inv.ac_volts) },
                    { 'topic': f"{MQTTTOPIC}/wh_today", 'payload': str(inv.wh_today) },
                    { 'topic': f"{MQTTTOPIC}/wh_total", 'payload': str(inv.wh_total) },
                    { 'topic': f"{MQTTTOPIC}/temp", 'payload': str(inv.temp) },
                    { 'topic': f"{MQTTTOPIC}/serial_no", 'payload': inv.serial_no },
                    { 'topic': f"{MQTTTOPIC}/model_no", 'payload': inv.model_no }
                ]

                if TEST_MODE:
                    for m in msgs:
                        logger.debug('MQTT (not sent): %s = %s', m['topic'], m['payload'])
                else:
                    try:
                        publish.multiple(msgs, hostname=MQTTBROKER, port=MQTTPORT,
                            auth={'username': MQTTUSER, 'password': MQTTPASS})
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


if __name__ == '__main__':
    try:
        main_loop()
    except KeyboardInterrupt:
        print('\nExiting by user request.\n', file=sys.stderr)
        sys.exit(0)

