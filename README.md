# Wi-Fi Monitor

Find out why your Wi-Fi drops during Zoom and Teams calls. WiFiMonitor records router and internet latency, signal quality, channel and band, and how crowded your channel is, then shows it all on a live dashboard so you can line up bad moments with their cause.

Runs on **macOS** and **Windows**. Everything stays on your machine. It is a standalone tool: no AI service, account or API key is involved, and the collector and dashboard server use only the Python standard library. Beyond the pings to your router and the internet target (about 100 bytes each), the dashboard page loads the Chart.js charting library from a CDN, and the optional **Internet download speed** test downloads 10 to 100 MB from a speed server, but only when you press Run now or choose a schedule for it. Nothing else leaves your computer.

## What it measures

| Signal | Why it matters |
|---|---|
| Router (gateway) ping | Slow or lost replies from the router point at Wi-Fi or the router, not your ISP |
| Internet ping (1.1.1.1) | Slow replies with a healthy router point upstream |
| RSSI, noise, SNR | Weak signal or a noisy channel. SNR of 25 dB or more is comfortable for calls |
| Channel, band, width, rate | Shows band hopping (5 GHz to 2.4 GHz) and rate drops |
| Nearby-network scan | How many networks share your channel, and which channels are quietest |
| Internet download speed (on request) | How fast data reaches this computer from one server, next to the Wi-Fi link at that moment, to tell a weak Wi-Fi link from a slow internet connection |

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
| Outbound HTTPS (or HTTP) download from the speed server, by default `speed.cloudflare.com` | Only for the Internet download speed test, and only when you run it or schedule it |

**Background service (optional)**

The `service` commands also run as a normal user and need no administrator rights. `start` and `stop` only start and end WiFiMonitor's own processes. `install` writes one file that you own: a LaunchAgent in `~/Library/LaunchAgents` on macOS, or `WiFiMonitor.vbs` in your Startup folder on Windows. It runs only while you are logged in, and `uninstall` removes the file. Windows Services and Task Scheduler are not used because they usually need administrator rights. If your organisation disables Windows Script Host, the Windows login start will not run, but `start` and `stop` still work.

**macOS**

- **No special permission was needed on macOS 26.** Signal, noise, channel, rate and nearby-network scans all work without Location Services.
- Because Location Services is not granted, macOS hides your network name and the access point's BSSID, so they are not logged. Granting Location Services to a command-line tool is not something macOS normally allows, and nothing here depends on it.
- If macOS ever asks whether Terminal or Python may find devices on your local network, allow it, since the collector pings your router. This prompt did not appear on the test machine.
- The first run compiles the helper with `swiftc` from the Xcode Command Line Tools. That is the only step that needs developer tools.
- If your Python comes from python.org, it may not trust HTTPS certificates until you run `Install Certificates.command` (in `/Applications/Python 3.x/`). Until then the Internet download speed test fails with a certificate error and the dashboard says so. Python from Xcode Command Line Tools or Homebrew does not need this.

**Windows**

- **No administrator rights are needed.** The collector runs `netsh wlan show ...`, `route print` and `ping`, which work for a standard user.
- Windows 11 24H2 and newer may return empty Wi-Fi results unless **Location** is turned on (Settings, Privacy and security, Location, and allow desktop apps). If the log shows no signal fields, check this first.
- Windows Firewall may ask whether Python can use the network the first time the dashboard starts. The server only listens on localhost, so you can decline the "public networks" option.

## Quick start

**Fastest way: let an AI coding assistant set it up.** Open this folder in [Claude Code](https://claude.com/claude-code), Codex or a similar agent and ask it:

> Read AGENTS.md and the README, install anything that is missing, start WiFiMonitor and open the dashboard.

[AGENTS.md](AGENTS.md) tells the agent exactly what to do, without asking you which mode to use: it checks Python and the Xcode Command Line Tools, runs `./start.sh --service` (background, no window), verifies the dashboard answers, fixes problems such as a busy port, and tells you the address and how to stop it. It uses the login-start option only if you ask for it. The agent runs commands on your computer, so review what it asks to do. This is optional: everything below works without any AI service.

Or do it yourself. Run the start script:

```bash
./start.sh        # macOS
```

```bat
start.bat         :: Windows
```

In an interactive terminal it asks how to run WiFiMonitor:

```
How do you want to run WiFiMonitor?
  1) In this terminal window   (stop with Ctrl-C)                          [default]
  2) As a background service   (no window; stop from the dashboard or ./service.sh stop)
  3) As a background service, and start it at every login
```

Then open <http://127.0.0.1:8765>.

To skip the question, pass a flag:

| Flag | What it does |
|---|---|
| `--terminal` | Run in this window (the foreground option below) |
| `--service` | Start the background service now |
| `--login` | Start the background service now and at every login |

Other arguments go to the collector, for example `./start.sh --service --interval 10 --scan-every 600`. The environment variable `WIFI_MODE=terminal`, `service` or `login` does the same as the flags. When there is no flag, no `WIFI_MODE` and no interactive terminal (an AI agent or a script), it never asks and starts the background service, because a foreground run would block the caller.

There are two ways to run WiFiMonitor. Run only one at a time, since both use port 8765.

| | Foreground | Background service |
|---|---|---|
| Start | `./start.sh --terminal` | `./start.sh --service` or `./service.sh start` |
| Terminal window | Stays open while it runs | None |
| Stop | Ctrl-C | `./service.sh stop`, or the **Stop** button on the dashboard |
| Start at login | No | Optional, with `--login` or `install` |
| Best for | Trying it out, a quick session | Long monitoring, leaving it running |

On Windows use `start.bat` and `service.bat` in place of `./start.sh` and `./service.sh`.

### Option 1: foreground

`./start.sh --terminal` (or `start.bat --terminal`) runs the collector and the dashboard in this window. You see each sample as it is logged, and Ctrl-C stops both.

To run them separately:

```bash
python3 src/collector.py        # python src\collector.py on Windows
python3 src/dashboard_server.py
```

### Option 2: background service

To keep monitoring without a terminal window, run it as a background service. It works the same on macOS and Windows, needs no administrator rights, and you can stop it whenever you like.

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
- `stop` ends it for now. If you used `install` or `--login`, it starts again at your next login until you uninstall it.
- Extra arguments go to the collector, for example `./service.sh install --interval 10 --scan-every 600`. `install` remembers them for the login start. Environment variables such as `PORT` are not remembered.
- A supervisor restarts the collector or the dashboard if either one crashes. Its own messages go to `logs/service.log`, which is rotated and stays under about 1.5 MB.
- It collects only while the computer is awake.
- macOS: `install` adds a login item (a LaunchAgent in `~/Library/LaunchAgents`), so macOS may show a "Background Items Added" notice.
- Windows: `install` adds `WiFiMonitor.vbs` to your Startup folder and uses `pythonw`, so no window appears. This has not been tested on a real Windows machine.

### Uninstall

Run `./uninstall.sh` (macOS) or `uninstall.bat` (Windows). It stops the background service and its web server, and removes the login item. Your logs and the project folder are kept, so delete the folder yourself if you want everything gone. If the dashboard is still answering afterwards, it was started in a terminal with `--terminal`, and the script tells you to press Ctrl-C in that window. It is the same as `./service.sh uninstall`.

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

These are the start-up values. The `--interval` and `--scan-every` periods can also be changed while running from the "Probes settings" card on the dashboard (collapsed by default; open it with its arrow button), which overrides them until you pick "start-up value" (see The dashboard). That card shows the settings in effect. The dashboard's own refresh rate is separate: set it with the menu in the dashboard header.

Scans make the radio briefly leave its channel and can cause a slow or lost ping, or a roaming decision. In testing, about one scan in three was followed by a blip at a 5-minute interval, so the default is 15 minutes. Channel crowding changes slowly, so this still gives a good picture. Keep the interval long, use the Scanning switch on the dashboard, or use `--scan-every 0` during important calls.

## The dashboard

- **Status and tiles:** current router and internet latency, packet loss, signal, SNR, channel, transmit rate and channel crowding, plus four summary tiles for the range you are looking at: **Events** (outages, channel or band changes and your notes), **Availability** (share of observed time connected and reachable, and the longest outage), **Jitter** (how much the internet ping varies from sample to sample) and **Call-ready** (share of samples good enough for a video call: router ping under 50 ms, internet ping under 150 ms, nothing lost, SNR at least 20 dB). The window buttons (15m, 1h, 3h, All), a selected day, or a zoomed range change what the tiles cover.
- **Charts:** latency with lost pings marked, signal and noise, SNR, transmit rate, networks sharing your channel over time, networks per channel from the latest scan, a ranking of the least crowded channels, and the Internet download speed card described below.
- **Internet download speed:** this card measures how fast data reaches this computer. Press **Run now** for one test, or pick a schedule (one time, every 10 min, 30 min or 1 hour) and press **Save**. You can also set the server address (empty means the default, Cloudflare at `speed.cloudflare.com`) and the size, 10 to 100 MB. A gauge shows the live speed during a test and the last result after it, a chart shows the recent tests (green close to your usual speed, orange below 80% and red below 50% of the median, with a dashed average line, and a failed test as ERR, never as 0), and a table lists the last 15 tests with the Wi-Fi signal, SNR and channel at the moment each one started. A short note under the table suggests what the results point to: a weak Wi-Fi link, a slow internet connection behind a healthy link, results that swing between tests, a slow first byte, or repeated failures. Each test also shades its time span green on the latency chart, and the pings taken during a test are drawn but left out of the packet loss, outage, jitter and slow-episode figures, because the test itself fills the link and slows them down. Daily summaries leave them out too.
  - **Data use:** every test downloads the full size, so a schedule adds up: 25 MB every 10 minutes is about 3.6 GB per day, or 108 GB per month. The card shows this estimate while you edit the settings and warns at about 1 GB per day or more. Avoid frequent schedules on a metered or capped connection, such as a mobile hotspot or a data plan. The default is one time (only Run now) at 25 MB. A test stops after 60 seconds even if the download is not finished. A custom address must be an `http` or `https` address; with a custom address the test asks for the first bytes of the file, and it never follows a redirect to anything but `http` or `https`.
  - **It is a single stream from one server, not a full speed test.** On a fast connection (for example 500 Mbps or more) one download from one server often reads lower than your plan, so compare with an Ethernet cable or another speed test before blaming your provider. Treat it as a way to see changes over time and to separate Wi-Fi from internet problems.
  - **Failures:** a test that cannot finish (no internet, a wrong or blocked address, a certificate problem, a timeout, a download too short to measure, or the computer sleeping during the test) is logged as an error with the reason, shown as ERR, and counts as a measurement error after three in a row. It is not zero speed.
  - Run now and the settings need the collector to be running (the card says so otherwise), and the settings only work when the page is opened from this computer (`127.0.0.1` or `localhost`). Opened from another address, the card shows only the host of a custom server, never the full address, and refuses changes. A "Measurement error: speed test" event in the Events card is about the speed test only: it does not trigger the red measurement banner and is not counted as a ping error.
- **Help icons:** every tile, chart, header control and table column has a small (i) icon. Hover it, focus it with the keyboard, or tap it to see a short explanation of what it shows and what a good or bad value looks like.
- **How to improve:** every tile and chart has a collapsible "How to improve" section. It lists what you can do yourself, what to change in your router's settings, and what may be locked by your ISP. Many ISP-supplied routers hide or lock the channel, width and band steering, so each section says to ask the ISP, or add your own router or access point, when an option is missing. An opened tile stretches to full width and stays open while the data refreshes.
- **Latency chart markers and causes:** dots at the top of the latency chart mark your notes, channel or band changes and outages (shaded), with small ticks for scans. Hover a dot for details. Under the chart, each slow or lost-ping episode is listed with a likely cause (a disconnect, a channel change, a scan by this tool, weak signal, Wi-Fi congestion, or something beyond your router). The causes are a heuristic based on what changed at the same moment.
- **Select a range:** drag across the latency chart, the signal and noise chart, or the SNR chart to select a time range. The selection is shared, so the band appears on all three. The signal and noise and SNR charts also show the time range they cover under their titles, including the selected range. A panel shows router and internet latency and loss, signal, SNR, channel changes, the events inside the range and the likely-cause table for just that range. "Zoom all charts to this range" stretches every chart and tile to the selection and pauses live refresh. "Reset zoom" or the Escape key returns to normal.
- **Export:** every chart and table has a download icon (a tray with a downward arrow) in its top right corner. It downloads that card's data as **CSV**, **Excel (.xlsx)** or **JSON**, and charts can also be saved as a **PNG image**. The menu shows exactly what will be exported. The range follows what you are looking at: the zoomed range if you zoomed, otherwise the dragged selection, otherwise the selected day or time window (15m, 1h, 3h, All). The Daily summary always exports all days. File names include the time range, for example `wifi-latency_20261009-1229_20261009-1251.csv`. JSON files also carry the range and row count. The Excel files are written by the page itself, so nothing extra is installed or downloaded.
- **Chart range:** the charts follow the 15m, 1h, 3h or All window. When the logs hold less data than the window, for example on a first run, the time axis starts at your first sample so the data fills the chart instead of leaving it mostly empty.
- **Measurement errors:** if a ping or the Wi-Fi status cannot be measured (not the same as a lost ping), the tiles show `ERR`, the status says "Measurement error", a banner explains why while it lasts, and the Events card and latency chart mark the start and the recovery. These samples are not counted as packet loss, outages or unavailability, and the Packet loss tile says how many were not measured.
- **Header:** the header controls are centered and wrap onto extra lines on narrow screens, each control staying together with its (i) icon.
- **Refresh interval:** the "↻ every 5 s" menu in the header sets how often the page reloads its data from the local server: 2 s, 5 s, 10 s, 30 s, 1 min or paused. When paused, the footer offers "Refresh now". The choice is remembered in the browser. It only changes how often the page looks for new data. How often the collector measures is separate (`--interval`, see Configuration), so refreshing faster than the sample interval shows nothing new.
- **Probes settings:** this card is collapsed by default. Open or close it with the arrow button or by clicking its title; while collapsed it shows a one-line summary of the current settings, and the browser remembers whether you left it open. The Every menus are inside it. Opened, it lists everything the collector does, with the target and the exact command, how often it runs, when it last ran, the last result, and how much network it uses. It shows the real settings, read from `logs/collector.json`, which the collector writes at start. If the collector is not running, the card says so. The table can be exported like the other cards. It also lists the Internet download speed test, with its server, the data it uses per test and per day at the current setting, and its last result; it is set in its own card, not in this table.
- **Changing probe intervals from the page:** the menus in the table's "Every" column retime the probes without restarting anything. The router, internet and Wi-Fi probes share one sampling loop, so one menu sets all three (2, 5, 10 or 15 s). Nearby-network scans can run every 5 min, 15 min, 30 min, 1 h or be turned off. A change applies within a few seconds, is noted in the Events card, and is remembered in `logs/control.json` until you change it again. "Start-up value" removes the override and returns to the `--interval` and `--scan-every` values the collector was started with. Samples slower than 15 s are not offered, because the charts treat a longer silence as missing data. The header's refresh menu is separate and only changes how often the page reloads.
- **Stop button:** when the dashboard is served by the background service (see [Option 2](#option-2-background-service)), a **Stop** button with an (i) help icon appears in the header. It ends the collector and the dashboard. It does not appear when you started with `--terminal`.
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
  wifi-speed-2026-10-09.log         Internet download speed tests (one JSON line per finished test)
  service.log                       background service output (rotated, about 1.5 MB at most)
  service.pid                       process id of the running background service
  collector.json                    the collector's start-up settings, read by the dashboard's probes table
  control.json                      settings changed from the dashboard (scan switch, probe intervals, speed_url, speed_mb, speed_every, speed_run)
  speed.json                        progress of the running Internet download speed test, read by the dashboard
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

- `gateway_ms` and `internet_ms` are round-trip times, or `LOST` when the ping ran and no reply came back.
- `ERR` (in place of a time) means the ping itself could not be done, for example the command failed, timed out or printed something unreadable. It is not packet loss and the dashboard does not count it as loss, an outage or unavailability.
- `rssi=NA (not associated)` means the computer was really not connected to Wi-Fi.
- `wifi=ERR` (in place of the Wi-Fi fields) means the Wi-Fi status could not be read, for example because Windows is not in English. It is not a disconnect.
- On Windows, `noise`, `snr` and `width` are not available, so those fields are left out.
- Lines starting with a date and `EVENT` are notes. When something cannot be measured for three samples in a row (at once for an unsupported language), the collector also writes `EVENT Measurement error: ...` with the reason, and `EVENT Measurement recovered: ...` when it works again. They show on the dashboard as measurement errors and are also printed to `logs/service.log`.

Scan lines are a timestamp followed by JSON: `{"own":[36,"5","80"],"nets":[[channel,"band",rssi],...]}`.

Speed test lines (`wifi-speed-*.log`) are the time the test finished followed by JSON, for example `{"v":1,"start":"2026-10-09 12:00:00","trigger":"manual","ok":true,"mbps":312.4,"ttfb_ms":84.2,"bytes":25000000,"dur_s":5.21,"status":200,"host":"speed.cloudflare.com","mb":25,"wifi":{"rssi":-52,"snr":38,"ch":149,"band":"5","tx":866}}` (a few more fields are kept). The speed is the data after the first chunk divided by the time it took, in megabits per second (1 MB is 1,000,000 bytes). A failed test has `"ok":false`, `"mbps":null`, a `kind` (dns, timeout, http, tls, connect, short or other) and a short `reason`. The address is never logged, only the host. A monitor line that was taken during or overlapping a test ends with `speed=1`: it is drawn on the charts but left out of the daily summary and the statistics. Scheduled tests add only a "Speed test finished" or "Speed test failed" note to the log, and a test started from the dashboard also adds "Speed test started".

## Project layout

```
README.md
start.sh, start.bat       start WiFiMonitor: asks, or takes --terminal / --service / --login
service.sh, service.bat   control the background service (start, stop, status, install)
uninstall.sh, .bat        stop the service and web server, remove the login item
AGENTS.md                 setup instructions for AI coding agents
src/
  collector.py            sampling and scanning (macOS and Windows)
  launch.py               what start.sh and start.bat run: mode choice, flags, foreground run
  service.py              background service: supervisor, start/stop/status, start at login
  dashboard_server.py     local web server and JSON API
  dashboard.html          the dashboard page
  logstore.py             daily log files, rotation, compression, reading
  macos/wifi-info.swift   CoreWLAN helper, built into bin/ on first run
tests/                    automated tests (see "Running the tests")
logs/                     log files (ignored by git)
bin/                      built macOS helper (ignored by git)
```

The server only listens on `127.0.0.1`. It only accepts notes, the scanning switch, the probe intervals, the Internet download speed settings and requests, and (in service mode) the Stop request, when they are posted from its own page. The speed settings are also refused unless the page was opened as `127.0.0.1` or `localhost`.

## Limitations

- **Network name and BSSID are not recorded.** macOS hides them from programs without Location Services permission, so the collector does not read them.
- **Windows signal strength is approximate.** Windows reports signal as a percentage, which is converted with `dBm = percent / 2 - 100`. Noise and channel width are not reported.
- **Measurement errors are kept apart from real problems.** A command that fails, times out or prints something unreadable is logged as `ERR` and a measurement error event. It is left out of packet loss, outages, availability and the call-ready score, and the dashboard shows a banner, an "ERR" value on the tiles, a "Measurement error" status and an entry in the Events card.
- **Missing fields are handled cleanly.** When the system does not report noise, SNR or channel width (Windows), the dashboard hides what has no data, shows "not reported here" on the tiles and adds a short note under the Signal and SNR charts. If no Wi-Fi details are logged at all, those notes say what to check, and the collector prints a one-time warning when `netsh` returns nothing. Ping-based tiles and charts keep working either way.
- **Windows needs an English display language, and other languages are not supported yet.** The collector reads `netsh` labels such as "State" and "Signal", and other languages use different words. When it sees non-English output, it logs `wifi=ERR` and a measurement error event that names the language, prints a warning at start-up, and the dashboard shows a banner saying the language is not supported yet. Ping times are still read in most languages, and real packet loss is still recognised in any language. macOS does not depend on the system language. Windows 11 24H2 and newer may also need Location Services turned on for `netsh wlan` to return results.
- **The speed test is one download from one server.** It cannot see your plan's real maximum on a fast connection, and the server, its distance and the time of day change the result. Use it to compare yourself against yourself. It uses real data (10 to 100 MB per test), so schedule it with care on a metered connection.
- **Windows scans use Windows' cached scan results,** which can be a little stale. macOS scans are live.
- **The Windows code is tested against sample `netsh` and `ping` output only,** not on a real Windows machine. The automated tests run on any system.
- Linux is not supported.

## Troubleshooting

- **"swiftc not found" on macOS:** run `xcode-select --install`, then start again. Until then only pings are logged.
- **Dashboard says "No recent data":** the collector is not running, or the computer was asleep. Keep the machine awake while monitoring (`caffeinate` on macOS).
- **Charts are blank:** check the browser can reach `cdnjs.cloudflare.com`.
- **The speed test fails with a certificate error on macOS:** the Python you use does not trust HTTPS certificates yet. If it is from python.org, run `Install Certificates.command` from its folder in `/Applications`. A custom `http` address avoids HTTPS altogether.
- **Run now is greyed out:** the collector is not running. Start the service (`./service.sh start`) or the collector, then try again. A test also cannot start at the same moment as a nearby-network scan, so it may wait a few seconds.
- **Port already in use:** start with another port, for example `PORT=8800 python3 src/dashboard_server.py`. If `start.sh --terminal` says the background service is already running, stop it with `./service.sh stop` or just open the dashboard.
- **Service port:** `PORT=8800 ./service.sh start` runs the service on that port. Use the same `PORT` for `status`, and note that the login start from `install` always uses the default 8765.
- **Dashboard is not reachable after `service.sh start`:** it prints where to look. Read `logs/service.log`, which shows the output of both processes and any restarts.
- **The Stop button is missing:** the page was not started by the service. Stop it with Ctrl-C in its window, or use the `service` commands to run it in the background.

## Running the tests

The tests use only the Python standard library, plus Node.js for the dashboard logic tests (they are skipped if Node is missing). They need no network, no Wi-Fi and no administrator rights, and they never touch your `logs/` folder.

```bash
python3 -m unittest discover -s tests          # python -m unittest discover -s tests on Windows
```

They cover the ping and `netsh` parsing for both systems (including real loss versus measurement errors, other languages and the unsupported-language error), the log line format, the collector loop, the server's parsing and daily summary, the start launcher and service helpers, and the dashboard's availability, loss and event logic. Windows and macOS behaviour is simulated with sample outputs, so a pass is not a substitute for trying the Windows build on a real machine.

## License

[MIT](LICENSE). You may use, modify and redistribute this tool, but you must keep the copyright notice (Alexander Uspensky) in copies and derived work.
