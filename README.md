# assay-lite

A cross-sectional signal tester that measures its own detection floor before it
measures your signal. Every test is registered before it runs, the significance
bar tightens with every trial, and "too small for this data to see" is reported as
UNDECIDABLE instead of "no effect".

One Python file. No network, no API keys, no accounts. Bring two CSVs.

Background: [STORY.md](STORY.md), on how a month of AI-run quant research found ten ways its own instrument was wrong, and why nothing got deployed.

## Quickstart

Requires [uv](https://docs.astral.sh/uv/) (it installs numpy, pandas and scipy for you).

```bash
uv run assay_lite.py demo
```

This writes synthetic prices for 40 assets over 2,520 days to `./demo/` with three
signals, and runs the whole pipeline. Three endings:

- **strong**: planted at a known strength. PASS.
- **weak**: planted too, but too small for this much data to tell from zero.
  UNDECIDABLE, not "no effect".
- **sector**: no planted effect, and a sector tilt that is redrawn every 63 days.
  Its daily IC swings with four sectors rather than forty assets, so `test` scales
  its floor up before concluding anything (see "Why the floor is per-signal").

The demo's trials are charged to `demo/.assay-lite`, not to your own registry.

## Your own data

Two long-format CSVs:

```
prices.csv   date,asset,close
signal.csv   date,asset,signal
```

The signal on a date must be known at that date's close. It is scored against the
log return from that close to the close `--horizon` rows later. A row is a date
that appears in your price file.

```bash
uv run assay_lite.py breadth  --prices prices.csv
uv run assay_lite.py floor    --prices prices.csv --horizon 1
uv run assay_lite.py test     --prices prices.csv --signal signal.csv --name my_idea --horizon 1
uv run assay_lite.py registry
```

| command | what it does |
|---|---|
| `breadth` | Effective number of independent bets, from the eigenvalues of the return correlation matrix: (Σλ)² / Σλ². Forty assets that move together are a handful of bets, not forty. |
| `floor` | Injects a synthetic signal of known strength β into your real forward returns, many times per β, and counts how often the same code that tests your signal catches it. The floor is the smallest β caught 80% of the time, and the IC it reads out as. |
| `test` | Registers the trial, then computes the mean daily Spearman IC, its t-statistic and a two-sided p. Prints PASS, UNDECIDABLE or FAIL. |
| `registry` | Every trial ever charged, its result, the bar it faced, and the current bar. |

State lives in `./.assay-lite/` (change it with `--home`).

## The three verdicts

| verdict | condition | meaning |
|---|---|---|
| PASS | p < 0.05 / (trials charged) | Cleared the multiple-testing bar. Not proof of profit. |
| UNDECIDABLE | \|IC\| below this signal's floor IC, or no floor measured for this data and horizon | Any true effect is smaller than this data can detect. Not evidence of no effect. |
| FAIL | \|IC\| at or above this signal's floor IC, but p does not clear the bar | An effect this size would usually be caught; this one is too inconsistent. |

"This signal's floor" is the calibrated floor scaled for the signal's own dispersion;
see below.

A signal with no effect at all will normally come out UNDECIDABLE, not FAIL. That
is deliberate: no test on finite data can show an effect is exactly zero. What it
can show is an upper bound, and the floor IC is that bound.

## Why re-runs are charged

`test` appends the trial to `registry.jsonl` before computing anything, and there
is no delete or refund command. The bar is 0.05 divided by the number of trials.

If you test twenty variations of an idea, one will clear p < 0.05 by luck about
two times in three. Charging every look, including re-runs of the same signal, is
what keeps the bar honest. A crashed or disappointing run still counts. Deleting
lines from the file, or starting a fresh `--home` to reset the count, defeats the
tool. Nothing stops you doing that; it is your file. The registry exists so that
you can see when you have.

## How the floor stays honest

- `floor` and `test` call the same function to compute IC and p. A calibration of
  a different code path calibrates something you never run.
- `floor` caches every repetition's IC and p, keyed by a hash of the price file's
  contents and the horizon. `test` recomputes the power at whatever bar it faces,
  so the floor tightens as trials accumulate. It never reuses a floor measured on
  different data.

### Why the floor is per-signal

Whether an effect is detectable depends on how much the daily IC swings, not only
on its average. The floor is calibrated with noise that is fresh every day. Real
signals often are not: a sector tilt or a slow-moving rank makes many assets move
together, so there are fewer independent bets and the daily IC swings more. The
same mean IC is then harder to see, and an unscaled floor would call some
undetectable effects FAIL.

So `test` measures the spread of your signal's daily IC, divides it by the spread
under the calibration noise, and multiplies the floor by that ratio. It never
shrinks the floor below the calibrated one. Measured on the demo data:

| signal | daily-IC spread vs calibration noise |
|---|---|
| planted signals (fresh noise each day) | 0.98 and 1.01 |
| trailing 20-day return, no planted effect | 1.17 |
| sector tilt redrawn every 63 days, no planted effect | 1.68 |

A sector-tilt signal on this data needs an IC about 1.7 times the calibrated floor
before a miss can be called FAIL.

## Limitations

- **Cross-sectional only.** It ranks assets against each other each day. It does
  not test timing signals on a single asset.
- **Daily data, Spearman IC only.** No other statistics, no portfolio simulation.
- **No transaction costs.** A PASS says nothing about returns after costs.
- **Bonferroni is conservative.** When your trials are correlated, it overstates
  how many independent looks you took, so the bar is stricter than it needs to be.
- **Overlapping horizons use every h-th day.** For `--horizon` above 1, only
  non-overlapping days are used, which discards data in exchange for independence.

## Claude Code skill

`skills/calibrate-first/SKILL.md` makes Claude Code run `floor` before reading any
result, compare p only with the registry bar, never suggest resetting the
registry, and report below-floor results as UNDECIDABLE. Copy the folder into
`.claude/skills/` in your project.

## Tests

```bash
uv run --with pytest --with numpy --with pandas --with scipy pytest tests
```

## License

MIT
