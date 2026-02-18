# Codes
STATUSCODES = {0: "Waiting", 1: "Normal", 3: "Fault"}

FAULTCODES = {
    0: "None",
    24: "Auto Test Failed",
    25: "No AC Connection",
    26: "PV Isolation Low",
    27: "Residual I High",
    28: "Output High DCI",
    29: "PV Voltage High",
    30: "AC V Outrange",
    31: "AC F Outrange",
    32: "Module Hot",
}
for i in range(1, 24):
    FAULTCODES[i] = "Generic Error Code: %s" % str(99 + i)

WARNINGCODES = {
    0x0000: "None",
    0x0001: "Fan warning",
    0x0002: "String communication abnormal",
    0x0004: "StrPID config Warning",
    0x0008: "Fail to read EEPROM",
    0x0010: "DSP and COM firmware unmatch",
    0x0020: "Fail to write EEPROM",
    0x0040: "SPD abnormal",
    0x0080: "GND and N connect abnormal",
    0x0100: "PV1 or PV2 circuit short",
    0x0200: "PV1 or PV2 boost driver broken",
    0x0400: "",
    0x0800: "",
    0x1000: "",
    0x2000: "",
    0x4000: "",
    0x8000: "",
}
