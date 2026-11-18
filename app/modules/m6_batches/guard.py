"""版本守卫：陈旧提交拒绝、生效区间重叠标记。"""

from __future__ import annotations

from app import errors
from app.modules.m6_batches import window as window_mod


def check_stale(base_version_seq, current_seq: int) -> None:
    """提交里带的基线版本落后于当前版本 → 拒绝，避免旧结果覆盖新结果。"""
    if base_version_seq is None:
        return
    try:
        base = int(base_version_seq)
    except (TypeError, ValueError):
        raise errors.BadInput("base_version_seq 必须是整数",
                             detail={"value": repr(base_version_seq)})
    if base < 0:
        raise errors.BadInput("base_version_seq 不能为负", detail={"value": base})
    if base != current_seq:
        raise errors.StaleVersion(
            f"基线版本 {base} 已落后于当前版本 {current_seq}",
            detail={"base_version_seq": base, "current_version_seq": current_seq,
                    "reason": "VERSION_STALE"},
        )


def overlap_warnings(target: dict, existing: list[dict]) -> list[dict]:
    """部分重叠才告警；同一区间的后发布批次算勘误，单独提示。"""
    hits = [
        hit for hit in window_mod.find_overlaps(target, existing)
        if not _same_window(target, hit)
    ]
    return [
        {
            "code": "VERSION_OVERLAP",
            "detail": f"{hit['run_id']} 已占用 {window_mod.describe(hit)}",
            "run_id": hit["run_id"],
        }
        for hit in hits
    ]


def _same_window(target: dict, other: dict) -> bool:
    return (other.get("from") == target["from"]
            and other.get("to") == target["to"])


def erratum_warnings(target: dict, existing: list[dict]) -> list[dict]:
    """同一生效区间已发布过版本：新批次是勘误，后发布的版本对取证生效。

    旧批次不被覆盖，仍可按 run_id 复取；这里只提示取证口径会切换。
    """
    same = [
        item for item in existing
        if item["effective_from"] == target["from"]
        and item.get("effective_to") == target["to"]
    ]
    if not same:
        return []
    superseded = sorted(item["run_id"] for item in same)
    return [{
        "code": "VERSION_ERRATUM",
        "detail": "同一生效区间的勘误，取证将取最新发布；旧批次仍可按 run_id 复取",
        "supersedes": superseded,
    }]
