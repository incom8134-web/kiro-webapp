# Battery monitor

The Matek I2C-INA-BM board on the Jetson is a TI INA228. The Battery page
(`web/battery.html`, header on the control page, the dashboard and Studio)
shows what that chip measures, plus three estimates the chip does not provide.

Nothing here moves the robot.

## What the chip measures

| Field | Meaning |
|---|---|
| Bus voltage | Pack voltage, 0–85 V |
| Current | Signed. Positive is discharge when IN+ faces the battery |
| Power | Voltage times the size of the current |
| Energy | Watt-hours counted since the board last powered on |
| Charge | Amp-hours counted since the board last powered on |
| Temperature | The chip itself, not the cells |
| Alerts | Limit and overflow flags from the chip |

Shunt on this board: 0.0002 ohm. Full-scale current: 204.8 A, which is 40.96 mV
across the shunt, so the reader uses the finer ADC range (ADCRANGE = 1).

`CURRENT_LSB = 204.8 / 2^19`. `SHUNT_CAL = 13107.2e6 × CURRENT_LSB × 0.0002 × 4`
which is 4096 for this board.

The board needs 4–9 V on its 5 V pin. On the Jetson 40-pin header:

| Board | Jetson pin |
|---|---|
| 5 V | 2 or 4 |
| Ground | 6 |
| SCL | 5 |
| SDA | 3 |

Pins 3 and 5 are usually `/dev/i2c-7`. The address jumpers are 69 (default,
0x45), 68 (0x44) or 65 (0x41).

## What the page estimates

The chip has no state of charge, no health and no cycle count. The page says
**Estimated** on health and cycles.

| Estimate | How |
|---|---|
| Percent | Starts from the cell voltage. Once the current stays near zero (default 0.15 A for 20 s), the percent is replaced by a rested-voltage table for LiPo, Li-ion or LiFePO4. Between those quiet moments it follows the chip's charge register. |
| Time remaining | Amp-hours left (or amp-hours to full, while charging) at the present current. Blank when the current is near zero. |
| Health | Learned full capacity divided by the capacity you configured, after a quiet-to-quiet swing of at least 30 points and 0.5 Ah in the same direction. Blank until that happens. 80% and above is shown as good in the header, 60% as fair, below that as poor. |
| Cycles | Amp-hours discharged, divided by the capacity you configured. One cycle is one full capacity of discharge, added up, not one plug-in. |

A power loss on the board zeros its energy and charge registers. A large step
backwards is ignored so it does not look like a sudden charge. The cycle count
and the learned capacity are kept in a JSON file:

| Mode | File |
|---|---|
| Robot | `~/.config/kiro-webapp/battery_state.json` |
| Mock (`./run.sh`) | `~/.config/kiro-webapp/battery_state_mock.json` |

`KIRO_BATT_STATE` overrides both. `KIRO_BATT_CYCLE_LIFE` (the maker's cycle
life) is stored in that file. It is not a measurement and it is not shown as one.

The pack defaults (4S LiPo, 10 Ah) are a stand-in so the page has numbers.
Set them to the pack on this robot before trusting the percent.

## Settings

```bash
KIRO_BATT_I2C_BUS=7
KIRO_BATT_I2C_ADDR=69          # or 0x45, 68, 65
KIRO_BATT_CHEMISTRY=lipo       # lipo, li-ion, lifepo4
KIRO_BATT_CELLS=4
KIRO_BATT_CAPACITY_AH=10
KIRO_BATT_CYCLE_LIFE=500       # optional, maker's number
KIRO_BATT_SHUNT_OHM=0.0002     # leave this on the Matek board
KIRO_BATT_MAX_A=204.8
```

The reader uses `smbus2`. If that package is missing, or no INA228 answers,
the page says **Not connected** and the header percent stays unknown.

```bash
.venv/bin/pip install smbus2
```

The process also needs permission to open the I2C device. On the Jetson that
is a one-time step you run yourself:

```bash
sudo usermod -aG i2c $USER
```

Log out and back in after that. The server does not run `sudo`.

## API

`GET /api/battery` needs the same login cookie as the other APIs. `source` is
`ina228`, `mock` or `unavailable`.

The header on every page still receives only `{percent, charging, health, source}`
inside the robot snapshot. `health` there is the word good, fair, poor or
unknown. The full numbers are on the Battery page.

In mock mode (`./run.sh`, PIN 0000) the source is `mock`: a sample pack near
76% that wanders, so the chart and the gauge can be checked with no board fitted.
