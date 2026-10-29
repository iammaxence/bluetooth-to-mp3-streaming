# Bluetooth to MP3 Streaming

Send phone audio to a USB MP3 player through a Raspberry Pi Zero W:

~~~text
Phone → Bluetooth → PCM engine → LAME MP3 → streamfs → USB MP3 player
~~~

The playback engine used on the project's Pi is now the default implementation.
There is no separate adaptive/ installation step.

## Raspberry Pi installation

On the Pi, from your local checkout:

~~~bash
sudo bash contrib/blue-me-usb/install.sh
sudo reboot
~~~

The installer builds this checkout and uses its bundled disk image.
It never clones another repository. System packages and missing Go modules
still require Internet access.

First Bluetooth pairing remains a manual step.
See [the installation and pairing guide](contrib/blue-me-usb/README.md).

## Source layout

- stream/: Go virtual disk, MP3 queue and TCP producer receiver.
- cmd/streamfs.go: streaming filesystem entry point.
- contrib/blue-me-usb/audio_bridge.py: Bluetooth capture and MP3 encoder bridge.
- contrib/blue-me-usb/engine.py: current PCM buffering and playback-state engine.
- files/demo3/: bundled disk image with the virtual 001.mp3 track.
- corrupt/ and timestamp/: original demonstration components.

## Verification and limits

Continuous playback was confirmed on the Pi with this audio engine.
The newly consolidated installer still needs a complete fresh-card hardware test.
The original car test used the earlier version; repeat car validation for updates.

The external player (for example VLC or Blue&Me) can read ahead and buffer **X
seconds of the MP3** before playing them. X depends on the player and its settings;
a delay of approximately **eight seconds was observed with VLC**. The corresponding
buffering delay has not been measured on Blue&Me.

When playback is paused on the phone, the Pi stops forwarding fresh music from
that playback generation and clears its own pending audio. It continues supplying
valid MP3 silence to keep the virtual file readable. **This does not pause the
external player:** it can keep playing music that it already holds in its own
buffer. Track changes can likewise remain delayed behind previously buffered audio.

To synchronize playback, the Pi would need a **separate control channel** to tell
the external player about pause, resume, and track changes. For a car, a CAN
interface is a possible route, provided the correct commands and their effects on
the installed RadioNav/Blue&Me are identified and verified. A PC player would need
its own supported control interface. **This control channel is not implemented.**
USB mass-storage reads currently carry the MP3 data, not these playback commands.

A pause command alone would not guarantee resynchronization: resume and track
changes would also need a verified way to discard or refresh the player's old
buffer, so it starts consuming the current audio rather than replaying stale data.

~~~mermaid
flowchart TD
    PHONE["Phone<br/>Music and playback status"]
    PI["Raspberry Pi<br/>PCM engine and MP3 encoder"]
    CONTROL["Proposed player control channel<br/>CAN for the car, if verified"]

    subgraph PLAYER["External player: VLC or Blue&Me"]
        CACHE["Read-ahead buffer<br/>X seconds of MP3"]
        PLAYBACK["MP3 decoding and playback"]
    end

    PHONE -->|"Bluetooth audio and AVRCP status"| PI
    PI -->|"MP3 over USB<br/>Silence when paused"| CACHE
    CACHE -->|"Previously buffered audio can still play"| PLAYBACK
    PI -.->|"Not implemented"| CONTROL
    CONTROL -.->|"Pause, resume, track-change handling"| PLAYBACK
    CONTROL -.->|"Buffer reset or resynchronization to verify"| CACHE
~~~

## Origin

Based on [rgsilva/gambiconf-usb-streaming](https://github.com/rgsilva/gambiconf-usb-streaming).
Original project explanation:
[Smartifying my hi-fi system](https://rgsilva.com/blog/smartifying-my-hi-fi-system/).
