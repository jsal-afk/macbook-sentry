# Install and operate Mac Sentry

[한국어](INSTALL.ko.md)

Use an Apple-silicon Mac (macOS 14+) with a compatible built-in or USB camera and a microphone. Run these commands in Terminal as the logged-in user, not as root. Setup does not start capture or add a login item.

## 1. Install unarmed

1. Open Terminal. If Command Line Tools are missing, run `xcode-select --install` and finish the Apple dialog. If Homebrew is missing, use the [official macOS `.pkg` installer](https://docs.brew.sh/Installation); no remote shell script is needed.
2. From this source directory, run:

   ```sh
   eval "$(/opt/homebrew/bin/brew shellenv)"
   brew install python@3.12 ffmpeg
   python3.12 -m venv .venv
   .venv/bin/python -B macbook_sentry.py init
   .venv/bin/python -B -m unittest -v test_candidate
   .venv/bin/python -B macbook_sentry.py status
   ```

   `init` compiles the Swift microphone helper. `status` should say `stopped`. The suite uses generated media with hardware VideoToolbox, disposable state, fake Telegram responses and no camera, phone or Tailscale. Run the CLI commands below from the same source directory, using `.venv/bin/python -B macbook_sentry.py` wherever you see **`sentry`**. For convenience, in each Terminal tab:

   ```sh
   alias sentry='.venv/bin/python -B macbook_sentry.py'
   ```

3. Recordings go to `~/.macbook-sentry/archive/recordings` by default. To change how long recordings are kept, run `configure` while stopped. For example:

   ```sh
   sentry configure --retention-hours 48
   ```

   The default is 24 hours; choose 1 to 720 hours. Retention is measured from capture time and runs on the selected archive even when you're home, as long as the controller is running. The Away recording lease is separate (`--away-lease-seconds`, default 14400, maximum 86400). Leave at least 5 GiB free on the selected storage. Settings are stored in `~/.macbook-sentry/config.json`.

   For a USB webcam on a Mac mini or another Mac, select its AVFoundation name or index with `sentry configure --camera 'Camera Name'` (default `0`). Use `ffmpeg -f avfoundation -list_devices true -i ''` to list devices and choose the microphone in macOS Sound settings.

   **Optional: save to an external drive.** While stopped, run `sentry archive '/Volumes/DRIVE_NAME'` with the actual mounted name. The drive must have a volume UUID; Sentry creates a new `Mac-Sentry` folder there. To switch back, run `sentry archive --internal --name archive-new`. When a folder name is already in use, pick a new one with `--name`. Changing storage doesn't move or delete existing footage. If an enrolled external drive is unavailable, Sentry doesn't switch to the internal disk.

## 2. Person detection and Telegram alerts

Install the local detection model and pair your Telegram bot to receive alerts and clips. From the same source directory:

```sh
.venv/bin/python -m pip --isolated --disable-pip-version-check install --index-url https://pypi.org/simple -r requirements-detection.txt
(
  set -eu
  umask 077
  state="$HOME/.macbook-sentry"
  tmp=$(mktemp "$state/nano.XXXXXXXX")
  trap 'rm -f "$tmp"' EXIT
  /usr/bin/curl -q --fail --show-error --location --proto '=https' \
    'https://storage.googleapis.com/rfdetr/nano_coco/checkpoint_best_regular.pth' -o "$tmp"
  printf 'd8d6b9ee57d4d0ed2b1f305163624712a0532cb7bce0c747317984fc5457440d  %s\n' "$tmp" | shasum -a 256 -c -
  mv -f "$tmp" "$state/rf-detr-nano.pth"
)
sentry check-model
.venv/bin/python -B -m unittest -v test_detection
```

This is the [RF-DETR 1.8.0 Nano COCO checkpoint](https://github.com/roboflow/rf-detr/blob/1.8.0/src/rfdetr/assets/model_weights.py); no runtime downloads. `check-model` runs on a generated blank image, with no camera. Create a **dedicated** bot in Telegram with BotFather (`/newbot`); don't add it to groups or connect another poller/webhook. On the Mac run `sentry pair-bot`. The **owner** enters the token at the hidden Terminal prompt, then sends the printed one-time `/start <code>` in the private chat with the exact `@bot` shown. Never share the token in chat, a command, a log, or the source directory. Sentry saves only one chat binding with the token in `~/.macbook-sentry/bot.json`, owner-only and link-checked. Polling ends at pairing; Bot API replies are checked directly on sends.

Run `sentry test-bot` to receive text and an MP4 test pattern with a tone (no camera); check both in that one chat and play the video. Enable detection with `sentry configure --detection on`. To rotate a token, stop, revoke/regenerate in BotFather, repeat `pair-bot` and `test-bot`, then restart unarmed. A changed bot or recipient does not inherit queued deliveries. `sentry configure --detection off` returns to clean A/V only. A paired bot sends `Sentry activated` when Sentry starts on Away and `Sentry disabled` when the recording worker has stopped on Home. Activation does not mean a file has already been written. Local retention does not delete clips already sent to Telegram.

## 3. iPhone automation

1. Install [Tailscale](https://tailscale.com/download) on the Mac and the iPhone. Sign in on both as the owner; confirm the phone uses an untagged owner device, can reach the Mac under your tailnet rules, and has automatic date/time enabled. Enable MagicDNS and HTTPS in Tailscale if you agree to the certificate tradeoff: a certificate may publicly log the Mac's MagicDNS name, even though requests are private. Use a non-identifying device name if that matters to you.
2. On the Mac set the exact MagicDNS `.ts.net` name and exact login Serve reports for that owner. Don't paste a real hostname, account, or generated shortcuts into source control. Inspect `tailscale serve status` and `tailscale funnel status` first. **If either uses port 8443 for anything, stop here; do not change it.** Set the two shell variables below locally, then start this foreground route in a dedicated Terminal tab and keep it open:

   ```sh
   sentry phone-setup "$MAC_MAGICDNS_NAME" "$TAILSCALE_OWNER_LOGIN"
   tailscale serve --https=8443 --set-path=/sentry-presence http://127.0.0.1:18765
   ```

   The origin binds `127.0.0.1` only. If local port `18765` is occupied, choose a free loopback port with `phone-setup ... --listen-port PORT` and match it in the proxy target. Verify Serve lists the intended mount and Funnel has no listener on 8443. Ctrl-C in the dedicated Serve tab removes the foreground route; confirm with `tailscale serve status`. If necessary and only if the exact Sentry mount remains, run `tailscale serve --https=8443 --set-path=/sentry-presence off`; never reset all routes.
3. While stopped, run `sentry shortcuts` and `sentry shortcuts --diagnostic`. The Mac signs the Away/Home templates without the key. Its output folder contains the two `.shortcut` files and a separate owner-only `Sentry Setup Key.txt`. Import each shortcut and privately paste its setup key when asked (or replace the first Text action's `PASTE_SETUP_KEY`). Don't share the key or put it in a URL. Keep the diagnostic files and their separate key for test mode; production rejects that key. When updating a phone, keep its existing Leave/Arrive conditions and replace only the shortcut action. Use `phone-setup ... --rotate` with the current transport if a key is exposed, then replace the key on the phone. Each request includes the phone's event time, set before networking.
4. Run `sentry run --test` in another tab; it cannot arm, capture, or prune. Tap **Sentry Away Test** on the phone and check `accepted: true`, `event: away`, `mode: test` in its result and a new `last_accepted.away` in `sentry status` on the Mac. Repeat separately with **Sentry Home Test** and check `last_accepted.home`. Allow the shortcut to connect when iOS prompts. Wrong/missing identity or key gets 403; wrong clock, duplicate/stale event or unarmed Away gets rejected. Sync both clocks and retry as a fresh tap.
5. Shortcuts → **Automation** → **+** → **Leave**, select your home boundary, choose **Run Immediately**, then select **Sentry Away**. Repeat for **Arrive** → **Sentry Home**. Optionally add **Wi-Fi** → when joining your home network → **Sentry Home** (never Away). Use the versions **without** `Test`; they don't display a blocking result. With the test controller still running, trigger Leave and Arrive separately with iPhone locked; test departure and arrival from cellular/off home Wi-Fi, and the join trigger separately if used. Read new matching timestamps after each trigger. If iOS blocks these triggers or delivery, use Mac manual mode rather than relying on them. Sentry doesn't interpret network failure as presence. Finish with `sentry stop`.

### Home/Away without Tailscale on the phone

The Mac still runs Tailscale. If you choose this mode, replace step 2 with the commands below. Have the installer confirm that HTTPS 443 is unused before opening the dedicated foreground Funnel route. Never serve the viewer on that port. If migrating, remove the old phone route and regenerate the shortcuts; changing transport rotates the key.

```sh
sentry phone-setup "$MAC_MAGICDNS_NAME" "$TAILSCALE_OWNER_LOGIN" --via-funnel
tailscale funnel --https=443 --set-path=/sentry-presence http://127.0.0.1:18765
```

The public address alone cannot start or stop recording; the receiver requires the separate setup key and rejects stale or replayed signals. A copied key can authorize another device. Test both diagnostic shortcuts over cellular with the phone's Tailscale disconnected before arming. To disable, quit the dedicated Funnel command and verify the mount is gone. If the exact mount remains, remove only that mount with `tailscale funnel --https=443 --set-path=/sentry-presence off`.

### Private viewing in Safari

With Sentry stopped and HTTPS 8444 free, use a separate terminal for Serve. The viewer is off until configured and does not create public routes.

```sh
sentry live-setup "$TAILSCALE_OWNER_LOGIN"
tailscale serve --https=8444 --set-path=/sentry-live http://127.0.0.1:18766/sentry-live
```

Run Sentry and open `https://<mac-magicdns-name>:8444/sentry-live/` in Safari while logged into Tailscale as the configured owner. Allow for a short delay as recordings are saved in 10-second chunks. The viewer only serves recent chunks of the active Away recording and stops serving them at Home. There is no archive browser. Use `tailscale serve status` and `tailscale funnel status` to verify the viewer only exists under Serve. Do not forward a camera port or set up a public viewer. To disable, stop Sentry, run `sentry live-disable`, and quit the dedicated Serve command or remove only its `/sentry-live` mount. The built-in origin runs on the Mac; a separately managed Docker gateway requires an authenticated private bridge and should not publish or mount the recording archive.

## 4. Run, test devices, and maintain

- **First real device test (only with owner permission):** Run `sentry authorize-mic` locally and allow the Terminal's microphone prompt. Choose the right input in macOS Sound settings. In one tab run `sentry run` and **keep it open**. In a second tab run `sentry arm` and then `sentry away`; allow camera access if macOS prompts for the Terminal/FFmpeg. If access was denied, use System Settings → Privacy & Security → Camera/Microphone and retry from unarmed. `sentry status` should show `signal: away` and a `capture_worker` (process indicator, not proof of media). Wait for at least one closed segment, run `sentry home`, and check `capture_worker: false`. Open a `.ts` in the recording folder, check both video and audio, and confirm the camera indicator turns off. For alerts, separately test with detection on and an authorized person in view; after the trigger, leave it running at least 25 seconds for the event window and check the alert and the boxed MP4 with sound. A pending clip when Home stops capture may be processed on the next armed Away if its media is still retained.
- **Daily use:** `run` always starts unarmed. `arm` permits a **new** Away signal; it doesn't start the camera. `away` and `home` also work locally without Tailscale. Home stops capture but leaves the controller armed for the next departure. `pause` stops capture and disarms until an explicit `arm`. `stop` shuts down the controller via its local socket; Ctrl-C in the running Terminal is also safe. In test mode no signal commands can start a recorder. After a lease expires or the worker fails, investigate and re-arm explicitly. A late Away can't override a newer Home or cross a startup/arm boundary; later Away signals don't extend an existing lease. Retention continues at Home and while paused, but not while stopped or in test mode.
- **Troubleshooting:** Check `sentry status` and the foreground console. For storage errors, check free space and the recording folder. If you use an external drive, reconnect the enrolled volume at the same mountpoint. For A/V issues check the default mic, camera index/name, permissions, and the lid if you use a built-in MacBook camera. For media delivery test the private bot separately. For phone failures check Serve's exact port/path, owner identity and both timestamps in test mode.
- **Update/rollback:** `sentry stop` and verify `sentry status` is `stopped`. Keep the previous source and its `.venv`, and a private backup of `~/.macbook-sentry`; it contains credentials. Prepare the new source in a separate plain directory; run step 1 there and re-install optional dependencies if used. `init` preserves the config and rebuilds the helper. Test unarmed before use. To roll back, stop the newer controller, return to the old directory/venv, restore the matching private state backup if the schema changed (do not restore revoked tokens), and run the old `init` and `run`. Do not switch source or virtualenv under a running controller.
- **Uninstall without deleting footage:** Stop Sentry, disable/remove the phone automations and imported shortcuts, and remove only its Serve mount if used. Revoke the dedicated bot token if no longer needed. If you recorded internally, move any archives you want to keep out of `~/.macbook-sentry` first. Then move the source directory (including `.venv`) and the remaining `~/.macbook-sentry` state to Trash. Leave any external archives you want to keep on their drive. Playback remains possible, but automatic retention stops.
