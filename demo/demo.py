#!/usr/bin/env python3
"""Walk through the two-company staggered reporting scenario against the API.

Usage:
    python demo/demo.py [base_url]        # default http://127.0.0.1:8000

Open http://127.0.0.1:8000/docs alongside this script to replay the same calls
by hand. Use a fresh database (docker compose down -v) for a clean run.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8000"
A = "company_a"
B = "company_b"


def call(method: str, path: str, body: Any | None = None) -> tuple[int, Any]:
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    # Percent-encode the path so non-ASCII route codes are valid request targets.
    request = urllib.request.Request(
        BASE + urllib.parse.quote(path, safe="/?=&"),
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def stops(*names: str) -> list[dict[str, str]]:
    return [
        {"stop_code": f"S{index:02d}", "stop_name": name}
        for index, name in enumerate(names, start=1)
    ]


def route(code: str, *names: str) -> dict[str, Any]:
    return {"route_code": code, "stops": stops(*names)}


def report(company: str, routes: list[dict[str, Any]], fault: bool = False) -> tuple[int, Any]:
    path = f"/companies/{company}/batches"
    if fault:
        path += "?inject_fault=true"
    return call("POST", path, {"routes": routes})


def show_supply(title: str) -> None:
    status, data = call("GET", "/routes")
    print(f"\n== 下游取数：{title} ==")
    print(f"{'线路':<8}{'运营方':<12}{'来源批次':<10}{'冻结':<6}停站顺序")
    for item in data["routes"]:
        order = " → ".join(stop["stop_name"] for stop in item["stops"])
        frozen = "是" if item["frozen"] else "否"
        print(
            f"{item['route_code']:<8}{item['operator']:<12}"
            f"{item['source_batch_number']:<10}{frozen:<6}{order}"
        )


def show_order(order_id: int) -> None:
    status, data = call("GET", f"/transfer-orders/{order_id}")
    print(
        f"\n换线单 #{data['id']} 状态={data['status']} "
        f"可提交={data['ready']}"
    )
    print(f"{'线路':<8}{'旧方':<12}{'新方':<12}{'旧方已撤':<10}{'新方已报':<10}")
    for line in data["lines"]:
        print(
            f"{line['route_code']:<8}{line['from_company']:<12}"
            f"{line['to_company']:<12}{str(line['old_withdrawn']):<10}"
            f"{str(line['new_reported']):<10}"
        )


def show_batches(company: str) -> None:
    status, data = call("GET", f"/companies/{company}/batches")
    print(f"\n== {company} 批次保留情况 ==")
    for batch in data["batches"]:
        print(
            f"批次 {batch['batch_number']}: 上报线路 {batch['route_count']} 条 / "
            f"停站 {batch['stop_count']} 行；保留 {batch['retained_stop_rows']} 行，"
            f"已裁剪 {batch['pruned_stop_rows']} 行"
        )


def preflight() -> None:
    """The scenario expects a fresh database; refuse to run on stale data."""
    status, data = call("GET", f"/companies/{A}/batches")
    if status != 200:
        print(f"无法读取公司信息：{status} {data}")
        raise SystemExit(1)
    if data["batches"]:
        print(
            "数据库中已有上报批次，演示需要空库。\n"
            "请先重置：docker compose down -v && docker compose up --build"
        )
        raise SystemExit(1)


def main() -> None:
    print(f"服务地址：{BASE}    API 文档：{BASE}/docs")
    preflight()

    print("\n[1] 两家公司错时上报首批全量线路表")
    print(report(A, [route("101路", "火车站", "人民广场"), route("102路", "西站", "东站")]))
    print(report(B, [route("201路", "公园", "医院")]))
    show_supply("首次上报后")

    print("\n[2] 开具换线单：101路 由一公司交给二公司")
    status, order = call(
        "POST",
        "/transfer-orders",
        {"lines": [{"route_code": "101路", "from_company": A, "to_company": B}]},
    )
    print(status, f"order_id={order['id']}")
    order_id = order["id"]
    show_order(order_id)

    print("\n[3] 一公司先上报：撤下 101路，同时刷新单外的 102路")
    print(report(A, [route("102路", "西站", "新东站")]))
    show_supply("旧方撤线后：101 冻结在撤下前的明细，102 已刷新")
    show_order(order_id)
    show_batches(A)

    print("\n[4] 二公司后上报：接手的 101路 带新停站表")
    print(report(B, [route("201路", "公园", "医院"), route("101路", "新甲站", "新乙站", "新丙站")]))
    show_supply("双方条件齐备后：归属与停站顺序一起切换")
    show_order(order_id)

    print("\n[5] 旧方重报 101路，归属不回退")
    print(report(A, [route("102路", "西站", "新东站"), route("101路", "旧方回抢站")]))
    show_supply("旧方重报后：101 仍归二公司")
    print("单条线路明细：", json.dumps(call("GET", "/routes/101路")[1], ensure_ascii=False))

    print("\n[6] 写入故障演示：上报一批带 3 个停站的 103路，注入失败")
    before = call("GET", "/routes")[1]["routes"]
    status, body = report(A, [route("102路", "西站", "新东站"), route("103路", "甲", "乙", "丙")], fault=True)
    after = call("GET", "/routes")[1]["routes"]
    print(status, body)
    print("取数结果保持不变：", before == after)
    show_batches(A)

    print("\n[7] 只剩继续供数所需的旧批明细")
    show_batches(B)


if __name__ == "__main__":
    main()
