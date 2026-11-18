"""M8 取证：按日期选生效版本、勘误替代、按批次复取、发布幂等与失败回滚。"""

from __future__ import annotations

import pytest

from app import errors, store
from app.modules.m6_batches import evidence, service as batch_service
from app.modules.m7_migration import service as migration


def _publish(client, publish_key: str, **body):
    return client.post("/api/runs", json={"publish_key": publish_key, **body})


def test_future_version_does_not_change_current_evidence(client):
    """提前登记下月版本会新增批次，但当月取证仍取已生效的版本。"""
    current = store.latest_run()
    response = client.post("/api/runs", json={
        "effective_from": "2026-12-01T00:00:00Z", "params": {"wave": "next-month"},
    })
    assert response.status_code == 200
    future_id = response.json()["run_id"]
    assert future_id != current["run_id"]

    now = evidence.resolve()
    assert now["run_id"] == current["run_id"]
    assert now["_evidence"]["mode"] == "effective_now"

    preview = client.get("/api/evidence?latest=1").json()
    assert preview["run_id"] == future_id
    assert preview["evidence"]["mode"] == "latest"

    in_december = client.get("/api/evidence?as_of=2026-12-15").json()
    assert in_december["run_id"] == future_id


def test_as_of_uses_half_open_window(client):
    """取证日期落在生效起点当天算新版，落在前一刻仍算旧版。"""
    base = store.latest_run()["run_id"]
    client.post("/api/runs", json={"effective_from": "2026-12-01T00:00:00Z",
                                   "params": {"wave": "boundary"}})
    assert client.get("/api/evidence?as_of=2026-11-30T23:59:59Z").json()["run_id"] == base
    assert client.get("/api/evidence?as_of=2026-12-01T00:00:00Z").json()["run_id"] != base


def test_no_effective_version_is_reported(client):
    response = client.get("/api/evidence?as_of=2026-01-01")
    assert response.status_code == 404
    assert response.json()["detail"]["reason"] == "NO_EFFECTIVE_VERSION"


def test_erratum_same_window_takes_latest_for_new_evidence(client):
    """同一生效区间的勘误晚到：新取证看到更正，并被标记为勘误。"""
    original = store.latest_run()
    erratum = _publish(client, "erratum-1", params={"wave": "erratum"}).json()
    assert erratum["version_seq"] > original["version_seq"]
    codes = [item["code"] for item in erratum["warnings"]]
    assert "VERSION_ERRATUM" in codes

    now = evidence.resolve()
    assert now["run_id"] == erratum["run_id"]
    assert client.get("/api/evidence?as_of=2026-09-15").json()["run_id"] == erratum["run_id"]


def test_issued_evidence_is_replayable_by_run_id(client):
    """已发证据按原批次复取：勘误来了也不改这份证据。"""
    original = store.latest_run()
    before = migration.fields_csv_text(original)
    _publish(client, "erratum-2", params={"wave": "erratum"})

    pinned = evidence.resolve(run_id=original["run_id"])
    assert pinned["run_id"] == original["run_id"]
    assert pinned["_evidence"]["mode"] == "pinned"
    assert migration.fields_csv_text(pinned) == before

    exported = client.get(f"/api/export/evidence.csv?run_id={original['run_id']}")
    assert exported.status_code == 200
    assert exported.text == before
    info = client.get(f"/api/evidence?run_id={original['run_id']}").json()
    assert info["superseded_by"]


def test_publish_retry_does_not_duplicate(client):
    """勘误发布失败后重试：同一发布键只发布一次。"""
    first = _publish(client, "key-retry").json()
    assert first["replayed"] is False
    count = store.count_runs()

    retry = _publish(client, "key-retry").json()
    assert retry["replayed"] is True
    assert retry["reused"] is True
    assert retry["run_id"] == first["run_id"]
    assert retry["version_seq"] == first["version_seq"]
    assert store.count_runs() == count


def test_publish_key_reused_for_other_input_conflicts(client):
    first = _publish(client, "key-conflict").json()
    count = store.count_runs()

    response = _publish(client, "key-conflict", effective_from="2026-12-01T00:00:00Z")
    assert response.status_code == 409
    assert response.json()["error"] == "CONFLICT"
    assert response.json()["detail"]["reason"] == "PUBLISH_KEY_REUSED"
    assert store.count_runs() == count
    assert store.run_by_id(first["run_id"]) is not None


def test_publish_failure_keeps_old_evidence(client):
    """切换失败：事务整体回滚，库里仍是旧版证据。"""
    original = store.latest_run()
    before = migration.fields_csv_text(original)

    def explode(*_args, **_kwargs):
        raise RuntimeError("publish boom")

    real_insert = store._insert_run
    store._insert_run = explode
    try:
        with pytest.raises(RuntimeError):
            batch_service.execute(params={"wave": "broken"}, publish_key="key-broken")
    finally:
        store._insert_run = real_insert

    assert store.count_runs() == 1
    assert store.find_publish_key("key-broken") is None
    assert store.latest_run()["run_id"] == original["run_id"]
    assert migration.fields_csv_text(evidence.resolve(run_id=original["run_id"])) == before


def test_manifest_records_evidence_mode(client):
    from app.modules.m7_migration import service as migration_service

    run = evidence.resolve(as_of="2026-09-15")
    payload = migration_service.manifest_json_text(run)
    assert '"mode": "as_of"' in payload
    assert '"as_of": "2026-09-15T00:00:00+00:00"' in payload


def test_unknown_run_id_is_404(client):
    response = client.get("/api/evidence?run_id=fm-not-there")
    assert response.status_code == 404
    assert response.json()["error"] == "NOT_FOUND"


def test_publish_key_validation(client):
    response = client.post("/api/runs", json={"publish_key": 123})
    assert response.status_code == 400


def test_spec_page_renders_empty_for_uncovered_date(client):
    response = client.get("/spec?as_of=2020-01-01")
    assert response.status_code == 200
    assert "该取证口径下没有批次" in response.text


def test_reconcile_follows_evidence_selection(client):
    """对账与导出同一口径：提前登记的下月版本不改动当月对账。"""
    current = store.latest_run()["run_id"]
    client.post("/api/runs", json={"effective_from": "2026-12-01T00:00:00Z",
                                   "params": {"wave": "next"}})
    assert client.get("/api/reconcile").json()["run_id"] == current
    preview = client.get("/api/reconcile?latest=1")
    assert preview.status_code == 200
    assert preview.json()["run_id"] != current


def test_same_window_not_double_counted_as_overlap(client):
    """同一生效区间只提示勘误，不再重复报区间重叠。"""
    _publish(client, "erratum-overlap", params={"wave": "erratum"})
    latest = store.latest_run()
    codes = [item["code"] for item in latest["warnings"]]
    assert codes.count("VERSION_ERRATUM") == 1
    assert codes.count("VERSION_OVERLAP") != 2


def test_evidence_module_requires_registered_run():
    with pytest.raises(errors.NotFound):
        evidence.resolve(run_id="fm-missing")
