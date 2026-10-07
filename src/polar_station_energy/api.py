"""能源承诺与负荷处置的 HTTP/JSON 边界，复用基础服务的路由风格。"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from polar_station_foundation.api import route as foundation_route
from polar_station_foundation.errors import DomainError, ValidationError
from polar_station_foundation.storage import Database

from .service import EnergyService


def route(service: EnergyService, method: str, path: str, body: dict[str, Any] | None,
          headers: dict[str, str] | None = None) -> tuple[int, dict[str, Any]]:
    """分派能源域请求；非能源路径回退到基础服务路由。"""

    headers = headers or {}
    body = body or {}
    parsed = urlparse(path)
    if not parsed.path.startswith("/energy/"):
        return foundation_route(service, method, path, body, headers)
    actor_id = headers.get("X-Actor-Id", "")
    query = parse_qs(parsed.query)
    try:
        if method == "POST" and parsed.path == "/energy/generators":
            return _receipt(service.register_generator(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/batteries":
            return _receipt(service.register_battery(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/wind-turbines":
            return _receipt(service.register_wind_turbine(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/fuel-batches":
            return _receipt(service.register_fuel_batch(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/fuel-batches/delay":
            return _receipt(service.delay_fuel_batch(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/circuits":
            return _receipt(service.register_circuit(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/experiment-windows":
            return _receipt(service.approve_experiment_window(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/forecasts":
            return _receipt(service.set_forecast(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/failures":
            return _receipt(service.report_failure(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/failures/clear":
            return _receipt(service.clear_failure(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/plans":
            return _receipt(service.create_plan(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/plans/confirm":
            return _receipt(service.confirm_plan(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/resume":
            return _receipt(service.resume_site(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/overrides":
            return _receipt(service.create_override(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/overrides/confirm":
            return _receipt(service.confirm_override(actor_id=actor_id, **body))
        if method == "POST" and parsed.path == "/energy/telemetry":
            return _receipt(service.ingest_telemetry(actor_id=actor_id, **body))
        if method == "GET" and parsed.path == "/energy/plans":
            site_id = _query(query, "site_id")
            return 200, {"items": service.list_plans(site_id)}
        if method == "GET" and parsed.path == "/energy/plan":
            return 200, service.get_plan(_query(query, "plan_id"))
        if method == "GET" and parsed.path == "/energy/plan-directives":
            plan_id = _query(query, "plan_id")
            slot = query.get("slot", [None])[0]
            return 200, {"items": service.plan_directives(plan_id, int(slot) if slot else None)}
        if method == "GET" and parsed.path == "/energy/settlements":
            return 200, {"items": service.settlements(_query(query, "site_id"))}
        if method == "GET" and parsed.path == "/energy/curtailment":
            return 200, service.curtailment_status(_query(query, "site_id"))
        if method == "GET" and parsed.path == "/energy/overview":
            return 200, service.site_overview(_query(query, "site_id"))
        if method == "GET" and parsed.path == "/energy/reconcile":
            site_id = _query(query, "site_id")
            tolerance = float(query.get("tolerance_kwh", ["0.05"])[0])
            return 200, service.reconcile(site_id, tolerance)
        return 404, {"error": "route_not_found", "message": "接口不存在"}
    except DomainError as exc:
        return exc.status, {"error": exc.code, "message": str(exc)}
    except (TypeError, ValueError) as exc:
        return 400, {"error": "invalid_request", "message": str(exc)}


def _receipt(receipt) -> tuple[int, dict[str, Any]]:
    return (200 if receipt.replayed else 201), receipt.__dict__


def _query(query: dict[str, list[str]], name: str) -> str:
    value = query.get(name, [""])[0]
    if not value:
        raise ValidationError(f"{name} 不能为空")
    return value


class Handler(BaseHTTPRequestHandler):
    """把标准库 HTTP 请求转换为能源路由调用。"""

    service: EnergyService

    def _handle(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write(400, {"error": "invalid_json", "message": "请求体必须是 UTF-8 JSON"})
            return
        status, payload = route(self.service, self.command, self.path, body,
                                {"X-Actor-Id": self.headers.get("X-Actor-Id", "")})
        self._write(status, payload)

    def _write(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:
        self._handle()

    def do_POST(self) -> None:
        self._handle()

    def log_message(self, format: str, *args: object) -> None:
        return


def main() -> int:
    """启动站内能源承诺与负荷处置 HTTP 服务。"""

    parser = argparse.ArgumentParser(description="启动站内能源承诺与负荷处置服务")
    parser.add_argument("--database", default="energy.sqlite3")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8081)
    args = parser.parse_args()
    database = Database(args.database)
    Handler.service = EnergyService(database)
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        database.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
