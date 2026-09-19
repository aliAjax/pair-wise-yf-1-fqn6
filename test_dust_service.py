#!/usr/bin/env python3
"""端到端冒烟测试：启动服务 -> 跑全部规则 -> 重启验证持久化。"""

import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:%d"
PORT = 8765

calls = 0


def call(method, path, body=None, expected=None):
    global calls
    calls += 1
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE % PORT + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            status, payload = resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        status, payload = e.code, json.loads(e.read())
    if expected is not None:
        assert status == expected, f"{method} {path}: 期望 {expected}, 实际 {status}: {payload}"
    return status, payload


def main():
    tmpdir = tempfile.mkdtemp(prefix="dust-test-")
    data_path = os.path.join(tmpdir, "data.json")
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    proc = subprocess.Popen(
        [sys.executable, os.path.join(os.path.dirname(__file__), "dust_service.py"),
         "--port", str(PORT), "--data", data_path],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
    )
    try:
        for _ in range(50):
            try:
                call("GET", "/summary", expected=200)
                break
            except OSError:
                time.sleep(0.1)
        else:
            print(proc.stdout.read())
            raise RuntimeError("服务启动失败")

        # 1. 登记区域（样本上限 3）和测点
        _, a1 = call("POST", "/areas",
                     {"name": "采煤工作面", "sample_limit": 3}, expected=201)
        _, a2 = call("POST", "/areas",
                     {"name": "掘进工作面", "sample_limit": 2}, expected=201)
        _, p1 = call("POST", "/points",
                     {"name": "1号测点", "area_id": a1["id"]}, expected=201)
        _, p2 = call("POST", "/points",
                     {"name": "2号测点", "area_id": a2["id"]}, expected=201)
        call("POST", "/areas", {"name": "采煤工作面", "sample_limit": 3}, expected=409)
        call("POST", "/points", {"name": "x", "area_id": "nope"}, expected=400)

        # 2. 合法巡检：1 条合格（2.0），1 条待复核（5.0 超标）
        _, insp1 = call("POST", "/inspections", {
            "team": "甲班",
            "samples": [
                {"point_id": p1["id"], "concentration": 2.0, "sampled_at": "2026-09-19T08:00:00"},
                {"point_id": p2["id"], "concentration": 5.0, "sampled_at": "2026-09-19T08:05:00"},
            ],
        }, expected=201)
        assert insp1["accepted_count"] == 1 and insp1["pending_count"] == 1
        pending_id = [r["id"] for r in insp1["readings"] if r["status"] == "pending"][0]
        assert "浓度超过" in insp1["readings"][1]["reasons"][0]

        # 3. 待复核存在 -> 新巡检整次拒绝，已有数据不变
        _, err = call("POST", "/inspections", {
            "team": "乙班",
            "samples": [{"point_id": p1["id"], "concentration": 1.0,
                         "sampled_at": "2026-09-19T09:00:00"}],
        }, expected=409)
        assert "待复核" in err["error"]

        # 4. 此时汇总只含 1 条合格记录，待复核不计入
        _, s = call("GET", "/summary", expected=200)
        assert s["total"]["sample_count"] == 1, s["total"]
        assert s["total"]["avg"] == 2.0
        assert s["pending_count"] == 1

        # 5. 放行后重新统计：总数 2，平均 3.5
        _, rel = call("POST", f"/readings/{pending_id}/release", expected=200)
        assert rel["released_count"] == 1
        assert rel["summary"]["total"]["sample_count"] == 2
        assert rel["summary"]["total"]["avg"] == 3.5
        assert rel["summary"]["released_count"] == 1
        call("POST", f"/readings/{pending_id}/release", expected=409)  # 不能重复放行

        # 6. 采样时间缺失 -> 待复核
        _, insp2 = call("POST", "/inspections", {
            "team": "乙班",
            "samples": [{"point_id": p1["id"], "concentration": 1.5}],
        }, expected=201)
        r = insp2["readings"][0]
        assert r["status"] == "pending" and r["reasons"] == ["采样时间缺失"]
        call("POST", "/inspections", {
            "team": "丙班",
            "samples": [{"point_id": p1["id"], "concentration": 1.0,
                         "sampled_at": "2026-09-19T10:00:00"}],
        }, expected=409)

        # 7. release-all 放行
        _, relall = call("POST", "/readings/release-all", expected=200)
        assert relall["released_count"] == 1
        assert relall["summary"]["total"]["sample_count"] == 3

        # 8. 区域上限：a1 已存 2 条（2.0 + 1.5），再提 2 条 -> 超过上限 3，整次拒绝
        before = call("GET", "/summary")[1]
        _, err = call("POST", "/inspections", {
            "team": "丙班",
            "samples": [
                {"point_id": p1["id"], "concentration": 1.0, "sampled_at": "2026-09-19T10:00:00"},
                {"point_id": p1["id"], "concentration": 1.1, "sampled_at": "2026-09-19T10:01:00"},
            ],
        }, expected=409)
        assert "超过上限" in err["error"]
        after = call("GET", "/summary")[1]
        assert after["total"] == before["total"], "拒绝后汇总发生变化"

        # 只加 1 条 -> 恰好等于上限，接受
        call("POST", "/inspections", {
            "team": "丙班",
            "samples": [{"point_id": p1["id"], "concentration": 1.0,
                         "sampled_at": "2026-09-19T10:00:00"}],
        }, expected=201)

        # 边界：浓度正好 4.0 不算超标
        _, edge = call("POST", "/inspections", {
            "team": "丁班",
            "samples": [{"point_id": p2["id"], "concentration": 4.0,
                         "sampled_at": "2026-09-19T11:00:00"}],
        }, expected=201)
        assert edge["readings"][0]["status"] == "accepted"

        # 坏请求
        call("POST", "/inspections", {"team": "x", "samples": []}, expected=400)
        call("POST", "/inspections", {"team": "x", "samples": [
            {"point_id": p1["id"], "concentration": -1, "sampled_at": "t"}]}, expected=400)
        call("GET", "/nope", expected=404)

        # 9. 重启服务，数据保留
        stored = call("GET", "/summary")[1]
        proc.terminate()
        proc.wait(timeout=5)
        assert os.path.exists(data_path)

        proc = subprocess.Popen(
            [sys.executable, os.path.join(os.path.dirname(__file__), "dust_service.py"),
             "--port", str(PORT), "--data", data_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env,
        )
        for _ in range(50):
            try:
                call("GET", "/summary", expected=200)
                break
            except OSError:
                time.sleep(0.1)
        restarted = call("GET", "/summary")[1]
        assert restarted["total"] == stored["total"], "重启后汇总不一致"
        assert restarted["areas"] == stored["areas"], "重启后区域数据不一致"
        assert restarted["inspection_count"] == 4
        # 重启后历史记录仍可放行查询
        assert len(call("GET", "/inspections", expected=200)[1]) == 4

        print(json.dumps(restarted, ensure_ascii=False, indent=2))
        print(f"\n全部断言通过，共发起 {calls} 次请求。")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    main()
