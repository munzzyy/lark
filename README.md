# Lark

A desktop front-end for OBS Studio, written in Python with PySide6. OBS stays the engine and does the streaming and recording. Lark is one window on top of it: a Go Live button with a pre-flight checklist, scenes, audio, camera controls, overlays, Twitch chat, stream alerts, a chat bot, a highlight logger and a button deck. It only runs locally.

Hardware and software it targets: Linux, OBS Studio 28 or newer with its WebSocket server, and Twitch for the chat and alert features. The Camera page is built for the EMEET SmartCam C60E 4K Dual (USB 328f:00e1) and needs the companion `cameractrls-emeet` fork checked out at `~/Projects/cameractrls-emeet` (change the path in Settings); on other cameras the page says the library is missing and the rest works. Licence: GPL-3.0-or-later, see `LICENSE`.

What is specific to the author's setup: the camera defaults above, the Twitch app id you have to register yourself (see the Twitch section), and the screenshots in `shots/`, which are taken against a fake OBS server.

## Run it

You need Python 3.14, PySide6 and aiohttp (nothing else).

    python3 -m lark                  # opens on the Live page
    python3 -m lark --view audio     # open on any page: live scenes audio camera overlays chat events highlights deck bot settings

In OBS, turn on the WebSocket server (Tools, WebSocket Server Settings). Lark reads the port and password from OBS's own config file, so there is nothing to type. If you start Lark before OBS it just waits and connects when OBS shows up.

Settings live in `~/.config/lark/config.json`. The OBS password is not copied there unless you pick "Use one I type here".

## What works

Every page sits under a status bar: OBS dot, current scene, live timer, Twitch viewers (needs Twitch connected) and a click-to-mute mic chip. The rail collapses to icons under 1100 px wide, the window title says LIVE while streaming, and the window size and last page are remembered. Rail order is Live, Scenes, Audio, Camera, Overlays, Chat, Events, Highlights, Deck, Bot, Settings.

- On the Live page, Go Live runs a pre-flight checklist (OBS connected, a scene, mic on and has signal, camera plugged in, stream key set, Twitch title set), with a fix button on rows that have one. Only OBS being down, no scene or no stream key block it. Then a cancellable 3 second countdown, and a live confirmation. Stop asks once. Live time, FPS, dropped frames, bitrate and CPU, plus Record, Save replay and Mark (adds a highlight). The virtual camera button only shows up if OBS has one (the Flatpak build doesn't). With Twitch connected there is a title and category editor. First launch shows a short first-run card, and actions report through non-blocking toasts.
- The Scenes page has every scene as a card, one click to switch, a toggle for each source in it. Set up scenes opens the scene wizard (below).
- The Audio page has a meter, a dB slider and a mute button per input, driven live by OBS.
- The Camera page has every control the camera exposes, built from the camera library in `~/Projects/cameractrls-emeet`, plus presets 1 to 4 (click loads, right click saves). It re-reads at the rate set by the Refresh every box (0.5 to 5 s, 1 s by default). Zoom shows the real magnification next to the raw value. Colour pickers are there for controls that need them. Pixel format, resolution and fps are shown read-only on the camera side; they are changed through OBS instead, with the Camera mode card (see Quality mode below).
  - Framing happens in OBS. The camera has no motors, so pan, tilt and zoom move the crop of the Camera scene item in OBS. Zoom runs 1.0x (the facecam crop) to 4.0x, pan and tilt -1 to 1, with a drag pad, sliders, the Face, Wide and Desk presets, your own saved framings, and a configurable move time (Settings, Framing in OBS). It is done in OBS, so it works at any hardware zoom, and the page shows the combined zoom.
  - Camera mode is one control with two options. Stream is the OBS camera input at 1920x1080 at 60 fps with the facecam crop as it is. Quality is 3840x2160 at 30 fps with the crop scaled by 2 so the facecam box shows the same framing, sampled from 4K. The Framing card shows the current mode as text, the header camera chip shows it, and the Deck has a Quality mode button for a pad button. 4K is decoded from MJPG on the CPU, so it carries a note, and it asks for a second click while you are live or recording. See Quality mode below.
  - Looks are Neutral, Warm, Punchy and Soft plus your own saved looks, applied as filters named "Lark: ..." on the camera input (colour, sharpness, optional LUT image). It only touches its own filters and leaves other filters alone.
  - The status bar shows a camera summary, and the Live pre-flight has a camera preset row with a Load it button.
- Overlays has a chat overlay and a starting-soon card with a countdown, served from `127.0.0.1` only. Copy the URL, or press Add to OBS to create the browser source in your current scene.
- Chat is the Twitch chat feed in the app. It reads anonymously; with Twitch connected you send from the box at the bottom.
- Events shows follows, subs, gifted subs, resubs, cheers, raids, stream online/offline and channel updates, over Twitch EventSub (WebSocket). It needs Twitch connected and stays idle without it. The page has type filters, a test button per type, and the URLs for two overlays: `/alerts` (one alert at a time, slide or fade, sound, `?test=1` loops a sample) and `/events` (recent events list). Events are logged to `~/.config/lark/events` as JSONL (mode 600, test events never logged). Gift recipients are dropped so a gift bomb doesn't flood. Settings has an Alerts section: on/off, seconds, text, sound, volume.
- Highlights is covered below.
- The Deck is big buttons for scenes, mic and desktop mute, replay, go live (pre-flight, two presses to stop), record, the starting-soon countdown, an alert test, and camera framing (Face, Wide, Desk, reset, zoom in and out, pan and tilt steps). Colours and labels are editable. With an Xbox pad (`/dev/input/js0`) the buttons map to pad buttons; the pad only fires while Lark is focused unless background mode is on, and the page says "no pad" when there isn't one.
- The bot is off by default. Built-in commands `!uptime`, `!title`, `!game`, `!commands`, `!lurk`, `!so`, `!clip` and `!permit`; custom commands with `{user}`, `{touser}` and `{count}`, aliases, four permission levels and global plus per-person cooldowns; timers with an interval and a chat-lines guard; moderation for links (allow list, `!permit` for 60 s), caps, emote spam, blocked terms and emails/phone numbers, with delete, timeout or warn. The blocked and private term lists ship empty. Settings are saved to `~/.config/lark/bot.json` (mode 600). With no Twitch token, or without the moderator scopes, the bot does nothing and logs why.
- Settings holds the OBS host, port and password source, overlay port, Twitch channel, Twitch client id, chat fade time, mic input name, accent colour, alert settings, and the shortcut list.

### Scene wizard

Set up scenes reads the scenes, inputs and input kinds already in OBS, shows a dry-run preview, and Apply creates only what is missing (CreateScene, CreateInput, CreateSceneItem, SetSceneItemTransform). It never deletes or renames anything and leaves existing scenes alone. Templates are JSON in `lark/templates`: `standard` (five scenes, facecam 480x270 cropped from the middle of the camera), `dev-stream` and `vertical`.

## Quality mode

The facecam box in the scene is 480x270, so OBS downscales the camera crop into it with lanczos. In Quality mode the crop comes from a 3840x2160 frame instead of 1920x1080, so there are twice as many real pixels behind every pixel in the box. The camera mode switch is `Eptz.set_4k`, the only writer of the camera resolution: it sets the OBS input, checks that OBS kept it, and scales the four crop values by the new width over the old one, so the framing does not move.

Measured with the camera free and OBS closed. For each mode I grabbed frame 40 from the camera over v4l2 (MJPG), cut the same two-thirds-width facecam crop, scaled it to 480x270 with ffmpeg lanczos, and took the mean squared gradient (horizontal plus vertical neighbour differences) over the centre half of that image. Three grabs per mode, the frames were deleted afterwards:

| Mode | Edge energy (3 runs) |
| --- | --- |
| Stream, 1080p60 | 1.62, 1.69, 1.63 |
| Quality, 4K30 | 4.13, 4.11, 4.06 |

Quality came out about 2.5 times sharper by this measure. It is one scene, indoors, with the camera's own auto settings, so it shows the direction and rough size, not a universal number. The price is CPU for the MJPG decode and the 30 fps cap.

## Highlights

The Highlights page is a local clip logger. It never uploads anything. When chat gets busy (messages in the last 10 seconds pass the chosen multiple of the recent median, with a minimum of 8 messages and a cooldown), Lark saves the OBS replay buffer and logs a highlight. Mark on the Live page, Ctrl+Shift+R and typing `!clip` in chat as the streamer or a mod each log one by hand. If the replay buffer is off, the highlight is still logged, just without a file. Each row shows a thumbnail made with ffmpeg, the chat before it, a Keep for Shorts switch, notes, and an Open folder button.

The log lives next to the replays, in `~/Videos/OBS/highlights.json` (change the folder with `highlights_dir` in `config.json`). It is the file the Shorts pipeline reads:

```json
{
  "version": 1,
  "highlights": [
    {
      "id": "1a141e54f03e6d4a1b2c",
      "time": "2026-10-15T20:41:07-0500",
      "ts": 1792114867.512,
      "trigger": "chat_spike",
      "chat": [{"user": "fan1", "text": "POG"}],
      "replay": "~/Videos/OBS/Replay 2026-10-15 20-41-07.mkv",
      "thumbnail": "~/Videos/OBS/highlight-thumbs/1a141e54f03e6d4a1b2c.jpg",
      "keep_for_shorts": false,
      "notes": ""
    }
  ]
}
```

`trigger` is `chat_spike`, `manual` (Mark button), `hotkey` or `command`. `chat` is up to the last 20 lines. `replay` and `thumbnail` are absolute paths, or `null` when there is no file. The file is written atomically with mode 600 because it holds chat text.

When a stream stops, Lark appends a summary to `~/Videos/OBS/sessions/<date>.json` (the date the stream started; two streams in a day share a file):

```json
{"version": 1, "date": "2026-10-15", "streams": [
  {"start": "2026-10-15T20:00:00-0500", "end": "2026-10-15T22:10:00-0500", "duration_s": 7800,
   "peak_chat_per_10s": 41, "chat_messages": 1893, "highlight_count": 6, "scene_change_count": 2,
   "scene_changes": [{"time": "2026-10-15T20:30:00-0500", "scene": "BRB"}]}
]}
```

## Camera watchdog

The C60E sometimes drops into a dark exposure state where every control still reads normal. While OBS is connected, Lark asks it for a 160 px screenshot of the camera input every 3 seconds (OBS holds the camera while streaming, so Lark cannot read frames itself) and takes the mean brightness of the centre half. If that stays under 22 of 255 for 9 seconds after the last minute averaged over 45, the Live page warns "Camera image looks dark" (it never blocks Go Live) and, while you're live, a box in the corner offers Fix. A room that was dim to begin with is not flagged. Brightness back over 35 clears it, and the status header shows a small Light dim / ok / bright badge.

Fix sets manual exposure, waits 300 ms, loads the Stream preset (the preset set in `camera_preflight_preset`, 1 by default) and reads the exposure mode back against the preset's, up to 3 tries. Manual 156 is the dark state in a lit room, so it never writes manual values. Then it waits 6 seconds and looks at the picture again. The camera only takes exposure writes while something is streaming from it, so when OBS isn't live Fix runs a short ffmpeg capture around the writes (ffmpeg has to be installed). If the mode still won't stick after two tries it resets the camera over USB (needs a udev `uaccess` rule for 328f:00e1, the error says what to add) and tries once more. The camera input name and the threshold are in Settings. If OBS has no input by that name the watchdog stays quiet.

## Keyboard shortcuts

They work anywhere in the app and do nothing while you're typing in a text field.

| Keys | What it does |
| --- | --- |
| Ctrl+1 to Ctrl+9 | Switch to scene 1 to 9, in the order OBS shows them |
| Ctrl+M | Mute or unmute the mic |
| Ctrl+Shift+R | Save the replay buffer and add a highlight (starts the buffer first if it's off) |
| Ctrl+G | Go live, or stop (press twice to stop) |
| Ctrl+, | Open Settings |

## Connecting Twitch

Lark signs in with Twitch's device code flow, which needs a public client and no secret. It still needs one thing from me: a Twitch app registered at dev.twitch.tv/console with the client type set to Public and `http://localhost` as the redirect URL. Once there is one:

1. Paste its client id into Settings, Twitch client id, and save.
2. Press Connect Twitch. Lark shows a code and a link. Open the link on any device, enter the code and approve it.
3. Lark stores the token in the system keyring (service `lark`, key `twitch`) through `secret-tool`. It never goes in `config.json`, and it refreshes itself before it expires.

The scopes asked for are `channel:manage:broadcast` (title and category), `chat:read` and `chat:edit` (reading and sending chat), `moderator:read:followers`, `channel:read:subscriptions` and `bits:read` (alerts), and `moderator:manage:chat_messages`, `moderator:manage:banned_users` and `moderator:manage:warnings` (bot moderation). A token made before these were added lacks them, so press Disconnect and Connect once for alerts and moderation to work. Disconnect in Settings removes the keyring entry. Until a client id is set, everything stays in the "not connected" state and nothing is sent to Twitch except the anonymous chat read.

## What's stubbed or not done

- Nothing has run against the real Twitch. Sign-in, EventSub, the Helix calls for bans, warnings and deletes, and viewer counts are tested only against fakes built from the docs, so the first real run is the first real test.
- Alert sounds and autoplay inside real OBS's browser source were not tried.
- The bot only learns it is a mod from Helix refusals, not from IRC. I asked for a "safety terms" list and none came, so the blocked and private lists are empty on purpose.
- The scene wizard's camera input uses packed v4l2 settings (pixel format, resolution, framerate) written from memory of the OBS plugin, and OBS sometimes shows a portal picker for the PipeWire sources. Nothing checks that the canvas is 1920x1080. Real Xbox pad events were only tried from recorded bytes.
- Camera mode, framing and looks were tested against the fake OBS and the OBS smoke run, but not watched on a real stream. The sharpness numbers come from raw camera grabs, not from a frame OBS rendered, and the framing crop is only as right as the crop math in the tests.
- Highlights: the timing of OBS's replay-saved event was only exercised against the fake.
- YouTube is not started, and nothing posts anywhere.
- Chat shows emote names as plain text, not images.
- The camera page needs the other repo to be there. If it isn't, the page says so.

## Check it

    tools/check.sh                   # compile, run the tests, screenshot every page, fail on any error
    tools/shoot.sh audio shots/a.png # one headless screenshot of one page

The unit tests include one that writes the camera's brightness and puts it back (it skips if the camera is missing or busy). `tools/real-obs-smoke.sh` is separate: it starts the real OBS headless on a temporary copy of my scene collection, records 5 seconds, saves a replay, checks that meters arrive, deletes the two files it made and restores OBS's settings. It refuses to run if OBS is open.

Check and shoot run against a fake OBS server in `tests/fake_obs.py` and a throwaway config, so they never touch real OBS or my settings. The fake server also runs alone with `python3 -m tests.fake_obs --port 4456 --password pw`, and then Lark pointed at it with `LARK_CONFIG_DIR` set to a scratch folder.

The only network traffic is to OBS on localhost and anonymous Twitch chat on port 6697 for the channel in Settings. With Twitch connected it also talks to Twitch's API and EventSub WebSocket.
