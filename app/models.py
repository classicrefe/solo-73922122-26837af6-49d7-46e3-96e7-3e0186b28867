"""Pydantic request/response models.

Route codes are city-level identifiers and are the join key downstream, so they
are never rewritten by a transfer; only the operator attribution changes.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StopIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    stop_code: Optional[str] = Field(default=None, max_length=64)
    stop_name: str = Field(min_length=1, max_length=200)

    @field_validator("stop_code")
    @classmethod
    def _blank_stop_code_to_none(cls, value: Optional[str]) -> Optional[str]:
        return value or None


class RouteIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    route_code: str = Field(min_length=1, max_length=64)
    stops: list[StopIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _reject_blank_stops(self) -> "RouteIn":
        if any(not stop.stop_name for stop in self.stops):
            raise ValueError("stop_name must not be blank")
        return self


class BatchIn(BaseModel):
    """A company's complete current route/stop table (full snapshot)."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "batch_number": 2,
                    "routes": [
                        {
                            "route_code": "101路",
                            "stops": [
                                {"stop_code": "S001", "stop_name": "火车站"},
                                {"stop_code": "S002", "stop_name": "人民广场"},
                            ],
                        }
                    ],
                }
            ]
        },
    )

    batch_number: Optional[int] = Field(
        default=None,
        ge=1,
        description="按公司递增的批次号；省略时由服务端分配上一批次号 + 1。",
    )
    routes: list[RouteIn] = Field(
        default_factory=list,
        description="全量快照。未出现在此列表中的线路视为本公司已撤下。",
    )

    @model_validator(mode="after")
    def _reject_duplicate_routes(self) -> "BatchIn":
        seen: set[str] = set()
        for route in self.routes:
            if route.route_code in seen:
                raise ValueError(f"duplicate route_code in snapshot: {route.route_code}")
            seen.add(route.route_code)
        return self


class TransferLineIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, extra="forbid")

    route_code: str = Field(min_length=1, max_length=64)
    from_company: str = Field(min_length=1, max_length=64)
    to_company: str = Field(min_length=1, max_length=64)


class TransferOrderIn(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "lines": [
                        {
                            "route_code": "101路",
                            "from_company": "company_a",
                            "to_company": "company_b",
                        }
                    ]
                }
            ]
        },
    )

    lines: list[TransferLineIn] = Field(min_length=1)

    @model_validator(mode="after")
    def _reject_duplicate_routes(self) -> "TransferOrderIn":
        seen: set[str] = set()
        for line in self.lines:
            if line.route_code in seen:
                raise ValueError(f"duplicate route_code in transfer order: {line.route_code}")
            seen.add(line.route_code)
            if line.from_company == line.to_company:
                raise ValueError(f"route {line.route_code}: from_company and to_company must differ")
        return self


class StopOut(BaseModel):
    seq: int
    stop_code: Optional[str]
    stop_name: str


class RouteOut(BaseModel):
    route_code: str
    operator: str
    operator_name: str
    source_batch_id: int
    source_batch_number: int
    frozen: bool
    stops: list[StopOut]


class RoutesResponse(BaseModel):
    as_of: str
    routes: list[RouteOut]


class RouteDetailOut(BaseModel):
    route_code: str
    operator: str
    operator_name: str
    in_supply: bool
    frozen: bool
    source_batch_id: Optional[int] = None
    source_batch_number: Optional[int] = None
    first_reported_by: str
    first_reported_at: str
    stops: list[StopOut]


class BatchInfoOut(BaseModel):
    company_code: str
    batch_number: int
    received_at: str
    route_count: int
    stop_count: int
    pruned_stop_rows: int
    retained_stop_rows: int


class BatchListOut(BaseModel):
    company_code: str
    batches: list[BatchInfoOut]


class OrderSummaryOut(BaseModel):
    id: int
    status: str
    committed_at: Optional[str] = None


class BatchReportOut(BaseModel):
    company_code: str
    batch_number: int
    received_at: str
    route_count: int
    stop_count: int
    auto_committed_order: Optional[OrderSummaryOut] = None


class TransferLineOut(BaseModel):
    route_code: str
    from_company: str
    to_company: str
    operator: str
    serving_company: str
    frozen: bool
    frozen_batch_number: Optional[int] = None
    old_has_route: bool
    new_has_route: bool
    old_withdrawn: bool
    new_reported: bool
    ready: bool


class TransferOrderOut(BaseModel):
    id: int
    status: str
    created_at: str
    committed_at: Optional[str] = None
    ready: bool
    lines: list[TransferLineOut]


class CurrentOrderOut(BaseModel):
    order: Optional[TransferOrderOut]


class CompanyOut(BaseModel):
    code: str
    name: str


class CompaniesOut(BaseModel):
    companies: list[CompanyOut]
