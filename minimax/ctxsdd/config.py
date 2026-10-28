"""运行配置。

所有可配置项优先读环境变量，其次用默认值；默认值对应本机验证样例的布局。
真实凭据只从环境变量读取，仓库内不保存任何密钥。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

#: ``minimax/`` 目录（本模块的上一级）
ROOT = Path(__file__).resolve().parent.parent
#: 本模块所在仓库根（PowerContext 仓库根），用于定位 ``powercontext`` 源码
REPO_ROOT = ROOT.parent


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return default if value is None or value == "" else value


@dataclass(frozen=True)
class Settings:
    """一次运行所需的全部配置。"""

    # --- seekdb（工程资料库） ---
    seekdb_host: str = field(default_factory=lambda: _env("SEEKDB_HOST", "127.0.0.1"))
    seekdb_port: int = field(default_factory=lambda: int(_env("SEEKDB_PORT", "2881")))
    seekdb_user: str = field(default_factory=lambda: _env("SEEKDB_USER", "root"))
    seekdb_password: str = field(default_factory=lambda: _env("SEEKDB_PASSWORD", ""))
    seekdb_database: str = field(default_factory=lambda: _env("SEEKDB_DATABASE", "minimax_ctx"))

    # --- PowerContext（任务状态） ---
    state_db: Path = field(
        default_factory=lambda: Path(_env("CTXSDD_STATE_DB", str(ROOT / "var" / "task_state.db")))
    )

    # --- MiniMax ---
    minimax_api_key: str = field(default_factory=lambda: _env("MINIMAX_API_KEY", ""))
    minimax_base_url: str = field(
        default_factory=lambda: _env("MINIMAX_BASE_URL", "https://api.minimaxi.com/v1")
    )
    minimax_model: str = field(
        default_factory=lambda: _env("MINIMAX_MODEL", "MiniMax-M3.1-Flash")
    )

    # --- 业务工程与规格 ---
    project_dir: Path = field(
        default_factory=lambda: Path(_env("CTXSDD_PROJECT", str(ROOT / "demo_project")))
    )
    specs_dir: Path = field(
        default_factory=lambda: Path(_env("CTXSDD_SPECS", str(ROOT / "specs")))
    )
    records_dir: Path = field(
        default_factory=lambda: Path(_env("CTXSDD_RECORDS", str(ROOT / "records")))
    )

    @property
    def sqlite_url(self) -> str:
        """PowerContext 使用的 SQLite URL（绝对路径，保证重启后可复用）。"""

        return "sqlite+aiosqlite:///" + self.state_db.resolve().as_posix()

    def require_api_key(self) -> str:
        if not self.minimax_api_key:
            raise SystemExit(
                "缺少 MINIMAX_API_KEY。请先设置环境变量后再运行本命令：\n"
                "  PowerShell: $env:MINIMAX_API_KEY='sk-...'\n"
                "  bash:       export MINIMAX_API_KEY='sk-...'"
            )
        return self.minimax_api_key
