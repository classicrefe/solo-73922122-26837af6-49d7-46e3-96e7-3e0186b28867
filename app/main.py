"""FastAPI entry point."""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from typing import Iterator, Optional

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from . import service
from .config import Settings
from .db import connect, init_db
from .errors import AppError, InjectedFault
from .models import (
    BatchIn,
    BatchListOut,
    BatchReportOut,
    CompaniesOut,
    CurrentOrderOut,
    RouteDetailOut,
    RoutesResponse,
    TransferOrderIn,
    TransferOrderOut,
)

DESCRIPTION = (
    "两家公交公司各自以全量快照上报线路停站表，线路号保持市级编号不变。\n\n"
    "**换线单** 一次只能有一张处于办理中；办理期间单内线路冻结在旧方最近一次有效停站明细，"
    "单外线路照常刷新；双方在现行快照中撤下交出线路并报齐接手线路后，"
    "归属与停站顺序在同一事务中切换。任何写入失败都会整体回滚。\n\n"
    "演示时可在请求上附加 `?inject_fault=true`（需 `ENABLE_FAULT_INJECTION=true`）验证回滚。"
)


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    resolved = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_db(resolved)
        yield

    app = FastAPI(
        title="公交线路互换与客流数仓",
        version="1.0.0",
        description=DESCRIPTION,
        lifespan=lifespan,
    )

    @app.exception_handler(AppError)
    async def _app_error_handler(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(InjectedFault)
    async def _fault_handler(request: Request, exc: InjectedFault) -> JSONResponse:
        return JSONResponse(
            status_code=500,
            content={"detail": "注入的写入故障，本次事务已全部回滚"},
        )

    def get_conn() -> Iterator[sqlite3.Connection]:
        conn = connect(resolved)
        try:
            yield conn
        finally:
            conn.close()

    def fault(request: Request) -> bool:
        if not resolved.enable_fault_injection:
            return False
        return request.query_params.get("inject_fault", "").lower() in {
            "1",
            "true",
            "yes",
            "on",
        }

    @app.get("/health", tags=["system"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/companies", response_model=CompaniesOut, tags=["companies"])
    def companies(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return service.list_companies(conn)

    @app.post(
        "/companies/{company_code}/batches",
        response_model=BatchReportOut,
        status_code=201,
        tags=["companies"],
        summary="上报本公司全量线路停站表",
    )
    def report_batch(
        company_code: str,
        payload: BatchIn,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        """批次号按公司严格递增；省略时服务端自动分配。

        未出现在 `routes` 中的线路视为本公司在本批次撤下。
        若该批上报使办理中的换线单满足条件，会在同一次事务内自动提交。
        """
        return service.report_batch(
            conn, company_code, payload, inject_fault=fault(request)
        )

    @app.get(
        "/companies/{company_code}/batches",
        response_model=BatchListOut,
        tags=["companies"],
        summary="查看某公司的批次及停站明细保留情况",
    )
    def list_batches(
        company_code: str, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        return service.list_batches(conn, company_code)

    @app.get(
        "/routes",
        response_model=RoutesResponse,
        tags=["downstream"],
        summary="下游取数：各线路运营方与停站顺序",
    )
    def routes(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        """返回当前在供的全部线路。办理中单据内的线路会以 `frozen=true` 标记。"""
        return service.list_routes(conn)

    @app.get(
        "/routes/{route_code}",
        response_model=RouteDetailOut,
        tags=["downstream"],
        summary="下游取数：单条线路",
    )
    def route(route_code: str, conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return service.get_route(conn, route_code)

    @app.post(
        "/transfer-orders",
        response_model=TransferOrderOut,
        status_code=201,
        tags=["transfer"],
        summary="新建换线单（一次仅一张办理中）",
    )
    def create_transfer_order(
        payload: TransferOrderIn,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        # The receipt is built inside the create transaction, so it always
        # reflects this write's own committed state.
        return service.create_transfer_order(
            conn, payload, inject_fault=fault(request)
        )

    @app.get(
        "/transfer-orders/current",
        response_model=CurrentOrderOut,
        tags=["transfer"],
        summary="查看当前办理中的换线单",
    )
    def current_order(conn: sqlite3.Connection = Depends(get_conn)) -> dict:
        return {"order": service.get_current_order(conn)}

    @app.get(
        "/transfer-orders/{order_id}",
        response_model=TransferOrderOut,
        tags=["transfer"],
        summary="查看换线单详情",
    )
    def get_transfer_order(
        order_id: int, conn: sqlite3.Connection = Depends(get_conn)
    ) -> dict:
        return service.get_order(conn, order_id)

    @app.post(
        "/transfer-orders/{order_id}/commit",
        response_model=TransferOrderOut,
        tags=["transfer"],
        summary="手动提交（条件未满足时拒绝；已提交则幂等返回）",
    )
    def commit_transfer_order(
        order_id: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        return service.commit_transfer_order(
            conn, order_id, inject_fault=fault(request)
        )

    @app.post(
        "/transfer-orders/{order_id}/cancel",
        response_model=TransferOrderOut,
        tags=["transfer"],
        summary="取消换线单，解除单内冻结、按现行快照取数",
    )
    def cancel_transfer_order(
        order_id: int,
        request: Request,
        conn: sqlite3.Connection = Depends(get_conn),
    ) -> dict:
        return service.cancel_transfer_order(
            conn, order_id, inject_fault=fault(request)
        )

    return app


app = create_app()
