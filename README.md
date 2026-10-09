# WiFiMonitor

Find out why your Wi-Fi drops during Zoom and Teams calls. WiFiMonitor records router and internet latency, signal quality, channel and band, and how crowded your channel is, then shows it all on a live dashboard so you can line up bad moments with their cause.

Runs on **macOS** and **Windows**. Everything stays on your machine. It is a standalone tool: no AI service, account, API key or internet service is involved. The collector and dashboard server use only the Python standard library, and the only thing the dashboard page fetches from outside your computer is the Chart.js charting library from a CDN.

## What it measures

| Signal | Why it matters |
|---|---|
| Router (gateway) ping | Slow or lost replies from the router point at Wi-Fi or the router, not your ISP |
| Internet ping (1.1.1.1) | Slow replies with a healthy router point upstream |
| RSSI, noise, SNR | Weak signal or a noisy channel. SNR of 25 dB or more is comfortable for calls |
| Channel, band, width, rate | Shows band hopping (5 GHz to 2.4 GHz) and rate drops |
| Nearby-network scan | How many networks share your channel, and which channels are quietest |

## Requirements

**Both systems**

- A computer with a Wi-Fi adapter, connected to the network you want to monitor
- **Python 3.8 or newer** (the code uses no third-party packages)
- A current web browser, with internet access so it can load Chart.js from a CDN

**macOS**

- macOS with Apple's built-in CoreWLAN framework. Developed and tested on **macOS 26**. Older versions may work but have not been tested.
- **Xcode Command Line Tools** (`xcode-select --install`). They provide `swiftc`, which builds a small Wi-Fi helper the first time you run the collector. They also supply a suitable `python3`.
- The built-in `ping` and `route` commands (always present)

**Windows**

- **Windows 10 or 11** with the **WLAN AutoConfig** service running (it is on by default on machines with a Wi-Fi adapter)
- The built-in `netsh`, `ping` and `route` commands (always present)
- **Python 3.8 or newer** from [python.org](https://www.python.org/downloads/) or the Microsoft Store, available on your `PATH` as `python`
- An English Windows display language (see [Limitations](#limitations))
- The Windows code has been tested against sample output only, not on a real machine

## Permissions

WiFiMonitor runs as a normal user. It needs no administrator rights, no `sudo`, no Wi-Fi password and no router login.

**On both systems**

| Access | Used for |
|---|---|
| Read and write in the project folder | Writing `logs/`, and on macOS building the helper into `bin/` |
| Outbound ping (ICMP) to your router and to the internet target (default `1.1.1.1`) | Latency and packet-loss measurements |
| Listening on `127.0.0.1:8765` | The dashboard. It is not reachable from other computers |
| Browser access to `cdnjs.cloudflare.com` | Loading Chart.js for the charts |

**macOS**

- **No special permission was needed on macOS 26.** Signal, noise, channel, rate and nearby-network scans all work without Location Services.
- Because Location Services is not granted, macOS hides your network name and the access point's BSSID, so they are not logged. Granting Location Services to a command-line tool is not something macOS normally allows, and nothing here depends on it.
- If macOS ever asks whether Terminal or Python may find devices on your local network, allow it, since the collector pings your router. This prompt did not appear on the test machine.
- The first run compiles the helper with `swiftc` from the Xcode Command Line Tools. That is the only step that needs developer tools.

**Windows**

- **No administrator rights are needed.** The collector runs `netsh wlan show ...`, `route print` and `ping`, which work for a standard user.
- Windows 11 24H2 and newer may return empty Wi-Fi results unless **Location** is turned on (Settings, Privacy and security, Location, and allow desktop apps). If the log shows no signal fields, check this first.
- Windows Firewall may ask whether Python can use the network the first time the dashboard starts. The server only listens on localhost, so you can decline the "public networks" option.

## Quick start

**Fastest way: let an AI coding assistant set it up.** Open this folder in [Claude Code](https://claude.com/claude-code), Codex or a similar agent and ask it:

> Read the README, install anything that is missing, start WiFiMonitor and open the dashboard.

It can check Python and the Xcode Command Line Tools, build the macOS helper, start the collector and dashboard, and fix problems such as a busy port, so you skip the manual steps below. The agent runs commands on your computer, so review what it asks to do. This is optional: everything below works without any AI service.

Or do it yourself. There are two ways to run WiFiMonitor, and you can use either one:

| | Foreground | Background service |
|---|---|---|
| Command | `./start.sh` or `start.bat` | `./service.sh start` or `service.bat start` |
| Terminal window | Stays open while it runs | None |
| Stop | Ctrl-C (macOS), or close the collector window (Windows) | `stop` command, or the **Stop** button on the dashboard |
| Start at login | No | Optional, with `install` |
| Best for | Trying it out, a quick session | Long monitoring, leaving it running |

### Option 1: foreground

macOS:

```bash
./start.sh
```

Windows:

```bat
start.bat
```

Then open <http://127.0.0.1:8765>.

The start scripts launch the collector and the dashboard together. On macOS, Ctrl-C stops both. On Windows the collector opens in its own window, so close that window to stop it.

To run them separately:

```bash
python3 src/collector.py        # python src\collector.py on Windows
python3 src/dashboard_server.py
```

Extra arguments are passed to the collector: `./start.sh --interval 10 --scan-every 600`.

### Option 2: background service

To keep monitoring without a terminal window, run it as a background service. It works the same on macOS and Windows, needs no administrator rights, and you can stop it whenever you like. Run only one of the two options at a time, since both use port 8765.

macOS (`./service.sh`) and Windows (`service.bat`) take the same commands:

| Command | What it does |
|---|---|
| `start` | Start the collector and dashboard in the background |
| `stop` | Stop both. Your logs stay |
| `restart` | Stop, then start |
| `status` | Shows whether it is running, whether the dashboard answers, and whether it starts at login |
| `open` | Start it if needed and open the dashboard in your browser |
| `install` | Start now and start again at every login |
| `uninstall` | Stop, and no longer start at login |

```bash
./service.sh start      # service.bat start on Windows
./service.sh status
./service.sh stop
```

- **Stop from the dashboard:** when the dashboard is served by the background service, a **Stop** button appears in the header. It stops the collector and the dashboard after you confirm. Starting again cannot be done from the page, because the page stops with the service. Use `start` (or `open`), or `install` to start at login. In foreground mode there is no Stop button, so use Ctrl-C.
- `stop` ends it for now. If you used `install`, it starts again at your next login until you run `uninstall`.
- Extra arguments go to the collector, for example `./service.sh install --interval 10 --scan-every 600`. `install` remembers them for the login start. Environment variables such as `PORT` are not remembered.
- A supervisor restarts the collector or the dashboard if either one crashes. Its own messages go to `logs/service.log`, which is rotated and stays under about 1.5 MB.
- It collects only while the computer is awake.
- macOS: `install` adds a login item (a LaunchAgent in `~/Library/LaunchAgents`), so macOS may show a "Background Items Added" notice. `uninstall` removes it.
- Windows: `install` adds `WiFiMonitor.vbs` to your Startup folder and uses `pythonw`, so no window appears. This has not been tested on a real Windows machine.

## Configuration

Command-line flags for the collector, with matching environment variables:

| Flag | Environment variable | Default | Meaning |
|---|---|---|---|
| `--interval` | `WIFI_INTERVAL` | `5` | Seconds between samples |
| `--scan-every` | `WIFI_SCAN_EVERY` | `900` | Seconds between nearby-network scans (15 minutes). `0` turns scanning off |
| `--host` | `WIFI_PING_HOST` | `1.1.1.1` | Internet ping target |

Other environment variables:

| Variable | Default | Meaning |
|---|---|---|
| `WIFI_LOG_DIR` | `logs/` | Where log files are written and read |
| `WIFI_LOG_KEEP_DAYS` | `14` | Days of logs to keep |
| `PORT` | `8765` | Dashboard port |
| `PYTHON` | `python3` (`python` on Windows) | Interpreter used by the start scripts |

Scans make the radio briefly leave its channel and can cause a slow or lost ping, or a roaming decision. In testing, about one scan in three was followed by a blip at a 5-minute interval, so the default is 15 minutes. Channel crowding changes slowly, so this still gives a good picture. Keep the interval long, use the Scanning switch on the dashboard, or use `--scan-every 0` during important calls.

## The dashboard

- **Status and tiles:** current router and internet latency, packet loss, signal, SNR, channel, transmit rate and channel crowding, plus four summary tiles for the range you are looking at: **Events** (outages, channel or band changes and your notes), **Availability** (share of observed time connected and reachable, and the longest outage), **Jitter** (how much the internet ping varies from sample to sample) and **Call-ready** (share of samples good enough for a video call: router ping under 50 ms, internet ping under 150 ms, nothing lost, SNR at least 20 dB). The window buttons (15m, 1h, 3h, All), a selected day, or a zoomed range change what the tiles cover.
- **Charts:** latency with lost pings marked, signal and noise, SNR, transmit rate, networks sharing your channel over time, networks per channel from the latest scan, and a ranking of the least crowded channels.
- **Help icons:** every tile, chart, header control and table column has a small (i) icon. Hover it, focus it with the keyboard, or tap it to see a short explanation of what it shows and what a good or bad value looks like.
- **How to improve:** every tile and chart has a collapsible "How to improve" section. It lists what you can do yourself, what to change in your router's settings, and what may be locked by your ISP. Many ISP-supplied routers hide or lock the channel, width and band steering, so each section says to ask the ISP, or add your own router or access point, when an option is missing. An opened tile stretches to full width and stays open while the data refreshes.
- **Latency chart markers and causes:** dots at the top of the latency chart mark your notes, channel or band changes and outages (shaded), with small ticks for scans. Hover a dot for details. Under the chart, each slow or lost-ping episode is listed with a likely cause (a disconnect, a channel change, a scan by this tool, weak signal, Wi-Fi congestion, or something beyond your router). The causes are a heuristic based on what changed at the same moment.
- **Select a range:** drag across the latency chart, the signal and noise chart, or the SNR chart to select a time range. The selection is shared, so the band appears on all three. The signal and noise and SNR charts also show the time range they cover under their titles, including the selected range. A panel shows router and internet latency and loss, signal, SNR, channel changes, the events inside the range and the likely-cause table for just that range. "Zoom all charts to this range" stretches every chart and tile to the selection and pauses live refresh. "Reset zoom" or the Escape key returns to normal.
- **Export:** every chart and table has a download icon (a tray with a downward arrow) in its top right corner. It downloads that card's data as **CSV**, **Excel (.xlsx)** or **JSON**, and charts can also be saved as a **PNG image**. The menu shows exactly what will be exported. The range follows what you are looking at: the zoomed range if you zoomed, otherwise the dragged selection, otherwise the selected day or time window (15m, 1h, 3h, All). The Daily summary always exports all days. File names include the time range, for example `wifi-latency_20261009-1229_20261009-1251.csv`. JSON files also carry the range and row count. The Excel files are written by the page itself, so nothing extra is installed or downloaded.
- **Scanning switch:** the "Scanning on" switch in the header pauses and resumes nearby-network scans without stopping the collector. Use it before an important call, because a scan briefly leaves your channel and can cost a lost ping. After you resume, the next scan waits a full interval. Each change is logged as a note, so pauses show up in the Events list. The setting is kept in `logs/control.json`, which the collector re-reads every sample.
- **Theme:** the button in the header cycles Auto (follows your system), Light and Dark. Your choice is remembered in the browser.
- **Viewing older days:** the dropdown at the top (default "Live") lists every day that has a log file. Pick one to see that whole day. Live refresh pauses for past days, and the 15m, 1h, 3h and All buttons return you to live view.
- **Daily summary:** a table at the bottom compares the days you have logs for: samples, router and internet packet loss, 95th-percentile latency, outage minutes, band or channel changes, average signal and SNR, and your notes. Click a row to open that day. Use it to check whether a router or setting change made things better or worse.
- **Events:** outages, band or channel changes, gaps where nothing was logged (for example the computer slept) and your own notes.
- **Adding notes:** type a note in the Events card, such as "changed router channel" or "moved rooms", and click Add. Notes are stored in the log, so you can compare the data before and after a change. You can also append one by hand: `echo "$(date '+%F %T') EVENT your note" >> logs/wifi-monitor-$(date +%F).log`.

The least-crowded-channel chart marks DFS channels. DFS channels (52 to 144 in 5 GHz) are often quiet, but the router must leave the channel if it detects radar, which causes a short disconnect. Channels 36 to 48 and 149 to 165 are not DFS.

## Logs

Logs live in `logs/` and rotate daily:

```
logs/
  wifi-monitor-2026-10-09.log       samples and notes (today, plain text)
  wifi-monitor-2026-10-08.log.gz    previous days, compressed
  wifi-scan-2026-10-09.log          nearby-network scans
```

- A new file starts at midnight.
- The collector compresses files from before today and deletes files older than `WIFI_LOG_KEEP_DAYS`. It does this at startup and at each new day.
- The dashboard reads both `.log` and `.log.gz` files, so history stays visible.
- At the default 5-second interval a day is roughly 1.5 MB of text and under 200 KB compressed, so two weeks takes a few MB.

### Log format

One line per sample:

```
2026-10-09 11:07:41 gateway_ms=3.834 internet_ms=32.525 rssi=-68 noise=-80 snr=12 ch=36 band=5GHz width=80MHz tx=325Mbps phy=11ac
```

- `gateway_ms` and `internet_ms` are round-trip times, or `LOST` when no reply came back.
- `rssi=NA (not associated)` means the computer was not connected to Wi-Fi.
- On Windows, `noise`, `snr` and `width` are not available, so those fields are left out.
- Lines starting with a date and `EVENT` are notes.

Scan lines are a timestamp followed by JSON: `{"own":[36,"5","80"],"nets":[[channel,"band",rssi],...]}`.

## Project layout

```
README.md
start.sh, start.bat       launch the collector and the dashboard in the foreground
service.sh, service.bat   run it as a background service (start, stop, status, install)
src/
  collector.py            sampling and scanning (macOS and Windows)
  service.py              background service: supervisor, start/stop/status, start at login
  dashboard_server.py     local web server and JSON API
  dashboard.html          the dashboard page
  logstore.py             daily log files, rotation, compression, reading
  macos/wifi-info.swift   CoreWLAN helper, built into bin/ on first run
logs/                     log files (ignored by git)
bin/                      built macOS helper (ignored by git)
```

The server only listens on `127.0.0.1` and only accepts notes posted from its own page.

## Limitations

- **Network name and BSSID are not recorded.** macOS hides them from programs without Location Services permission, so the collector does not read them.
- **Windows signal strength is approximate.** Windows reports signal as a percentage, which is converted with `dBm = percent / 2 - 100`. Noise and channel width are not reported.
- **Missing fields are handled cleanly.** When the system does not report noise, SNR or channel width (Windows), the dashboard hides what has no data, shows "not reported here" on the tiles and adds a short note under the Signal and SNR charts. If no Wi-Fi details are logged at all, those notes say what to check, and the collector prints a one-time warning when `netsh` returns nothing. Ping-based tiles and charts keep working either way.
- **Windows needs an English display language.** The collector reads `netsh` labels such as "Signal" and "Channel", and other languages use different words. Windows 11 24H2 and newer may also need Location Services turned on for `netsh wlan` to return results.
- **Windows scans use Windows' cached scan results,** which can be a little stale. macOS scans are live.
- **The Windows code is tested against sample `netsh` output only,** not on a real Windows machine.
- Linux is not supported.

## Troubleshooting

- **"swiftc not found" on macOS:** run `xcode-select --install`, then start again. Until then only pings are logged.
- **Dashboard says "No recent data":** the collector is not running, or the computer was asleep. Keep the machine awake while monitoring (`caffeinate` on macOS).
- **Charts are blank:** check the browser can reach `cdnjs.cloudflare.com`.
- **Port already in use:** start with another port, for example `PORT=8800 python3 src/dashboard_server.py`.

## License

[MIT](LICENSE). You may use, modify and redistribute this tool, but you must keep the copyright notice (Alexander Uspensky) in copies and derived work.
