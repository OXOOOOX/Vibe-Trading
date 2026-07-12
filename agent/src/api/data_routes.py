"""HTTP routes for the unified data layer.

The existing ``/market-cache/*`` routes intentionally remain untouched for
backwards compatibility. New callers should use these policy-aware endpoints.
"""

from __future__ import annotations

import asyncio
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from src.data_layer import get_unified_data_service
from src.data_layer.prewarm import get_data_prewarm_scheduler


class DataContextRequest(BaseModel):
    symbols: list[str] = Field(min_length=1, max_length=25)
    purpose: Literal["latest_price", "holding", "premarket", "intraday", "long_term", "backtest"] = "holding"
    lookback_days: int | None = Field(default=None, ge=1, le=10_000)
    include: list[Literal["market", "fundamentals", "news", "reports"]] | None = None
    force_live: bool | None = None


class WatchlistRequest(BaseModel):
    symbol: str = Field(min_length=1, max_length=32)
    note: str | None = Field(default=None, max_length=300)


class PrewarmRequest(BaseModel):
    phase: Literal["premarket", "intraday"] = "premarket"


def register_data_routes(app: FastAPI, require_local_or_auth) -> None:
    """Mount authenticated unified-data routes on the server application."""

    @app.post("/data/context", dependencies=[Depends(require_local_or_auth)])
    async def get_data_context(payload: DataContextRequest):
        service = get_unified_data_service()
        return await asyncio.to_thread(
            service.get_context,
            symbols=payload.symbols,
            purpose=payload.purpose,
            lookback_days=payload.lookback_days,
            include=payload.include,
            force_live=payload.force_live,
        )

    @app.get("/data/bars", dependencies=[Depends(require_local_or_auth)])
    async def get_data_bars(handle: str = Query(..., min_length=8), cursor: int = Query(0, ge=0)):
        try:
            return await asyncio.to_thread(get_unified_data_service().read_bars, handle, cursor)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/data/coverage", dependencies=[Depends(require_local_or_auth)])
    async def get_data_coverage():
        return await asyncio.to_thread(get_unified_data_service().coverage)

    @app.get("/data/sources", dependencies=[Depends(require_local_or_auth)])
    async def get_data_sources():
        return await asyncio.to_thread(get_unified_data_service().sources)

    @app.get("/data/storage", dependencies=[Depends(require_local_or_auth)])
    async def get_data_storage():
        return await asyncio.to_thread(get_unified_data_service().storage)

    @app.get("/data/watchlist", dependencies=[Depends(require_local_or_auth)])
    async def get_data_watchlist():
        return {"status": "ok", "watchlist": await asyncio.to_thread(get_unified_data_service().control.list_watchlist)}

    @app.post("/data/watchlist", dependencies=[Depends(require_local_or_auth)])
    async def add_data_watchlist(payload: WatchlistRequest):
        entry = await asyncio.to_thread(get_unified_data_service().control.add_watchlist, payload.symbol, payload.note)
        return {"status": "ok", "entry": entry}

    @app.delete("/data/watchlist/{symbol}", dependencies=[Depends(require_local_or_auth)])
    async def delete_data_watchlist(symbol: str):
        deleted = await asyncio.to_thread(get_unified_data_service().control.remove_watchlist, symbol)
        if not deleted:
            raise HTTPException(status_code=404, detail="watchlist symbol not found")
        return {"status": "ok", "deleted": symbol.upper()}

    @app.get("/data/requests/{request_id}", dependencies=[Depends(require_local_or_auth)])
    async def get_data_request(request_id: str):
        result = await asyncio.to_thread(get_unified_data_service().control.get_request, request_id)
        if result is None:
            raise HTTPException(status_code=404, detail="data request not found")
        return result

    @app.post("/data/prewarm", dependencies=[Depends(require_local_or_auth)])
    async def prewarm_data(payload: PrewarmRequest):
        try:
            return await asyncio.to_thread(get_unified_data_service().prewarm, phase=payload.phase)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/data/prewarm/status", dependencies=[Depends(require_local_or_auth)])
    async def get_data_prewarm_status():
        return get_data_prewarm_scheduler().status()

    @app.on_event("startup")
    async def start_data_prewarm_scheduler() -> None:
        await get_data_prewarm_scheduler().start()

    @app.on_event("shutdown")
    async def stop_data_prewarm_scheduler() -> None:
        await get_data_prewarm_scheduler().stop()
