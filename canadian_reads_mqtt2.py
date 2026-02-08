# -*- coding: utf-8 -*-
import sys
import requests
import paho.mqtt.publish as publish

from datetime import datetime
from pytz import timezone
from time import sleep, time
from configobj import ConfigObj
from pyowm import OWM
from pymodbus.client.sync import ModbusSerialClient as ModbusClient

def load_config(path="pvoutput.txt"):
    """Read settings from config file. Exits with a clear message on error."""
    required_keys = [
        'SYSTEMID', 'APIKEY', 'OWMKEY', 'Longitude', 'Latitude',
        'TimeZone', 'INVERTERPORT', 'MQTTUSER', 'MQTTPASS',
        'MQTTBROKER', 'MQTTPORT', 'MQTTTOPIC',
    ]
    try:
        config = ConfigObj(path, file_error=True)
    except IOError as e:
        print("Error: Could not read config file '{}': {}".format(path, e))
        sys.exit(1)

    missing = [k for k in required_keys if k not in config]
    if missing:
        print("Error: Missing config keys: {}".format(', '.join(missing)))
        sys.exit(1)

    return config


config = load_config()
SYSTEMID = config['SYSTEMID']
APIKEY = config['APIKEY']
OWMKey = config['OWMKEY']
OWMLon = float(config['Longitude'])
OWMLat = float(config['Latitude'])
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
            print('Error connecting to port')
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
            print('Error connecting to port')
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

        self.firmware = ''
        self.control_fw = ''
        self.model_no = ''
        self.serial_no = ''
        self.dtc = -1
        self._inv.close()  # close on error to reset serial state
        return False


class Weather(object):

    def __init__(self, API, lat, lon):
        self._API = API
        self._lat = float(lat)
        self._lon = float(lon)
        self._owm = OWM(self._API)

        self.temperature = 0.0
        self.cloud_pct = 0
        self.cmo_str = ''

    def get(self):
        obs = self._owm.weather_at_coords(self._lat, self._lon)
        w = obs.get_weather()
        status = w.get_detailed_status()
        self.temperature = w.get_temperature(unit='celsius')['temp']
        self.cloud_pct = w.get_clouds()
        self.cmo_str = ('%s with cloud coverage of %s percent' % (status, self.cloud_pct))


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
                    print("Only {} requests left, reset after {} seconds".format(
                        r.headers['X-Rate-Limit-Remaining'],
                        reset))
                if r.status_code == 403:
                    print("Forbidden: " + r.reason)
                    sleep(reset + 1)
                else:
                    r.raise_for_status()
                    break
            except requests.exceptions.HTTPError as errh:
                print(localnow().strftime('%Y-%m-%d %H:%M'), " Http Error:", errh)
            except requests.exceptions.ConnectionError as errc:
                print(localnow().strftime('%Y-%m-%d %H:%M'), "Error Connecting:", errc)
            except requests.exceptions.Timeout as errt:
                print(localnow().strftime('%Y-%m-%d %H:%M'), "Timeout Error:", errt)
            except requests.exceptions.RequestException as err:
                print(localnow().strftime('%Y-%m-%d %H:%M'), "OOps: Something Else", err)

            sleep(5)
        else:
            print(localnow().strftime('%Y-%m-%d %H:%M'),
                  "Failed to call PVOutput API after 3 attempts.")

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
        self.add_status(payload, system_id)


def main_loop():
    # init
    inv = Inverter(0x1, INVERTERPORT)
    inv.connect()
    inv.version()
    if OWMKey:
        owm = Weather(OWMKey, OWMLat, OWMLon)
        owm.fresh = False
    else:
        owm = False

    pvo = PVOutputAPI(APIKEY, SYSTEMID)

    # start and stop monitoring (hour of the day)
    shStart = 5
    shStop = 21

    WEATHER_INTERVAL = 600  # fetch weather every 10 minutes
    last_weather_fetch = 0

    PVOUTPUT_INTERVAL = 300  # upload to PVOutput every 5 minutes
    last_pvoutput_upload = 0

    # Loop until end of universe
    while True:
        # print(inv.status, inv.firmware, inv.control_fw, inv.model_no, inv.pv_power, inv.pv_volts, inv.ac_volts, inv.wh_today, inv.wh_total)
        if shStart <= localnow().hour < shStop:
            # get fresh temperature from OWM (throttled)
            if owm and (time() - last_weather_fetch >= WEATHER_INTERVAL):
                try:
                    owm.get()
                    owm.fresh = True
                except Exception as e:
                    print('Error getting weather: {}'.format(e))
                    owm.fresh = False
                last_weather_fetch = time()

            # get readings from inverter, if success send  to pvoutput
            inv.read_inputs()
            if inv.status != -1:

                # temperature report only if available
                temp = owm.temperature if owm and owm.fresh else None

                # Upload to PVOutput every 5 minutes
                if time() - last_pvoutput_upload >= PVOUTPUT_INTERVAL:
                    pvo.send_status(date=inv.date, energy_gen=inv.wh_today,
                                    power_gen=inv.ac_power, vdc=inv.pv_volts,
                                    vac=inv.ac_volts, temp=temp,
                                    temp_inv=inv.temp, energy_life=inv.wh_total,
                                    power_vdc=inv.pv_power)
                    last_pvoutput_upload = time()
                    print("PVOutput updated successfully.")

                msgs = [
                    { 'topic': f"{MQTTTOPIC}/status", 'payload': str(inv.status) },
                    { 'topic': f"{MQTTTOPIC}/pv_power", 'payload': str(inv.pv_power) },
                    { 'topic': f"{MQTTTOPIC}/pv_volts", 'payload': str(inv.pv_volts) },
                    { 'topic': f"{MQTTTOPIC}/ac_power", 'payload': str(inv.ac_power) },
                    { 'topic': f"{MQTTTOPIC}/ac_volts", 'payload': str(inv.ac_volts) },
                    { 'topic': f"{MQTTTOPIC}/wh_today", 'payload': str(inv.wh_today) },
                    { 'topic': f"{MQTTTOPIC}/wh_total", 'payload': str(inv.wh_total) },
                    { 'topic': f"{MQTTTOPIC}/temp", 'payload': str(temp) if temp is not None else '' },
                    { 'topic': f"{MQTTTOPIC}/serial_no", 'payload': inv.serial_no },
                    { 'topic': f"{MQTTTOPIC}/model_no", 'payload': inv.model_no }
                ]

                try:
                    publish.multiple(msgs, hostname=MQTTBROKER, port=MQTTPORT,
                        auth={'username': MQTTUSER, 'password': MQTTPASS})
                    print("Message published successfully.")
                except Exception as e:
                    print(f"Error publishing message: {e}")

                # sleep until next multiple of 1 minutes
                sleep(60 - localnow().second)

            else:
                # some error
                sleep(60)  # 1 minute before try again
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
            print(localnow().strftime('%Y-%m-%d %H:%M') + ' - Next shift starts in ' + \
                str(snooze) + ' minutes')
            sys.stdout.flush()
            snooze = snooze * 60  # seconds
            sleep(snooze)


if __name__ == '__main__':
    try:
        main_loop()
    except KeyboardInterrupt:
        print('\nExiting by user request.\n', file=sys.stderr)
        sys.exit(0)

