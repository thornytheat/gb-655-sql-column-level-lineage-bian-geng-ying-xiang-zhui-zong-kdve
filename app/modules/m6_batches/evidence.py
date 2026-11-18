"""取证选版：按日期挑出生效批次，按批次号复取已发证据。

页面与导出的默认口径仍是「最新提交」，但取证必须能钉到生效区间：
提前登记的下月版本虽然序号更大，只要还没到生效起点，就不改变本月的取证。
同一生效区间的勘误晚到，取得是序号最大的那一版；已发证据用 run_id 钉住，
不会被后续勘误顶掉。
"""

from __future__ import annotations

from datetime import datetime

from app import config, errors, store
from app.modules.m6_batches import window as window_mod


def parse_as_of(value) -> datetime:
    if isinstance(value, datetime):
        return value.astimezone(config.UTC)
    return window_mod.parse_instant(str(value or ""), "as_of")


def endpoint(value: str | None) -> datetime | None:
    return window_mod.parse_instant(value, "effective_to") if value else None


def covers(run: dict, moment: datetime) -> bool:
    """半开区间 [from, to)；to 为空表示不设终点。"""
    start = window_mod.parse_instant(run["effective_from"], "effective_from")
    end = endpoint(run.get("effective_to"))
    if moment < start:
        return False
    return end is None or moment < end


def effective_run(runs: list[dict], moment: datetime) -> dict | None:
    """生效版本：覆盖该时刻的批次里，先取生效起点最晚的一版，再取序号最大的。

    生效起点更能代表「哪一版是为这段时间立的」：下月版本一旦生效就压过本月的
    无终点区间；同一生效区间内晚到的勘误，起点相同，按序号取后发布的那一版。
    """
    hits = [run for run in runs if covers(run, moment)]
    if not hits:
        return None
    return max(hits, key=lambda run: (
        window_mod.parse_instant(run["effective_from"], "effective_from"),
        int(run["version_seq"]),
        run["run_id"],
    ))


def resolve(as_of=None, run_id: str | None = None, latest: bool = False,
            now: datetime | None = None) -> dict:
    """取证入口：run_id 钉原批次，as_of 按日期，latest 看最后提交。

    不给参数时按「此刻」取证，也就是当月导出走已生效的那一版，而不是最后提交的
    那一版；此刻没有任何生效版本时才退回最新提交，与页面浏览口径一致。
    `latest=True` 是给下游提前核对新版用的显式预览，不进入取证默认路径。
    """
    if run_id:
        pinned = store.run_by_id(str(run_id))
        if pinned is None:
            raise errors.NotFound("没有这个批次", detail={"run_id": str(run_id)})
        return dict(pinned, _evidence={"mode": "pinned", "run_id": pinned["run_id"],
                                       "as_of": None})
    runs = store.list_run_payloads()
    if as_of not in (None, ""):
        moment = parse_as_of(as_of)
        run = effective_run(runs, moment)
        if run is None:
            raise errors.NotFound(
                "该日期没有生效版本",
                detail={"reason": "NO_EFFECTIVE_VERSION", "as_of": moment.isoformat()},
            )
        return dict(run, _evidence={"mode": "as_of", "run_id": run["run_id"],
                                    "as_of": moment.isoformat()})
    if latest:
        run = store.latest_run()
        if run is None:
            raise errors.NotFound("还没有批次，先在版本历史页跑一次")
        return dict(run, _evidence={"mode": "latest", "run_id": run["run_id"],
                                    "as_of": None})

    moment = (now or config.now_utc()).astimezone(config.UTC)
    run = effective_run(runs, moment) if runs else None
    if run is not None:
        return dict(run, _evidence={"mode": "effective_now", "run_id": run["run_id"],
                                    "as_of": moment.isoformat()})
    if not runs:
        raise errors.NotFound("还没有批次，先在版本历史页跑一次")
    run = store.latest_run()
    return dict(run, _evidence={"mode": "latest", "run_id": run["run_id"], "as_of": None})


def superseded_by(run: dict, runs: list[dict]) -> list[str]:
    """同一生效区间的后发布批次；只用于展示与复取提示。"""
    return sorted(
        other["run_id"] for other in runs
        if other["run_id"] != run["run_id"]
        and other.get("effective_from") == run.get("effective_from")
        and other.get("effective_to") == run.get("effective_to")
        and int(other["version_seq"]) > int(run["version_seq"])
    )
