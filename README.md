# Mac Sentry

**Leave home and your Mac starts recording. Come back and it stops.** Once set up and armed, Mac Sentry follows your iPhone's departure and arrival automations. Use a built-in camera or a webcam on a Mac mini.

- **Local recordings.** Video and sound are saved on the Mac by default. You can use an external drive instead. Recordings are kept for 24 hours from capture by default, and you can change the duration.
- **Person alerts in Telegram.** When Sentry detects someone, the bot you set up sends an alert and a clip with sound and boxes around detected people. The original recording stays unchanged.
- **No live feed, cloud inference, or background AI agent.** Person detection runs on your Mac.

## What you need

An Apple-silicon Mac running macOS 14 or newer, a compatible built-in or USB camera, and a microphone. Keep the Mac powered and awake. If you're using a MacBook's built-in camera, leave its lid open.

Automatic iPhone Home/Away **currently requires Tailscale** on both devices with private Serve HTTPS on the Mac. It doesn't send coordinates and doesn't use Funnel. Manual Mac control does not require Tailscale; automatic Home/Away without Tailscale is a future direction.

## Install

Copy the repository link and this request to your setup agent, or follow [INSTALL.md](INSTALL.md) yourself:

> Install Mac Sentry from this repository on my Mac. Follow INSTALL.md, keep it unarmed, and ask me to handle any local permission or pairing prompts.

This is an early version. The runbook includes a camera-free test mode and separate device checks. Check both iPhone triggers before relying on automatic recording.
