#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dust_service.py 的端到端自测：真实拉起子进程，通过 HTTP 验证全部业务规则，
包括服务重启后数据保留。仅用标准库。

用法：python3 test_dust_service.py
"""

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))

PASS = 0
FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s" % name)
    else:
        FAIL += 1
        print("  ✗ %s  %s" % (name, detail))


def start_server(state_file):
    proc = subprocess.Popen(
        [sys.executable, os.path.join(HERE, "dust_service.py"),
         "--port", "0", "--state-file", state_file],
        stderr=subprocess.PIPE, text=True,
    )
    port = None
    for _ in range(100):
        line = proc.stderr.readline()
        if not line:
            if proc.poll() is not None:
                raise RuntimeError("服务启动失败：%s" % proc.stderr.read())
            time.sleep(0.05)
            continue
        m = re.search(r"http://[^:]+:(\d+)", line)
        if m:
            port = int(m.group(1))
            break
    if port is None:
        proc.kill()
        raise RuntimeError("未能从启动日志获取端口")
    base = "http://127.0.0.1:%d" % port
    for _ in range(100):  # 等待端口就绪
        try:
            request(base, "GET", "/health")
            break
        except Exception:
            time.sleep(0.05)
    return proc, base


def stop_server(proc):
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def request(base, method, path, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def area(summary, name):
    for a in summary["areas"]:
        if a["area"] == name:
            return a
    return None


def main():
    tmpdir = tempfile.mkdtemp(prefix="dust_test_")
    state_file = os.path.join(tmpdir, "state.json")
    proc, base = start_server(state_file)

    try:
        print("== 1. 登记测点 ==")
        st, r = request(base, "POST", "/points",
                        {"point_id": "P1", "area": "A", "limit": 4})
        check("P1 登记 201", st == 201 and r["duplicate"] is False, str(r))
        st, r = request(base, "POST", "/points",
                        {"point_id": "P2", "area": "A", "limit": 4})
        check("P2 同区域同上限 201", st == 201, str(r))
        st, r = request(base, "POST", "/points",
                        {"point_id": "P3", "area": "A", "limit": 3})
        check("同区域不同上限 409", st == 409 and r["error"] == "area_limit_conflict",
              str(r))
        st, r = request(base, "POST", "/points",
                        {"point_id": "P1", "area": "A", "limit": 4})
        check("重复登记（信息一致）幂等", st == 201 and r["duplicate"] is True, str(r))
        st, r = request(base, "POST", "/points",
                        {"point_id": "P1", "area": "B", "limit": 4})
        check("重复登记（信息冲突）409", st == 409 and r["error"] == "point_conflict",
              str(r))
        st, r = request(base, "POST", "/points",
                        {"point_id": "P4", "area": "B", "limit": 1})
        check("P4 登记（B 区，上限 1）", st == 201, str(r))
        st, r = request(base, "POST", "/points",
                        {"point_id": "P5", "area": "B"})
        check("缺 limit 400", st == 400 and r["error"] == "missing_field", str(r))

        print("== 2. 正常巡检：3.0 / 4.0 均放行计入汇总 ==")
        st, r = request(base, "POST", "/inspections", {"samples": [
            {"point_id": "P1", "concentration": 3.0,
             "sampled_at": "2026-09-19T08:00:00"},
            {"point_id": "P2", "concentration": 4.0,
             "sampled_at": "2026-09-19T08:05:00"},
        ]})
        check("整批接受", st == 201 and r["accepted"] == 2, str(r))
        check("4.0 不超标（严格大于 4）",
              all(m["status"] == "normal" for m in r["measurements"]), str(r))

        st, r = request(base, "GET", "/summary")
        a = area(r, "A")
        check("汇总 A 区 2 个样本、均值 3.5",
              a["sample_count"] == 2 and a["avg_concentration"] == 3.5, str(a))
        check("总计 2、无待复核",
              r["total_samples"] == 2 and r["total_pending_measurements"] == 0,
              str(r))

        print("== 3. 超标(>4)与缺采样时间 -> 待复核，不计汇总；整批其余样本仍接受 ==")
        st, r = request(base, "POST", "/inspections", {"samples": [
            {"point_id": "P1", "concentration": 4.5,
             "sampled_at": "2026-09-19T09:00:00"},
            {"point_id": "P2", "concentration": 2.0},  # 缺 sampled_at
        ]})
        check("整批接受、2 个待复核",
              st == 201 and r["accepted"] == 2
              and sorted(r["pending_after_submit"]) == ["P1", "P2"], str(r))

        st, r = request(base, "GET", "/summary")
        a = area(r, "A")
        check("待复核不计入：A 区仍 2 个、均值 3.5",
              a["sample_count"] == 2 and a["avg_concentration"] == 3.5, str(a))
        check("总待复核测量 2、待复核测点 P1/P2",
              r["total_pending_measurements"] == 2
              and sorted(p["point_id"] for p in r["pending_points"]) == ["P1", "P2"],
              str(r))
        reasons = {p["point_id"]: p["reasons"] for p in r["pending_points"]}
        check("P1 原因 dust_over_limit / P2 原因 missing_sampled_at",
              reasons["P1"] == ["dust_over_limit"]
              and reasons["P2"] == ["missing_sampled_at"], str(reasons))

        print("== 4. 存在待复核测点时，整次巡检被拒绝且数据不变 ==")
        st, r = request(base, "POST", "/inspections", {"samples": [
            {"point_id": "P4", "concentration": 1.0,
             "sampled_at": "2026-09-19T09:30:00"},
        ]})
        check("拒绝 409 pending_points", st == 409
              and r["error"] == "pending_points"
              and sorted(p["point_id"] for p in r["pending_points"]) == ["P1", "P2"],
              str(r))
        st, s2 = request(base, "GET", "/summary")
        check("拒绝后汇总不变（B 区 0 样本）",
              area(s2, "B")["sample_count"] == 0
              and s2["total_samples"] == 2, str(s2))

        print("== 5. 复核放行后重新统计 ==")
        st, r = request(base, "POST", "/reviews", {"point_id": "P1"})
        check("放行 P1 200", st == 200 and r["status"] == "released", str(r))
        a = area(r["summary"], "A")
        check("放行响应内重算：A 区 3 样本（含 4.5）",
              a["sample_count"] == 3, str(a))
        check("放行响应内均值 ≈ (7.0+4.5)/3",
              abs(a["avg_concentration"] - round(11.5 / 3, 4)) < 1e-9, str(a))
        st, r = request(base, "POST", "/reviews", {"point_id": "P2"})
        check("放行 P2 200", st == 200, str(r))
        st, r = request(base, "POST", "/reviews", {"point_id": "P1"})
        check("无待复核时重复放行 409", st == 409 and r["error"] == "not_pending",
              str(r))
        st, r = request(base, "POST", "/reviews", {"point_id": "NOPE"})
        check("放行未登记测点 404", st == 404 and r["error"] == "point_not_found",
              str(r))
        st, r = request(base, "GET", "/summary")
        check("全部放行后：A 区 4 样本、均值 (7+4.5+2)/4",
              area(r, "A")["sample_count"] == 4
              and abs(area(r, "A")["avg_concentration"] - 3.375) < 1e-9
              and r["total_pending_measurements"] == 0, str(r))

        print("== 6. 区域样本数超上限：整次拒绝，既有数据不变 ==")
        st, r = request(base, "POST", "/inspections", {"samples": [
            {"point_id": "P4", "concentration": 1.0,
             "sampled_at": "2026-09-19T10:00:00"},
        ]})
        check("B 区第 1 个样本接受", st == 201, str(r))
        before = request(base, "GET", "/summary")[1]
        st, r = request(base, "POST", "/inspections", {"samples": [
            {"point_id": "P4", "concentration": 1.5,
             "sampled_at": "2026-09-19T10:10:00"},
        ]})
        check("B 区第 2 个样本超上限被拒 409",
              st == 409 and r["error"] == "area_limit_exceeded"
              and r["areas"][0]["limit"] == 1 and r["areas"][0]["total"] == 2,
              str(r))
        after = request(base, "GET", "/summary")[1]
        check("拒绝前后汇总完全一致", before == after,
              "before=%s after=%s" % (before, after))
        # A 区当前 4 个样本，上限 2：跨区域批次也应整次拒绝
        st, r = request(base, "POST", "/inspections", {"samples": [
            {"point_id": "P4", "concentration": 0.9,
             "sampled_at": "2026-09-19T10:20:00"},
            {"point_id": "P1", "concentration": 0.8,
             "sampled_at": "2026-09-19T10:21:00"},
        ]})
        check("多区域批次任一超限即整次拒绝",
              st == 409 and r["error"] == "area_limit_exceeded"
              and sorted(x["area"] for x in r["areas"]) == ["A", "B"], str(r))
        check("拒绝后仍无待复核、数据不变",
              request(base, "GET", "/summary")[1] == before, "")

        print("== 7. 其他非法请求 ==")
        st, r = request(base, "POST", "/inspections", {"samples": []})
        check("空 samples 400", st == 400, str(r))
        st, r = request(base, "POST", "/inspections", {"samples": [
            {"point_id": "P9", "concentration": 1.0,
             "sampled_at": "2026-09-19T10:00:00"},
        ]})
        check("未登记测点 404", st == 404 and r["error"] == "point_not_found", str(r))
        st, r = request(base, "POST", "/inspections", {"samples": [
            {"point_id": "P4", "concentration": 1.0,
             "sampled_at": "not-a-time"},
        ]})
        check("时间格式非法 400", st == 400 and r["error"] == "invalid_field", str(r))
        st, r = request(base, "POST", "/inspections", {"samples": [
            {"point_id": "P4", "concentration": -1.0,
             "sampled_at": "2026-09-19T10:00:00"},
        ]})
        check("负浓度 400", st == 400, str(r))
        st, r = request(base, "GET", "/nope")
        check("未知路径 404", st == 404, str(r))

        summary_before_restart = request(base, "GET", "/summary")[1]
    finally:
        stop_server(proc)

    print("== 8. 重启后数据保留 ==")
    proc2, base2 = start_server(state_file)
    try:
        st, points = request(base2, "GET", "/points")
        check("测点保留：P1/P2/P4", st == 200
              and sorted(p["point_id"] for p in points["points"]) == ["P1", "P2", "P4"],
              str(points))
        st, summary2 = request(base2, "GET", "/summary")
        check("汇总与重启前一致", summary2 == summary_before_restart,
              "before=%s after=%s" % (summary_before_restart, summary2))
        # 重启后仍能继续工作：B 区已满，再提仍拒绝
        st, r = request(base2, "POST", "/inspections", {"samples": [
            {"point_id": "P4", "concentration": 2.2,
             "sampled_at": "2026-09-19T11:00:00"},
        ]})
        check("重启后区域上限仍然生效",
              st == 409 and r["error"] == "area_limit_exceeded", str(r))
    finally:
        stop_server(proc2)

    print("\n结果：%d 通过，%d 失败" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
