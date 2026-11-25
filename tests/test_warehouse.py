from __future__ import annotations

import sys
import threading
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import service  # noqa: E402
from app.config import Settings  # noqa: E402
from app.db import connect, read_transaction, transaction  # noqa: E402
from app.main import create_app  # noqa: E402

A = "company_a"
B = "company_b"


def stops(*names: str) -> list[dict[str, str]]:
    return [{"stop_code": f"S{index}", "stop_name": name} for index, name in enumerate(names, 1)]


def route(code: str, *names: str) -> dict[str, Any]:
    return {"route_code": code, "stops": stops(*names)}


@pytest.fixture()
def env(tmp_path: Path):
    settings = Settings(
        database_path=str(tmp_path / "warehouse.db"),
        enable_fault_injection=True,
    )
    app = create_app(settings)
    with TestClient(app) as test_client:
        yield settings, test_client


@pytest.fixture()
def client(env) -> TestClient:
    return env[1]


def report(
    client: TestClient,
    company: str,
    batch_number: int | None = None,
    routes: list[dict[str, Any]] | None = None,
    inject_fault: bool = False,
) -> Any:
    payload: dict[str, Any] = {"routes": routes or []}
    if batch_number is not None:
        payload["batch_number"] = batch_number
    url = f"/companies/{company}/batches"
    if inject_fault:
        url += "?inject_fault=true"
    return client.post(url, json=payload)


def routes_by_code(client: TestClient) -> dict[str, dict[str, Any]]:
    response = client.get("/routes")
    assert response.status_code == 200
    return {item["route_code"]: item for item in response.json()["routes"]}


def batch_rows(client: TestClient, company: str) -> dict[int, dict[str, Any]]:
    response = client.get(f"/companies/{company}/batches")
    assert response.status_code == 200
    return {item["batch_number"]: item for item in response.json()["batches"]}


def new_order(client: TestClient, lines: list[tuple[str, str, str]], inject_fault: bool = False) -> Any:
    url = "/transfer-orders"
    if inject_fault:
        url += "?inject_fault=true"
    return client.post(
        url,
        json={
            "lines": [
                {"route_code": code, "from_company": source, "to_company": target}
                for code, source, target in lines
            ]
        },
    )


def seed_two_companies(client: TestClient) -> None:
    assert report(client, A, 1, [route("101路", "火车站", "人民广场"), route("102路", "西站", "东站")]).status_code == 201
    assert report(client, B, 1, [route("201路", "公园", "医院")]).status_code == 201


# ---------------------------------------------------------------------------
# Intake and ownership
# ---------------------------------------------------------------------------


def test_new_route_belongs_to_first_reporter(client: TestClient) -> None:
    seed_two_companies(client)
    # B reports a route A already owns; ownership must not move.
    response = report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "B甲", "B乙")])
    assert response.status_code == 201

    rows = routes_by_code(client)
    assert rows["101路"]["operator"] == A
    assert [s["stop_name"] for s in rows["101路"]["stops"]] == ["火车站", "人民广场"]


def test_brand_new_route_belongs_to_first_reporter(client: TestClient) -> None:
    assert report(client, B, 1, [route("301路", "首报站")]).status_code == 201
    assert report(client, A, 1, [route("301路", "后报站")]).status_code == 201
    row = routes_by_code(client)["301路"]
    assert row["operator"] == B
    assert [s["stop_name"] for s in row["stops"]] == ["首报站"]
    assert client.get("/routes/301路").json()["first_reported_by"] == B


def test_batch_number_must_increase_per_company(client: TestClient) -> None:
    seed_two_companies(client)
    response = report(client, A, 1, [route("101路", "火车站")])
    assert response.status_code == 409
    assert "must increase" in response.json()["detail"]
    # Equal numbers across companies are independent.
    assert report(client, B, 1, [route("201路", "公园")]).status_code == 409


def test_auto_batch_number_when_omitted(client: TestClient) -> None:
    seed_two_companies(client)
    response = report(client, A, None, [route("101路", "火车站")])
    assert response.status_code == 201
    assert response.json()["batch_number"] == 2


def test_unknown_company_is_rejected(client: TestClient) -> None:
    response = report(client, "company_x", 1, [route("999路", "某站")])
    assert response.status_code == 404


def test_snapshot_withdrawal_removes_route_from_supply(client: TestClient) -> None:
    seed_two_companies(client)
    assert report(client, A, 2, [route("102路", "西站", "东站")]).status_code == 201
    rows = routes_by_code(client)
    assert "101路" not in rows
    detail = client.get("/routes/101路").json()
    assert detail["in_supply"] is False
    assert detail["first_reported_by"] == A


# ---------------------------------------------------------------------------
# Transfer order flow
# ---------------------------------------------------------------------------


def test_full_transfer_freezes_then_switches_together(client: TestClient) -> None:
    seed_two_companies(client)
    response = new_order(client, [("101路", A, B)])
    assert response.status_code == 201
    order = response.json()
    assert order["status"] == "open"
    assert order["ready"] is False
    order_id = order["id"]

    # A refreshes 102 outside the order; it should follow the new batch at once.
    assert report(client, A, 2, [route("101路", "火车站", "人民广场"), route("102路", "西站", "新东站")]).status_code == 201
    rows = routes_by_code(client)
    assert [s["stop_name"] for s in rows["102路"]["stops"]] == ["西站", "新东站"]
    assert rows["101路"]["operator"] == A
    assert rows["101路"]["frozen"] is False

    # B reports its share (not yet withdrawn by A): order stays open.
    response = report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "新甲", "新乙")])
    assert response.json()["auto_committed_order"] is None
    assert client.get(f"/transfer-orders/{order_id}").json()["status"] == "open"
    assert routes_by_code(client)["101路"]["operator"] == A

    # A withdraws 101: both sides satisfied, commit happens in the same batch.
    response = report(client, A, 3, [route("102路", "西站", "新东站")])
    assert response.status_code == 201
    assert response.json()["auto_committed_order"]["status"] == "committed"

    rows = routes_by_code(client)
    assert rows["101路"]["operator"] == B
    assert rows["101路"]["frozen"] is False
    assert [s["stop_name"] for s in rows["101路"]["stops"]] == ["新甲", "新乙"]

    order = client.get(f"/transfer-orders/{order_id}").json()
    assert order["status"] == "committed"
    assert order["lines"][0]["old_withdrawn"] is True
    assert order["lines"][0]["new_reported"] is True


def test_withdrawn_first_uses_pre_withdrawal_detail(client: TestClient) -> None:
    seed_two_companies(client)
    order_id = new_order(client, [("101路", A, B)]).json()["id"]

    assert report(client, A, 2, [route("102路", "西站", "东站")]).status_code == 201
    rows = routes_by_code(client)
    frozen = rows["101路"]
    assert frozen["operator"] == A
    assert frozen["frozen"] is True
    assert frozen["source_batch_number"] == 1
    assert [s["stop_name"] for s in frozen["stops"]] == ["火车站", "人民广场"]

    detail = client.get(f"/transfer-orders/{order_id}").json()
    assert detail["lines"][0]["frozen_batch_number"] == 1

    # B finally reports the route; the order commits and stops switch.
    assert report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "接手甲", "接手乙")]).status_code == 201
    rows = routes_by_code(client)
    assert rows["101路"]["operator"] == B
    assert [s["stop_name"] for s in rows["101路"]["stops"]] == ["接手甲", "接手乙"]


def test_order_commits_only_when_every_line_is_ready(client: TestClient) -> None:
    seed_two_companies(client)
    order_id = new_order(client, [("101路", A, B), ("102路", A, B)]).json()["id"]

    # A withdraws only 101; 102 is still in its current table.
    assert report(client, A, 2, [route("102路", "西站", "东站")]).status_code == 201
    # B reports 101 but not 102 yet.
    response = report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "B甲")])
    assert response.json()["auto_committed_order"] is None
    order = client.get(f"/transfer-orders/{order_id}").json()
    assert order["status"] == "open"
    assert order["ready"] is False
    assert routes_by_code(client)["101路"]["operator"] == A

    # A withdraws 102, B reports 102: the last write flips both lines atomically.
    assert report(client, A, 3, []).status_code == 201
    response = report(
        client, B, 3,
        [route("201路", "公园", "医院"), route("101路", "B甲"), route("102路", "B西", "B东")],
    )
    assert response.json()["auto_committed_order"]["status"] == "committed"
    rows = routes_by_code(client)
    assert rows["101路"]["operator"] == B
    assert rows["102路"]["operator"] == B


def test_frozen_detail_uses_last_pre_withdrawal_report(client: TestClient) -> None:
    seed_two_companies(client)
    new_order(client, [("101路", A, B)])
    # The old company refreshes 101 while the order is open...
    assert report(
        client, A, 2,
        [route("101路", "火车站", "改线广场", "新终点"), route("102路", "西站", "东站")],
    ).status_code == 201
    # ...then withdraws it: the frozen detail is the refreshed pre-withdrawal one.
    assert report(client, A, 3, [route("102路", "西站", "东站")]).status_code == 201
    row = routes_by_code(client)["101路"]
    assert row["frozen"] is True
    assert row["source_batch_number"] == 2
    assert [s["stop_name"] for s in row["stops"]] == ["火车站", "改线广场", "新终点"]


def test_old_company_rereport_cannot_take_route_back(client: TestClient) -> None:
    seed_two_companies(client)
    new_order(client, [("101路", A, B)])
    report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "新甲", "新乙")])
    report(client, A, 2, [route("102路", "西站", "东站")])
    assert routes_by_code(client)["101路"]["operator"] == B

    # Old company reports the handed-over route again with different stops.
    assert report(client, A, 3, [route("102路", "西站", "东站"), route("101路", "回抢站")]).status_code == 201
    row = routes_by_code(client)["101路"]
    assert row["operator"] == B
    assert [s["stop_name"] for s in row["stops"]] == ["新甲", "新乙"]
    assert client.get("/routes/101路").json()["first_reported_by"] == A


def test_old_company_reports_route_again_before_new_side_is_ready(client: TestClient) -> None:
    seed_two_companies(client)
    new_order(client, [("101路", A, B)])
    # Withdraw, then re-add the route in a later batch: service follows the
    # current table again and the order is not ready.
    assert report(client, A, 2, [route("102路", "西站", "东站")]).status_code == 201
    assert routes_by_code(client)["101路"]["frozen"] is True
    assert report(client, A, 3, [route("101路", "回填甲"), route("102路", "西站", "东站")]).status_code == 201
    row = routes_by_code(client)["101路"]
    assert row["frozen"] is False
    assert row["source_batch_number"] == 3
    assert [s["stop_name"] for s in row["stops"]] == ["回填甲"]


def test_after_commit_new_owner_refresh_is_used(client: TestClient) -> None:
    seed_two_companies(client)
    new_order(client, [("101路", A, B)])
    report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "新甲")])
    report(client, A, 2, [route("102路", "西站", "东站")])
    assert routes_by_code(client)["101路"]["operator"] == B
    assert report(client, B, 3, [route("201路", "公园", "医院"), route("101路", "新甲", "新乙")]).status_code == 201
    row = routes_by_code(client)["101路"]
    assert row["operator"] == B
    assert row["source_batch_number"] == 3
    assert [s["stop_name"] for s in row["stops"]] == ["新甲", "新乙"]


def test_only_one_open_order(client: TestClient) -> None:
    seed_two_companies(client)
    assert new_order(client, [("101路", A, B)]).status_code == 201
    second = new_order(client, [("102路", A, B)])
    assert second.status_code == 409


def test_order_requires_route_currently_operated_by_from_company(client: TestClient) -> None:
    seed_two_companies(client)
    response = new_order(client, [("201路", A, B)])
    assert response.status_code == 409
    assert "not company_a" in response.json()["detail"] or "operated by" in response.json()["detail"]


def test_cancel_restores_original_supply(client: TestClient) -> None:
    seed_two_companies(client)
    order_id = new_order(client, [("101路", A, B)]).json()["id"]
    report(client, A, 2, [route("102路", "西站", "东站")])
    assert routes_by_code(client)["101路"]["frozen"] is True

    response = client.post(f"/transfer-orders/{order_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"
    # The withdrawal A already reported is real, so 101 is no longer in supply;
    # cancel only stops the transfer bookkeeping, it cannot resurrect the route.
    detail = client.get("/routes/101路").json()
    assert detail["in_supply"] is False
    assert detail["operator"] == A
    row = routes_by_code(client)["102路"]
    assert row["frozen"] is False
    assert row["source_batch_number"] == 2


def test_manual_commit_rejected_until_ready(client: TestClient) -> None:
    seed_two_companies(client)
    order_id = new_order(client, [("101路", A, B)]).json()["id"]
    response = client.post(f"/transfer-orders/{order_id}/commit")
    assert response.status_code == 409
    assert "not ready" in response.json()["detail"]


def test_manual_commit_after_auto_commit_is_idempotent(client: TestClient) -> None:
    seed_two_companies(client)
    order_id = new_order(client, [("101路", A, B)]).json()["id"]
    report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "新甲")])
    report(client, A, 2, [route("102路", "西站", "东站")])
    before = client.get("/routes").json()
    response = client.post(f"/transfer-orders/{order_id}/commit")
    assert response.status_code == 200
    assert response.json()["status"] == "committed"
    assert client.get("/routes").json()["routes"] == before["routes"]


def test_two_way_exchange_in_one_order(client: TestClient) -> None:
    seed_two_companies(client)
    order_id = new_order(client, [("101路", A, B), ("201路", B, A)]).json()["id"]
    # Both sides withdraw and report in the right order.
    assert report(client, B, 2, [route("101路", "B甲", "B乙")]).status_code == 201
    rows = routes_by_code(client)
    assert rows["201路"]["frozen"] is True
    assert rows["201路"]["operator"] == B

    assert report(client, A, 2, [route("102路", "西站", "东站"), route("201路", "A甲", "A乙")]).status_code == 201
    order = client.get(f"/transfer-orders/{order_id}").json()
    assert order["status"] == "committed"
    rows = routes_by_code(client)
    assert rows["101路"]["operator"] == B
    assert [s["stop_name"] for s in rows["101路"]["stops"]] == ["B甲", "B乙"]
    assert rows["201路"]["operator"] == A
    assert [s["stop_name"] for s in rows["201路"]["stops"]] == ["A甲", "A乙"]


# ---------------------------------------------------------------------------
# Retention
# ---------------------------------------------------------------------------


def test_old_batches_keep_only_rows_needed_for_supply(client: TestClient) -> None:
    seed_two_companies(client)
    new_order(client, [("101路", A, B)])

    # A withdraws 101 before B reports: batch 1 keeps only 101's frozen detail.
    report(client, A, 2, [route("102路", "西站", "东站")])
    batches = batch_rows(client, A)
    assert batches[1]["pruned_stop_rows"] == 2  # 102's two stops
    assert batches[1]["retained_stop_rows"] == 2  # 101's two stops
    assert batches[1]["route_count"] == 2  # header survives

    # After commit nothing reads batch 1 any more, so its detail is pruned.
    report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "新甲", "新乙")])
    batches = batch_rows(client, A)
    assert batches[1]["retained_stop_rows"] == 0
    assert batches[1]["pruned_stop_rows"] == 4
    assert batches[2]["retained_stop_rows"] == 2  # 102's two stops
    # Latest batch is never pruned.
    assert batches[2]["pruned_stop_rows"] == 0


def test_pruning_does_not_touch_stops_still_read(client: TestClient) -> None:
    seed_two_companies(client)
    # B batch 1 still serves 201; B batch 2 adds 101 while keeping 201.
    report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "新甲", "新乙")])
    batches = batch_rows(client, B)
    # Batch 1's 201 rows are still served from batch 1? No: batch 2 also has 201,
    # so serving uses the latest batch and batch 1 is fully pruned.
    assert batches[1]["retained_stop_rows"] == 0
    assert batches[2]["retained_stop_rows"] == 4


# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------


def test_failed_batch_write_changes_nothing(client: TestClient) -> None:
    seed_two_companies(client)
    order_id = new_order(client, [("101路", A, B)]).json()["id"]
    report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "新甲", "新乙")])

    before_routes = client.get("/routes").json()
    before_batches = batch_rows(client, A)
    before_order = client.get(f"/transfer-orders/{order_id}").json()

    response = report(client, A, 2, [route("102路", "西站", "东站")], inject_fault=True)
    assert response.status_code == 500

    assert client.get("/routes").json()["routes"] == before_routes["routes"]
    assert batch_rows(client, A) == before_batches
    assert client.get(f"/transfer-orders/{order_id}").json() == before_order


def test_failed_create_order_leaves_nothing(client: TestClient) -> None:
    seed_two_companies(client)
    before = client.get("/routes").json()
    response = new_order(client, [("101路", A, B)], inject_fault=True)
    assert response.status_code == 500
    assert client.get("/transfer-orders/current").json() == {"order": None}
    assert client.get("/routes").json()["routes"] == before["routes"]


def test_failed_auto_commit_rolls_back_batch_order_and_supply(
    env: tuple[Settings, TestClient],
) -> None:
    settings, client = env
    seed_two_companies(client)
    order_id = new_order(client, [("101路", A, B)]).json()["id"]
    report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "新甲", "新乙")])

    # A's withdrawal is the batch that would satisfy and auto-commit the order.
    # The injected fault lands after every write, in the same transaction.
    before_routes = client.get("/routes").json()
    before_batches = batch_rows(client, A)
    before_order = client.get(f"/transfer-orders/{order_id}").json()

    response = report(client, A, 2, [route("102路", "西站", "东站")], inject_fault=True)
    assert response.status_code == 500

    assert client.get("/routes").json()["routes"] == before_routes["routes"]
    assert batch_rows(client, A) == before_batches
    assert client.get(f"/transfer-orders/{order_id}").json() == before_order
    assert client.get("/transfer-orders/current").json()["order"]["status"] == "open"

    # Same request without the fault still commits cleanly.
    response = report(client, A, 2, [route("102路", "西站", "东站")])
    assert response.json()["auto_committed_order"]["status"] == "committed"
    assert routes_by_code(client)["101路"]["operator"] == B


def test_open_order_line_uses_current_old_company_table(client: TestClient) -> None:
    seed_two_companies(client)
    order_id = new_order(client, [("101路", A, B)]).json()["id"]
    # The old company refreshes the stop table of a line still on the order.
    assert report(
        client, A, 2,
        [route("101路", "火车站", "改线广场", "新终点"), route("102路", "西站", "东站")],
    ).status_code == 201
    row = routes_by_code(client)["101路"]
    assert row["operator"] == A
    assert row["frozen"] is False
    assert row["source_batch_number"] == 2
    assert [s["stop_name"] for s in row["stops"]] == ["火车站", "改线广场", "新终点"]
    assert client.get(f"/transfer-orders/{order_id}").json()["status"] == "open"


def test_batch_numbers_are_per_company(client: TestClient) -> None:
    seed_two_companies(client)
    response = report(client, B, 7, [route("201路", "公园")])
    assert response.status_code == 201
    assert response.json()["batch_number"] == 7
    assert batch_rows(client, A).keys() == {1}


def test_concurrent_reports_get_distinct_increasing_numbers(client: TestClient) -> None:
    from concurrent.futures import ThreadPoolExecutor

    seed_two_companies(client)

    def send(index: int) -> int:
        response = report(client, A, None, [route("101路", f"站{index}")])
        assert response.status_code == 201
        return response.json()["batch_number"]

    with ThreadPoolExecutor(max_workers=6) as pool:
        numbers = sorted(pool.map(send, range(6)))
    assert numbers == [2, 3, 4, 5, 6, 7]
    assert sorted(batch_rows(client, A)) == [1, 2, 3, 4, 5, 6, 7]


def test_openapi_and_docs_are_served(client: TestClient) -> None:
    assert client.get("/docs").status_code == 200
    schema = client.get("/openapi.json").json()
    assert "/routes" in schema["paths"]
    assert "/companies/{company_code}/batches" in schema["paths"]
    assert "/transfer-orders" in schema["paths"]


def test_duplicate_route_in_snapshot_is_rejected(client: TestClient) -> None:
    response = report(client, A, 1, [route("101路", "甲"), route("101路", "乙")])
    assert response.status_code == 422


def test_unknown_route_lookup_404(client: TestClient) -> None:
    assert client.get("/routes/无此线路").status_code == 404


# ---------------------------------------------------------------------------
# Receipt/snapshot timing: the withdrawal batch may finish the line switch
# before the create/cancel receipt has been returned.
# ---------------------------------------------------------------------------


def test_create_receipt_reflects_its_own_commit_under_racing_report(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    import time

    seed_two_companies(client)
    # B already supplies 101, so A's next withdrawal would auto-commit any
    # open order for 101 the instant it lands.
    report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "新甲", "新乙")])

    parked = threading.Event()
    release = threading.Event()

    def hook(order_id: int) -> None:
        parked.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr(service, "create_order_precommit_hook", hook)
    receipts: dict[str, Any] = {}

    def create_order() -> None:
        receipts["create"] = new_order(client, [("101路", A, B)]).json()

    def withdraw() -> None:
        receipts["withdraw"] = report(
            client, A, 2, [route("102路", "西站", "东站")]
        ).json()

    creator = threading.Thread(target=create_order)
    creator.start()
    assert parked.wait(timeout=5)

    # The write is complete but the receipt has not returned. The withdrawal
    # report queues on the create transaction's write lock instead of failing.
    writer = threading.Thread(target=withdraw)
    writer.start()
    time.sleep(0.2)

    release.set()
    creator.join(timeout=5)
    writer.join(timeout=5)

    # The create receipt must show the state at ITS commit: still open.
    create_receipt = receipts["create"]
    assert create_receipt["status"] == "open"
    assert create_receipt["ready"] is False
    assert create_receipt["lines"][0]["operator"] == A
    assert create_receipt["lines"][0]["serving_company"] == A

    # The queued report then auto-commits normally.
    assert receipts["withdraw"]["auto_committed_order"]["status"] == "committed"
    order_id = create_receipt["id"]
    final = client.get(f"/transfer-orders/{order_id}").json()
    assert final["status"] == "committed"
    assert final["lines"][0]["operator"] == B
    assert final["lines"][0]["serving_company"] == B
    assert routes_by_code(client)["101路"]["operator"] == B


def test_order_reads_inside_snapshot_ignore_concurrent_commit(
    env: tuple[Settings, TestClient],
) -> None:
    settings, client = env
    seed_two_companies(client)
    order_id = new_order(client, [("101路", A, B)]).json()["id"]
    report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "新甲", "新乙")])

    raw = connect(settings)
    try:
        with read_transaction(raw):
            before = service._load_order(raw, order_id)
            assert before["status"] == "open"
            assert before["lines"][0]["operator"] == A
            assert before["lines"][0]["serving_company"] == A

            # A read is in flight while the satisfying report auto-commits on
            # another connection; the reader must not block the writer.
            response = report(client, A, 2, [route("102路", "西站", "东站")])
            assert response.status_code == 201
            assert response.json()["auto_committed_order"]["status"] == "committed"

            # Same snapshot, so the response cannot mix an open header with
            # post-commit line conditions or serving party.
            assert service._load_order(raw, order_id) == before
        after = service.get_order(raw, order_id)
    finally:
        raw.close()

    assert after["status"] == "committed"
    assert after["lines"][0]["operator"] == B
    assert after["lines"][0]["serving_company"] == B
    assert after["lines"][0]["ready"] is True


def test_concurrent_order_gets_never_mix_open_header_with_committed_lines(
    client: TestClient,
) -> None:
    seed_two_companies(client)
    report(client, B, 2, [route("201路", "公园", "医院"), route("101路", "新甲", "新乙")])
    order_id = new_order(client, [("101路", A, B)]).json()["id"]

    go = threading.Barrier(6)  # main + writer + four readers
    stop = threading.Event()

    def validate(order: dict[str, Any]) -> None:
        if order["status"] == "open":
            for line in order["lines"]:
                assert line["operator"] == A
                assert line["serving_company"] == A
                assert line["ready"] is False
        elif order["status"] == "committed":
            for line in order["lines"]:
                assert line["operator"] == B
                assert line["serving_company"] == B
                assert line["ready"] is True
        else:
            raise AssertionError(f"unexpected status {order['status']}")

    def reader() -> None:
        go.wait()
        while not stop.is_set():
            validate(client.get(f"/transfer-orders/{order_id}").json())
            current = client.get("/transfer-orders/current").json()["order"]
            if current is not None:
                validate(current)

    readers = [threading.Thread(target=reader) for _ in range(4)]
    for thread in readers:
        thread.start()

    def writer() -> None:
        go.wait()
        response = report(client, A, 2, [route("102路", "西站", "东站")])
        assert response.json()["auto_committed_order"]["status"] == "committed"

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    go.wait()
    writer_thread.join(timeout=10)
    stop.set()
    for thread in readers:
        thread.join(timeout=10)

    final = client.get(f"/transfer-orders/{order_id}").json()
    assert final["status"] == "committed"
    assert final["lines"][0]["operator"] == B


def test_failed_cancel_keeps_order_open_and_supply_unchanged(client: TestClient) -> None:
    seed_two_companies(client)
    order_id = new_order(client, [("101路", A, B)]).json()["id"]
    report(client, A, 2, [route("102路", "西站", "东站")])
    assert routes_by_code(client)["101路"]["frozen"] is True

    before_order = client.get(f"/transfer-orders/{order_id}").json()
    before_routes = client.get("/routes").json()

    response = client.post(f"/transfer-orders/{order_id}/cancel?inject_fault=true")
    assert response.status_code == 500

    assert client.get(f"/transfer-orders/{order_id}").json() == before_order
    assert client.get("/routes").json()["routes"] == before_routes["routes"]
    assert client.get("/transfer-orders/current").json()["order"]["id"] == order_id

    # A clean cancel afterwards still works.
    response = client.post(f"/transfer-orders/{order_id}/cancel")
    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_manual_commit_receipt_carries_committed_lines(
    env: tuple[Settings, TestClient],
) -> None:
    settings, client = env
    seed_two_companies(client)
    order_id = new_order(client, [("101路", A, B)]).json()["id"]
    report(client, A, 2, [route("102路", "西站", "东站")])
    assert client.get(f"/transfer-orders/{order_id}").json()["status"] == "open"

    # Build the incoming side on a separate connection WITHOUT the auto-commit
    # path of report_batch, leaving the order open yet fully ready.
    raw = connect(settings)
    try:
        with transaction(raw):
            at = service.now_iso()
            cursor = raw.execute(
                "INSERT INTO batches (company_code, batch_number, received_at, "
                "route_count, stop_count, payload_hash) VALUES (?, 2, ?, 2, 4, 'test')",
                (B, at),
            )
            batch_id = cursor.lastrowid
            raw.executemany(
                "INSERT INTO batch_stops (batch_id, route_code, seq, stop_code, stop_name) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (batch_id, "201路", 1, "S1", "公园"),
                    (batch_id, "201路", 2, "S2", "医院"),
                    (batch_id, "101路", 1, "S1", "新甲"),
                    (batch_id, "101路", 2, "S2", "新乙"),
                ],
            )
            service.recompute_serving(raw, at)
            assert service._order_is_ready(raw, order_id) is True

        body = service.commit_transfer_order(raw, order_id)
    finally:
        raw.close()

    # Receipt is built inside the committing transaction: header, per-line
    # conditions and serving party all describe the committed state.
    assert body["status"] == "committed"
    assert body["committed_at"] is not None
    assert body["ready"] is True
    line = body["lines"][0]
    assert line["operator"] == B
    assert line["serving_company"] == B
    assert line["old_withdrawn"] is True
    assert line["new_reported"] is True
    assert routes_by_code(client)["101路"]["operator"] == B

    # Repeated manual commit stays idempotent and coherent.
    raw = connect(settings)
    try:
        again = service.commit_transfer_order(raw, order_id)
    finally:
        raw.close()
    assert again["status"] == "committed"
    assert again["lines"][0]["serving_company"] == B
