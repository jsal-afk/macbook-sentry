# MacBook Sentry

Turn an Apple-silicon MacBook into an Away-only camera with sound. Leave, record; come home, stop. Keep the footage on your own external SSD and, if you choose, get a person alert and a playable clip in your own Telegram bot chat.

- **Away only.** Use iPhone Leave and Arrive automations, or control Sentry from the Mac. Each controller start is unarmed. An Away signal expires even if your phone never sends Home.
- **Your recordings stay local.** Hardware H.264 video and microphone audio go to a new, Sentry-owned SSD directory. Capture-time retention defaults to 24 hours and keeps running while you're Home, as long as the controller is running.
- **Person clips are optional.** RF-DETR Nano runs locally. Telegram receives the alert and a separate MP4 with boxes and audio; boxes never modify the clean archive. With detection off, Sentry doesn't load a model, sample frames, or start a delivery thread.
- **No live feed, cloud inference, or background AI agent.** Footage leaves the Mac only if you turn on Telegram delivery.

## What you need

An Apple-silicon Mac running macOS 14 or newer, a camera and microphone, and a mounted external SSD that macOS identifies as solid-state. Keep the Mac powered, awake, and able to use its camera (normally lid open). Camera/mic permissions must be granted locally.

Automatic iPhone Home/Away **currently requires Tailscale** on both devices with private Serve HTTPS on the Mac. It doesn't send coordinates and doesn't use Funnel. Manual Mac control does not require Tailscale; automatic Home/Away without Tailscale is a future direction. Telegram is optional and is not a control channel.

## Install

Copy the repository link and this request to your setup agent, or follow [INSTALL.md](INSTALL.md) yourself:

> Install MacBook Sentry from this repository on this Mac. Follow INSTALL.md, keep it unarmed, and ask me to choose the SSD and handle any local permission or pairing prompts.

This is an early version. The runbook includes a camera-free test mode and separate device checks. Check both iPhone triggers before relying on automatic recording.
