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

Run `sentry test-bot` to receive text and an MP4 test pattern with a tone (no camera); check both in that one chat and play the video. Enable detection with `sentry configure --detection on`. To rotate a token, stop, revoke/regenerate in BotFather, repeat `pair-bot` and `test-bot`, then restart unarmed. A changed bot or recipient does not inherit queued deliveries. `sentry configure --detection off` returns to clean A/V only. Local retention does not delete clips already sent to Telegram.

## 3. Optional iPhone automation (private Tailscale Serve only)

1. Install [Tailscale](https://tailscale.com/download) on the Mac and the iPhone. Sign in on both as the owner; confirm the phone uses an untagged owner device, can reach the Mac under your tailnet rules, and has automatic date/time enabled. Enable MagicDNS and HTTPS in Tailscale if you agree to the certificate tradeoff: a certificate may publicly log the Mac's MagicDNS name, even though requests are private. Use a non-identifying device name if that matters to you.
2. On the Mac set the exact MagicDNS `.ts.net` name and exact login Serve reports for that owner. Don't paste a real hostname, account, or generated shortcuts into source control. Inspect `tailscale serve status` and `tailscale funnel status` first. **If either uses port 8443 for anything, stop here; do not change it.** Set the two shell variables below locally, then start this foreground route in a dedicated Terminal tab and keep it open:

   ```sh
   sentry phone-setup "$MAC_MAGICDNS_NAME" "$TAILSCALE_OWNER_LOGIN"
   tailscale serve --https=8443 --set-path=/sentry-presence http://127.0.0.1:18765
   ```

   The origin binds `127.0.0.1` only. Never use Funnel or a shared public ingress. If local port `18765` is occupied, choose a free loopback port with `phone-setup ... --listen-port PORT` and match it in the Serve target. Verify Serve lists only the intended new mount and Funnel has no listener on 8443. Leave existing routes untouched. Ctrl-C in the dedicated Serve tab removes the foreground route; confirm with `tailscale serve status`. If necessary and only if the exact Sentry mount remains, run `tailscale serve --https=8443 --set-path=/sentry-presence off`; never reset all routes.
3. While the controller is stopped, run `sentry shortcuts` and `sentry shortcuts --diagnostic`. The Mac signs independent pairs (`Sentry Away`, `Sentry Home`, plus `Test` counterparts in another folder). Apple's `shortcuts sign` can contact Apple and sees the workflow, including its private signal header and endpoint; no bot token or coordinates are included. Treat the files as private even though the signing mode allows import by anyone: Serve requires the matching owner **and** the per-install header. Rotate the signal key with `phone-setup ... --rotate` and regenerate if exposed. Disable old iPhone automations before replacing files. Transfer only to your own phone, e.g. with AirDrop, open each file, and tap **Add Shortcut**. The format-date action runs before networking. The diagnostic files have a separate key that the production controller rejects. The local payload check is not an iOS import or locked-phone test.
4. Run `sentry run --test` in another tab; it cannot arm, capture, or prune. Tap **Sentry Away Test** on the phone and check `accepted: true`, `event: away`, `mode: test` in its result and a new `last_accepted.away` in `sentry status` on the Mac. Repeat separately with **Sentry Home Test** and check `last_accepted.home`. Allow the shortcut to connect when iOS prompts. Wrong/missing identity or key gets 403; wrong clock, duplicate/stale event or unarmed Away gets rejected. Sync both clocks and retry as a fresh tap.
5. Shortcuts → **Automation** → **+** → **Leave**, select your home boundary, choose **Run Immediately**, then select **Sentry Away**. Repeat for **Arrive** → **Sentry Home**. Optionally add **Wi-Fi** → when joining your home network → **Sentry Home** (never Away). Use the versions **without** `Test`; they don't display a blocking result. With the test controller still running, trigger Leave and Arrive separately with iPhone locked; test departure and arrival from cellular/off home Wi-Fi, and the join trigger separately if used. Read new matching timestamps after each trigger. If iOS blocks these triggers or delivery, use Mac manual mode rather than relying on them. Sentry doesn't interpret network failure as presence. Finish with `sentry stop`.

## 4. Run, test devices, and maintain

- **First real device test (only with owner permission):** Run `sentry authorize-mic` locally and allow the Terminal's microphone prompt. Choose the right input in macOS Sound settings. In one tab run `sentry run` and **keep it open**. In a second tab run `sentry arm` and then `sentry away`; allow camera access if macOS prompts for the Terminal/FFmpeg. If access was denied, use System Settings → Privacy & Security → Camera/Microphone and retry from unarmed. `sentry status` should show `signal: away` and a `capture_worker` (process indicator, not proof of media). Wait for at least one closed segment, run `sentry home`, and check `capture_worker: false`. Open a `.ts` in the recording folder, check both video and audio, and confirm the camera indicator turns off. For alerts, separately test with detection on and an authorized person in view; after the trigger, leave it running at least 25 seconds for the event window and check the alert and the boxed MP4 with sound. A pending clip when Home stops capture may be processed on the next armed Away if its media is still retained.
- **Daily use:** `run` always starts unarmed. `arm` permits a **new** Away signal; it doesn't start the camera. `away` and `home` also work locally without Tailscale. Home stops capture but leaves the controller armed for the next departure. `pause` stops capture and disarms until an explicit `arm`. `stop` shuts down the controller via its local socket; Ctrl-C in the running Terminal is also safe. In test mode no signal commands can start a recorder. After a lease expires or the worker fails, investigate and re-arm explicitly. A late Away can't override a newer Home or cross a startup/arm boundary; later Away signals don't extend an existing lease. Retention continues at Home and while paused, but not while stopped or in test mode.
- **Troubleshooting:** Check `sentry status` and the foreground console. For storage errors, check free space and the recording folder. If you use an external drive, reconnect the enrolled volume at the same mountpoint. For A/V issues check the default mic, camera index/name, permissions, and the lid if you use a built-in MacBook camera. For media delivery test the private bot separately. For phone failures check Serve's exact port/path, owner identity and both timestamps in test mode.
- **Update/rollback:** `sentry stop` and verify `sentry status` is `stopped`. Keep the previous source and its `.venv`, and a private backup of `~/.macbook-sentry`; it contains credentials. Prepare the new source in a separate plain directory; run step 1 there and re-install optional dependencies if used. `init` preserves the config and rebuilds the helper. Test unarmed before use. To roll back, stop the newer controller, return to the old directory/venv, restore the matching private state backup if the schema changed (do not restore revoked tokens), and run the old `init` and `run`. Do not switch source or virtualenv under a running controller.
- **Uninstall without deleting footage:** Stop Sentry, disable/remove the phone automations and imported shortcuts, and remove only its Serve mount if used. Revoke the dedicated bot token if no longer needed. If you recorded internally, move any archives you want to keep out of `~/.macbook-sentry` first. Then move the source directory (including `.venv`) and the remaining `~/.macbook-sentry` state to Trash. Leave any external archives you want to keep on their drive. Playback remains possible, but automatic retention stops.
