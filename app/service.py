"""Core domain logic.

Invariants
----------
* A report is an immutable, complete snapshot for one company. Its batch number
  is strictly increasing per company.
* ``route_ownership`` changes only when a brand-new route is first seen (it
  belongs to the first reporter) or when an open transfer order commits. No
  later report can move a route back.
* ``route_serving`` is the single downstream read model. It is recomputed from
  ownership + the open transfer order after every write.
* While a transfer order is open, lines on the order are served by the outgoing
  company: its current (latest) snapshot if the route is still there, otherwise
  the newest snapshot that still contains the route (the pre-withdrawal detail).
  Lines outside the order refresh normally.
* A transfer order commits only when, for every line, the outgoing company's
  current snapshot no longer contains the route and the incoming company's
  current snapshot does. Attribution and stop order switch in one transaction.
* Non-latest batches keep only stop rows still used as a serving source; all
  other stop detail is pruned. Batch headers remain for audit.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Optional

import sqlite3

from .db import read_transaction, transaction
from .errors import ConflictError, InjectedFault, NotFoundError
from .models import BatchIn, TransferOrderIn

# Test hook: fired just before a create-order transaction commits, while the
# write lock is still held. Lets timing tests park the writer so another
# connection can observe the "write pending / receipt not returned" window.
create_order_precommit_hook = None

ROUTE_COLUMNS = """
    rs.route_code,
    rs.company_code AS operator,
    co.name AS operator_name,
    rs.source_batch_id,
    b.batch_number AS source_batch_number,
    rs.is_frozen,
    ro.first_company,
    ro.first_reported_at
"""

ROUTE_FROM = """
    FROM route_serving rs
    JOIN companies co ON co.code = rs.company_code
    JOIN batches b ON b.id = rs.source_batch_id
    JOIN route_ownership ro ON ro.route_code = rs.route_code
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# Small lookups
# ---------------------------------------------------------------------------


def _company_name(conn: sqlite3.Connection, code: str) -> str:
    row = conn.execute("SELECT name FROM companies WHERE code = ?", (code,)).fetchone()
    if row is None:
        raise NotFoundError(f"unknown company: {code}")
    return row["name"]


def _latest_batch(conn: sqlite3.Connection, company: str) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT id, batch_number FROM batches WHERE company_code = ? "
        "ORDER BY batch_number DESC LIMIT 1",
        (company,),
    ).fetchone()


def _batch_has_route(conn: sqlite3.Connection, batch_id: int, route_code: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM batch_stops WHERE batch_id = ? AND route_code = ? LIMIT 1",
            (batch_id, route_code),
        ).fetchone()
        is not None
    )


def _newest_batch_with_route(
    conn: sqlite3.Connection, company: str, route_code: str
) -> Optional[sqlite3.Row]:
    return conn.execute(
        """
        SELECT b.id, b.batch_number
        FROM batches b
        WHERE b.company_code = ?
          AND EXISTS (
              SELECT 1 FROM batch_stops s
              WHERE s.batch_id = b.id AND s.route_code = ?
          )
        ORDER BY b.batch_number DESC
        LIMIT 1
        """,
        (company, route_code),
    ).fetchone()


def _open_order(conn: sqlite3.Connection) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM transfer_orders WHERE status = 'open' LIMIT 1"
    ).fetchone()


def _order_row(conn: sqlite3.Connection, order_id: int) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM transfer_orders WHERE id = ?", (order_id,)
    ).fetchone()
    if row is None:
        raise NotFoundError(f"unknown transfer order: {order_id}")
    return row


def _order_lines(conn: sqlite3.Connection, order_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM transfer_lines WHERE order_id = ? ORDER BY route_code",
        (order_id,),
    ).fetchall()


# ---------------------------------------------------------------------------
# Serving recomputation (read model) and pruning
# ---------------------------------------------------------------------------


def recompute_serving(conn: sqlite3.Connection, at: str) -> None:
    open_order = _open_order(conn)
    lines: dict[str, sqlite3.Row] = {}
    if open_order is not None:
        lines = {row["route_code"]: row for row in _order_lines(conn, open_order["id"])}

    ownership_rows = conn.execute(
        "SELECT route_code, owner_company FROM route_ownership ORDER BY route_code"
    ).fetchall()

    seen: set[str] = set()
    for ownership in ownership_rows:
        route = ownership["route_code"]
        seen.add(route)
        line = lines.get(route)

        company: Optional[str] = None
        batch_id: Optional[int] = None
        frozen = 0

        if line is not None:
            old_company = line["from_company"]
            latest = _latest_batch(conn, old_company)
            if latest is not None and _batch_has_route(conn, latest["id"], route):
                company, batch_id = old_company, latest["id"]
            else:
                prior = _newest_batch_with_route(conn, old_company, route)
                if prior is not None:
                    company, batch_id, frozen = old_company, prior["id"], 1
        else:
            owner = ownership["owner_company"]
            latest = _latest_batch(conn, owner)
            if latest is not None and _batch_has_route(conn, latest["id"], route):
                company, batch_id = owner, latest["id"]

        if company is not None and batch_id is not None:
            conn.execute(
                """
                INSERT INTO route_serving
                    (route_code, company_code, source_batch_id, is_frozen, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(route_code) DO UPDATE SET
                    company_code = excluded.company_code,
                    source_batch_id = excluded.source_batch_id,
                    is_frozen = excluded.is_frozen,
                    updated_at = excluded.updated_at
                """,
                (route, company, batch_id, frozen, at),
            )
        else:
            conn.execute("DELETE FROM route_serving WHERE route_code = ?", (route,))

        if line is not None:
            conn.execute(
                "UPDATE transfer_lines SET frozen_batch_id = ? "
                "WHERE order_id = ? AND route_code = ?",
                (batch_id if frozen else None, line["order_id"], route),
            )

    # Defensive: never leave a serving row for a route without ownership.
    if seen:
        placeholders = ",".join("?" for _ in seen)
        conn.execute(
            f"DELETE FROM route_serving WHERE route_code NOT IN ({placeholders})",
            tuple(seen),
        )
    else:
        conn.execute("DELETE FROM route_serving")


def prune_old_batches(conn: sqlite3.Connection) -> int:
    """Drop stop detail from non-latest batches unless it still serves reads.

    ``recompute_serving`` runs first, so the only old rows kept are exactly the
    ones a frozen transfer line still reads from. Everything else in an older
    batch is detail no longer needed for supply; the batch header stays for
    audit, including ``pruned_stop_rows``.
    """
    total = 0
    companies = [
        row["code"] for row in conn.execute("SELECT code FROM companies ORDER BY code")
    ]
    for company in companies:
        latest = _latest_batch(conn, company)
        if latest is None:
            continue
        candidates = conn.execute(
            "SELECT id FROM batches WHERE company_code = ? AND id != ?",
            (company, latest["id"]),
        ).fetchall()
        for candidate in candidates:
            cursor = conn.execute(
                """
                DELETE FROM batch_stops
                WHERE batch_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM route_serving rs
                      WHERE rs.source_batch_id = batch_stops.batch_id
                        AND rs.route_code = batch_stops.route_code
                  )
                """,
                (candidate["id"],),
            )
            deleted = max(cursor.rowcount, 0)
            if deleted:
                conn.execute(
                    "UPDATE batches SET pruned_stop_rows = pruned_stop_rows + ? WHERE id = ?",
                    (deleted, candidate["id"]),
                )
                total += deleted
    return total


# ---------------------------------------------------------------------------
# Batch intake
# ---------------------------------------------------------------------------


def _payload_hash(payload: BatchIn) -> str:
    canonical = json.dumps(
        payload.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def report_batch(
    conn: sqlite3.Connection,
    company_code: str,
    payload: BatchIn,
    inject_fault: bool = False,
) -> dict[str, Any]:
    received_at = now_iso()
    with transaction(conn):
        _company_name(conn, company_code)
        latest = _latest_batch(conn, company_code)
        previous_number = latest["batch_number"] if latest else 0

        if payload.batch_number is None:
            batch_number = previous_number + 1
        elif payload.batch_number <= previous_number:
            raise ConflictError(
                f"batch_number must increase per company: "
                f"{payload.batch_number} <= last {previous_number}"
            )
        else:
            batch_number = payload.batch_number

        stop_count = sum(len(route.stops) for route in payload.routes)
        cursor = conn.execute(
            """
            INSERT INTO batches
                (company_code, batch_number, received_at, route_count,
                 stop_count, payload_hash)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                company_code,
                batch_number,
                received_at,
                len(payload.routes),
                stop_count,
                _payload_hash(payload),
            ),
        )
        batch_id = cursor.lastrowid
        conn.executemany(
            """
            INSERT INTO batch_stops
                (batch_id, route_code, seq, stop_code, stop_name)
            VALUES (?, ?, ?, ?, ?)
            """,
            [
                (
                    batch_id,
                    route.route_code,
                    seq,
                    stop.stop_code,
                    stop.stop_name,
                )
                for route in payload.routes
                for seq, stop in enumerate(route.stops, start=1)
            ],
        )

        for route in payload.routes:
            conn.execute(
                """
                INSERT OR IGNORE INTO route_ownership
                    (route_code, owner_company, first_company, first_batch_id,
                     first_reported_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    route.route_code,
                    company_code,
                    company_code,
                    batch_id,
                    received_at,
                    received_at,
                ),
            )

        recompute_serving(conn, received_at)

        auto_committed: Optional[dict[str, Any]] = None
        open_order = _open_order(conn)
        if open_order is not None and _order_is_ready(conn, open_order["id"]):
            order_id = open_order["id"]
            commit_order(conn, order_id)
            committed = _order_row(conn, order_id)
            auto_committed = {
                "id": order_id,
                "status": committed["status"],
                "committed_at": committed["committed_at"],
            }

        prune_old_batches(conn)

        if inject_fault:
            # Everything above is rolled back by the transaction context.
            raise InjectedFault()

        return {
            "company_code": company_code,
            "batch_number": batch_number,
            "received_at": received_at,
            "route_count": len(payload.routes),
            "stop_count": stop_count,
            "auto_committed_order": auto_committed,
        }


# ---------------------------------------------------------------------------
# Transfer orders
# ---------------------------------------------------------------------------


def _order_is_ready(conn: sqlite3.Connection, order_id: int) -> bool:
    lines = _order_lines(conn, order_id)
    if not lines:
        return False
    for line in lines:
        route = line["route_code"]
        old_latest = _latest_batch(conn, line["from_company"])
        old_has = old_latest is not None and _batch_has_route(
            conn, old_latest["id"], route
        )
        new_latest = _latest_batch(conn, line["to_company"])
        new_has = new_latest is not None and _batch_has_route(
            conn, new_latest["id"], route
        )
        if old_has or not new_has:
            return False
    return True


def commit_order(conn: sqlite3.Connection, order_id: int) -> None:
    """Switch attribution then stop order. Caller owns the transaction."""
    order = _order_row(conn, order_id)
    if order["status"] == "committed":
        # Auto-commit already switched this order; a repeated commit is a no-op.
        return
    if order["status"] != "open":
        raise ConflictError(f"transfer order {order_id} is {order['status']}, not open")
    if not _order_is_ready(conn, order_id):
        raise ConflictError(f"transfer order {order_id} is not ready to commit")

    at = now_iso()
    lines = _order_lines(conn, order_id)

    # Mark committed first so the serving recomputation no longer treats these
    # routes as in-flight.
    conn.execute(
        "UPDATE transfer_orders SET status = 'committed', committed_at = ? WHERE id = ?",
        (at, order_id),
    )
    for line in lines:
        cursor = conn.execute(
            "UPDATE route_ownership SET owner_company = ?, updated_at = ? "
            "WHERE route_code = ?",
            (line["to_company"], at, line["route_code"]),
        )
        if cursor.rowcount == 0:
            raise ConflictError(f"route {line['route_code']} lost its ownership record")
        conn.execute(
            "UPDATE transfer_lines SET frozen_batch_id = NULL "
            "WHERE order_id = ? AND route_code = ?",
            (order_id, line["route_code"]),
        )

    recompute_serving(conn, at)
    prune_old_batches(conn)


def create_transfer_order(
    conn: sqlite3.Connection,
    payload: TransferOrderIn,
    inject_fault: bool = False,
) -> dict[str, Any]:
    """Create the order and return its receipt, all materialised in the same
    transaction as the write. The receipt therefore reflects the state at the
    order's own commit, never a later auto-commit by a concurrent report."""
    created_at = now_iso()
    with transaction(conn):
        if _open_order(conn) is not None:
            raise ConflictError(
                "another transfer order is already open; finish it before creating a new one"
            )

        lines = payload.lines
        for line in lines:
            _company_name(conn, line.from_company)
            _company_name(conn, line.to_company)

            ownership = conn.execute(
                "SELECT owner_company FROM route_ownership WHERE route_code = ?",
                (line.route_code,),
            ).fetchone()
            if ownership is None:
                raise ConflictError(f"route {line.route_code} has never been reported")
            if ownership["owner_company"] != line.from_company:
                raise ConflictError(
                    f"route {line.route_code} is operated by "
                    f"{ownership['owner_company']}, not {line.from_company}"
                )
            serving = conn.execute(
                "SELECT company_code, is_frozen FROM route_serving WHERE route_code = ?",
                (line.route_code,),
            ).fetchone()
            if (
                serving is None
                or serving["company_code"] != line.from_company
                or serving["is_frozen"]
            ):
                raise ConflictError(
                    f"route {line.route_code} is not currently in supply by "
                    f"{line.from_company}"
                )

        cursor = conn.execute(
            "INSERT INTO transfer_orders (status, created_at) VALUES ('open', ?)",
            (created_at,),
        )
        order_id = cursor.lastrowid
        conn.executemany(
            """
            INSERT INTO transfer_lines (order_id, route_code, from_company, to_company)
            VALUES (?, ?, ?, ?)
            """,
            [
                (order_id, line.route_code, line.from_company, line.to_company)
                for line in lines
            ],
        )

        recompute_serving(conn, created_at)
        prune_old_batches(conn)

        if inject_fault:
            raise InjectedFault()

        receipt = _load_order(conn, order_id)
        if create_order_precommit_hook is not None:
            # The write is complete but not committed; the lock is still held.
            create_order_precommit_hook(order_id)
        return receipt


def cancel_transfer_order(
    conn: sqlite3.Connection,
    order_id: int,
    inject_fault: bool = False,
) -> dict[str, Any]:
    """Cancel inside one write transaction and return the receipt built from
    that same transaction's snapshot."""
    at = now_iso()
    with transaction(conn):
        order = _order_row(conn, order_id)
        if order["status"] != "open":
            raise ConflictError(f"transfer order {order_id} is {order['status']}, not open")
        conn.execute(
            "UPDATE transfer_orders SET status = 'cancelled', cancelled_at = ? WHERE id = ?",
            (at, order_id),
        )
        recompute_serving(conn, at)
        prune_old_batches(conn)
        if inject_fault:
            raise InjectedFault()
        return _load_order(conn, order_id)


def commit_transfer_order(
    conn: sqlite3.Connection,
    order_id: int,
    inject_fault: bool = False,
) -> dict[str, Any]:
    """Manual commit wrapper. The returned receipt comes from the committing
    transaction, so it can never report a state different from the write."""
    with transaction(conn):
        commit_order(conn, order_id)
        if inject_fault:
            raise InjectedFault()
        return _load_order(conn, order_id)


# ---------------------------------------------------------------------------
# Read models
# ---------------------------------------------------------------------------


def _stops(
    conn: sqlite3.Connection, batch_id: int, route_code: str
) -> list[dict[str, Any]]:
    return [
        {"seq": row["seq"], "stop_code": row["stop_code"], "stop_name": row["stop_name"]}
        for row in conn.execute(
            "SELECT seq, stop_code, stop_name FROM batch_stops "
            "WHERE batch_id = ? AND route_code = ? ORDER BY seq",
            (batch_id, route_code),
        )
    ]


def list_routes(conn: sqlite3.Connection) -> dict[str, Any]:
    # One snapshot for the whole response so every operator/stop pair comes
    # from the same committed state (a commit between two statements cannot
    # mix two supply states in a single response).
    with read_transaction(conn):
        rows = conn.execute(
            f"SELECT {ROUTE_COLUMNS} {ROUTE_FROM} ORDER BY rs.route_code"
        ).fetchall()
        routes = [
            {
                "route_code": row["route_code"],
                "operator": row["operator"],
                "operator_name": row["operator_name"],
                "source_batch_id": row["source_batch_id"],
                "source_batch_number": row["source_batch_number"],
                "frozen": bool(row["is_frozen"]),
                "stops": _stops(conn, row["source_batch_id"], row["route_code"]),
            }
            for row in rows
        ]
    return {"as_of": now_iso(), "routes": routes}


def get_route(conn: sqlite3.Connection, route_code: str) -> dict[str, Any]:
    with read_transaction(conn):
        ownership = conn.execute(
            "SELECT * FROM route_ownership WHERE route_code = ?", (route_code,)
        ).fetchone()
        if ownership is None:
            raise NotFoundError(f"unknown route: {route_code}")
        serving = conn.execute(
            f"SELECT {ROUTE_COLUMNS} {ROUTE_FROM} WHERE rs.route_code = ?", (route_code,)
        ).fetchone()

        if serving is None:
            return {
                "route_code": route_code,
                "operator": ownership["owner_company"],
                "operator_name": _company_name(conn, ownership["owner_company"]),
                "in_supply": False,
                "frozen": False,
                "source_batch_id": None,
                "source_batch_number": None,
                "first_reported_by": ownership["first_company"],
                "first_reported_at": ownership["first_reported_at"],
                "stops": [],
            }

        return {
            "route_code": route_code,
            "operator": serving["operator"],
            "operator_name": serving["operator_name"],
            "in_supply": True,
            "frozen": bool(serving["is_frozen"]),
            "source_batch_id": serving["source_batch_id"],
            "source_batch_number": serving["source_batch_number"],
            "first_reported_by": ownership["first_company"],
            "first_reported_at": ownership["first_reported_at"],
            "stops": _stops(conn, serving["source_batch_id"], route_code),
        }


def list_batches(conn: sqlite3.Connection, company_code: str) -> dict[str, Any]:
    with read_transaction(conn):
        _company_name(conn, company_code)
        rows = conn.execute(
            "SELECT * FROM batches WHERE company_code = ? ORDER BY batch_number",
            (company_code,),
        ).fetchall()
        batches = []
        for row in rows:
            retained = conn.execute(
                "SELECT COUNT(*) AS n FROM batch_stops WHERE batch_id = ?", (row["id"],)
            ).fetchone()["n"]
            batches.append(
                {
                    "company_code": row["company_code"],
                    "batch_number": row["batch_number"],
                    "received_at": row["received_at"],
                    "route_count": row["route_count"],
                    "stop_count": row["stop_count"],
                    "pruned_stop_rows": row["pruned_stop_rows"],
                    "retained_stop_rows": retained,
                }
            )
    return {"company_code": company_code, "batches": batches}


def _line_detail(conn: sqlite3.Connection, line: sqlite3.Row) -> dict[str, Any]:
    route = line["route_code"]
    ownership = conn.execute(
        "SELECT owner_company FROM route_ownership WHERE route_code = ?", (route,)
    ).fetchone()
    old_latest = _latest_batch(conn, line["from_company"])
    new_latest = _latest_batch(conn, line["to_company"])
    old_has = old_latest is not None and _batch_has_route(conn, old_latest["id"], route)
    new_has = new_latest is not None and _batch_has_route(conn, new_latest["id"], route)
    serving = conn.execute(
        "SELECT company_code, source_batch_id, is_frozen FROM route_serving "
        "WHERE route_code = ?",
        (route,),
    ).fetchone()
    frozen_batch_number = None
    if serving is not None and serving["is_frozen"]:
        frozen_batch = conn.execute(
            "SELECT batch_number FROM batches WHERE id = ?",
            (serving["source_batch_id"],),
        ).fetchone()
        frozen_batch_number = frozen_batch["batch_number"] if frozen_batch else None
    return {
        "route_code": route,
        "from_company": line["from_company"],
        "to_company": line["to_company"],
        "operator": ownership["owner_company"] if ownership else line["from_company"],
        "serving_company": serving["company_code"] if serving else line["from_company"],
        "frozen": bool(serving["is_frozen"]) if serving else False,
        "frozen_batch_number": frozen_batch_number,
        "old_has_route": old_has,
        "new_has_route": new_has,
        "old_withdrawn": not old_has,
        "new_reported": new_has,
        "ready": (not old_has) and new_has,
    }


def _load_order(conn: sqlite3.Connection, order_id: int) -> dict[str, Any]:
    """Assemble the full order detail from the current transaction's snapshot.

    Must be called inside an open transaction (a write transaction for
    receipts, :func:`read_transaction` for GET endpoints). All statements then
    see one consistent committed state.
    """
    order = _order_row(conn, order_id)
    lines = [_line_detail(conn, line) for line in _order_lines(conn, order_id)]
    return {
        "id": order["id"],
        "status": order["status"],
        "created_at": order["created_at"],
        "committed_at": order["committed_at"],
        "ready": all(line["ready"] for line in lines) if lines else False,
        "lines": lines,
    }


def get_order(conn: sqlite3.Connection, order_id: int) -> dict[str, Any]:
    with read_transaction(conn):
        return _load_order(conn, order_id)


def get_current_order(conn: sqlite3.Connection) -> Optional[dict[str, Any]]:
    with read_transaction(conn):
        order = _open_order(conn)
        if order is None:
            return None
        return _load_order(conn, order["id"])


def list_companies(conn: sqlite3.Connection) -> dict[str, Any]:
    with read_transaction(conn):
        return {
            "companies": [
                {"code": row["code"], "name": row["name"]}
                for row in conn.execute(
                    "SELECT code, name FROM companies ORDER BY code"
                ).fetchall()
            ]
        }
