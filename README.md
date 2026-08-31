# ADT+ for Home Assistant

> **Unofficial community integration.** This project is not affiliated with,
> endorsed by, or supported by ADT. ADT and ADT+ are trademarks of their
> respective owners.

An unofficial Home Assistant custom integration for ADT+ systems using the
ADT+ cloud/SRV1 interfaces.

## Current status

**Beta — v0.3.5**

The integration uses undocumented ADT+ interfaces. ADT may change those
interfaces without notice, which can temporarily break the integration.

## Features

- Live ADT+ alarm partition state
- Arm Home / Stay
- Arm Away
- Arm Night
- Disarm
- Door and window contact sensors
- Motion and supported life-safety sensor states
- Battery information when ADT supplies it
- Signal strength when ADT supplies a real RSSI value
- Sensor Tamper and Device Health diagnostics
- Last Shock timestamp for premium contact sensors when ADT supplies it
- ADT-managed Z-Wave Yale lock state
- Lock / Unlock control through ADT
- Lock Jam diagnostic when a lock command is not confirmed
- Police, Medical, and Fire emergency actions


## Premium contact sensor shock data

Some premium ADT door/window sensors advertise an `openCloseShock` capability.
ADT does not expose that capability as a persistent on/off shock state, so
treating it as a binary sensor results in an unusable `Shock: Unknown` entity.

As of v0.3.5, the integration no longer creates that false Boolean entity.
Actual shock history is represented by the **Last Shock** timestamp when ADT
provides `ShockStatusService.lastAlertTimestamp`.

## Critical emergency-action warning

The Police, Medical, and Fire actions can send **real monitored emergency
signals** through the configured ADT account.

They are intentionally not exposed as casual one-tap device buttons and require
`confirm: true`.

Do not test these actions unless you have an appropriate reason and understand
the consequences for the monitored ADT account.

## Installation with HACS

Until this repository is included in the default HACS catalog, install it as a
custom repository:

1. Install HACS if you do not already have it.
2. Open HACS.
3. Open the three-dot menu and choose **Custom repositories**.
4. Paste this GitHub repository URL.
5. Select **Integration** as the repository type.
6. Add the repository.
7. Open **ADT+** in HACS and download it.
8. Restart Home Assistant.
9. Go to **Settings → Devices & services → Add integration → ADT+**.

## Manual installation

Copy:

`custom_components/adtplus/`

to:

`/config/custom_components/adtplus/`

and restart Home Assistant.

## Authentication

The current beta configuration flow accepts an ADT+/Auth0 **refresh token**.
The token is stored in the Home Assistant config entry and used to obtain
short-lived access tokens.

A simpler browser-login onboarding flow is planned. Until then, users need to
obtain a refresh token using a compatible local authentication helper.

### Never post authentication material in issues

Do **not** upload or paste any of the following into a GitHub issue,
discussion, log attachment, or screenshot:

- ADT+ password
- refresh token
- access token
- ID token
- OAuth callback URL containing authorization data
- local token/location files
- raw push/WebSocket traces

Sanitize logs before sharing them.

## Brand icon

Home Assistant 2026.3+ supports local branding for custom integrations. This
repository includes:

`custom_components/adtplus/brand/icon.png`

The icon is community-created artwork for this unofficial integration.

## Privacy

This repository contains no user account credentials or device data.
Per-installation account metadata and credentials are stored by Home Assistant
after the user configures the integration.

The integration communicates with ADT+ cloud services and therefore requires
internet access.

## Lock behavior

ADT-managed Z-Wave locks remain paired to the ADT system. Home Assistant sends
lock/unlock requests through ADT rather than joining the lock to Home
Assistant's own Z-Wave network.

The Lock entity remains truthful: it reports Locked only when ADT confirms the
secured state. The separate **Lock Jam** diagnostic reports a failed/unconfirmed
lock request.

## Troubleshooting

After updating the integration:

1. Restart Home Assistant.
2. Hard-refresh the browser if branding or newly added entities do not appear.
3. Check **Settings → System → Logs** for `adtplus` errors.
4. Never include tokens or account-identifying payloads when filing an issue.

## Disclaimer

This software controls security-related equipment. Review automations carefully
before enabling them. You are responsible for how you configure and use the
integration.

## License

MIT
