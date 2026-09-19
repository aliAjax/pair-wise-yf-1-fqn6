#!/usr/bin/env python3
"""矿井粉尘监测复核服务。

仅依赖 Python 标准库，只监听本机回环地址。

接口：
  POST /areas                       登记区域
  POST /points                      登记测点
  POST /inspections                 提交班组巡检
  POST /readings/<id>/release       复核放行（待复核 -> 计入）
  POST /readings/release-all        一次性放行全部待复核记录
  GET  /summary                     查询汇总
  GET  /areas /points /inspections  查询登记与巡检记录

规则：
  * 粉尘浓度 > 4 mg/m3，或采样时间缺失，该条测量进入待复核且不计入汇总。
  * 提交巡检时，只要已存在待复核测量，或任一区域样本数超过上限，
    整次拒绝：不写入任何测量，区域样本数与汇总保持不变。
  * 复核放行后重新统计；所有数据落盘，重启保留。
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

DUST_LIMIT = 4.0  # mg/m3，超过该值（严格大于）需复核
PERSIST_VERSION = 1

# 全局状态（由 STATE_LOCK 保护）
STATE_LOCK = threading.Lock()
STATE: dict = {}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def new_id() -> str:
    return uuid.uuid4().hex


def default_state() -> dict:
    return {
        "version": PERSIST_VERSION,
        "areas": {},        # area_id -> {id, name, sample_limit, created_at}
        "points": {},       # point_id -> {id, name, area_id, created_at}
        "inspections": [],  # [{id, team, submitted_at, readings:[...]}]
        "area_counts": {},  # area_id -> 已接受巡检中该区域的测量条数
        "created_at": now_iso(),
    }


# ---------------------------------------------------------------- 持久化


def save_state_locked(path: str) -> None:
    """原子写入：先写临时文件再 rename，避免崩溃产生半截文件。"""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dust-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(STATE, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load_state(path: str) -> dict:
    if not os.path.exists(path):
        return default_state()
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    state = default_state()
    if isinstance(data, dict):
        for key in ("areas", "points", "inspections", "area_counts"):
            if isinstance(data.get(key), (dict, list)):
                state[key] = data[key]
        if data.get("created_at"):
            state["created_at"] = data["created_at"]
    # 校验/补建区域计数，防止旧文件缺字段
    counts: dict[str, int] = {aid: 0 for aid in state["areas"]}
    for insp in state["inspections"]:
        for r in insp.get("readings", []):
            aid = r.get("area_id")
            if aid in counts:
                counts[aid] += 1
    state["area_counts"] = counts
    return state


# ---------------------------------------------------------------- 业务校验


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate_concentration(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ApiError(400, "concentration 必须是数字，单位 mg/m3")
    conc = float(value)
    if conc != conc or conc in (float("inf"), float("-inf")):
        raise ApiError(400, "concentration 不能为 NaN 或无穷大")
    if conc < 0:
        raise ApiError(400, "concentration 不能为负数")
    return conc


def validate_samples(raw, points: dict) -> list[dict]:
    """校验一次巡检的样本，返回规范化样本（尚未生成测量记录）。"""
    if not isinstance(raw, list) or not raw:
        raise ApiError(400, "samples 必须是非空数组")
    samples = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ApiError(400, f"samples[{i}] 必须是对象")
        point_id = item.get("point_id")
        if point_id not in points:
            raise ApiError(400, f"samples[{i}].point_id 未登记: {point_id!r}")
        conc = validate_concentration(item.get("concentration"))
        sampled_at = item.get("sampled_at")
        sampled_at = sampled_at.strip() if isinstance(sampled_at, str) else None
        samples.append(
            {"point_id": point_id, "concentration": conc, "sampled_at": sampled_at}
        )
    return samples


# ---------------------------------------------------------------- 业务操作


def create_area(body: dict, path: str) -> dict:
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ApiError(400, "name 必须是非空字符串")
    name = name.strip()
    limit = body.get("sample_limit")
    if not is_int(limit) or limit < 1:
        raise ApiError(400, "sample_limit 必须是 >= 1 的整数")

    with STATE_LOCK:
        for area in STATE["areas"].values():
            if area["name"] == name:
                raise ApiError(409, f"区域已存在: {name}")
        area_id = new_id()
        STATE["areas"][area_id] = {
            "id": area_id,
            "name": name,
            "sample_limit": limit,
            "created_at": now_iso(),
        }
        STATE["area_counts"][area_id] = 0
        save_state_locked(path)
        return STATE["areas"][area_id]


def create_point(body: dict, path: str) -> dict:
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        raise ApiError(400, "name 必须是非空字符串")
    name = name.strip()
    area_id = body.get("area_id")
    with STATE_LOCK:
        if area_id not in STATE["areas"]:
            raise ApiError(400, f"area_id 未登记: {area_id!r}")
        for point in STATE["points"].values():
            if point["area_id"] == area_id and point["name"] == name:
                raise ApiError(409, f"该区域下测点已存在: {name}")
        point_id = new_id()
        STATE["points"][point_id] = {
            "id": point_id,
            "name": name,
            "area_id": area_id,
            "created_at": now_iso(),
        }
        save_state_locked(path)
        return STATE["points"][point_id]


def submit_inspection(body: dict, path: str) -> dict:
    team = body.get("team")
    if not isinstance(team, str) or not team.strip():
        raise ApiError(400, "team 必须是非空字符串")
    team = team.strip()

    # 先在锁外完成输入解析，拿锁后做一致性检查
    with STATE_LOCK:
        samples = validate_samples(body.get("samples"), STATE["points"])

        # 规则 1：存在任何待复核测量 -> 整次拒绝
        pending = [
            r["id"]
            for insp in STATE["inspections"]
            for r in insp["readings"]
            if r["status"] == "pending"
        ]
        if pending:
            raise ApiError(
                409,
                f"存在 {len(pending)} 条待复核测量，本次巡检整次拒绝",
            )

        # 规则 2：任一区域在加入本次样本后样本数超过上限 -> 整次拒绝
        added_per_area: dict[str, int] = {}
        for s in samples:
            aid = STATE["points"][s["point_id"]]["area_id"]
            added_per_area[aid] = added_per_area.get(aid, 0) + 1
        for aid, added in added_per_area.items():
            limit = STATE["areas"][aid]["sample_limit"]
            total = STATE["area_counts"].get(aid, 0) + added
            if total > limit:
                raise ApiError(
                    409,
                    f"区域 {STATE['areas'][aid]['name']} 样本数将达 {total}，"
                    f"超过上限 {limit}，本次巡检整次拒绝",
                )

        # 通过：整批写入（要么全写、要么不写）
        inspection_id = new_id()
        submitted_at = now_iso()
        readings = []
        for s in samples:
            aid = STATE["points"][s["point_id"]]["area_id"]
            reasons = []
            if s["concentration"] > DUST_LIMIT:
                reasons.append("浓度超过 %.0f mg/m3" % DUST_LIMIT)
            if not s["sampled_at"]:
                reasons.append("采样时间缺失")
            reading = {
                "id": new_id(),
                "inspection_id": inspection_id,
                "point_id": s["point_id"],
                "area_id": aid,
                "concentration": s["concentration"],
                "sampled_at": s["sampled_at"],
                "submitted_at": submitted_at,
                "status": "pending" if reasons else "accepted",
                "reasons": reasons,
                "released_at": None,
            }
            readings.append(reading)
            STATE["area_counts"][aid] += 1

        inspection = {
            "id": inspection_id,
            "team": team,
            "submitted_at": submitted_at,
            "readings": readings,
        }
        STATE["inspections"].append(inspection)
        save_state_locked(path)

        return {
            "inspection_id": inspection_id,
            "team": team,
            "submitted_at": submitted_at,
            "accepted_count": sum(1 for r in readings if r["status"] == "accepted"),
            "pending_count": sum(1 for r in readings if r["status"] == "pending"),
            "readings": readings,
        }


def find_reading_locked(reading_id: str) -> dict | None:
    for insp in STATE["inspections"]:
        for r in insp["readings"]:
            if r["id"] == reading_id:
                return r
    return None


def release_reading(reading_id: str, path: str, release_all: bool = False) -> dict:
    with STATE_LOCK:
        if release_all:
            targets = [
                r
                for insp in STATE["inspections"]
                for r in insp["readings"]
                if r["status"] == "pending"
            ]
        else:
            target = find_reading_locked(reading_id)
            if target is None:
                raise ApiError(404, f"测量记录不存在: {reading_id}")
            if target["status"] != "pending":
                raise ApiError(409, f"测量记录 {reading_id} 不是待复核状态")
            targets = [target]

        released_at = now_iso()
        for r in targets:
            r["status"] = "released"
            r["released_at"] = released_at

        if targets:
            save_state_locked(path)
        # 放行后调用方重新获取汇总，实现“重新统计”
        return {
            "released_count": len(targets),
            "released_reading_ids": [r["id"] for r in targets],
            "released_at": released_at if targets else None,
            "summary": build_summary_locked(),
        }


# ---------------------------------------------------------------- 汇总


def stats_for(values: list[float]) -> dict:
    if not values:
        return {"sample_count": 0, "avg": None, "max": None, "min": None,
                "over_limit_count": 0}
    return {
        "sample_count": len(values),
        "avg": round(sum(values) / len(values), 4),
        "max": max(values),
        "min": min(values),
        "over_limit_count": sum(1 for v in values if v > DUST_LIMIT),
    }


def build_summary_locked() -> dict:
    # 计入汇总的测量：初始合格的，以及复核放行后的；待复核不计入
    counted = [
        r
        for insp in STATE["inspections"]
        for r in insp["readings"]
        if r["status"] in ("accepted", "released")
    ]
    pending = [
        r
        for insp in STATE["inspections"]
        for r in insp["readings"]
        if r["status"] == "pending"
    ]

    by_point: dict[str, list[float]] = {}
    by_area: dict[str, list[float]] = {}
    released_count = 0
    for r in counted:
        by_point.setdefault(r["point_id"], []).append(r["concentration"])
        by_area.setdefault(r["area_id"], []).append(r["concentration"])
        if r["status"] == "released":
            released_count += 1

    areas_out = []
    for aid in sorted(STATE["areas"], key=lambda a: STATE["areas"][a]["name"]):
        area = STATE["areas"][aid]
        point_ids = sorted(
            (p["id"] for p in STATE["points"].values() if p["area_id"] == aid),
            key=lambda pid: STATE["points"][pid]["name"],
        )
        points_out = []
        for pid in point_ids:
            point = STATE["points"][pid]
            points_out.append({
                "point_id": pid,
                "point_name": point["name"],
                **stats_for(by_point.get(pid, [])),
            })
        areas_out.append({
            "area_id": aid,
            "area_name": area["name"],
            "sample_limit": area["sample_limit"],
            "stored_sample_count": STATE["area_counts"].get(aid, 0),
            "counted_sample_count": len(by_area.get(aid, [])),
            "points": points_out,
            **stats_for(by_area.get(aid, [])),
        })

    return {
        "generated_at": now_iso(),
        "dust_limit_mg_per_m3": DUST_LIMIT,
        "areas": areas_out,
        "total": stats_for([r["concentration"] for r in counted]),
        "pending_count": len(pending),
        "released_count": released_count,
        "inspection_count": len(STATE["inspections"]),
    }


# ---------------------------------------------------------------- HTTP 层


class DustHandler(BaseHTTPRequestHandler):
    server_version = "DustMonitor/1.0"

    # --- 工具 ---
    def _send_json(self, status: int, payload) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            raise ApiError(400, "请求体不能为空")
        if length > 10 * 1024 * 1024:
            raise ApiError(413, "请求体过大")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(400, f"请求体不是合法 JSON: {exc}")
        if not isinstance(data, dict):
            raise ApiError(400, "请求体必须是 JSON 对象")
        return data

    def log_message(self, fmt, *args):  # 安静一点
        pass

    # --- 路由 ---
    def do_GET(self):
        self._route("GET")

    def do_POST(self):
        self._route("POST")

    def _route(self, method: str) -> None:
        path = urlparse(self.path).path.rstrip("/") or "/"
        try:
            if method == "POST" and path == "/areas":
                self._send_json(201, create_area(self._read_body(), self.server.data_path))
            elif method == "POST" and path == "/points":
                self._send_json(201, create_point(self._read_body(), self.server.data_path))
            elif method == "POST" and path == "/inspections":
                self._send_json(201, submit_inspection(self._read_body(), self.server.data_path))
            elif method == "POST" and path == "/readings/release-all":
                self._send_json(200, release_reading("", self.server.data_path, release_all=True))
            elif method == "POST" and path.startswith("/readings/") and path.endswith("/release"):
                rid = path[len("/readings/"):-len("/release")]
                if not rid:
                    raise ApiError(404, "未知接口")
                self._send_json(200, release_reading(rid, self.server.data_path))
            elif method == "GET" and path == "/summary":
                with STATE_LOCK:
                    self._send_json(200, build_summary_locked())
            elif method == "GET" and path == "/areas":
                with STATE_LOCK:
                    self._send_json(200, list(STATE["areas"].values()))
            elif method == "GET" and path == "/points":
                with STATE_LOCK:
                    self._send_json(200, list(STATE["points"].values()))
            elif method == "GET" and path == "/inspections":
                with STATE_LOCK:
                    self._send_json(200, STATE["inspections"])
            else:
                allow = {
                    "/areas": ["GET", "POST"],
                    "/points": ["GET", "POST"],
                    "/inspections": ["GET", "POST"],
                    "/summary": ["GET"],
                }.get(path)
                if allow and method not in allow:
                    self._send_json(405, {"error": f"该接口仅支持 {', '.join(allow)}"})
                else:
                    self._send_json(404, {"error": f"未知接口: {path}"})
        except ApiError as exc:
            self._send_json(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001 - 兜底，避免连接挂死
            self._send_json(500, {"error": f"服务器内部错误: {exc}"})


# ---------------------------------------------------------------- 启动


def main() -> None:
    parser = argparse.ArgumentParser(description="矿井粉尘监测复核服务")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址，默认 127.0.0.1（仅本机）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口，默认 8080")
    parser.add_argument("--data", default="dust_data.json",
                        help="数据持久化文件路径，默认 dust_data.json")
    args = parser.parse_args()

    # 不允许误绑到所有网卡；强制回环
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        raise SystemExit("出于安全限制，本服务只允许监听回环地址 127.0.0.1/::1")

    global STATE
    STATE = load_state(args.data)

    server = ThreadingHTTPServer((args.host, args.port), DustHandler)
    server.data_path = args.data
    print(f"矿井粉尘监测复核服务已启动: http://{args.host}:{args.port}")
    print(f"数据文件: {os.path.abspath(args.data)}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭服务...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
