"""Memory-limit-derived policy and the worker cap (browser mode optimisation).

The supervisor's figures are derived from the cgroup's ``memory.max`` so the
same image behaves correctly in a 512 MB container and in a 4 GB one.  Every
derived figure has an environment override.
"""

from __future__ import annotations

import asyncio

import pytest

from studio_web.config import WebConfig, cgroup_memory_limit_mb, scale_to_limit
from studio_web.launcher import PRELOAD
from studio_web.supervisor import Supervisor, Worker

from .conftest import FakeLauncher


def test_no_limit_keeps_the_documented_defaults():
    auto = scale_to_limit(0)
    assert auto == {"pressure": 1450.0, "soft": 550.0, "hard": 1100.0,
                    "idle": 900.0, "max_workers": 4}


@pytest.mark.parametrize("limit,max_workers", [
    (256, 1), (512, 1), (768, 1), (1024, 2), (1500, 3), (2048, 4), (4096, 8),
])
def test_max_workers_follows_the_limit(limit, max_workers):
    assert scale_to_limit(limit)["max_workers"] == max_workers


def test_caps_stay_below_a_tight_limit():
    for limit in (256, 512, 768, 1024, 1500, 2048):
        auto = scale_to_limit(limit)
        assert auto["pressure"] < limit
        assert auto["hard"] < limit
        assert auto["soft"] < auto["hard"]


def test_a_tight_box_reaps_idle_workers_sooner():
    assert scale_to_limit(512)["idle"] < scale_to_limit(768)["idle"]
    assert scale_to_limit(768)["idle"] < scale_to_limit(1024)["idle"]
    assert scale_to_limit(1024)["idle"] < scale_to_limit(1500)["idle"]


def test_from_env_derives_from_the_cgroup_and_env_overrides():
    cfg = WebConfig.from_env({"STUDIO_MEM_LIMIT_MB": "1024"})
    assert cfg.mem_limit_mb == 1024 and cfg.max_workers == 2
    assert cfg.pressure_mb < 1024 and cfg.hard_cap_mb < 1024

    env = {"STUDIO_MEM_LIMIT_MB": "1024", "STUDIO_WEB_MAX_WORKERS": "5",
           "STUDIO_WEB_PRESSURE_MB": "900", "STUDIO_WEB_IDLE_SECS": "42"}
    cfg = WebConfig.from_env(env)
    assert (cfg.max_workers, cfg.pressure_mb, cfg.idle_secs) == (5, 900.0, 42.0)


def test_cgroup_limit_reader_never_raises():
    value = cgroup_memory_limit_mb()
    assert isinstance(value, int) and value >= 0


def test_forkserver_preload_has_the_app_and_freezes_last():
    assert "studio_web._preload" in PRELOAD
    assert PRELOAD[-1] == "studio_web._freeze"
    # scipy/openpyxl are lazy now: preloading them would defeat the saving
    assert not any(m.startswith("scipy") or m == "openpyxl" for m in PRELOAD)


# ── the worker cap ────────────────────────────────────────────────────────


def _cfg(tmp_path, **over):
    return WebConfig(data_dir=str(tmp_path / "web"), server="http://studio.invalid",
                     worker_script="unused", monitor_secs=3600, **over)


def test_a_new_user_evicts_the_least_recently_used_idle_worker(tmp_path):
    cfg = _cfg(tmp_path, max_workers=1)
    launcher = FakeLauncher()
    sup = Supervisor(cfg, launcher)

    async def main():
        anna = await sup.get(2, "p1", "tok-a")
        await sup.get(3, "p1", "tok-b")
        assert not anna.alive()
        assert len([w for w in sup.workers.values() if w.alive()]) == 1
        assert any(e["event"] == "max_workers" for e in sup.events)
        await sup.close()

    asyncio.run(main())


def test_the_cap_refuses_instead_of_overcommitting_when_all_are_busy(tmp_path):
    cfg = _cfg(tmp_path, max_workers=2)
    sup = Supervisor(cfg, FakeLauncher())

    async def main():
        a = await sup.get(2, "p1", "tok-a")
        b = await sup.get(3, "p1", "tok-b")
        a.inflight = b.inflight = 1
        cfg.max_workers = 1                      # both slots are now busy
        third = Worker(4, "p1", "t", "s", sup.home_of(4))
        with pytest.raises(Exception) as exc:
            await sup._make_room(third)
        assert "busy" in str(exc.value)
        await sup.close()

    asyncio.run(main())
