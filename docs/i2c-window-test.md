# Timed I2C observation

English | [日本語](ja/i2c-window-test.md)

[Setup](setup.md) · [Python and C API](api.md)

`examples/i2c_window_test.py` uses `CXD56xx` to observe communication, position
fixes and update intervals separately for a fixed duration. It does not change
the driver's startup sequence. Use the [checkout build instructions](api.md#building-from-a-checkout)
to install the current driver into your virtual environment, with the GPIO
chip verified on your board. Complete the wiring, 400 kHz I2C and access
permissions first, and stop other processes using the receiver.

## Run

Activate that environment and run from the repository root:

```sh
python examples/i2c_window_test.py \
  --duration 600 --board-model 'Verified board model'
```

The default poll interval is 0.2 seconds; override it with `--interval`.
Progress is printed every 30 seconds without coordinates. Ctrl+C or SIGTERM
requests an orderly stop with a partial summary and `close()`.
An outer Linux timeout can bound a stalled native shutdown:

```sh
timeout --signal=INT --kill-after=15s 660s \
  python -u examples/i2c_window_test.py --duration 600 --board-model 'Verified board model'
```

## Results

Logs default to `$XDG_STATE_HOME/rpi-cxd56xx-gnss/<UTC>/` or
`~/.local/state/rpi-cxd56xx-gnss/<UTC>/`. `--log-dir` can select a fresh directory
outside any Git checkout. It is created with owner-only access. Logs contain
positions and local diagnostics; do not commit them.

| File | Contents |
| --- | --- |
| `samples.jsonl` | Poll results, timestamps, position and fix fields |
| `summary.json` | Communication/fix status, first response/fix, update intervals and shutdown result |
| `driver.stderr.log` | Python and C worker diagnostics |

`communication_success` means at least one successful `peek()`; initialization
alone does not qualify. `live_updates_observed` requires at least two different
receiver timestamps. Cached reads do not count as new updates. A valid fix
requires mode 2 or 3, status 1 or 2, valid coordinates and a recent timestamp
update. First-fix time is measured from this run's driver initialization, not
as cold-start TTFF.

`fix_success` retains any valid fix observed during the run. Inspect
`last_timestamp_update_age_s` to detect a later stall and `driver.stderr.log`
for startup failures. Satellite fields count signal entries/tracking flags,
which can include multiple signals from one satellite. `close_success` reports
acknowledged positioning stop and resource release; shutdown failures are also
recorded in `errors`.

Exit codes are `0` when communication succeeded, `1` for errors, `2` for no
successful reads, and `130` for a clean interrupted observation. Lack of a fix
alone is not a communication failure. Forced termination may leave the summary
incomplete.
