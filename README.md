# Prime

Prime sits on your desktop as a floating orb. **Double clap** (or click the orb) opens the Gemini-style Prime window; the first double clap also speaks a welcome line (ElevenLabs voice) and can bring Cursor to the front on Windows. No websites, songs or browser windows are opened.

- **Floating orb:** bobs above every window while Prime is closed. Drag it anywhere (it snaps to the nearest side), click to open, right-click for Open / Quit.
- **Window:** a holographic HUD console in cyan and amber: animated arc core (reacts to Standby / Processing / Responding / Speaking), system readout, conversation log, and a command bar with attach, mic mute, send and stop. Tap the core to toggle listening. `Esc` or the minimise button sends it back to the orb.
- **Brain:** chat runs on the **ElevenLabs Agents platform** (text-only), so the one ElevenLabs key is enough. Replies stream into the window and are spoken in your voice. The Stop button cancels a reply. See `brain.py`.
- **Desktop control (Windows):** ask in plain words and Prime does it: open or close any installed app, open sites, search Google/YouTube, type text (any language), press shortcuts, move/click/scroll the mouse, volume and media keys, window snapping, screenshots (saved to `Pictures/Prime Screenshots`), shell commands, lock/sleep/shutdown. How it works: the agent ends its reply with action tags (see `actions.py`), Prime hides them from the chat and runs them. Actions show in the log as `▸ ... ✓`.
  - **Safety:** closing apps, shell commands and power actions always ask **ALLOW / DENY** first. The Stop button aborts everything, and slamming the mouse into a screen corner aborts too.
  - Before typing/clicking, Prime minimises its window to the orb so keystrokes reach the target app.
  - Prime can't see the screen yet, so it works through apps, URLs and keyboard shortcuts rather than guessing where to click.
- **Not built yet:** speech recognition (talking to Prime) and screen vision.

## Setup

```bash
python -m pip install -r requirements.txt
```

## Environment variables

Prime loads `.env` and `api.env` from the same folder as `prime.py`.

| Variable | Purpose |
| -------- | ------- |
| `ELEVENLABS_API_KEY` | ElevenLabs API key. It needs the *ElevenLabs Agents* permission for chat. |
| `ELEVENLABS_VOICE_ID` | Voice ID to speak with (required for the welcome speech). |
| `ELEVENLABS_MODEL_ID` | Optional. Default `eleven_multilingual_v2`. |
| `ELEVENLABS_OUTPUT_FORMAT` | Optional. Default `pcm_24000`. |
| `ELEVENLABS_PCM_SAMPLE_RATE` | Optional. Override PCM sample rate. |
| `PRIME_WELCOME_CACHE_DIR` | Optional. Cache folder (default `.cache/prime_welcome/`). |
| `ELEVENLABS_AGENT_ID` | Optional. Use an agent you made in the dashboard instead of the auto-created one. |
| `PRIME_LLM` | Optional. LLM the agent uses (default `gemini-2.5-flash`; e.g. `claude-sonnet-4-5`). |
| `PRIME_SPEAK_REPLIES` | Optional. `0` = don't speak chat replies. |
| `PRIME_USER_NAME` | Optional. Name shown in the greeting and avatar (default `Spidy`). |
| `PRIME_INPUT_DEVICE` | Optional mic override: device index or name substring. |

## Run

```bash
python prime.py
```

Stop with **Ctrl+C**, or right-click the orb and choose Quit. `python prime.py --no-ui` runs the old clap-only mode without the window.

## Tuning

Edit the constants at the top of `prime.py`: `SPIKE_RATIO` (raise for fewer false triggers, lower if claps are missed), `COOLDOWN_S`, `BLOCK_MS`, `MIN_RMS`, `SAMPLE_RATE` (try `48000` if your device rejects `44100`), and `PRIME_WELCOME_PHRASE` for the spoken line.

## Troubleshooting

- **Wrong or quiet mic:** Prime auto-picks the loudest working input if the default is silent. Force one with `PRIME_INPUT_DEVICE`.
- **No reaction to claps:** lower `SPIKE_RATIO` slightly or clap closer to the mic.
- **No welcome speech:** check the two ElevenLabs values in `.env` / `api.env`.
