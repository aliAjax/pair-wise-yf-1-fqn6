# 矿井粉尘监测复核服务

仅依赖 Python 3 标准库（`http.server`），只监听本机回环地址 `127.0.0.1`，
数据持久化到本地 JSON 文件，重启后保留。

## 运行

```bash
python3 dust_service.py                      # 默认 127.0.0.1:8000，状态文件 ./dust_state.json
python3 dust_service.py --port 9000          # 自定义端口
python3 dust_service.py --state-file /path/to/state.json
```

启动时若传入非回环地址（如 `0.0.0.0`）会直接拒绝并退出。

## 自测

```bash
python3 test_dust_service.py
```

脚本会真实启动服务子进程，通过 HTTP 覆盖全部业务规则，共 37 项断言，
包括服务重启后的数据保留校验。

## 业务规则

1. **待复核**：粉尘浓度严格大于 `4 mg/m³`，或采样时间 `sampled_at` 缺失（字段缺省或为 `null`）。
   待复核测量**不计入汇总**。
2. **整次拒绝**：提交班组巡检时，只要
   - 当前存在任一待复核测点（即使不属于本次巡检），或
   - 提交后任一区域的累计样本数超过该区域上限，

   则整批拒绝（HTTP 409），既有测量、区域、汇总均不变（先校验、后落库）。
3. **复核放行**后该测量重新计入统计，响应中直接返回重算后的汇总。
4. 每次成功写操作都**原子写入**本地文件（临时文件 + `os.replace` + `fsync`）。

## 接口

请求/响应均为 JSON（`Content-Type: application/json`）。

### 1. 登记测点 `POST /points`

```json
{ "point_id": "a1", "area": "A", "limit": 10 }
```

- `limit`：该区域样本数上限（正整数）。同一区域所有测点的上限必须一致。
- 同一 `point_id` 重复登记且信息一致 → 幂等返回 `duplicate: true`；
  信息冲突或与同区域已有上限冲突 → 409。

### 2. 提交班组巡检 `POST /inspections`

```json
{
  "samples": [
    { "point_id": "a1", "concentration": 3.2, "sampled_at": "2026-09-19T08:00:00" },
    { "point_id": "a2", "concentration": 5.1 }
  ]
}
```

- `sampled_at` 为 ISO 8601 字符串；缺失/为 `null` 表示采样时间缺失 → 该样本待复核。
- 成功返回每条测量的 `measurement_id` 与 `status`（`normal` / `pending`）。
- 整次拒绝时返回 `error=pending_points`（附带待复核测点明细）或
  `error=area_limit_exceeded`（附带各超限区域的当前数/提交数/上限）。

### 3. 复核放行 `POST /reviews`

```json
{ "point_id": "a2", "note": "复测合格" }
```

- 将该测点最近一条待复核测量置为 `released` 并重新统计；
  无待复核测量 → 409，测点未登记 → 404。

### 4. 查询汇总 `GET /summary`

返回：全局样本数（不含待复核）与平均浓度、各区域样本数/均值/上限/是否超限、
待复核测点明细及原因（`dust_over_limit`、`missing_sampled_at`）。

### 辅助接口

- `GET /points`：测点列表，含各区域当前样本数与是否待复核。
- `GET /health`：健康检查。

## 示例

```bash
curl -s -X POST http://127.0.0.1:8000/points \
  -H 'Content-Type: application/json' \
  -d '{"point_id":"a1","area":"A","limit":10}'

curl -s -X POST http://127.0.0.1:8000/inspections \
  -H 'Content-Type: application/json' \
  -d '{"samples":[{"point_id":"a1","concentration":3.2,"sampled_at":"2026-09-19T08:00:00"}]}'

curl -s http://127.0.0.1:8000/summary
```
