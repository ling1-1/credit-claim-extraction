# CQUAE 平台访问增强方案

## 当前问题分析

`www.cquae.com` 网站设置了严格的 WAF 防护，导致普通 HTTP 请求被拦截，返回 403 错误。

## 解决方案

### 1. 增强浏览器访问能力

在 `multi_platform_runner.py` 中，我们可以通过以下方式增强浏览器访问：

```python
# 在 CquaeLiveHandler 类中添加更强大的浏览器配置
class CquaeLiveHandler:
    def __init__(
        self,
        *,
        request_timeout: int | float | None = 0,
        use_browser: bool = True,
        browser_headless: bool = True,
        browser_profile_path: str | None = None,
        browser_user_agent: str | None = None,
        browser_additional_headers: dict[str, str] | None = None,
    ) -> None:
        self.adapter = CquaeAdapter()
        self.client = RequestsHTMLClient(timeout=request_timeout)
        self.use_browser = use_browser
        self.browser_user_agent = browser_user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.browser_additional_headers = browser_additional_headers or {}
        
        self.browser = (
            CquaeBrowserFetcher(
                headless=browser_headless,
                timeout_ms=0,
                profile_path=browser_profile_path,
                user_agent=self.browser_user_agent,
                additional_headers=self.browser_additional_headers,
            )
            if use_browser
            else None
        )
```

### 2. 优化浏览器访问策略

在 `CquaeBrowserFetcher` 中添加重试机制和更智能的等待策略：

```python
# 添加到 multi_platform_runner.py 中的 CquaeBrowserFetcher 类
class CquaeBrowserFetcher:
    def __init__(
        self,
        headless: bool = True,
        timeout_ms: int = 0,
        profile_path: str | None = None,
        user_agent: str | None = None,
        additional_headers: dict[str, str] | None = None,
    ):
        self.headless = headless
        self.timeout_ms = timeout_ms
        self.profile_path = profile_path
        self.user_agent = user_agent or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        self.additional_headers = additional_headers or {}
        
    def fetch_html(self, url: str) -> str:
        # 实现增强的浏览器访问逻辑
        # 包括重试、等待、用户代理伪装等
        pass
```

### 3. 为平台分离做准备

将 SDCQJY 相关代码从 CQUAE 适配器中分离出来，创建独立的适配器文件。

## 实施步骤

1. **增强浏览器访问**：修改 `multi_platform_runner.py` 中的浏览器配置
2. **添加重试机制**：在浏览器访问失败时自动重试
3. **优化用户代理**：使用更真实的浏览器用户代理
4. **平台分离准备**：为后续将 SDCQJY 单独成平台做准备