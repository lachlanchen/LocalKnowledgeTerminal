# Voice hardware direction

Voice is a future input adapter, not a dependency of card generation. The first
hardware prototype should use **Raspberry Pi Codec Zero**. It is an official HAT
for any Raspberry Pi with a 40-pin header, has a built-in MEMS microphone, a
DA7212 I2S codec, HAT EEPROM auto-configuration, an external microphone input,
and a 1.2 W mono speaker output. That makes it the shortest supported route to
both capture and response audio on the Pi 5.

Official references:

- <https://www.raspberrypi.com/products/codec-zero/>
- <https://www.raspberrypi.com/documentation/accessories/audio.html>
- <https://www.raspberrypi.com/documentation/computers/io-controllers.html>

## Decision ladder

1. **First prototype: Codec Zero.** Prefer the official, EEPROM-configured
   board. Validate capture before adding speech recognition.
2. **Fast diagnostic fallback: USB Audio Class microphone.** It is physically
   inelegant but isolates software from GPIO/I2S overlay problems and is useful
   when debugging a headless build.
3. **Compact product experiment: bare I2S/PDM MEMS.** An INMP441-style breakout
   is inexpensive and compact, while RP1 exposes I2S and PDM microphone
   hardware. It still requires a verified device-tree/ALSA path and should not
   replace the working prototype merely to save board area.
4. **Do not make ReSpeaker the baseline.** The public Seeed voice-card driver
   lists older Pi families and has unresolved Pi 5 reports. A microphone array
   can be revisited when an upstream Pi 5 path is maintained and testable.

No audio configuration is changed until hardware is attached. In particular,
do not pre-emptively edit `/boot/firmware/config.txt`, create `~/.asoundrc`, or
disable the working desktop audio route.

## Staged software path

```text
ALSA/PipeWire capture
        │
        ▼
short WAV acceptance sample
        │
        ▼
VAD and bounded utterance capture
        │
        ▼
local speech-to-text adapter
        │
        ▼
existing LKT mode/query API
        │
        ├──► saved multilingual card
        └──► optional local speech output
```

Each arrow is a test boundary. Audio code will call the existing API and must
not be imported by `corpus`, `retrieval`, `llm`, or `service`.

## Hardware acceptance sequence

After installing the board and rebooting:

```bash
./scripts/probe_audio.sh
./scripts/probe_audio.sh --capture /tmp/lkt-mic-test.wav 5
```

The first command is read-only. The second records a bounded five-second WAV,
prints its metadata, and never changes the system mixer. Only after that sample
is intelligible should the official Codec Zero mixer state be selected and made
persistent according to Raspberry Pi's current documentation.

