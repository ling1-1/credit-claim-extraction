import unittest
from pathlib import Path
from unittest.mock import patch

from web_admin.config import WebConfig
from fastapi import HTTPException

from web_admin.routers import batches, jobs, models, queues
from web_admin.services import task_trigger


class WebAdminTaskRuntimeTests(unittest.TestCase):
    def setUp(self):
        task_trigger._running_tasks.clear()

    def tearDown(self):
        task_trigger._running_tasks.clear()

    def test_trigger_crawl_forwards_scheduler_options_to_runner_cli(self):
        threads = []

        class FakeThread:
            def __init__(self, target, args=(), kwargs=None, daemon=None):
                self.target = target
                self.args = args
                self.kwargs = kwargs or {}
                self.daemon = daemon
                threads.append(self)

            def start(self):
                return None

        config = WebConfig(project_root="F:\\codex_project\\jd", task_timeout=30)
        with patch.object(task_trigger.threading, "Thread", FakeThread):
            task_id = task_trigger.trigger_crawl(
                config,
                platform="jd",
                limit=2,
                category="109,102",
                attachment_parse_enabled=True,
                mode="incremental",
                ai_mode="async",
                platform_concurrency=3,
                item_concurrency=4,
                run_id=99,
            )

        self.assertTrue(task_id.startswith("crawl_jd_"))
        self.assertEqual(len(threads), 1)
        cmd = threads[0].args[1]
        self.assertIn("--mode", cmd)
        self.assertIn("incremental", cmd)
        self.assertIn("--jd-categories", cmd)
        self.assertIn("109,102", cmd)
        self.assertNotIn("--categories", cmd)
        self.assertIn("--parse-attachments", cmd)
        self.assertIn("--platform-concurrency", cmd)
        self.assertIn("3", cmd)
        self.assertIn("--item-concurrency", cmd)
        self.assertIn("4", cmd)
        self.assertIn("--ai-mode", cmd)
        self.assertIn("async", cmd)

    def test_trigger_crawl_cquae_full_uses_latest_browser_defaults(self):
        threads = []

        class FakeThread:
            def __init__(self, target, args=(), kwargs=None, daemon=None):
                self.target = target
                self.args = args
                self.kwargs = kwargs or {}
                self.daemon = daemon
                threads.append(self)

            def start(self):
                return None

        config = WebConfig(project_root="F:\\codex_project\\jd", task_timeout=30)
        with patch.object(task_trigger.threading, "Thread", FakeThread):
            task_trigger.trigger_crawl(
                config,
                platform="cquae",
                limit=10,
                mode="full",
                ai_mode="async",
                item_concurrency=1,
            )

        cmd = threads[0].args[1]
        self.assertIn("--platform", cmd)
        self.assertIn("cquae", cmd)
        self.assertIn("--mode", cmd)
        self.assertIn("full", cmd)
        self.assertIn("--limit", cmd)
        self.assertEqual(cmd[cmd.index("--limit") + 1], "0")
        expected = {
            "--request-timeout": "0",
            "--browser-timeout-ms": "0",
            "--cquae-page-size": "60",
            "--cquae-max-pages": "0",
            "--cquae-browser-settle-ms": "800",
            "--cquae-profile-path": "F:\\codex_project\\jd\\.browser\\cquae",
        }
        for flag, value in expected.items():
            self.assertIn(flag, cmd)
            self.assertEqual(cmd[cmd.index(flag) + 1], value)
        self.assertEqual(threads[0].args[3], 0)

    def test_run_async_marks_latest_running_crawl_batch_on_timeout(self):
        config = WebConfig(project_root="F:\\codex_project\\jd", task_timeout=1)
        calls = []

        class FakeProcess:
            returncode = None

            def communicate(self, timeout=None):
                raise task_trigger.subprocess.TimeoutExpired(["python"], timeout)

            def kill(self):
                return None

            def poll(self):
                return None

        task_trigger._running_tasks["task-1"] = {
            "type": "crawl",
            "platform": "cquae",
            "status": "pending",
            "timeout": 1,
        }

        with patch.object(task_trigger.subprocess, "Popen", return_value=FakeProcess()):
            with patch.object(task_trigger, "execute", lambda cfg, sql, params=None: calls.append((sql, params)), create=True):
                task_trigger._run_async("task-1", ["python", "-V"], ".", 1, config, None)

        self.assertTrue(any("UPDATE crawl_batches" in sql for sql, _params in calls))
        self.assertTrue(any(params and params[0] == "stopped" and params[2] == "cquae" for _sql, params in calls))

    def test_list_batches_marks_stale_running_batches_when_no_task_exists(self):
        calls = []

        batches.init(WebConfig(project_root="F:\\codex_project\\jd"))
        with patch.object(batches, "get_running_tasks", return_value=[]):
            with patch.object(batches, "execute", lambda cfg, sql, params=None: calls.append((sql, params))):
                with patch.object(batches, "query_one", return_value={"cnt": 0}):
                    with patch.object(batches, "query_all", return_value=[]):
                        batches.list_batches()

        self.assertTrue(any("UPDATE crawl_batches" in sql and "status='stopped'" in sql for sql, _params in calls))

    def test_run_async_updates_crawl_job_run_when_subprocess_finishes(self):
        config = WebConfig(project_root="F:\\codex_project\\jd", task_timeout=30)
        calls = []

        class FakeProcess:
            returncode = 0

            def communicate(self, timeout=None):
                return ("stdout text", "")

            def poll(self):
                return 0

        task_trigger._running_tasks["task-1"] = {
            "type": "crawl",
            "status": "pending",
            "timeout": 30,
        }

        with patch.object(task_trigger.subprocess, "Popen", return_value=FakeProcess()):
            with patch.object(task_trigger, "execute", lambda cfg, sql, params=None: calls.append((sql, params)), create=True):
                task_trigger._run_async("task-1", ["python", "-V"], ".", 30, config, 123)

        self.assertEqual(task_trigger._running_tasks["task-1"]["status"], "completed")
        self.assertTrue(any("UPDATE crawl_job_runs" in sql for sql, _params in calls))
        self.assertTrue(any(params and params[0] == "success" for _sql, params in calls))

    def test_run_job_now_passes_job_options_and_links_run_record(self):
        fake_job = {
            "job_id": 7,
            "source_platform": "jd",
            "per_category_limit": 2,
            "category_scope": "109,102",
            "page_limit": 5,
            "attachment_parse_enabled": 1,
            "ai_enabled": 1,
            "crawl_mode": "incremental",
        }
        executes = []
        trigger_calls = []

        def fake_query_one(_config, sql, params=None):
            if "LAST_INSERT_ID" in sql:
                return {"rid": 321}
            return fake_job

        def fake_execute(_config, sql, params=None):
            executes.append((sql, params))
            return 1

        def fake_trigger(_config, **kwargs):
            trigger_calls.append(kwargs)
            return "task-xyz"

        jobs.init(WebConfig(project_root="F:\\codex_project\\jd"))
        with patch.object(jobs, "query_one", fake_query_one):
            with patch.object(jobs, "execute", fake_execute):
                with patch.object(jobs, "trigger_crawl", fake_trigger):
                    result = jobs.run_job_now(7)

        self.assertEqual(result["task_id"], "task-xyz")
        self.assertEqual(result["run_id"], 321)
        self.assertEqual(trigger_calls[0]["mode"], "incremental")
        self.assertEqual(trigger_calls[0]["category"], "109,102")
        self.assertEqual(trigger_calls[0]["page_limit"], 5)
        self.assertTrue(trigger_calls[0]["attachment_parse_enabled"])
        self.assertEqual(trigger_calls[0]["ai_mode"], "async")
        self.assertEqual(trigger_calls[0]["run_id"], 321)
        self.assertTrue(any("task_ref" in sql for sql, _params in executes))

    def test_retry_batch_reuses_original_crawl_options(self):
        fake_batch = {
            "batch_id": "batch-1",
            "source_platform": "jd",
            "parameters_json": (
                '{"limit": 3, "category_scope": "109", "crawl_mode": "full", '
                '"page_limit": 8, "attachment_parse_enabled": true, '
                '"ai_enabled": false, "platform_concurrency": 2, '
                '"item_concurrency": 5, "ai_profile": "qwen"}'
            ),
        }
        trigger_calls = []

        def fake_query_one(_config, sql, params=None):
            return fake_batch

        def fake_trigger(_config, **kwargs):
            trigger_calls.append(kwargs)
            return "task-retry"

        batches.init(WebConfig(project_root="F:\\codex_project\\jd"))
        with patch.object(batches, "query_one", fake_query_one):
            with patch.object(batches, "execute", lambda *_args, **_kwargs: 1):
                with patch.object(batches, "get_running_tasks", lambda: []):
                    with patch.object(batches, "trigger_crawl", fake_trigger):
                        result = batches.retry_batch("batch-1")

        self.assertEqual(result["task_id"], "task-retry")
        self.assertEqual(trigger_calls[0]["platform"], "jd")
        self.assertEqual(trigger_calls[0]["limit"], 3)
        self.assertEqual(trigger_calls[0]["category"], "109")
        self.assertEqual(trigger_calls[0]["mode"], "full")
        self.assertEqual(trigger_calls[0]["page_limit"], 8)
        self.assertTrue(trigger_calls[0]["attachment_parse_enabled"])
        self.assertEqual(trigger_calls[0]["ai_mode"], "off")
        self.assertEqual(trigger_calls[0]["platform_concurrency"], 2)
        self.assertEqual(trigger_calls[0]["item_concurrency"], 5)
        self.assertEqual(trigger_calls[0]["ai_profile"], "qwen")

    def test_trigger_ai_enrich_forwards_profile_to_runner_cli(self):
        threads = []

        class FakeThread:
            def __init__(self, target, args=(), kwargs=None, daemon=None):
                self.target = target
                self.args = args
                self.kwargs = kwargs or {}
                self.daemon = daemon
                threads.append(self)

            def start(self):
                return None

        config = WebConfig(project_root="F:\\codex_project\\jd", task_timeout=30)
        with patch.object(task_trigger.threading, "Thread", FakeThread):
            task_id = task_trigger.trigger_ai_enrich(
                config,
                limit=100,
                concurrency=8,
                ai_profile="qwen_flash",
                task_types=["debt", "text"],
            )

        self.assertTrue(task_id.startswith("ai_enrich_"))
        cmd = threads[0].args[1]
        self.assertIn("--ai-profile", cmd)
        self.assertIn("qwen_flash", cmd)
        self.assertIn("--concurrency", cmd)
        self.assertIn("8", cmd)
        self.assertIn("--task-types", cmd)
        self.assertIn("debt,text", cmd)
        self.assertEqual(task_trigger._running_tasks[task_id]["ai_profile"], "qwen_flash")
        self.assertEqual(task_trigger._running_tasks[task_id]["task_types"], ["debt", "text"])

    def test_queue_profile_policy_clamps_concurrency(self):
        queues.init(WebConfig(project_root="F:\\codex_project\\jd"))

        with patch.object(
            queues,
            "query_one",
            return_value={"profile_name": "vision_qwen", "enabled": 1, "max_concurrency": 2},
        ):
            profile, concurrency = queues._resolve_ai_profile_policy("vision_qwen", 10)

        self.assertEqual(profile, "vision_qwen")
        self.assertEqual(concurrency, 2)

    def test_process_ai_queue_blank_profile_fans_out_enabled_profiles(self):
        queues.init(WebConfig(project_root="F:\\codex_project\\jd"))
        rows = [
            {
                "profile_name": "deepseek",
                "enabled": 1,
                "max_concurrency": 2,
                "task_types": ["debt"],
                "priority": 100,
                "is_default": 1,
            },
            {
                "profile_name": "qwen",
                "enabled": 1,
                "max_concurrency": 3,
                "task_types": ["text", "long_text"],
                "priority": 90,
                "is_default": 0,
            },
        ]
        calls = []

        def fake_trigger(_config, **kwargs):
            calls.append(kwargs)
            return f"task-{kwargs['ai_profile']}"

        with patch.object(queues, "query_all", return_value=rows):
            with patch.object(queues, "trigger_ai_enrich", side_effect=fake_trigger):
                result = queues.process_ai_queue(limit=20, concurrency=10, ai_profile="")

        self.assertEqual(result["mode"], "auto")
        self.assertEqual(result["task_ids"], ["task-deepseek", "task-qwen"])
        self.assertEqual([call["ai_profile"] for call in calls], ["deepseek", "qwen"])
        self.assertEqual([call["concurrency"] for call in calls], [2, 3])
        self.assertEqual(calls[0]["task_types"], ["debt"])
        self.assertEqual(calls[1]["task_types"], ["text", "long_text"])
        self.assertEqual(sum(call["limit"] for call in calls), 20)

    def test_queue_frontend_keeps_blank_profile_for_auto_distribution(self):
        text = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertNotIn("defaultProfile = profiles.value.find", text)
        self.assertNotIn("cfg.ai_profile || selectedProfile.value", text)
        self.assertIn("自动分配（按适用任务）", text)
        self.assertIn("模型配置（空=自动分配）", text)

    def test_queue_process_button_describes_worker_start_not_fixed_rows(self):
        text = Path("web_admin/static/index.html").read_text(encoding="utf-8")

        self.assertNotIn(">处理 20 条<", text)
        self.assertIn("启动 AI 解析", text)
        self.assertIn("重复点击会启动新的后台 worker", text)

    def test_resume_crawl_checkpoint_triggers_platform_from_checkpoint(self):
        queues.init(WebConfig(project_root="F:\\codex_project\\jd"))
        checkpoint = {
            "source_platform": "jd",
            "category_key": "default",
            "crawl_mode": "full",
            "checkpoint_status": "running",
            "total_items_seen": 7,
            "last_item_id": "jd-7",
        }
        calls = []

        def fake_query_one(_config, sql, params=None):
            return checkpoint

        def fake_trigger(_config, **kwargs):
            calls.append(kwargs)
            return "crawl_jd_resume"

        with patch.object(queues, "query_one", fake_query_one):
            with patch.object(queues, "get_running_tasks", lambda: []):
                with patch.object(queues, "trigger_crawl", fake_trigger):
                    result = queues.resume_crawl_checkpoint(source_platform="jd")

        self.assertEqual(result["task_id"], "crawl_jd_resume")
        self.assertEqual(result["source_platform"], "jd")
        self.assertEqual(result["last_item_id"], "jd-7")
        self.assertEqual(calls[0]["platform"], "jd")
        self.assertEqual(calls[0]["mode"], "full")
        self.assertEqual(calls[0]["limit"], 0)
        self.assertEqual(calls[0]["ai_mode"], "async")

    def test_process_selected_ai_tasks_resets_selected_rows_and_triggers_worker(self):
        queues.init(WebConfig(project_root="F:\\codex_project\\jd"))
        execute_calls = []
        trigger_calls = []

        def fake_execute(_config, sql, params=None):
            execute_calls.append((sql, params))
            return 2

        def fake_trigger(_config, **kwargs):
            trigger_calls.append(kwargs)
            return "ai_selected"

        with patch.object(queues, "execute", fake_execute):
            with patch.object(queues, "query_one", return_value=None):
                with patch.object(queues, "trigger_ai_enrich", fake_trigger):
                    result = queues.process_selected_ai_tasks(
                        payload={"task_ids": ["a1", "a2"], "ai_profile": "", "concurrency": 3}
                    )

        self.assertEqual(result["updated"], 2)
        self.assertEqual(result["task_id"], "ai_selected")
        sql, params = execute_calls[0]
        self.assertIn("ai_task_id IN (%s, %s)", sql)
        self.assertEqual(params, ["a1", "a2"])
        self.assertEqual(trigger_calls[0]["limit"], 2)
        self.assertEqual(trigger_calls[0]["concurrency"], 3)
        self.assertEqual(trigger_calls[0]["ai_profile"], "")

    def test_process_ai_queue_selected_profile_uses_single_profile_task_types(self):
        queues.init(WebConfig(project_root="F:\\codex_project\\jd"))
        calls = []

        def fake_trigger(_config, **kwargs):
            calls.append(kwargs)
            return "task-vision"

        with patch.object(
            queues,
            "query_one",
            return_value={
                "profile_name": "vision_qwen",
                "enabled": 1,
                "max_concurrency": 2,
                "task_types": ["vision"],
                "priority": 50,
            },
        ):
            with patch.object(queues, "trigger_ai_enrich", side_effect=fake_trigger):
                result = queues.process_ai_queue(limit=10, concurrency=8, ai_profile="vision_qwen")

        self.assertEqual(result["mode"], "single")
        self.assertEqual(result["task_id"], "task-vision")
        self.assertEqual(calls[0]["ai_profile"], "vision_qwen")
        self.assertEqual(calls[0]["concurrency"], 2)
        self.assertEqual(calls[0]["task_types"], ["vision"])

    def test_queue_profile_policy_rejects_disabled_profile(self):
        queues.init(WebConfig(project_root="F:\\codex_project\\jd"))

        with patch.object(
            queues,
            "query_one",
            return_value={"profile_name": "deepseek", "enabled": 0, "max_concurrency": 5},
        ):
            with self.assertRaises(HTTPException):
                queues._resolve_ai_profile_policy("deepseek", 5)

    def test_ai_queue_pause_only_targets_pending_and_running_tasks(self):
        queues.init(WebConfig(project_root="F:\\codex_project\\jd"))
        calls = []

        def fake_execute(_config, sql, params=None):
            calls.append((sql, params))
            return 3

        with patch.object(queues, "execute", fake_execute):
            result = queues.pause_ai_tasks(platform="jd")

        self.assertEqual(result["updated"], 3)
        sql, params = calls[0]
        self.assertIn("queue_status IN ('pending', 'running')", sql)
        self.assertNotIn("'parsing'", sql)
        self.assertIn("queue_status='paused'", sql)
        self.assertEqual(params, ["jd"])

    def test_ai_queue_resume_returns_paused_tasks_to_pending(self):
        queues.init(WebConfig(project_root="F:\\codex_project\\jd"))
        calls = []

        def fake_execute(_config, sql, params=None):
            calls.append((sql, params))
            return 2

        with patch.object(queues, "execute", fake_execute):
            result = queues.resume_ai_tasks(platform="ali")

        self.assertEqual(result["updated"], 2)
        sql, params = calls[0]
        self.assertIn("queue_status = 'paused'", sql)
        self.assertIn("queue_status='pending'", sql)
        self.assertEqual(params, ["ali"])

    def test_model_profile_task_types_are_validated_and_deduplicated(self):
        dumped = models._dump_task_types(["vision", "text", "vision"])

        self.assertEqual(models._parse_task_types(dumped), ["text", "vision"])
        with self.assertRaises(HTTPException):
            models._dump_task_types(["unknown"])
