# Changelog

## 0.3.4
- Cleaned diagnostic naming and removed redundant/unknown diagnostics.
- `Battery Level` renamed to `Battery`.
- `Tamper` renamed to `Sensor Tamper`.
- `Trouble` renamed to `Device Health`.
- Low Battery hidden when a useful primary battery state exists.
- Signal Strength only created when ADT supplies a numeric RSSI value.

## 0.3.3
- Added Lock Jam diagnostic and lock-command confirmation timeout.

## 0.3.2
- Added native Home Assistant Lock entity and ADT Lock/Unlock control.

## 0.3.1
- Cleaned premium sensor presentation, categorical battery handling, and
  stale Shock/Reed entities.

## 0.3.0
- Added Night arming.
- Added emergency Police/Medical/Fire actions requiring explicit confirmation.
- Added battery, tamper, trouble, and signal diagnostics.
