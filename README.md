# Tadiran Infrared

A Home Assistant infrared consumer integration for Tadiran inverter air
conditioners using the YB1FA remote protocol.

The integration controls the air conditioner through any Home Assistant
`infrared` emitter entity and can optionally follow a physical remote through
an `infrared` receiver entity. It supports:

- Auto, cool, dry, fan-only, heat, and off
- 16-30 °C target temperature
- Auto, low, medium, and high fan speeds
- Turbo mode in cool and heat
- Static, automatic, top, and bottom vertical louver positions
- State restoration and optional state updates from received IR frames

## Requirements

- A Home Assistant version that includes the
  [infrared entity platform](https://developers.home-assistant.io/docs/core/entity/infrared/)
- An configured infrared emitter entity, such as a compatible Broadlink or
  ESPHome device

## Installation

### HACS

1. Add `https://github.com/Lurebat/tadiran-infrared` as a custom integration
   repository in HACS.
2. Install **Tadiran Infrared**.
3. Restart Home Assistant.

### Manual

Copy `custom_components/tadiran_infrared` into the `custom_components`
directory in your Home Assistant configuration, then restart Home Assistant.

## Configuration

Go to **Settings > Devices & services > Add integration**, select
**Tadiran Infrared**, enter a SmartIR climate device code, and choose an
infrared emitter. Device code `1344` uses the optimized Tadiran implementation
and optionally supports a receiver. Other Broadlink/Base64 SmartIR climate
profiles are downloaded and validated during setup, then stored locally in the
config entry; they do not require SmartIR itself.

## Protocol

SmartIR climate code set
[1344](https://github.com/smartHomeHub/SmartIR/blob/master/codes/climate/1344.json)
contains Broadlink Base64 captures for the Tadiran YB1FA remote. Decoding those
captures reveals the Gree 64-bit air-conditioner protocol:

- 38 kHz carrier
- 9,000/4,500 µs header
- 620 µs bit marks with 540/1,600 µs zero/one spaces
- Two 32-bit, least-significant-bit-first blocks separated by `010`
- Kelvinator-style four-bit checksum

The encoder generates frames from climate state, replacing the 1,321
non-empty captured commands with a small validated implementation. The
YB1FA-specific model, light, and fixed-state bits match code set 1344.

## Reusing other SmartIR profiles

`custom_components/tadiran_infrared/smartir.py` provides a generic,
hardware-independent adapter for other SmartIR climate profiles:

- `BroadlinkBase64Command` converts a Broadlink Base64 capture into the signed
  raw timings used by Home Assistant infrared emitters.
- `SmartIrClimateProfile` validates SmartIR metadata and resolves off, optional
  on, and complete mode/fan/swing/temperature state commands.
- Malformed, empty, unsupported-controller, and incomplete matrix entries fail
  explicitly instead of sending an unintended command.
- Sparse profiles are reduced to their largest complete mode/fan/swing matrix,
  so Home Assistant never advertises an unavailable combination.

This makes Broadlink/Base64 SmartIR climate profiles directly usable through
Home Assistant's native infrared emitter abstraction without
reverse-engineering each protocol. Compact protocol implementations like
Tadiran's remain preferable when available because they support receiver
decoding and avoid storing large lookup tables.
