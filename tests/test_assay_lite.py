import importlib.util
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

SRC = Path(__file__).resolve().parent.parent / "assay_lite.py"
spec = importlib.util.spec_from_file_location("assay_lite", SRC)
al = importlib.util.module_from_spec(spec)
spec.loader.exec_module(al)


def wide(long_df, col):
    df = long_df.copy()
    df["date"] = pd.to_datetime(df["date"])
    return df.pivot(index="date", columns="asset", values=col).sort_index()


@pytest.fixture(scope="module")
def demo_small():
    prices, strong, weak, _ = al.make_demo(n_assets=40, n_days=1200, seed=3)
    px = wide(prices, "close")
    return px, wide(strong, "signal"), wide(weak, "signal")


def test_forward_return_is_strictly_after_signal_date():
    px = pd.DataFrame({"a": [100.0, 110.0, 121.0, 133.1]},
                      index=pd.bdate_range("2024-01-01", periods=4))
    f1 = al.forward_returns(px, 1)
    assert f1.iloc[0, 0] == pytest.approx(np.log(110 / 100))
    assert np.isnan(f1.iloc[-1, 0])
    f2 = al.forward_returns(px, 2)
    assert f2.iloc[0, 0] == pytest.approx(np.log(121 / 100))
    assert f2.iloc[-2:, 0].isna().all()


def test_signal_that_peeks_at_the_same_day_return_gets_no_credit():
    rng = np.random.default_rng(0)
    r = rng.normal(0, 0.01, (600, 30))
    px = pd.DataFrame(100 * np.exp(np.cumsum(r, axis=0)))
    same_day = pd.DataFrame(r)   # return INTO date t, already known at t's close
    res = al.evaluate(same_day.to_numpy(), al.forward_returns(px, 1).to_numpy(), 1)
    assert abs(res["ic"]) < 0.02


def test_daily_ic_matches_scipy_spearman_with_ties_and_gaps():
    from scipy import stats
    x = np.array([[1, 2, 2, 4, np.nan, 6, 7, 8, 9, 10, 11, 12]], dtype=float)
    y = np.array([[3, 1, 2, 5, 4, np.nan, 9, 8, 7, 12, 10, 11]], dtype=float)
    ok = ~np.isnan(x[0]) & ~np.isnan(y[0])
    expected = stats.spearmanr(x[0][ok], y[0][ok]).statistic
    assert al.daily_ic(x, y)[0] == pytest.approx(expected)


def test_too_few_assets_gives_no_ic():
    x = np.arange(5, dtype=float)[None, :]
    assert np.isnan(al.daily_ic(x, x)[0])


def test_overlapping_horizon_uses_every_hth_day(demo_small):
    px, strong, _ = demo_small
    y = al.forward_returns(px, 5).to_numpy()
    r = al.evaluate(strong.to_numpy(), y, 5)
    assert r["n"] == pytest.approx(len(px) / 5, abs=2)


def test_registry_charges_reruns_and_bar_tightens(tmp_path, demo_small):
    px, strong, _ = demo_small
    home = al.Home(str(tmp_path))
    key = "pricesid:h1"
    a = al.run_test(px, strong, "same", 1, home, key, "sig")
    b = al.run_test(px, strong, "same", 1, home, key, "sig")
    assert (a["trial"], b["trial"]) == (1, 2)
    assert b["bar"] == pytest.approx(0.025)
    lines = (tmp_path / "registry.jsonl").read_text().splitlines()
    kinds = [json.loads(line)["kind"] for line in lines]
    assert kinds == ["trial", "result", "trial", "result"]


def test_trial_is_written_before_computing(tmp_path, demo_small, monkeypatch):
    px, strong, _ = demo_small
    home = al.Home(str(tmp_path))

    def boom(*_):
        raise RuntimeError("crash mid-computation")
    monkeypatch.setattr(al, "evaluate", boom)
    with pytest.raises(RuntimeError):
        al.run_test(px, strong, "crashes", 1, home, "k:h1", "sig")
    assert home.trials() == 1


def test_no_floor_means_undecidable_not_fail(tmp_path, demo_small):
    px, _, weak = demo_small
    r = al.run_test(px, weak, "weak", 1, al.Home(str(tmp_path)), "never-floored:h1", "sig")
    assert r["verdict"] == "UNDECIDABLE"
    assert "no floor" in r["why"]


def test_floor_from_other_data_is_not_reused(tmp_path, demo_small):
    px, _, weak = demo_small
    home = al.Home(str(tmp_path))
    al.run_floor(px, 1, home, "otherdata:h1", [0.0, 0.02], reps=5, seed=0)
    r = al.run_test(px, weak, "weak", 1, home, "thisdata:h1", "sig")
    assert r["verdict"] == "UNDECIDABLE" and "no floor" in r["why"]


def test_floor_false_positives_low_and_power_rises(tmp_path, demo_small):
    px, _, _ = demo_small
    curve = al.run_floor(px, 1, al.Home(str(tmp_path)), "k:h1", [0.0, 0.01, 0.05],
                         reps=20, seed=1)
    rows, floor = al.floor_at(curve, 0.05)
    power = {b: pw for b, _, pw in rows}
    assert power[0.0] <= 0.10
    assert power[0.05] >= 0.95
    assert floor is not None and floor[0] in (0.01, 0.05)


def test_floor_and_test_share_one_code_path(tmp_path, demo_small, monkeypatch):
    px, strong, _ = demo_small
    calls = []
    real = al.evaluate

    def spy(*args):
        calls.append(1)
        return real(*args)
    monkeypatch.setattr(al, "evaluate", spy)
    home = al.Home(str(tmp_path))
    al.run_floor(px, 1, home, "k:h1", [0.02], reps=3, seed=0)   # beta 0 is always added
    assert len(calls) == 6
    al.run_test(px, strong, "s", 1, home, "k:h1", "sig")
    assert len(calls) == 7


def test_planted_strong_passes_and_weak_is_undecidable(tmp_path, demo_small):
    px, strong, weak = demo_small
    home = al.Home(str(tmp_path))
    al.run_floor(px, 1, home, "k:h1", [0.0, 0.01, 0.015, 0.02, 0.03], reps=20, seed=2)
    assert al.run_test(px, strong, "strong", 1, home, "k:h1", "s")["verdict"] == "PASS"
    assert al.run_test(px, weak, "weak", 1, home, "k:h1", "w")["verdict"] == "UNDECIDABLE"


def sector_tilt(shape, seed=11, block=63):
    """A null signal with factor structure: one random score per sector, redrawn
    every `block` days, shared by every asset in the sector."""
    rng = np.random.default_rng(seed)
    n_days, n = shape
    blocks = np.repeat(rng.standard_normal((n_days // block + 1, 4)), block, axis=0)[:n_days]
    return blocks[:, np.arange(n) % 4] + 1e-9 * rng.standard_normal(shape)


def test_sector_tilt_null_gets_a_scaled_floor(tmp_path, demo_small):
    px, _, _ = demo_small
    home = al.Home(str(tmp_path))
    al.run_floor(px, 1, home, "k:h1", [0.0, 0.01, 0.015, 0.02, 0.03], reps=20, seed=4)
    tilt = pd.DataFrame(sector_tilt(px.shape), index=px.index, columns=px.columns)
    r = al.run_test(px, tilt, "tilt", 1, home, "k:h1", "t")
    assert r["ratio"] > 1.3
    assert r["eff_floor"] == pytest.approx(r["floor"][1] * r["ratio"])
    assert r["eff_floor"] > r["floor"][1]


def test_low_dispersion_signal_clamps_ratio_to_one(tmp_path, demo_small):
    px, _, _ = demo_small
    home = al.Home(str(tmp_path))
    al.run_floor(px, 1, home, "k:h1", [0.0, 0.02], reps=5, seed=5)
    perfect = al.forward_returns(px, 1)          # daily IC ~1 every day: tiny spread
    r = al.run_test(px, perfect, "perfect", 1, home, "k:h1", "p")
    assert r["ratio_raw"] < 1.0
    assert r["ratio"] == 1.0


def test_demo_sector_signal_has_factor_dispersion(tmp_path):
    prices, _, _, sector = al.make_demo(n_assets=40, n_days=1200, seed=3)
    px, sig = wide(prices, "close"), wide(sector, "signal")
    home = al.Home(str(tmp_path))
    al.run_floor(px, 1, home, "k:h1", [0.0, 0.01, 0.015, 0.02, 0.03], reps=20, seed=6)
    r = al.run_test(px, sig, "sector", 1, home, "k:h1", "s")
    assert r["ratio"] >= 1.3
    assert r["eff_floor"] > r["floor"][1]
    assert r["verdict_unscaled"] == al.decide(r, r["bar"], True, r["floor"][1], 1)[0]


def test_scaling_turns_a_wrong_fail_into_undecidable():
    r = {"ic": 0.012, "t": 1.3, "p": 0.19, "n": 2500, "sd": 0.27}
    floor_ic, ratio = 0.0103, 1.68                 # iid floor, sector-tilt dispersion
    assert al.decide(r, 0.025, True, floor_ic, 1)[0] == "FAIL"
    assert al.decide(r, 0.025, True, floor_ic * ratio, 1)[0] == "UNDECIDABLE"


def test_breadth_participation_ratio():
    rng = np.random.default_rng(0)
    common = rng.normal(0, 0.01, (500, 1))
    same = pd.DataFrame(100 * np.exp(np.cumsum(common + 1e-6 * rng.normal(size=(500, 20)), axis=0)))
    indep = pd.DataFrame(100 * np.exp(np.cumsum(rng.normal(0, 0.01, (500, 20)), axis=0)))
    assert al.breadth(same)[1] < 1.5
    assert al.breadth(indep)[1] > 15


def test_demo_refuses_to_overwrite_existing_folder(tmp_path):
    d = tmp_path / "demo"
    d.mkdir()
    with pytest.raises(SystemExit):
        al.main(["demo", "--dir", str(d)])
