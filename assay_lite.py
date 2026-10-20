# /// script
# requires-python = ">=3.10"
# dependencies = ["numpy", "pandas", "scipy"]
# ///
"""assay-lite: calibrate first, then measure. A cross-sectional signal tester that
knows the difference between "no effect" and "too small for this data to see".

    uv run assay_lite.py demo                                    # synthetic walkthrough
    uv run assay_lite.py floor    --prices P [--horizon H]       # detection floor
    uv run assay_lite.py test     --prices P --signal S --name N [--horizon H]
    uv run assay_lite.py registry                                # every trial ever run
    uv run assay_lite.py breadth  --prices P                     # independent bets

State lives in ./.assay-lite/ (override with --home). Nothing touches the network.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import warnings
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

ALPHA = 0.05            # family-wise error rate, split across every trial ever charged
MIN_ASSETS = 10         # a day needs at least this many assets to produce an IC
MIN_OBS = 20            # fewer non-overlapping days than this and nothing is reported
POWER_TARGET = 0.80
DEFAULT_BETAS = [0.0, 0.005, 0.01, 0.015, 0.02, 0.03, 0.05, 0.1]


# --- data ------------------------------------------------------------------------

def read_long(path: str, value: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    missing = {"date", "asset", value} - set(df.columns)
    if missing:
        raise SystemExit(f"{path}: missing column(s) {sorted(missing)}; expected date,asset,{value}")
    if df.duplicated(["date", "asset"]).any():
        raise SystemExit(f"{path}: duplicate (date, asset) rows")
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot(index="date", columns="asset", values=value).sort_index()


def load_prices(path: str) -> pd.DataFrame:
    px = read_long(path, "close").astype(float)
    if (px <= 0).any().any():
        raise SystemExit(f"{path}: close must be positive")
    return px


def load_signal(path: str, px: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    sig = read_long(path, "signal").astype(float)
    unmatched = len(sig.index.difference(px.index))
    return sig.reindex(index=px.index, columns=px.columns), unmatched


def forward_returns(px: pd.DataFrame, h: int) -> pd.DataFrame:
    """Log return from row t's close to row t+h's close: strictly after the signal
    date. The last h rows have no target (NaN)."""
    return np.log(px.shift(-h) / px)


def file_id(path: str) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:16]


# --- the measurement: floor and test both call evaluate() and nothing else --------

def daily_ic(x: np.ndarray, y: np.ndarray) -> np.ndarray:
    """Spearman correlation across assets, one per row, on jointly observed cells."""
    x, y = np.array(x, dtype=float), np.array(y, dtype=float)
    bad = np.isnan(x) | np.isnan(y)
    x[bad], y[bad] = np.nan, np.nan
    rx = pd.DataFrame(x).rank(axis=1).to_numpy()
    ry = pd.DataFrame(y).rank(axis=1).to_numpy()
    n = (~bad).sum(axis=1)
    with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        dx = rx - np.nanmean(rx, axis=1, keepdims=True)
        dy = ry - np.nanmean(ry, axis=1, keepdims=True)
        ic = np.nansum(dx * dy, axis=1) / np.sqrt(np.nansum(dx * dx, axis=1)
                                                    * np.nansum(dy * dy, axis=1))
    ic[n < MIN_ASSETS] = np.nan
    return ic


def evaluate(x: np.ndarray, y: np.ndarray, h: int) -> dict:
    """Mean daily IC, t and two-sided p; for h > 1 only every h-th (non-overlapping)
    day. The ONE code path: calibrating a different pipeline calibrates nothing."""
    ic = daily_ic(x, y)[::h]
    ic = ic[~np.isnan(ic)]
    n = len(ic)
    if n < MIN_OBS:
        return {"ic": float("nan"), "t": float("nan"), "p": 1.0, "n": n, "sd": float("nan")}
    m, sd = float(ic.mean()), float(ic.std(ddof=1))
    t = m / (sd / math.sqrt(n)) if sd > 0 else math.inf
    return {"ic": m, "t": t, "p": float(2 * stats.t.sf(abs(t), n - 1)), "n": n, "sd": sd}


# --- registry: append-only JSONL -------------------------------------------------

class Home:
    def __init__(self, root: str):
        self.root = Path(root)
        self.registry = self.root / "registry.jsonl"
        self.floors = self.root / "floors.json"

    def append(self, rec: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.registry, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")

    def records(self) -> list[dict]:
        if not self.registry.exists():
            return []
        lines = self.registry.read_text(encoding="utf-8").splitlines()
        return [json.loads(line) for line in lines if line.strip()]

    def trials(self) -> int:
        return sum(1 for r in self.records() if r["kind"] == "trial")

    def load_floors(self) -> dict:
        return json.loads(self.floors.read_text(encoding="utf-8")) if self.floors.exists() else {}

    def save_floor(self, key: str, value: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        cache = self.load_floors()
        cache[key] = value
        self.floors.write_text(json.dumps(cache), encoding="utf-8")


def bar_for(trials: int) -> float:
    return ALPHA / max(1, trials)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- floor -------------------------------------------------------------------------

def zscore_rows(y: np.ndarray) -> np.ndarray:
    with warnings.catch_warnings(), np.errstate(invalid="ignore", divide="ignore"):
        warnings.simplefilter("ignore", RuntimeWarning)
        return (y - np.nanmean(y, axis=1, keepdims=True)) / np.nanstd(y, axis=1, keepdims=True)


def run_floor(px: pd.DataFrame, h: int, home: Home, key: str, betas: list[float],
              reps: int, seed: int) -> dict:
    """Inject a signal of known strength beta into the real forward returns and count
    how often evaluate() catches it. Every rep's (IC, p, sd) is cached: power can be
    recomputed at any later bar, and the beta=0 sd is the noise `test` scales by."""
    y = forward_returns(px, h).to_numpy()
    z = zscore_rows(y)
    rng = np.random.default_rng(seed)
    curve = {}
    for b in sorted({0.0, *betas}):
        rs = []
        for _ in range(reps):
            x = b * z + math.sqrt(max(0.0, 1 - b * b)) * rng.standard_normal(z.shape)
            x[np.isnan(z)] = np.nan
            rs.append(evaluate(x, y, h))
        curve[str(b)] = {k: [r[k] for r in rs] for k in ("ic", "p", "sd")}
    noise_sd = float(np.nanmedian(curve["0.0"]["sd"]))
    home.save_floor(key, {"created": now(), "reps": reps, "noise_sd": noise_sd, "curve": curve})
    return curve


def floor_at(curve: dict, bar: float) -> tuple[list, tuple | None]:
    rows, floor = [], None
    for b in sorted(curve, key=float):
        ic, p = np.array(curve[b]["ic"], dtype=float), np.array(curve[b]["p"], dtype=float)
        power = float(np.mean((p < bar) & (ic > 0)))
        rows.append((float(b), float(np.nanmean(ic)), power))
        if floor is None and float(b) > 0 and power >= POWER_TARGET:
            floor = (float(b), float(np.nanmean(ic)))
    return rows, floor


# --- test --------------------------------------------------------------------------

def run_test(px: pd.DataFrame, sig: pd.DataFrame, name: str, h: int, home: Home,
             key: str, signal_id: str) -> dict:
    n = home.trials() + 1   # registered BEFORE computing: a crash or a dud still counts
    home.append({"kind": "trial", "n": n, "ts": now(), "name": name, "horizon": h,
                 "prices": key.split(":")[0], "signal": signal_id})
    bar = bar_for(n)
    r = evaluate(sig.to_numpy(), forward_returns(px, h).to_numpy(), h)
    cached = home.load_floors().get(key)
    if cached and "noise_sd" not in cached:
        cached = None                   # cached before per-signal scaling existed
    floor = floor_at(cached["curve"], bar)[1] if cached else None
    # Calibration noise is fresh every day; a factor-structured signal's daily IC is more
    # dispersed, so the same mean IC is harder to detect. Scale up, never down.
    raw = r["sd"] / cached["noise_sd"] if cached and not math.isnan(r["sd"]) else 1.0
    ratio, eff = max(1.0, raw), (floor[1] * max(1.0, raw) if floor else None)
    verdict, why = decide(r, bar, cached is not None, eff, h)
    unscaled = decide(r, bar, cached is not None, floor[1] if floor else None, h)[0]
    home.append({"kind": "result", "trial": n, "ts": now(), "verdict": verdict, "ic": r["ic"],
                 "t": r["t"], "p": r["p"], "n_obs": r["n"], "bar": bar,
                 "floor_ic": floor[1] if floor else None, "dispersion_ratio": ratio,
                 "dispersion_ratio_raw": raw, "effective_floor_ic": eff})
    return {**r, "trial": n, "bar": bar, "floor": floor, "verdict": verdict, "why": why,
            "ratio": ratio, "ratio_raw": raw, "eff_floor": eff, "verdict_unscaled": unscaled,
            "floor_created": cached["created"] if cached else None}


def decide(r: dict, bar: float, floored: bool, eff_floor: float | None, h: int) -> tuple[str, str]:
    if math.isnan(r["ic"]):
        return "UNDECIDABLE", f"only {r['n']} usable days (need {MIN_OBS})"
    if r["p"] < bar:
        return "PASS", "p clears the bar" + (" (negative IC: ranks inversely)" * (r["ic"] < 0))
    if not floored:
        return "UNDECIDABLE", f"no floor for this data at horizon {h}; run `floor` first"
    if eff_floor is None:
        return "UNDECIDABLE", f"no beta in the floor grid reaches {POWER_TARGET:.0%} power at bar {bar:.5f}"
    if abs(r["ic"]) < eff_floor:
        return "UNDECIDABLE", (f"|IC| {abs(r['ic']):.4f} is below the floor IC {eff_floor:.4f}: any "
                               "true effect this small cannot be told from zero with this data. "
                               "This is not 'no effect'.")
    return "FAIL", (f"|IC| {abs(r['ic']):.4f} is at or above the floor IC {eff_floor:.4f}, an effect "
                    "this data could see, yet p does not clear the bar: it is too inconsistent to count")


# --- breadth -----------------------------------------------------------------------

def breadth(px: pd.DataFrame) -> tuple[int, float, float]:
    """Participation ratio of the correlation eigenvalues: (sum l)^2 / sum l^2.
    N identical assets give 1; N independent ones give N."""
    c = np.log(px).diff().corr(min_periods=60)
    ok = c.notna().all()
    c = c.loc[ok, ok]
    lam = np.clip(np.linalg.eigvalsh(c.to_numpy()), 0, None)
    return len(c), float(lam.sum() ** 2 / (lam ** 2).sum()), float(lam.max() / lam.sum())


# --- demo data ---------------------------------------------------------------------

A_STRONG, A_WEAK = 0.0008, 0.00004   # planted payoff per unit of signal, per day


def make_demo(n_assets: int = 40, n_days: int = 2520, seed: int = 7):
    """Market + 4 sectors (breadth far below N); two planted signals (known at t, paid
    t->t+1); a sector tilt redrawn every 63 days with NO planted effect."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range("2015-01-02", periods=n_days)
    assets = [f"A{i:02d}" for i in range(n_assets)]
    sector = np.arange(n_assets) % 4
    loading = 1 + 0.2 * rng.standard_normal(n_assets)
    ret = (0.010 * rng.standard_normal(n_days)[:, None] * loading
           + 0.007 * rng.standard_normal((n_days, 4))[:, sector]
           + 0.012 * rng.standard_normal((n_days, n_assets)))
    strong, weak = rng.standard_normal((2, n_days, n_assets))
    ret[1:] += A_STRONG * strong[:-1] + A_WEAK * weak[:-1]
    px = 100 * np.exp(np.cumsum(ret, axis=0))
    tilt = np.repeat(rng.standard_normal((n_days // 63 + 1, 4)), 63, axis=0)[:n_days][:, sector]

    def long(a, col):
        return (pd.DataFrame(a, index=dates, columns=assets).rename_axis("date")
                .reset_index().melt(id_vars="date", var_name="asset", value_name=col))
    return long(px, "close"), long(strong, "signal"), long(weak, "signal"), long(tilt, "signal")


# --- CLI ---------------------------------------------------------------------------

def show_floor(curve: dict, bar: float, h: int) -> None:
    rows, floor = floor_at(curve, bar)
    print(f"{'beta':>7}{'measured IC':>13}{'power@bar':>11}")
    for b, ic, pw in rows:
        note = "   <- false-positive rate; should be ~0" if b == 0 else ""
        print(f"{b:>7.3f}{ic:>+13.4f}{pw:>10.0%}{note}")
    print("FLOOR: " + (f"beta >= {floor[0]:.3f}, measured IC {floor[1]:.4f} "
                       f"({POWER_TARGET:.0%} power at bar {bar:.5f}, horizon {h})"
                       if floor else f"not reached in this grid at bar {bar:.5f}; widen --betas"))


def cmd_floor(a) -> int:
    home, px = Home(a.home), load_prices(a.prices)
    key = f"{file_id(a.prices)}:h{a.horizon}"
    nxt = bar_for(home.trials() + 1)
    print(f"prices {a.prices}: {px.shape[1]} assets x {px.shape[0]} days, horizon {a.horizon}, "
          f"{a.reps} reps per beta")
    print(f"trials so far {home.trials()} -> the next test faces bar p < {nxt:.5f}")
    show_floor(run_floor(px, a.horizon, home, key, a.betas, a.reps, a.seed), nxt, a.horizon)
    print("Cached. `test` recomputes power at whatever bar it faces.")
    return 0


def cmd_test(a) -> int:
    home, px = Home(a.home), load_prices(a.prices)
    sig, unmatched = load_signal(a.signal, px)
    if unmatched:
        print(f"note: {unmatched} signal dates are not in the price file and were ignored")
    r = run_test(px, sig, a.name, a.horizon, home, f"{file_id(a.prices)}:h{a.horizon}",
                 file_id(a.signal))
    print(f"trial #{r['trial']} registered BEFORE computing: {a.name} (horizon {a.horizon})")
    print(f"bar = {ALPHA} / {r['trial']} trials = {r['bar']:.5f}")
    print(f"mean IC {r['ic']:+.4f}   t {r['t']:.2f}   p {r['p']:.5f}   "
          f"({r['n']} non-overlapping days)")
    if r["floor"]:
        f, e = r["floor"][1], r["eff_floor"]
        print(f"floor at this bar: IC {f:.4f} (floor run {r['floor_created'][:10]})")
        why = ("factor structure: fewer independent bets" if r["ratio"] >= 1.10
               else "within sampling noise of 1" if r["ratio"] > 1.0
               else f"{r['ratio_raw']:.2f}x, clamped: the floor is never shrunk")
        print(f"your signal's daily IC is {r['ratio']:.2f}x as dispersed as the calibration "
              f"noise ({why}); floor {f:.4f} -> {e:.4f}")
    print(f"{r['verdict']}: {r['why']}")
    if r["verdict_unscaled"] != r["verdict"]:
        print(f"an iid-calibrated floor ({r['floor'][1]:.4f}) would have called this {r['verdict_unscaled']}")
    return 0


def cmd_registry(a) -> int:
    home = Home(a.home)
    recs = home.records()
    results = {r["trial"]: r for r in recs if r["kind"] == "result"}
    trials = [r for r in recs if r["kind"] == "trial"]
    print(f"{'#':>3}  {'registered (UTC)':<20}{'name':<20}{'h':>3}  {'verdict':<12}"
          f"{'IC':>9}{'p':>10}{'bar then':>10}")
    for t in trials:
        r = results.get(t["n"], {})
        ic = f"{r['ic']:+.4f}" if r.get("ic") is not None and not math.isnan(r["ic"]) else "-"
        p, bar = (f"{r['p']:.5f}", f"{r['bar']:.5f}") if r else ("-", "-")
        print(f"{t['n']:>3}  {t['ts'][:19]:<20}{t['name'][:19]:<20}{t['horizon']:>3}  "
              f"{r.get('verdict', 'NO RESULT'):<12}{ic:>9}{p:>10}{bar:>10}")
    n = len(trials)
    print(f"trials charged: {n} -> current bar p < {bar_for(n):.5f}; "
          f"the next test faces {bar_for(n + 1):.5f}")
    print("Append-only. There is no delete or refund: a re-run of the same signal is a new "
          "look at the data and is charged again.")
    return 0


def cmd_breadth(a) -> int:
    n, eff, top = breadth(load_prices(a.prices))
    print(f"assets with enough overlapping history: {n}")
    print(f"effective independent bets: {eff:.1f}   (largest eigenvalue explains {top:.0%})")
    print("A cross-sectional test has the power of ~this many bets, not of N.")
    return 0


def cmd_demo(a) -> int:
    d = Path(a.dir)
    if d.exists():
        raise SystemExit(f"{d}/ already exists. Its registry has charged trials; "
                         "delete the folder yourself to run the demo again.")
    d.mkdir(parents=True)
    names = ("prices", "signal_strong", "signal_weak", "signal_sector")
    for fname, df in zip(names, make_demo()):
        df.to_csv(d / f"{fname}.csv", index=False)
    print(f"wrote {d}/prices.csv (40 assets x 2520 days) and three signals.\n"
          f"The demo charges trials to {d}/.assay-lite, never to your own registry.\n")
    p, home, ns = str(d / "prices.csv"), str(d / ".assay-lite"), argparse.Namespace
    steps = [("breadth", cmd_breadth, ns(home=home, prices=p)),
             ("floor", cmd_floor, ns(home=home, prices=p, horizon=1, reps=a.reps,
                                     seed=0, betas=DEFAULT_BETAS))]
    steps += [(f"test {s}", cmd_test, ns(home=home, prices=p, signal=str(d / f"signal_{s}.csv"),
                                        name=s, horizon=1)) for s in ("strong", "weak", "sector")]
    for title, fn, args in steps + [("registry", cmd_registry, ns(home=home))]:
        print(f"== {title} " + "=" * (60 - len(title)))
        fn(args)
        print()
    print("Three endings. strong: planted, PASSES. weak: planted but too small for this data, "
          "UNDECIDABLE.\nsector: no planted effect; its daily IC swings with 4 sectors, not 40 "
          "assets, so its floor is scaled up before anything is concluded.")
    return 0


def main(argv: list[str] | None = None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", default=".assay-lite", help="registry and floor cache folder")
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    req, hz, reps = {"required": True}, {"type": int, "default": 1}, {"type": int, "default": 50}
    options = {"demo": [("--dir", {"default": "demo"}), ("--reps", reps)],
               "floor": [("--prices", req), ("--horizon", hz), ("--reps", reps),
                         ("--seed", {"type": int, "default": 0}),
                         ("--betas", {"type": float, "nargs": "+", "default": DEFAULT_BETAS})],
               "test": [("--prices", req), ("--signal", req), ("--name", req), ("--horizon", hz)],
               "registry": [], "breadth": [("--prices", req)]}
    for name, opts in options.items():
        s = sub.add_parser(name, parents=[common])
        for flag, kw in opts:
            s.add_argument(flag, **kw)
    a = p.parse_args(argv)
    if getattr(a, "horizon", 1) < 1:
        raise SystemExit("--horizon must be >= 1")
    return {"demo": cmd_demo, "floor": cmd_floor, "test": cmd_test,
            "registry": cmd_registry, "breadth": cmd_breadth}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
