import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    username: str
    password: str
    ip: str
    port: int
    http_host: str = "0.0.0.0"
    http_port: int = 3021

    @classmethod
    def from_env(cls) -> "Config":
        return cls(
            username=os.environ.get("AMAZINGDATA_USERNAME", ""),
            password=os.environ.get("AMAZINGDATA_PASSWORD", ""),
            ip=os.environ.get("AMAZINGDATA_IP", ""),
            port=int(os.environ.get("AMAZINGDATA_PORT", "0") or "0"),
            http_host=os.environ.get("HTTP_HOST", "0.0.0.0") or "0.0.0.0",
            http_port=int(os.environ.get("HTTP_PORT", "3021") or "3021"),
        )

    def is_configured(self) -> bool:
        return bool(self.username and self.password and self.ip and self.port)
