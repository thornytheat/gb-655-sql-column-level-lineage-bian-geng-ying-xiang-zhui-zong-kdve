"""M7 入口：迁移单页与导出物。

导出物可按取证日期（as_of）或原批次（run_id）定位：
- `as_of=2026-09-15` 取该日期下生效区间覆盖它的、序号最大的版本；
- `run_id=fm-…` 按原批次复取，之后再来勘误也不影响这份证据；
- `latest=1` 是给下游提前核对新版的显式预览，取最后提交的批次；
- 都不给时按此刻生效的版本取证：提前登记的下月版本不改动本月证据。
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, Response

from app import store, web
from app.modules.m6_batches import evidence
from app.modules.m7_migration import service

router = APIRouter()


def _csv(body: str, filename: str) -> Response:
    return Response(
        content=body,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _selected(as_of: str | None = None, run_id: str | None = None,
              latest: bool = False) -> dict:
    return evidence.resolve(as_of=as_of, run_id=run_id, latest=latest)


@router.get("/spec", response_class=HTMLResponse)
def spec_page(request: Request, as_of: str | None = None, run_id: str | None = None,
              latest: bool = False):
    from app import errors

    try:
        run = _selected(as_of, run_id, latest)
    except errors.NotFound:
        run = None
    return web.page_or_404(
        request,
        "spec.html",
        "spec",
        run,
        selected=run,
        evidence=(run or {}).get("_evidence") or {},
        clauses=service.clauses(run) if run else [],
        ledger=(run or {}).get("ledger") or {},
        aliases=((run or {}).get("mapping") or {}).get("aliases") or [],
        warnings=(run or {}).get("warnings") or [],
    )


@router.get("/api/evidence")
def evidence_api(as_of: str | None = None, run_id: str | None = None,
                 latest: bool = False):
    run = _selected(as_of, run_id, latest)
    return {
        "run_id": run["run_id"],
        "version_seq": run["version_seq"],
        "version_label_old": run.get("version_label_old"),
        "version_label_new": run.get("version_label_new"),
        "effective_from": run.get("effective_from"),
        "effective_to": run.get("effective_to"),
        "evidence": run.get("_evidence") or {},
        "superseded_by": evidence.superseded_by(run, store.list_run_payloads()),
        "warnings": run.get("warnings") or [],
    }


@router.get("/api/export/evidence.csv")
def export_evidence(as_of: str | None = None, run_id: str | None = None,
                    latest: bool = False):
    run = _selected(as_of, run_id, latest)
    return _csv(service.fields_csv_text(run), f"lineageguard-evidence-{run['run_id']}.csv")


@router.get("/api/export/spec.md")
def export_spec(as_of: str | None = None, run_id: str | None = None,
                latest: bool = False):
    run = _selected(as_of, run_id, latest)
    return Response(
        content=service.spec_markdown(run),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="lineageguard-{run["run_id"]}.md"'},
    )


@router.get("/api/export/fields.csv")
def export_fields(as_of: str | None = None, run_id: str | None = None,
                  latest: bool = False):
    run = _selected(as_of, run_id, latest)
    return _csv(service.fields_csv_text(run), f"lineageguard-fields-{run['run_id']}.csv")


@router.get("/api/export/refs.csv")
def export_refs(as_of: str | None = None, run_id: str | None = None,
                latest: bool = False):
    run = _selected(as_of, run_id, latest)
    return _csv(service.refs_csv_text(run), f"lineageguard-refs-{run['run_id']}.csv")


@router.get("/api/export/ledger.csv")
def export_ledger(as_of: str | None = None, run_id: str | None = None,
                  latest: bool = False):
    run = _selected(as_of, run_id, latest)
    return _csv(service.ledger_csv_text(run), f"lineageguard-ledger-{run['run_id']}.csv")


@router.get("/api/export/manifest.json")
def export_manifest(as_of: str | None = None, run_id: str | None = None,
                    latest: bool = False):
    run = _selected(as_of, run_id, latest)
    return Response(
        content=service.manifest_json_text(run),
        media_type="application/json; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="lineageguard-{run["run_id"]}.json"'},
    )
