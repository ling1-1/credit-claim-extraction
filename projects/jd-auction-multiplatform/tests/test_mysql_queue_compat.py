import unittest
from unittest.mock import patch

import jd_mysql_store
from jd_mysql_store import MySQLConfig, MySQLJDScraperDatabase


class _FakeCursor:
    def __init__(self):
        self.executed = []
        self.description = []
        self._last_sql = ""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, sql, params=None):
        self._last_sql = sql
        self.executed.append((sql, params))

    def fetchone(self):
        if "COUNT(*) AS cnt" in self._last_sql:
            return {"cnt": 1}
        return None

    def fetchall(self):
        if "FROM crawl_queue" in self._last_sql:
            return [
                {
                    "queue_id": 10,
                    "batch_id": "b1",
                    "source_platform": "jd",
                    "source_item_id": "3101",
                    "queue_status": "success",
                }
            ]
        return []


class _FakeConnection:
    def __init__(self, cursor):
        self._cursor = cursor
        self.committed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True


class MySQLQueueCompatTests(unittest.TestCase):
    def test_write_crawl_queue_item_uses_v2_crawl_queue_when_present(self):
        cursor = _FakeCursor()
        conn = _FakeConnection(cursor)
        db = MySQLJDScraperDatabase(MySQLConfig())

        with patch.object(jd_mysql_store, "mysql_table_exists", return_value=True):
            with patch.object(jd_mysql_store, "mysql_connection", return_value=conn):
                db.write_crawl_queue_item(
                    batch_id="b1",
                    source_platform="jd",
                    source_item_id="3101",
                    item_id=1,
                    project_name="测试项目",
                    asset_group="debt",
                    asset_group_label="债权",
                    status="success",
                )

        sql = "\n".join(statement for statement, _params in cursor.executed)
        self.assertIn("INSERT INTO crawl_queue", sql)
        self.assertNotIn("INSERT INTO crawl_queue_items", sql)
        self.assertTrue(conn.committed)

    def test_list_crawl_queue_items_handles_dict_cursor_rows(self):
        cursor = _FakeCursor()
        conn = _FakeConnection(cursor)
        db = MySQLJDScraperDatabase(MySQLConfig())

        with patch.object(jd_mysql_store, "mysql_table_exists", return_value=True):
            with patch.object(jd_mysql_store, "mysql_connection", return_value=conn):
                result = db.list_crawl_queue_items(batch_id="b1", status="success")

        self.assertEqual(result["total"], 1)
        self.assertEqual(result["items"][0]["queue_id"], 10)
        sql = "\n".join(statement for statement, _params in cursor.executed)
        self.assertIn("FROM crawl_queue q", sql)

    def test_write_crawl_queue_item_uses_only_v2_table(self):
        cursor = _FakeCursor()
        conn = _FakeConnection(cursor)
        db = MySQLJDScraperDatabase(MySQLConfig())

        with patch.object(jd_mysql_store, "mysql_table_exists", side_effect=AssertionError("legacy probing disabled")):
            with patch.object(jd_mysql_store, "mysql_connection", return_value=conn):
                db.write_crawl_queue_item(
                    batch_id="b1",
                    source_platform="jd",
                    source_item_id="3101",
                    item_id=1,
                    status="success",
                )

        sql = "\n".join(statement for statement, _params in cursor.executed)
        self.assertIn("INSERT INTO crawl_queue", sql)
        self.assertNotIn("crawl_queue_items", sql)

    def test_list_crawl_queue_items_uses_only_v2_table(self):
        cursor = _FakeCursor()
        conn = _FakeConnection(cursor)
        db = MySQLJDScraperDatabase(MySQLConfig())

        with patch.object(jd_mysql_store, "mysql_table_exists", side_effect=AssertionError("legacy probing disabled")):
            with patch.object(jd_mysql_store, "mysql_connection", return_value=conn):
                result = db.list_crawl_queue_items(batch_id="b1", status="success")

        self.assertEqual(result["total"], 1)
        sql = "\n".join(statement for statement, _params in cursor.executed)
        self.assertIn("FROM crawl_queue q", sql)
        self.assertNotIn("crawl_queue_items", sql)


if __name__ == "__main__":
    unittest.main()
