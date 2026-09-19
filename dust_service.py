#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""矿井粉尘监测复核服务（仅使用 Python 标准库）。

仅监听本机回环地址，提供 JSON 接口：
  POST /points         登记测点
  POST /inspections    提交班组巡检
  POST /reviews        复核放行
  GET  /summary        查询汇总
  GET  /points         查询测点（辅助）
  GET  /health         健康检查

业务规则：
  1. 粉尘浓度 > 4 mg/m³ 或采样时间缺失 -> 该测点待复核，且该次测量不计入汇总。
  2. 提交班组巡检时，只要当前存在任一待复核测点，或任一区域（含本次提交后）
     样本数超过该区域上限，则整次拒绝：既有测量、区域上限、汇总均不变。
  3. 复核放行后，对应测量重新计入统计。
  4. 全部数据持久化到本地 JSON 文件，服务重启后保留。
"""

import json
import os
import sys
import threading
import tempfile
from collections import Counter, defaultdict
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

# ---------- 常量 ----------

DUST_LIMIT_MG = 4.0          # 粉尘浓度阈值（mg/m³），超过（严格大于）即待复核
DEFAULT_HOST = "127.0.0.1"   # 仅监听本机
DEFAULT_PORT = 8000
DEFAULT_STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "dust_state.json"
)


class ApiError(Exception):
    """业务错误：携带 HTTP 状态码、错误码与说明。"""

    def __init__(self, status, code, message, details=None):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.details = details


# ---------- 服务核心（不依赖 HTTP，便于测试） ----------


class DustService:
    """矿井粉尘监测复核服务的业务逻辑与持久化。"""

    def __init__(self, state_file=DEFAULT_STATE_FILE):
        self.state_file = state_file
        self._lock = threading.RLock()
        self._load()

    # ===== 持久化 =====

    def _empty_state(self):
        return {
            "points": {},       # point_id -> {"point_id","area","limit"}
            "measurements": [], # {"id","point_id","area","concentration","sampled_at",
                                #  "status","submitted_at"}
            "seq": 0,
        }

    def _load(self):
        """启动时从本地文件载入；文件不存在则初始化；损坏则报错退出。"""
        if os.path.exists(self.state_file):
            with open(self.state_file, "r", encoding="utf-8") as f:
                self.state = json.load(f)
            for key, value in self._empty_state().items():
                self.state.setdefault(key, value)
        else:
            self.state = self._empty_state()
            self._save_locked()

    def _save_locked(self):
        """原子写入：先写临时文件再 os.replace，避免写坏既有数据。"""
        directory = os.path.dirname(os.path.abspath(self.state_file))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".dust_state.", suffix=".tmp", dir=directory)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.state, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.state_file)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    # ===== 工具 =====

    def _next_id(self, prefix):
        self.state["seq"] += 1
        return "%s-%d" % (prefix, self.state["seq"])

    @staticmethod
    def _require_str(payload, field):
        if field not in payload:
            raise ApiError(400, "missing_field", "缺少字段：%s" % field)
        value = payload[field]
        if not isinstance(value, str) or not value.strip():
            raise ApiError(400, "invalid_field", "字段 %s 必须是非空字符串" % field)
        return value.strip()

    @staticmethod
    def _parse_limit(limit):
        """解析区域样本数上限（正整数）。"""
        if isinstance(limit, bool):  # bool 是 int 的子类，显式拒绝
            raise ApiError(400, "invalid_field", "样本数上限必须是正整数")
        if not isinstance(limit, int) or isinstance(limit, float) or limit <= 0:
            raise ApiError(400, "invalid_field", "样本数上限必须是正整数")
        return limit

    @staticmethod
    def _parse_concentration(value):
        """解析粉尘浓度（非负数值，mg/m³）。"""
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ApiError(400, "invalid_field",
                           "concentration 必须是数值（mg/m³）")
        conc = float(value)
        if conc < 0 or conc != conc or conc in (float("inf"), float("-inf")):
            raise ApiError(400, "invalid_field",
                           "concentration 必须是非负有限数值")
        return conc

    @staticmethod
    def _parse_sampled_at(value):
        """解析采样时间；缺失返回 None（业务上构成待复核），格式错则请求非法。

        支持 ISO 8601，如 2026-09-19T08:30:00，或带时区的 2026-09-19T08:30:00+08:00。
        """
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ApiError(400, "invalid_field",
                           "sampled_at 必须是字符串或 null（缺失）")
        text = value.strip()
        try:
            datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            raise ApiError(400, "invalid_field",
                           "sampled_at 不是合法的 ISO 8601 时间：%s" % text)
        return text

    @staticmethod
    def _is_pending(conc, sampled_at):
        """浓度超过 4 mg/m³ 或采样时间缺失 -> 待复核。"""
        return conc > DUST_LIMIT_MG or sampled_at is None

    def _pending_view(self):
        """返回 {point_id: measurement}：每个测点最近一条仍待复核的测量。"""
        pending = {}
        for m in self.state["measurements"]:
            if m["status"] == "pending":
                pending[m["point_id"]] = m  # 列表按提交顺序，后者覆盖前者
        return pending

    # ===== 接口 1：登记测点 =====

    def register_point(self, payload):
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_body", "请求体必须是 JSON 对象")
        point_id = self._require_str(payload, "point_id")
        area = self._require_str(payload, "area")
        if "limit" not in payload:
            raise ApiError(400, "missing_field", "缺少字段：limit")
        limit = self._parse_limit(payload["limit"])

        with self._lock:
            existing = self.state["points"].get(point_id)
            if existing is not None:
                if existing["area"] != area or existing["limit"] != limit:
                    raise ApiError(
                        409, "point_conflict",
                        "测点 %s 已登记为区域 %s、上限 %d，登记信息不一致"
                        % (point_id, existing["area"], existing["limit"]),
                    )
                return {"point": dict(existing), "duplicate": True}

            # 同一区域内所有测点的样本数上限必须一致（区域级上限）
            for p in self.state["points"].values():
                if p["area"] == area and p["limit"] != limit:
                    raise ApiError(
                        409, "area_limit_conflict",
                        "区域 %s 的样本数上限已为 %d" % (area, p["limit"]),
                    )

            point = {"point_id": point_id, "area": area, "limit": limit}
            self.state["points"][point_id] = point
            self._save_locked()
            return {"point": dict(point), "duplicate": False}

    # ===== 接口 2：提交班组巡检 =====

    def submit_inspection(self, payload):
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_body", "请求体必须是 JSON 对象")
        if "samples" not in payload:
            raise ApiError(400, "missing_field", "缺少字段：samples")
        samples = payload["samples"]
        if not isinstance(samples, list) or len(samples) == 0:
            raise ApiError(400, "invalid_field", "samples 必须是非空数组")

        # —— 先在内存里校验并构造整次巡检；任何一步失败都不落地 ——
        prepared = []
        for i, s in enumerate(samples):
            if not isinstance(s, dict):
                raise ApiError(400, "invalid_sample",
                               "samples[%d] 必须是对象" % i)
            point_id = self._require_str(s, "point_id")
            with self._lock:
                point = self.state["points"].get(point_id)
            if point is None:
                raise ApiError(404, "point_not_found",
                               "samples[%d]：测点 %s 未登记" % (i, point_id))
            if "concentration" not in s:
                raise ApiError(400, "missing_field",
                               "samples[%d] 缺少 concentration" % i)
            conc = self._parse_concentration(s["concentration"])
            sampled_at = self._parse_sampled_at(s.get("sampled_at"))
            prepared.append({
                "point_id": point_id,
                "area": point["area"],
                "concentration": conc,
                "sampled_at": sampled_at,
            })

        with self._lock:
            # 规则：只要存在待复核测点（不限于本次巡检涉及的测点），整次拒绝
            pending = self._pending_view()
            if pending:
                raise ApiError(
                    409, "pending_points",
                    "存在待复核测点，禁止提交新巡检，请先复核放行",
                    {"pending_points": [
                        {
                            "point_id": pid,
                            "measurement_id": m["id"],
                            "area": m["area"],
                            "concentration": m["concentration"],
                            "sampled_at": m["sampled_at"],
                        }
                        for pid, m in sorted(pending.items())
                    ]},
                )

            # 规则：任一区域（既有测量 + 本次提交后）样本数超过上限，整次拒绝。
            # 样本数按测量条数统计（待复核测量同样占位）。
            counts = Counter(m["area"] for m in self.state["measurements"])
            added = Counter(p["area"] for p in prepared)
            limits = {p["area"]: p["limit"] for p in self.state["points"].values()}
            over = []
            for area, n in added.items():
                total = counts[area] + n
                if total > limits[area]:
                    over.append({"area": area, "current": counts[area],
                                 "submitted": n, "limit": limits[area],
                                 "total": total})
            if over:
                raise ApiError(
                    409, "area_limit_exceeded",
                    "区域样本数超过上限，本次巡检整次拒绝",
                    {"areas": over},
                )

            # 全部检查通过，一次性落库
            now = datetime.now().isoformat(timespec="seconds")
            accepted = []
            for p in prepared:
                status = "pending" if self._is_pending(
                    p["concentration"], p["sampled_at"]) else "normal"
                measurement = {
                    "id": self._next_id("m"),
                    "point_id": p["point_id"],
                    "area": p["area"],
                    "concentration": p["concentration"],
                    "sampled_at": p["sampled_at"],
                    "status": status,
                    "submitted_at": now,
                }
                self.state["measurements"].append(measurement)
                accepted.append({
                    "measurement_id": measurement["id"],
                    "point_id": measurement["point_id"],
                    "area": measurement["area"],
                    "concentration": measurement["concentration"],
                    "sampled_at": measurement["sampled_at"],
                    "status": status,
                })
            self._save_locked()

            return {
                "accepted": len(accepted),
                "measurements": accepted,
                "pending_after_submit": [
                    a["point_id"] for a in accepted if a["status"] == "pending"
                ],
            }

    # ===== 接口 3：复核放行 =====

    def review_point(self, payload):
        if not isinstance(payload, dict):
            raise ApiError(400, "invalid_body", "请求体必须是 JSON 对象")
        point_id = self._require_str(payload, "point_id")
        note = payload.get("note")
        if note is not None and not isinstance(note, str):
            raise ApiError(400, "invalid_field", "note 必须是字符串")

        with self._lock:
            if point_id not in self.state["points"]:
                raise ApiError(404, "point_not_found",
                               "测点 %s 未登记" % point_id)
            pending = self._pending_view()
            if point_id not in pending:
                raise ApiError(409, "not_pending",
                               "测点 %s 当前没有待复核测量" % point_id)

            measurement = pending[point_id]
            measurement["status"] = "released"
            measurement["reviewed_at"] = datetime.now().isoformat(
                timespec="seconds")
            if note:
                measurement["review_note"] = note
            self._save_locked()

            # 放行后重新统计
            return {
                "point_id": point_id,
                "measurement_id": measurement["id"],
                "previous_status": "pending",
                "status": "released",
                "summary": self._summary_locked(),
            }

    # ===== 接口 4：查询汇总 =====

    def _summary_locked(self):
        """根据当前全部测量重新统计；待复核测量不计入。"""
        area_buckets = defaultdict(lambda: {"sum": 0.0, "count": 0})
        pending_ids = set()
        total_sum = 0.0
        total_count = 0
        total_pending = 0

        for m in self.state["measurements"]:
            if m["status"] == "pending":
                total_pending += 1
                pending_ids.add(m["point_id"])
                continue
            b = area_buckets[m["area"]]
            b["sum"] += m["concentration"]
            b["count"] += 1
            total_sum += m["concentration"]
            total_count += 1

        limits = {p["area"]: p["limit"] for p in self.state["points"].values()}
        areas = []
        for area in sorted(area_buckets.keys() | set(limits.keys())):
            b = area_buckets.get(area, {"sum": 0.0, "count": 0})
            count = b["count"]
            mean = round(b["sum"] / count, 4) if count else None
            areas.append({
                "area": area,
                "sample_count": count,
                "avg_concentration": mean,
                "limit": limits.get(area),
                "within_limit": count <= limits[area] if area in limits else None,
            })

        pending_points = []
        for pid, m in sorted(self._pending_view().items()):
            reasons = []
            if m["concentration"] > DUST_LIMIT_MG:
                reasons.append("dust_over_limit")
            if m["sampled_at"] is None:
                reasons.append("missing_sampled_at")
            pending_points.append({
                "point_id": pid,
                "measurement_id": m["id"],
                "area": m["area"],
                "concentration": m["concentration"],
                "sampled_at": m["sampled_at"],
                "reasons": reasons,
            })

        return {
            "dust_limit_mg_per_m3": DUST_LIMIT_MG,
            "total_samples": total_count,                 # 不计待复核
            "total_pending_measurements": total_pending,
            "avg_concentration": round(total_sum / total_count, 4)
            if total_count else None,
            "areas": areas,
            "pending_points": pending_points,
        }

    def get_summary(self):
        with self._lock:
            return self._summary_locked()

    def list_points(self):
        with self._lock:
            counts = Counter(m["area"] for m in self.state["measurements"])
            pending = self._pending_view()
            points = []
            for pid, p in sorted(self.state["points"].items()):
                points.append({
                    "point_id": pid,
                    "area": p["area"],
                    "limit": p["limit"],
                    "area_sample_count": counts[p["area"]],
                    "pending": pid in pending,
                })
            return {"points": points}


# ---------- HTTP 层 ----------


class DustHandler(BaseHTTPRequestHandler):
    server_version = "DustMonitor/1.0"

    # 关闭默认访问日志（避免每次请求刷 stderr）；出错时由 do_* 自行记录
    def log_message(self, fmt, *args):
        pass

    def _send_json(self, status, body):
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read_json(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            raise ApiError(400, "invalid_header", "Content-Length 非法")
        if length <= 0:
            raise ApiError(400, "invalid_body", "请求体为空，需要 JSON 对象")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ApiError(400, "invalid_json", "请求体不是合法 JSON")

    def _call(self, func, *args, created=True):
        try:
            result = func(*args)
            if result is not None:
                # 创建类接口返回 201；动作型接口（如复核放行）返回 200
                self._send_json(201 if created else 200, result)
        except ApiError as e:
            body = {"error": e.code, "message": e.message}
            if e.details is not None:
                body.update(e.details)
            self._send_json(e.status, body)
        except Exception as e:  # 兜底：不把异常栈暴露给客户端
            sys.stderr.write("[ERROR] %s\n" % e)
            self._send_json(500, {"error": "internal_error", "message": "服务内部错误"})

    def do_GET(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        svc = self.server.dust_service
        if path == "/summary":
            self._call(svc.get_summary, created=False)
        elif path == "/points":
            self._call(svc.list_points, created=False)
        elif path == "/health":
            self._send_json(200, {"status": "ok"})
        else:
            self._send_json(404, {"error": "not_found", "message": "未知接口：%s" % path})

    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/") or "/"
        svc = self.server.dust_service
        if path == "/points":
            self._call(svc.register_point, self._safe_body())
        elif path == "/inspections":
            self._call(svc.submit_inspection, self._safe_body())
        elif path == "/reviews":
            self._call(svc.review_point, self._safe_body(), created=False)
        else:
            self._send_json(404, {"error": "not_found", "message": "未知接口：%s" % path})

    def _safe_body(self):
        """读取并解析请求体；解析失败时抛出，由 _call 统一转 JSON 错误。"""
        return self._read_json()


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(description="矿井粉尘监测复核服务（仅监听本机）")
    parser.add_argument("--host", default=DEFAULT_HOST,
                        help="监听地址（默认 127.0.0.1，仅本机）")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT,
                        help="监听端口（默认 %d，0 表示由系统分配）" % DEFAULT_PORT)
    parser.add_argument("--state-file", default=DEFAULT_STATE_FILE,
                        help="持久化状态文件路径")
    args = parser.parse_args(argv)

    if args.host not in ("127.0.0.1", "localhost", "::1"):
        sys.stderr.write("拒绝绑定非本机地址：%s（本服务只允许监听回环地址）\n"
                         % args.host)
        return 2

    service = DustService(args.state_file)
    httpd = ThreadingHTTPServer((args.host, args.port), DustHandler)
    httpd.daemon_threads = True
    httpd.dust_service = service
    host, port = httpd.server_address[:2]
    print("矿井粉尘监测复核服务启动：http://%s:%d （仅本机访问）" % (host, port),
          file=sys.stderr, flush=True)
    print("状态文件：%s" % os.path.abspath(args.state_file),
          file=sys.stderr, flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n正在停止服务...\n")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
