"""配置读写。auth-key 存在用户目录，不进工作区、不进版本库。"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "gzh" / "config.json"
DEFAULT_KB_ROOT = "01_内容生产HQ/公众号素材库"


@dataclass
class Config:
    """gzh 的运行配置。"""

    auth_key: str = ""
    kb_root: str = DEFAULT_KB_ROOT
    api_base: str = "https://down.mptext.top"
    timeout: int = 30
    tags: list[str] = field(default_factory=list)
    path: Path = field(default=DEFAULT_CONFIG_PATH, repr=False, compare=False)

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        target = Path(path).expanduser() if path else DEFAULT_CONFIG_PATH
        cfg = cls(path=target)

        if target.exists():
            try:
                data = json.loads(target.read_text(encoding="utf-8"))
                cfg.auth_key = data.get("auth_key", "") or ""
                cfg.kb_root = data.get("kb_root") or DEFAULT_KB_ROOT
                cfg.api_base = data.get("api_base") or "https://down.mptext.top"
                cfg.timeout = int(data.get("timeout") or 30)
                cfg.tags = list(data.get("tags") or [])
            except (json.JSONDecodeError, OSError, ValueError):
                pass

        # 环境变量优先级最高，便于临时覆盖
        if env_key := os.environ.get("GZH_AUTH_KEY"):
            cfg.auth_key = env_key.strip()
        if env_kb := os.environ.get("GZH_KB_ROOT"):
            cfg.kb_root = env_kb

        return cfg

    def save(self) -> Path:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "auth_key": self.auth_key,
            "kb_root": self.kb_root,
            "api_base": self.api_base,
            "timeout": self.timeout,
            "tags": self.tags,
        }
        self.path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        # auth-key 属于凭证，收紧权限
        try:
            self.path.chmod(0o600)
        except OSError:
            pass
        return self.path

    def masked(self) -> dict:
        """用于展示，绝不回显完整 auth-key。"""
        key = self.auth_key
        shown = f"{key[:4]}…{key[-4:]}" if len(key) > 10 else ("(已设置)" if key else "")
        return {
            "auth_key": shown,
            "auth_key_configured": bool(key),
            "kb_root": self.kb_root,
            "api_base": self.api_base,
            "timeout": self.timeout,
            "tags": self.tags,
            "config_path": str(self.path),
        }

    def resolve_kb_root(self, cwd: str | Path | None = None) -> Path:
        """把知识库目录解析成绝对路径（相对路径按当前工作区解析）。"""
        p = Path(self.kb_root).expanduser()
        if p.is_absolute():
            return p
        return (Path(cwd) if cwd else Path.cwd()) / p
