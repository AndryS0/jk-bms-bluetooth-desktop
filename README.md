# jk-bms-logger
|General view | Settings tab|
| :--- | ---: |
|<img width="1308" height="954" alt="image" src="https://github.com/user-attachments/assets/58d858f5-01c8-4254-bfe0-cb38d09284c9" />|<img width="1439" height="838" alt="image" src="https://github.com/user-attachments/assets/d41f5e41-2e31-4489-b89c-dbd15a2c4b61" />|


A lightweight logger and dashboard for a JK BMS, read over Bluetooth from a
Windows PC. I made it for my own setup: a 4S LiFePO4 UPS battery (REPT
72174L4 280 Ah cells) on a JK_B1A8S10P.

**This is not a general-purpose solution**, and it isn't meant to compete with
or replace existing projects like
[esphome-jk-bms](https://github.com/syssi/esphome-jk-bms),
[batmon-ha](https://github.com/fl4p/batmon-ha) or
[BMS_BLE-HA](https://github.com/patman15/BMS_BLE-HA). It's small, built around
my own needs, and only tested with my BMS. If you try it, read the code first,
and set your own `[limits]` in `config.toml`: the ones in `config.example.toml`
are for my cells.

## What it does

- Reads the BMS every 5 seconds and stores the readings in SQLite
  (`data/bms.sqlite3`).
- Serves a dashboard at http://127.0.0.1:8765 (only reachable from this PC):
  - current readings, graphs (voltage, current, cells, cell difference, state
    of charge, temperatures, switches), charge/discharge runs, events and CSV
    export;
  - a BMS settings tab to view the settings and change the common ones. After
    each write the logger reads all settings back to confirm the change. What
    may be changed, and within which range, comes from `[limits]` in
    `config.toml`; settings without a range there stay read-only.

    


## Run

Needs Windows 10 or later with Bluetooth LE, and [uv](https://docs.astral.sh/uv/).

```
copy config.example.toml config.toml    # then set your BMS's Bluetooth name and [limits]
uv sync
uv run jkbms_logger.py
```

To start it hidden at login, put a shortcut to
`.venv\Scripts\pythonw.exe jkbms_logger.py` (started in this folder) into
`shell:startup`. To stop it, end the `pythonw.exe` processes running
`jkbms_logger.py`.

The BMS accepts one Bluetooth connection at a time. Press **Free Bluetooth for
10 min** on the dashboard before using the JK phone app.

## Credits

- [aiobmsble](https://pypi.org/p/aiobmsble/): Bluetooth connection and readings.
- [esphome-jk-bms](https://github.com/syssi/esphome-jk-bms): settings frame
  layout and write registers.
- [uPlot](https://github.com/leeoniya/uPlot): charts (MIT license, included in
  `web/vendor`).
