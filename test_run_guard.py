"""Delivery-window guard tests. Run: python3 test_run_guard.py"""
import os, random, sys, tempfile
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.chdir(tempfile.mkdtemp())
import run_guard as rg

UK = ZoneInfo("Europe/London")
failures = []


def expect(cond, msg):
    print(("  ok: " if cond else "  FAIL: ") + msg)
    if not cond:
        failures.append(msg)


def at(dt):
    rg._uk_now = lambda: dt
    return dt


def uk(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=UK)


MON = uk(2026, 9, 28, 0)   # Monday 28 Sep 2026

print("== 1. Delivery window (UK time) ==")
for (h, m), want in {(6, 59): False, (7, 0): True, (8, 17): True,
                     (11, 59): True, (12, 0): False, (1, 17): False,
                     (13, 30): False, (20, 23): False}.items():
    rg._save_state({})
    at(MON.replace(hour=h, minute=m))
    ok, why = rg.should_deliver("news")
    expect(ok == want, f"Mon {h:02d}:{m:02d} -> {'deliver' if want else 'stand down'} ({why})")

print("\n== 2. Right day ==")
rg._save_state({})
at(uk(2026, 9, 26, 8, 17))   # Saturday
expect(not rg.should_deliver("news")[0], "news stands down on Saturday")
at(uk(2026, 10, 1, 8, 17))   # Thursday
expect(rg.should_deliver("linkedin", days=rg.THURSDAY)[0], "linkedin delivers Thursday")
at(uk(2026, 9, 30, 8, 17))   # Wednesday
expect(not rg.should_deliver("linkedin", days=rg.THURSDAY)[0], "linkedin stands down Wednesday")

print("\n== 3. Exactly once per day ==")
rg._save_state({})
at(MON.replace(hour=7, minute=17))
expect(rg.should_deliver("news")[0], "07:17 delivers")
rg.mark_delivered("news")
for h in (8, 9, 10, 11):
    at(MON.replace(hour=h, minute=17))
    ok, why = rg.should_deliver("news")
    expect(not ok and "already delivered" in why, f"{h:02d}:17 stands down ({why})")
at(MON.replace(day=29, hour=7, minute=17))
expect(rg.should_deliver("news")[0], "next morning delivers again")

print("\n== 4. A failed send does not close the day ==")
rg._save_state({})
at(MON.replace(hour=7, minute=17))
expect(rg.should_deliver("news")[0], "07:17 attempts")   # send fails: no mark
at(MON.replace(hour=8, minute=17))
expect(rg.should_deliver("news")[0], "08:17 retries")

print("\n== 5. Duplicate protection when the ledger write fails ==")
rg._save_state({})
at(MON.replace(hour=8, minute=17))
ok, why = rg.should_deliver("news", fallback_delivered_today=lambda: True)
expect(not ok and "own records" in why, f"bot's own records stop a second digest ({why})")
def boom():
    raise RuntimeError("OneDrive down")
expect(rg.should_deliver("news", fallback_delivered_today=boom)[0],
       "a broken fallback fails open")
real = rg._save_state
rg._save_state = lambda st: False
expect(rg.mark_delivered("news") is False, "mark_delivered reports a failed write")
rg._save_state = real

print("\n== 6. Unreadable ledger fails open ==")
open(rg.STATE_FILE, "w").write("{not json")
at(MON.replace(hour=8, minute=17))
expect(rg.should_deliver("news")[0], "corrupt ledger -> run proceeds")

print("\n== 7. Simulation: GitHub's real delays, hourly slots ==")
# Delays GitHub has actually produced on this repo (hours): Aug 3-26 and Sep.
OBSERVED = [0.8, 1.3, 2.7, 3.8, 5.5, 6.0, 7.2, 8.2, 11.2, 12.4]


def simulate(weekdays, delay_for_slot, drop_rate, rng):
    rg._save_state({})
    delivered = {}
    start = uk(2026, 9, 27, 0, 17)   # Sunday: runs before Monday matter
    runs = []
    for hour in range(24 * (weekdays + 3)):
        if rng.random() < drop_rate:
            continue
        slot = start + timedelta(hours=hour)
        runs.append(slot + timedelta(hours=delay_for_slot(slot)))
    for fire in sorted(runs):
        at(fire)
        ok, _ = rg.should_deliver("news")
        if ok:
            rg.mark_delivered("news")
            delivered.setdefault(fire.date(), []).append(fire)
    return delivered


rng = random.Random(28)
worst_days, dup_days, off_window = 0, 0, 0
for trial in range(40):
    # GitHub's delay tracks its load, so it drifts rather than jumping at
    # midnight: anchor a value from the observed set every 12 hours and
    # interpolate between anchors, plus +/- 30 min jitter per run.
    t0 = uk(2026, 9, 26, 0)
    anchors = [rng.choice(OBSERVED) for _ in range(20)]
    def delay(slot):
        x = (slot - t0).total_seconds() / 3600 / 12
        i = int(x); f = x - i
        base = anchors[i] * (1 - f) + anchors[i + 1] * f
        return max(0, base + rng.uniform(-0.5, 0.5))
    got = simulate(5, delay, drop_rate=0.3, rng=rng)
    weekdays = [MON.date() + timedelta(days=i) for i in range(5)]
    worst_days += sum(1 for d in weekdays if d not in got)
    dup_days += sum(1 for v in got.values() if len(v) > 1)
    off_window += sum(1 for v in got.values() for f in v
                      if not (7 <= f.hour < 12) or f.weekday() > 4)
total = 40 * 5
expect(dup_days == 0, f"never two digests in a day ({dup_days} duplicates in {total} weekdays)")
expect(off_window == 0, f"every delivery is a weekday morning ({off_window} outside)")
expect(worst_days <= total * 0.02,
       f"a digest arrives on {total - worst_days}/{total} weekdays "
       f"despite 30% of runs dropped and 1-12h delays")

print()
if failures:
    print(f"{len(failures)} FAILURE(S)")
    raise SystemExit(1)
print("ALL TESTS PASSED")
