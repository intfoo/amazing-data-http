from app.gateway import Gateway


class HealthService:
    def __init__(self, config, gateway: Gateway):
        self._config = config
        self._gw = gateway

    def status(self) -> dict:
        ready = self._config.is_configured() and self._gw.is_ready()
        return {
            "status": "ok" if ready else "degraded",
            "sdk": "ready" if self._gw.is_ready() else "not_ready",
            "config": "complete" if self._config.is_configured() else "incomplete",
        }

    def is_ok(self) -> bool:
        return self._config.is_configured() and self._gw.is_ready()
