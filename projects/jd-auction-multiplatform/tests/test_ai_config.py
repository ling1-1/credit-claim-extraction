import tempfile
import unittest
from pathlib import Path

from jd.ai_config import load_dotenv_file, resolve_ai_config
from jd.ai_extractor import AIExtractionContext, AIFieldExtractor
from jd.config import Config


class AIConfigTests(unittest.TestCase):
    def test_config_does_not_ship_hardcoded_api_key(self):
        self.assertEqual(Config().ai.api_key, "")

    def test_deepseek_profile_can_be_loaded_from_dotenv(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_path = Path(tmp) / ".env"
            env_path.write_text(
                "\n".join(
                    [
                        "AI_ACTIVE_PROFILE=deepseek",
                        "AI_DEEPSEEK_API_KEY=dummy-test-deepseek",
                        "AI_DEEPSEEK_MODEL_NAME=deepseek-chat",
                        "AI_DEEPSEEK_BASE_URL=https://api.deepseek.com",
                    ]
                ),
                encoding="utf-8",
            )
            env = {}

            load_dotenv_file(env_path, env=env)
            resolved = resolve_ai_config(env=env)

        self.assertEqual(resolved.profile_name, "deepseek")
        self.assertEqual(resolved.provider, "deepseek")
        self.assertEqual(resolved.model_name, "deepseek-chat")
        self.assertEqual(resolved.api_key, "dummy-test-deepseek")
        self.assertEqual(resolved.base_url, "https://api.deepseek.com")

    def test_mysql_profile_uses_env_named_secret_and_beats_dotenv(self):
        env = {
            "AI_ACTIVE_PROFILE": "qwen",
            "AI_QWEN_API_KEY": "dummy-qwen-env",
            "DEEPSEEK_PROD_KEY": "dummy-deepseek-from-env-var",
        }
        mysql_profile = {
            "profile_name": "prod_deepseek",
            "provider": "deepseek",
            "model_name": "deepseek-v4-flash",
            "base_url": "https://api.deepseek.com",
            "api_key_env_var": "DEEPSEEK_PROD_KEY",
            "enabled": 1,
        }

        resolved = resolve_ai_config(env=env, mysql_profile=mysql_profile)

        self.assertEqual(resolved.profile_name, "prod_deepseek")
        self.assertEqual(resolved.provider, "deepseek")
        self.assertEqual(resolved.model_name, "deepseek-v4-flash")
        self.assertEqual(resolved.api_key, "dummy-deepseek-from-env-var")

    def test_cli_values_override_mysql_and_env(self):
        env = {
            "AI_ACTIVE_PROFILE": "deepseek",
            "AI_DEEPSEEK_API_KEY": "dummy-deepseek-env",
        }
        mysql_profile = {
            "profile_name": "prod_deepseek",
            "provider": "deepseek",
            "model_name": "deepseek-chat",
            "api_key_value": "dummy-deepseek-mysql",
            "enabled": 1,
        }

        resolved = resolve_ai_config(
            env=env,
            mysql_profile=mysql_profile,
            cli={
                "profile_name": "cli_qwen",
                "provider": "qwen",
                "model_name": "qwen-plus",
                "api_key": "dummy-qwen-cli",
                "base_url": "https://dashscope.aliyuncs.com",
            },
        )

        self.assertEqual(resolved.profile_name, "cli_qwen")
        self.assertEqual(resolved.provider, "qwen")
        self.assertEqual(resolved.model_name, "qwen-plus")
        self.assertEqual(resolved.api_key, "dummy-qwen-cli")

    def test_empty_cli_values_do_not_override_mysql_profile(self):
        mysql_profile = {
            "profile_name": "deepseek_mysql",
            "provider": "deepseek",
            "model_name": "deepseek-chat",
            "base_url": "https://api.deepseek.com",
            "api_key_value": "dummy-from-mysql",
            "timeout_seconds": 30,
            "max_retries": 2,
            "qps": 3,
            "enabled": 1,
        }

        resolved = resolve_ai_config(
            env={},
            mysql_profile=mysql_profile,
            cli={"provider": "", "model_name": "", "api_key": "", "base_url": ""},
        )

        self.assertEqual(resolved.source, "mysql")
        self.assertEqual(resolved.provider, "deepseek")
        self.assertEqual(resolved.model_name, "deepseek-chat")
        self.assertEqual(resolved.api_key, "dummy-from-mysql")
        self.assertEqual(resolved.timeout, 30)
        self.assertEqual(resolved.max_retries, 2)
        self.assertEqual(resolved.qps, 3)

    def test_ai_field_extractor_passes_configured_model_name(self):
        class FakeClient:
            def __init__(self):
                self.kwargs = None

            def chat_completion(self, messages, **kwargs):
                self.kwargs = kwargs
                return {
                    "choices": [
                        {
                            "message": {
                                "content": (
                                    '{"value":"ok","confidence":0.9,'
                                    '"reasoning":"matched","source_text":"ok"}'
                                )
                            }
                        }
                    ]
                }

            def extract_json_from_response(self, response):
                return response["choices"][0]["message"]["content"]

        extractor = AIFieldExtractor(
            provider="deepseek",
            api_key="dummy-test",
            model_name="deepseek-v4-flash",
        )
        fake_client = FakeClient()
        extractor.client = fake_client

        result = extractor.extract_field(
            "test_field",
            "测试字段",
            "用于确认模型名称会传给客户端",
            AIExtractionContext(detail_text="ok", notice_text="", html_key_values={}),
        )

        self.assertEqual(result.value, "ok")
        self.assertEqual(fake_client.kwargs["model"], "deepseek-v4-flash")


if __name__ == "__main__":
    unittest.main()
