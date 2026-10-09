# Instructions for AI coding agents

This is WiFiMonitor, a Wi-Fi stability monitor (Python standard library only, macOS and Windows). To set it up and run it for the user, do the following without asking the user which mode to use.

1. Check that Python 3.8+ is available (`python3 --version`, or `python --version` on Windows). On macOS also check `xcode-select -p`; if it is missing, tell the user to run `xcode-select --install`, since that needs their interaction.
2. Start it in the background, which does not block you:
   - macOS: `./start.sh --service`
   - Windows: `start.bat --service`
   Use `--login` instead of `--service` only if the user asked for it to start at every login, because that adds a login item to their account. Do not use `--terminal`: it blocks until Ctrl-C.
3. Verify with `./service.sh status` (`service.bat status` on Windows). If the dashboard is not answering, read `logs/service.log`, fix the cause (for example a busy port: `PORT=8800 ./service.sh start`) and try again.
4. Tell the user the dashboard URL (default <http://127.0.0.1:8765>) and how to stop it: the **Stop** button in the dashboard header, or `./service.sh stop`.

Extra arguments go to the collector, for example `./start.sh --service --interval 10 --scan-every 600`. `./start.sh` with no flag never prompts when there is no interactive terminal, and starts the background service.

Details, permissions and troubleshooting are in README.md. Do not commit `logs/` or `bin/`.
