from typing import Optional
"""拍卖数据采集管理平台 — FastAPI 后端入口"""

import argparse
import logging
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from .config import WebConfig
from .routers import dashboard, jobs, batches, items, platforms, queues, reports, scheduler, tasks, models
from .services import scheduler_service as _sched_svc
from .services import ai_queue_auto as _ai_auto

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """应用生命周期：启动/关闭定时调度器"""
    global _config
    # 启动时自动启动调度器
    if _config:
        try:
            result = _sched_svc.start(_config)
            logger.info(f"调度器启动结果: {result}")
        except Exception as e:
            logger.warning(f"调度器启动失败(不影响其他功能): {e}")
        try:
            _ai_auto.start(_config)
        except Exception as e:
            logger.warning(f"AI 队列自动处理启动失败(不影响其他功能): {e}")
    yield
    # 关闭时停止调度器
    try:
        _sched_svc.stop()
        logger.info("调度器已停止")
    except Exception:
        pass


app = FastAPI(
    title="拍卖数据采集管理平台",
    description="京东资产拍卖多平台数据采集系统的 Web 管理面板",
    version="1.1.0",
    lifespan=_lifespan,
)

# CORS（开发环境允许前端跨域）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 全局配置实例
_config: Optional[WebConfig] = None


def _register_routes(config: WebConfig) -> None:
    """注册所有 API 路由"""
    dashboard.init(config)
    jobs.init(config)
    batches.init(config)
    items.init(config)
    platforms.init(config)
    queues.init(config)
    reports.init(config)
    scheduler.init(config)
    models.init(config)
    app.include_router(dashboard.router)
    app.include_router(jobs.router)
    app.include_router(batches.router)
    app.include_router(items.router)
    app.include_router(platforms.router)
    app.include_router(queues.router)
    app.include_router(reports.router)
    app.include_router(scheduler.router)
    app.include_router(tasks.router)
    app.include_router(models.router)


def _setup_static() -> None:
    """挂载静态文件服务（SPA 前端）"""
    from fastapi.responses import HTMLResponse, JSONResponse
    static_dir = Path(__file__).parent / "static"
    if static_dir.exists() and (static_dir / "index.html").exists():
        from fastapi.staticfiles import StaticFiles
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        @app.get("/")
        async def serve_index():
            html_path = static_dir / "index.html"
            return HTMLResponse(html_path.read_text(encoding="utf-8"))

        @app.exception_handler(404)
        async def spa_fallback(request, exc):
            # API 请求返回 JSON 而非 HTML
            if request.url.path.startswith("/api/"):
                return JSONResponse(
                    {"error": f"API endpoint not found: {request.url.path}", "detail": str(exc)},
                    status_code=404,
                )
            html_path = static_dir / "index.html"
            if html_path.exists():
                return HTMLResponse(html_path.read_text(encoding="utf-8"))
            return JSONResponse({"error": "Not Found"}, status_code=404)

        @app.exception_handler(Exception)
        async def global_exception_handler(request, exc):
            logger.error(f"未处理的异常 {request.url.path}: {exc}", exc_info=True)
            if request.url.path.startswith("/api/"):
                return JSONResponse(
                    {"error": "Internal Server Error", "detail": str(exc)},
                    status_code=500,
                )
            html_path = static_dir / "index.html"
            if html_path.exists():
                return HTMLResponse(html_path.read_text(encoding="utf-8"))
            return JSONResponse({"error": "Internal Server Error"}, status_code=500)
        return
    @app.get("/")
    async def root():
        return {"message": "API 运行中", "docs": "/docs"}


# 模块导入时自动初始化
_default_config = WebConfig()
_config = _default_config
_register_routes(_default_config)
_setup_static()


def create_app(config: Optional[WebConfig] = None) -> FastAPI:
    """创建并配置应用（已自动初始化，此函数可覆盖配置）"""
    global _config
    if config is not None:
        _config = config
        dashboard.init(config)
        jobs.init(config)
        batches.init(config)
        items.init(config)
        platforms.init(config)
        queues.init(config)
        reports.init(config)
        scheduler.init(config)
        models.init(config)
    return app


def main():
    parser = argparse.ArgumentParser(description="拍卖数据采集管理平台")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8000, help="监听端口")
    parser.add_argument("--mysql-host", default="127.0.0.1", help="MySQL 地址")
    parser.add_argument("--mysql-port", type=int, default=3306, help="MySQL 端口")
    parser.add_argument("--mysql-user", default="root", help="MySQL 用户")
    parser.add_argument("--mysql-password", default="root", help="MySQL 密码")
    parser.add_argument("--mysql-database", default="auction_data", help="MySQL 数据库名")
    parser.add_argument("--open", action="store_true", help="启动后自动打开浏览器")
    parser.add_argument("--reload", action="store_true", help="自动重载（开发模式）")
    args = parser.parse_args()

    config = WebConfig(
        host=args.host,
        port=args.port,
        mysql_host=args.mysql_host,
        mysql_port=args.mysql_port,
        mysql_user=args.mysql_user,
        mysql_password=args.mysql_password,
        mysql_database=args.mysql_database,
    )

    create_app(config)

    if args.open:
        webbrowser.open(f"http://{args.host}:{args.port}")

    if args.reload:
        logger.warning("--reload 模式使用模块字符串启动，命令行 MySQL 配置不会自动注入；开发时建议使用默认 .env 配置")
        uvicorn.run(
            "web_admin.main:app",
            host=args.host,
            port=args.port,
            reload=True,
            log_level="info",
        )
        return

    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
