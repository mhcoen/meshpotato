# USB serial troubleshooting

[Back to the manual](manual.md#troubleshooting) | [Prepare the radio](manual.md#prepare-the-radio)

**"cannot open /dev/...: No such file or directory".** Either the `port`
setting does not match this computer, or the radio has not enumerated. The
message lists the serial ports it can see. If the radio is among them under
another name, set `port` in `config.toml`; port names differ between
machines even for the same radio, and on Linux your user must be in the
`dialout` group. If the radio is not listed at all, press its reset button,
or unplug it and plug it back in. After a Mac reboot or a macOS update the
Heltec Wireless Paper's USB bridge often does not come up until the radio
itself is reset, and `ls /dev/cu.*` shows nothing for it until then.

**The radio appears twice, as `cu.usbserial-XXXX` and `cu.SLAB_USBtoUART`.**
Two drivers are attached to the one CP210x bridge: Apple's built-in
`AppleUSBSLCOM` and the Silicon Labs VCP extension, which is redundant on any
recent macOS and a source of flaky serial behaviour when both are present.
`ioreg -l -w0 | grep -E 'IOUserServerName|IOTTYBaseName'` shows both
attached to the same device. Remove the Silicon Labs one: delete its app
(usually `CP210xVCPDriver` in `/Applications`), approve the removal of the
extension, reboot, and reset the radio. The bot uses the Apple node.

**"no response from a MeshCore companion" on a radio that was working.**
Boards with a CP2102 USB bridge and the usual ESP32 auto program circuit (the
Heltec Wireless Paper is one) can end up in the serial bootloader. pyserial
asserts DTR when it opens the port and the meshcore library then clears RTS,
which holds the chip's IO0 line low for as long as the port is open. The radio
keeps running, but if it resets for any reason while the port is open, a
brownout at full transmit power for instance, it comes back in the bootloader
and stays silent until it is power cycled. The bot releases both lines right
after opening the port, waits for a possible reboot, and retries the handshake
without reopening, so this should not happen while it runs. If it does, unplug
the radio, wait a few seconds, and plug it back in. To confirm the diagnosis,
`esptool --port <port> --before no-reset chip-id` connects immediately when
the chip is in the bootloader and fails when it is running MeshCore.
