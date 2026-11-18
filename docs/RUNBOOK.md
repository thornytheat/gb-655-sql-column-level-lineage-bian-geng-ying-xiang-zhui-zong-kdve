# 运行手册 lineageguard

## 1 启动

```bash
docker compose up -d --build
curl -s localhost:18214/api/health
```

宿主端口 `18214`，容器内 `8000`。启动时如果库是空的，服务会自动把 `sample_data/` 里的样例跑一遍，页面打开即有数据。

## 2 截图与验收命令

```bash
docker compose config --quiet
docker compose exec -T app python -m pytest -q
```

## 3 主流程

1. 打开 `http://localhost:18214/`，看规格单的字段清单与采信诊断。
2. `/types` 比较两个类型写法，看读者 / 写者两向结论。
3. `/mapping` 看旧名到新名、别名 SQL、冲突项。
4. `/impact` 看受影响节点、环、断链、单边登记。
5. `/batches` 点「跑一次」，再点「陈旧提交」验证 409；「发布（幂等键）」连点两次看只发布一次。
6. `/spec` 导出迁移单与三份账目 CSV；可填取证日期或原批次号切换取证口径。

## 4 重建数据库

```bash
docker compose down -v
docker compose up -d --build
```

命名卷 `lineageguard-data` 挂着 `/data/lineageguard.sqlite`。删卷即清空批次、快照与隔离区。

## 5 接口

| 方法 | 路径 | 作用 |
|---|---|---|
| GET | `/api/health` | 服务与库状态、当前批次号 |
| POST | `/api/schemas/import` | 采信样例文件，返回快照摘要与诊断 |
| GET | `/api/schemas/quarantine` | 隔离区 JSON |
| GET | `/api/schemas/quarantine.csv` | 隔离区 CSV |
| GET | `/api/types` | 类型格与别名表 |
| POST | `/api/types/compare` | 两个类型写法比较 |
| POST | `/api/compat` | 一对版本的两向判定 |
| GET | `/api/mapping` | 映射表、别名、冲突 |
| GET | `/api/impact` | 影响面、环、断链 |
| POST | `/api/runs` | 跑批次，可带基线版本号、生效区间与发布键 |
| GET | `/api/batches` | 批次历史 |
| GET | `/api/evidence` | 取证口径：按 `as_of` / `run_id` / `latest=1` |
| GET | `/api/reconcile` | 四账对账（与导出同口径，可带 `as_of` / `run_id`） |
| GET | `/api/export/spec.md` | 迁移单 Markdown |
| GET | `/api/export/fields.csv` | 字段账 CSV |
| GET | `/api/export/refs.csv` | 引用账 CSV |
| GET | `/api/export/ledger.csv` | 账目 CSV |
| GET | `/api/export/manifest.json` | 运行清单 |

## 6 取证与勘误

```bash
# 当月取证：按日期取生效版本（提前登记的下月版本不影响）
curl -s "localhost:18214/api/evidence?as_of=2026-11-15"

# 下游提前核对下一版
curl -s "localhost:18214/api/evidence?latest=1"

# 复取已发证据：钉住原批次，之后来的勘误不改这份证据
curl -s "localhost:18214/api/export/evidence.csv?run_id=fm-xxxxxxxxxxxx"

# 勘误发布：同一发布键重试只发布一次
curl -s -X POST localhost:18214/api/runs \
  -H 'Content-Type: application/json' \
  -d '{"publish_key":"erratum-2026-09-01"}'
```

- 新取证默认按「此刻」取生效版本；同一生效区间的勘误发布后，新取证看到更正，
  旧批次保留，按 `run_id` 复取到的仍是原样。
- 发布键用于同一条输入的失败重试；同一键换一组输入会被 409 拒绝。
- 发布在单个事务里完成，失败时序号、批次与发布键一起回滚，库里仍是旧版证据。

## 7 失败处置

| 现象 | 原因 | 处置 |
|---|---|---|
| 409 `VERSION_STALE` | 提交带的基线序号落后 | 去掉 `base_version_seq` 或填当前序号 |
| 400 `WINDOW_INVERTED` | 生效区间起点不早于终点 | 检查 `effective_from` / `effective_to` |
| 400 `BAD_INPUT` | 文件名、参数或 JSON 结构不对 | 看返回的 `detail` |
| 404 `NOT_FOUND` | 还没跑过批次，或 `run_id` 不存在 | 先在 `/batches` 点「跑一次」，或核对批次号 |
| 404 `NO_EFFECTIVE_VERSION` | 取证日期早于任何生效起点 | 换取证日期，或不带日期取此刻 |
| 409 `CONFLICT` / `PUBLISH_KEY_REUSED` | 发布键被用于另一组输入 | 换发布键，或确认是否该复用原批次 |

## 8 端口与并发

本服务固定 18214。同时起多个栈时改 `docker-compose.yml` 的端口映射与 `lineageguard-data` 卷名，避免与本机其他项目抢端口、串数据。
