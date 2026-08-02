from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import threading
import time
from types import SimpleNamespace

from q15_upgrade.checkpoint_v95 import CheckpointPolicyV95


def test_interval_research_replay_never_blocks_live_overlay_dispatch(monkeypatch):
    import q15_upgrade.high_vol_flip.runner as high_vol_runner
    import q15_upgrade.interval_research.runner as interval_runner
    import q15_upgrade.marketlead.runner as marketlead_runner
    import q15_upgrade.ultoim.runner as ultoim_runner
    import q15_upgrade.ultoim_v2.runner as ultoim_v2_runner

    for module in (
        marketlead_runner, ultoim_runner, ultoim_v2_runner, high_vol_runner,
    ):
        monkeypatch.setattr(module, "get_runner", lambda: None)

    started = threading.Event()
    release = threading.Event()
    observed = []

    class SlowRunner:
        def observe(self, **kwargs):
            started.set()
            assert release.wait(timeout=3.0)
            observed.append(kwargs)

    monkeypatch.setattr(interval_runner, "get_runner", lambda: SlowRunner())
    policy = CheckpointPolicyV95.__new__(CheckpointPolicyV95)
    policy._interval_research_executor = ThreadPoolExecutor(max_workers=1)
    policy._interval_research_future = None
    policy._interval_research_enqueued = 0
    policy._interval_research_skipped_busy = 0
    policy._interval_research_failures = 0
    analyses = {"BTC": {"point_in_time": 1}}
    canonicals = {
        "BTC": SimpleNamespace(
            ticker="KXBTC", seconds_remaining=700.0, settlement_time=1800.0,
        )
    }
    sources = {"BTC": {"quote": 1}}
    try:
        began = time.monotonic()
        timings = policy._dispatch_research_overlays(
            analyses, canonicals, 1000.0, source_snapshots=sources,
        )
        assert time.monotonic() - began < 0.5
        assert started.wait(timeout=1.0)
        assert timings["interval_research"] < 0.5
        assert policy._interval_research_enqueued == 1

        analyses["BTC"]["point_in_time"] = 999
        sources["BTC"]["quote"] = 999
        policy._dispatch_research_overlays(
            analyses, canonicals, 1001.0, source_snapshots=sources,
        )
        assert policy._interval_research_skipped_busy == 1
        release.set()
        policy._interval_research_future.result(timeout=2.0)
        assert observed[0]["analyses"]["BTC"]["point_in_time"] == 1
        assert observed[0]["source_snapshots"]["BTC"]["quote"] == 1
    finally:
        release.set()
        policy._interval_research_executor.shutdown(wait=True)

