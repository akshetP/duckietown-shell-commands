TIPS_AND_TRICKS = """

## Tips and tricks

### Multiple networks

    dts init_sd_card --wifi network1:password1,network2:password2 --country US



### Steps

Without arguments the script performs the steps:

    license
    download
    flash
    setup

You can use --steps to run only some of those:

    dts init_sd_card --steps flash,setup

You can use --no-steps to exclude some steps:

    dts init_sd_card --no-steps download


"""

LIST_DEVICES_CMD = "lsblk -p --output NAME,TYPE,SIZE,VENDOR | grep --color=never 'disk\|TYPE'"

WPA_OPEN_NETWORK_CONFIG = """
network={{
  id_str="{cname}"
  ssid="{ssid}"
  key_mgmt=NONE
}}
"""

WPA_PSK_NETWORK_CONFIG = """
network={{
  id_str="{cname}"
  ssid="{ssid}"
  psk="{psk}"
  key_mgmt=WPA-PSK
}}
"""

WPA_EAP_NETWORK_CONFIG = """
network={{
    id_str="{cname}"
    ssid="{ssid}"
    key_mgmt=WPA-EAP
    group=CCMP TKIP
    pairwise=CCMP TKIP
    eap=PEAP
    proto=RSN
    identity="{username}"
    password="{password}"
    phase1="peaplabel=0"
    phase2="auth=MSCHAPV2"
    priority=1
}}
"""
