"""Public read-only MU API."""

import asyncio
from contextlib import asynccontextmanager
import json
import logging
import ipaddress
import traceback
from datetime import datetime, timezone
import os
import time
from concurrent.futures import ThreadPoolExecutor
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException
from . import Grump, MODEL
from .coordinates import calculate
from .tables import ensure_tables, max_level

BODY_LIMIT = int(os.environ.get("MU_BODY_LIMIT", 32768))
BULK_LIMIT = int(os.environ.get("MU_BULK_LIMIT", 50))
TIME_BUDGET = float(os.environ.get("MU_TIME_BUDGET", 20))
TRUSTED_PROXY_HEADERS = os.environ.get("MU_TRUSTED_PROXY_HEADERS", "0").lower() in ("1", "true")
SELF_TEST = ["52.324297,10.182664", "52.244048,10.167482", "52.295984,10.231064"]
# Serialize calculations: one bounded worker owns the density cache.
executor = ThreadPoolExecutor(max_workers=1)


@asynccontextmanager
async def lifespan(app):
    app.state.table_max_level = max_level()
    app.state.grump = Grump()
    app.state.grump.density_table = await asyncio.to_thread(
        ensure_tables, app.state.grump.folder
    )
    try:
        self_test_ok = bool(app.state.grump.r) and calculate(app.state.grump, SELF_TEST)["mu"] == 3046
    except Exception:
        self_test_ok = False
    if not self_test_ok:
        raise RuntimeError(
            "Population data startup self-test failed."
        )
    yield


app = FastAPI(title="MU Calculator", lifespan=lifespan)


class BodyTooLarge(Exception):
    pass


class BodyLimit:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        size = 0

        async def limited_receive():
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > BODY_LIMIT:
                raise BodyTooLarge()
            return message

        await self.app(scope, limited_receive, send)


app.add_middleware(BodyLimit)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


def error(message, status=400, **kwargs):
    return JSONResponse(
        {"error": message, "model": MODEL}, status_code=status, **kwargs
    )


@app.exception_handler(HTTPException)
async def http_error(request, exception):
    return error(str(exception.detail), exception.status_code)


@app.exception_handler(BodyTooLarge)
async def too_large(request, exception):
    request.scope["mu_error"] = "too_large"
    return error("Request body exceeds limit.", 413)


@app.exception_handler(Exception)
async def unexpected(request, exception):
    # Frame locations only: exception messages, chained exceptions and source
    # lines can contain input. Never pass the original exception to a logger.
    frames = traceback.extract_tb(exception.__traceback__)
    logging.getLogger("uvicorn.error").error(
        "Request failed: internal\nTraceback (frame locations only):\n%s",
        "\n".join(f"  {frame.filename}:{frame.lineno} in {frame.name}" for frame in frames),
    )
    # The request logger catches outside CORS, so add the header here.
    return error("Calculation failed.", 500, headers={"Access-Control-Allow-Origin": "*"})


def reject_constant(name):
    raise ValueError(name)


async def payload(request):
    try:
        value = json.loads(await request.body(), parse_constant=reject_constant)
    except (ValueError, RecursionError):
        request.scope["mu_error"] = "malformed_json"
        raise ValueError("Malformed JSON.") from None
    if not isinstance(value, dict):
        request.scope["mu_error"] = "invalid_payload"
        raise ValueError("Expected a JSON object.")
    return value


def detail_flag(value):
    if isinstance(value, str):
        value = value.lower()
        if value in ("true", "false"):
            return value == "true"
    if value in (False, None, "0", 0):
        return False
    if value in (True, "1", 1):
        return True
    raise ValueError("detail must be true, false, 1 or 0.")


def result(grump, coordinates, detail):
    response = calculate(grump, coordinates)
    if not detail:
        response = {key: response[key] for key in ("mu", "area_km2", "coordinates")}
    return dict(response, model=MODEL)


async def within_deadline(function, deadline):
    def guarded():
        if time.monotonic() >= deadline:
            raise TimeoutError()
        return function()

    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError()
    return await asyncio.wait_for(
        asyncio.get_running_loop().run_in_executor(executor, guarded), remaining
    )


@app.get("/")
def root():
    return {
        "description": "Independent empirical Ingress MU calculator.",
        "docs": "https://mu.investigatexm.com",
        "api_docs": "/docs",
        "table_max_level": app.state.table_max_level,
        "model": MODEL,
    }


@app.get("/health")
def health():
    return {"ok": True, "rasters": len(app.state.grump.r), "model": MODEL,
            "table_max_level": app.state.table_max_level,
            "table_levels_ready": sorted(app.state.grump.density_table)}


@app.api_route("/getMU", methods=["GET", "POST"])
async def get_mu(request: Request):
    deadline = time.monotonic() + TIME_BUDGET
    try:
        body = (
            await payload(request)
            if request.method == "POST"
            else {"coordinates": request.query_params.getlist("c")}
        )
        detail = detail_flag(
            request.query_params.get("detail", body.get("detail", False))
        )
        return await within_deadline(
            lambda: result(app.state.grump, body.get("coordinates"), detail), deadline
        )
    except ValueError as exception:
        request.scope.setdefault("mu_error", "invalid_coordinates")
        return error(str(exception))
    except TimeoutError:
        request.scope["mu_error"] = "deadline"
        return error("deadline exceeded", 504)


@app.post("/getMUbulk")
async def get_bulk(request: Request):
    deadline = time.monotonic() + TIME_BUDGET
    try:
        body = await payload(request)
        fields = body.get("fields")
        if isinstance(fields, list):
            request.scope["mu_fields"] = len(fields)
        if not isinstance(fields, list) or not 1 <= len(fields) <= BULK_LIMIT:
            request.scope["mu_error"] = "invalid_payload"
            raise ValueError(f"fields must be an array of 1 to {BULK_LIMIT} triangles.")
        detail = detail_flag(
            request.query_params.get("detail", body.get("detail", False))
        )
    except ValueError as exception:
        request.scope.setdefault("mu_error", "invalid_coordinates")
        return error(str(exception))
    results = []
    for index, field in enumerate(fields):
        item = {"index": index}
        if isinstance(field, dict) and "id" in field:
            item["id"] = field["id"]
        coordinates = field.get("coordinates") if isinstance(field, dict) else field
        try:
            calculated = await within_deadline(
                lambda: result(app.state.grump, coordinates, detail), deadline
            )
            calculated.pop("model")
            item.update(ok=True, **calculated)
        except ValueError as exception:
            request.scope["mu_error"] = "invalid_coordinates"
            item.update(ok=False, error=str(exception))
        except TimeoutError:
            request.scope["mu_error"] = "deadline"
            item.update(ok=False, error="deadline exceeded")
        results.append(item)
    return {"count": len(results), "results": results, "model": MODEL}


def clean_header(value):
    return "".join(c for c in value if c.isprintable())[:256]


class RequestLog:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        start = time.monotonic()
        status, size, started = 500, 0, False
        request = Request(scope)
        peer = scope.get("client")
        client = peer[0] if peer else "unknown"
        if TRUSTED_PROXY_HEADERS:
            candidate = request.headers.get("cf-connecting-ip")
            if candidate is None:
                candidate = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
            try:
                client = str(ipaddress.ip_address(candidate))
            except ValueError:
                pass

        async def logged_send(message):
            nonlocal status, size, started
            if message["type"] == "http.response.start":
                status = message["status"]
                started = True
            elif message["type"] == "http.response.body":
                size += len(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, logged_send)
        except Exception as exception:
            scope["mu_error"] = "internal"
            response = await unexpected(request, exception)
            if not started:
                await response(scope, receive, logged_send)
        finally:
            category = scope.get("mu_error")
            if category is None and status >= 400:
                category = {404: "not_found", 413: "too_large", 422: "validation", 504: "deadline"}.get(status, "http_error")
            # Route templates prevent arbitrary unknown paths from logging input.
            route = scope.get("route")
            record = dict(
                timestamp=datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
                client=clean_header(client), method=clean_header(scope["method"]),
                path=getattr(route, "path", "/<unmatched>"), status=status,
                duration_ms=round((time.monotonic() - start) * 1000, 3),
                response_bytes=size,
                user_agent=clean_header(request.headers.get("user-agent", "")),
            )
            if "mu_fields" in scope:
                record["fields"] = scope["mu_fields"]
            if category:
                record["error"] = category
            logging.getLogger("uvicorn.error").info(json.dumps(record, ensure_ascii=True))


app.add_middleware(RequestLog)
