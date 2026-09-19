# 矿井粉尘监测复核服务

仅使用 Python 标准库（`http.server`），只监听本机回环地址 `127.0.0.1`，数据落盘 JSON、重启保留。

## 启动

```bash
python3 dust_service.py                 # 默认 127.0.0.1:8080，数据文件 dust_data.json
python3 dust_service.py --port 9000 --data /path/to/data.json
```

`--host` 仅允许 `127.0.0.1` / `::1` / `localhost`，绑定其他地址会直接拒绝启动。

## 业务规则

- 粉尘浓度 **严格大于 4 mg/m³**，或**采样时间缺失**（空字符串/不传）：该测量为 `pending`（待复核），**不计入汇总**。
- 提交班组巡检时，只要满足以下任一条件，**整次拒绝**（已有测量、区域计数、汇总均不变，返回 409）：
  1. 系统中存在任何待复核测量；
  2. 本次提交后任一区域的累计样本数超过该区域 `sample_limit`。
- 复核放行（`accepted`/`released` 才计入汇总）后立即重新统计。
- 数据通过「写临时文件 + `os.replace`」原子落盘。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/areas` | 登记区域 `{"name","sample_limit"}` |
| POST | `/points` | 登记测点 `{"name","area_id"}` |
| POST | `/inspections` | 提交班组巡检 `{"team","samples":[...]}` |
| POST | `/readings/<id>/release` | 放行单条待复核测量 |
| POST | `/readings/release-all` | 放行全部待复核测量 |
| GET | `/summary` | 查询汇总（总计 / 区域 / 测点三级） |
| GET | `/areas` `/points` `/inspections` | 查询登记与巡检明细 |

样本对象：`{"point_id": "...", "concentration": 3.2, "sampled_at": "2026-09-19T08:00:00"}`

巡检提交响应包含每条测量的 `id` 与 `status`（`accepted` / `pending`），待复核记录带 `reasons`。

汇总示例片段：

```json
{
  "total": {"sample_count": 5, "avg": 2.7, "max": 5.0, "min": 1.0, "over_limit_count": 1},
  "pending_count": 0,
  "released_count": 2,
  "areas": [{"area_name": "...", "sample_limit": 3,
             "stored_sample_count": 3, "counted_sample_count": 3,
             "points": [{"point_name": "...", "sample_count": 3, "avg": 1.5}]}]
}
```

- `stored_sample_count`：已接受巡检中该区域的全部测量条数（用于上限判断）。
- `counted_sample_count`：计入汇总的条数（不含待复核）。

## 测试

```bash
python3 test_dust_service.py   # 自动启停服务，覆盖全部规则 + 重启持久化
```
