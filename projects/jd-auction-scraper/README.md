# JD Auction Scraper Snapshot

这是京东资产拍卖采集器的当前代码快照，包含采集、字段提取、SQLite/MySQL 同步和本地 Viewer。

## 目录

- `jd_scraper_v2.py`：当前主要采集程序。
- `jd_scraper.py`：早期采集程序。
- `jd_mysql_store.py`：SQLite 到 MySQL 的导入和 MySQL 查询支持。
- `jd_viewer.py`：本地数据查看页面。
- `jd/`：配置、AI 提取、标准化、冲突检测、日志和异常模块。
- `tests/`：当前回归测试。

## 敏感配置

仓库不保存真实 API Key、数据库账号或密码。运行前复制 `.env.example` 为本地 `.env`，或通过命令行参数传入：

```powershell
$env:JD_AI_API_KEY="your_api_key"
$env:JD_MYSQL_USER="your_user"
$env:JD_MYSQL_PASSWORD="your_password"
```

`.env`、数据库文件、采集输出和日志已经在本目录 `.gitignore` 中排除。
