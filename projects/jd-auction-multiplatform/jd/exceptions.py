"""
自定义异常体系
统一管理各类异常，便于上层统一处理
"""


from typing import Optional


class JDScraperError(Exception):
    """采集器基础异常"""
    pass


class JDAPIError(JDScraperError):
    """JD API 调用失败异常"""

    def __init__(
        self,
        function_id: str,
        message: str = "API 调用失败",
        status_code: Optional[int] = None,
        response_body: Optional[str] = None,
        original_error: Optional[Exception] = None,
    ) -> None:
        self.function_id = function_id
        self.status_code = status_code
        self.response_body = response_body
        self.original_error = original_error
        super().__init__(f"{message} [function_id={function_id}, status_code={status_code}]")


class CrawlError(JDScraperError):
    """爬取过程中的一般错误"""

    def __init__(
        self,
        message: str,
        paimai_id: Optional[str] = None,
        category_id: Optional[str] = None,
        original_error: Optional[Exception] = None,
    ) -> None:
        self.paimai_id = paimai_id
        self.category_id = category_id
        self.original_error = original_error
        super().__init__(f"{message} [paimai_id={paimai_id}, category_id={category_id}]")


class ExtractionError(JDScraperError):
    """字段提取失败异常"""

    def __init__(
        self,
        field_key: str,
        message: str = "字段提取失败",
        paimai_id: Optional[str] = None,
        source_type: Optional[str] = None,
        original_error: Optional[Exception] = None,
    ) -> None:
        self.field_key = field_key
        self.paimai_id = paimai_id
        self.source_type = source_type
        self.original_error = original_error
        super().__init__(f"{message} [field={field_key}, paimai_id={paimai_id}, source={source_type}]")


class DatabaseError(JDScraperError):
    """数据库操作失败异常"""

    def __init__(
        self,
        operation: str,
        message: str = "数据库操作失败",
        table_name: Optional[str] = None,
        paimai_id: Optional[str] = None,
        original_error: Optional[Exception] = None,
    ) -> None:
        self.operation = operation
        self.table_name = table_name
        self.paimai_id = paimai_id
        self.original_error = original_error
        super().__init__(f"{message} [operation={operation}, table={table_name}, paimai_id={paimai_id}]")
