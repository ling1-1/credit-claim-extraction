from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent
DEFAULT_CODING_BASE_URL = "https://ark.cn-beijing.volces.com/api/coding/v3"


def load_env_file(path: Path, override: bool = True) -> None:
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and (override or key not in os.environ):
            os.environ[key] = value


def parse_args() -> argparse.Namespace:
    load_env_file(ROOT / ".env")
    parser = argparse.ArgumentParser(description="Reusable workflow wrapper for Chinese credit-claim image extraction.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    def add_common(subparser: argparse.ArgumentParser) -> None:
        subparser.add_argument("--input-dir", default="image", help="Folder containing images to process.")
        subparser.add_argument("--output-dir", default="outputs_image_whole", help="Output folder.")
        subparser.add_argument(
            "--provider",
            default="auto",
            choices=["auto", "ark_coding", "ark", "agnes", "compat", "xiaomi", "bailian", "openai"],
            help="Model provider. auto prefers Ark CodingPlan when ARK_CODING_API_KEY exists.",
        )
        subparser.add_argument("--model", default="", help="Optional model override.")
        subparser.add_argument("--base-url", default="", help="Optional base URL override.")
        subparser.add_argument("--workers", type=int, default=1, help="Concurrent model calls.")
        subparser.add_argument("--max-tokens", type=int, default=16000, help="Max output tokens per image/block.")

    prepare = subparsers.add_parser("prepare", help="Check images and generate full-image crops/debug previews.")
    add_common(prepare)

    smoke = subparsers.add_parser("smoke-test", help="Run one image/block to validate provider image JSON support.")
    add_common(smoke)
    smoke.add_argument("--fallback-provider", default="ark", help="Provider to try if Ark CodingPlan smoke test fails.")
    smoke.add_argument("--no-fallback", action="store_true", help="Do not try fallback provider.")

    extract = subparsers.add_parser("extract", help="Run full extraction and generate CSV outputs.")
    add_common(extract)
    extract.add_argument("--no-cache", action="store_true", help="Do not reuse cached model outputs.")
    extract.add_argument("--smoke-first", action="store_true", help="Run a smoke test before full extraction.")
    extract.add_argument("--fallback-provider", default="ark", help="Provider to use when smoke-first fails.")

    audit = subparsers.add_parser("audit", help="Generate random audit sample and report.")
    audit.add_argument("--output-dir", default="outputs_image_whole", help="Output folder from extract.")
    audit.add_argument("--sample-size", type=int, default=8, help="Sample size.")
    audit.add_argument("--seed", type=int, default=20260612, help="Random seed.")

    load_db = subparsers.add_parser("load-db", help="Load extraction CSVs into MySQL staging tables.")
    load_db.add_argument("--output-dir", default="outputs_image_whole", help="Output folder from extract.")
    load_db.add_argument("--input-dir", default="image", help="Original input image folder.")
    load_db.add_argument("--provider", default="auto", help="Provider metadata.")
    load_db.add_argument("--model", default="", help="Model metadata.")
    load_db.add_argument("--run-uid", default="", help="Stable run uid.")
    load_db.add_argument("--run-name", default="", help="Human-readable run name.")
    load_db.add_argument("--dry-run", action="store_true", help="Read CSVs without database write.")
    load_db.add_argument("--check", action="store_true", help="Only run SELECT 1.")
    load_db.add_argument("--no-create-tables", action="store_true", help="Skip CREATE TABLE IF NOT EXISTS.")

    return parser.parse_args()


def resolve_provider(provider: str) -> str:
    if provider != "auto":
        return provider
    if os.getenv("ARK_CODING_API_KEY"):
        return "ark_coding"
    if os.getenv("ARK_API_KEY"):
        return "ark"
    if os.getenv("XIAOMI_API_KEY"):
        return "xiaomi"
    if os.getenv("AGNES_API_KEY"):
        return "agnes"
    if os.getenv("BAILIAN_API_KEY"):
        return "bailian"
    if os.getenv("OPENAI_API_KEY"):
        return "openai"
    return "ark_coding"


def provider_env_args(provider: str, model: str, base_url: str) -> list[str]:
    args = ["--provider", provider]
    if provider == "ark_coding":
        args.extend(["--api-key-env", "ARK_CODING_API_KEY"])
        if not base_url:
            base_url = os.getenv("ARK_CODING_BASE_URL") or DEFAULT_CODING_BASE_URL
    if model:
        args.extend(["--model", model])
    if base_url:
        args.extend(["--base-url", base_url])
    return args


def run_command(cmd: list[str]) -> int:
    print("Running:", " ".join(cmd))
    return subprocess.run(cmd, cwd=ROOT).returncode


def extraction_cmd(args: argparse.Namespace, provider: str, extra: list[str] | None = None) -> list[str]:
    cmd = [
        sys.executable,
        str(ROOT / "extract_credit_claims.py"),
        "--input-dir",
        args.input_dir,
        "--output-dir",
        args.output_dir,
        "--whole-image",
        "--workers",
        str(args.workers),
        "--max-tokens",
        str(args.max_tokens),
    ]
    cmd.extend(provider_env_args(provider, args.model, args.base_url))
    if extra:
        cmd.extend(extra)
    return cmd


def command_prepare(args: argparse.Namespace) -> int:
    provider = resolve_provider(args.provider)
    return run_command(extraction_cmd(args, provider, ["--prepare-only"]))


def command_smoke_test(args: argparse.Namespace) -> int:
    provider = resolve_provider(args.provider)
    code = run_command(extraction_cmd(args, provider, ["--smoke-test"]))
    if code == 0 or args.no_fallback or provider != "ark_coding":
        return code
    fallback_provider = resolve_provider(args.fallback_provider)
    print(f"Ark CodingPlan smoke test failed; trying fallback provider: {fallback_provider}")
    return run_command(extraction_cmd(args, fallback_provider, ["--smoke-test"]))


def command_extract(args: argparse.Namespace) -> int:
    provider = resolve_provider(args.provider)
    if args.smoke_first:
        code = run_command(extraction_cmd(args, provider, ["--smoke-test"]))
        if code != 0 and provider == "ark_coding":
            fallback_provider = resolve_provider(args.fallback_provider)
            print(f"Ark CodingPlan smoke test failed; switching full extraction to fallback provider: {fallback_provider}")
            fallback_code = run_command(extraction_cmd(args, fallback_provider, ["--smoke-test"]))
            if fallback_code != 0:
                return fallback_code
            provider = fallback_provider
        elif code != 0:
            return code
    extra = ["--no-cache"] if args.no_cache else []
    return run_command(extraction_cmd(args, provider, extra))


def command_audit(args: argparse.Namespace) -> int:
    cmd = [
        sys.executable,
        str(ROOT / "audit_credit_claims.py"),
        "--output-dir",
        args.output_dir,
        "--sample-size",
        str(args.sample_size),
        "--seed",
        str(args.seed),
    ]
    return run_command(cmd)


def command_load_db(args: argparse.Namespace) -> int:
    provider = resolve_provider(args.provider)
    cmd = [
        sys.executable,
        str(ROOT / "load_credit_claims_to_mysql.py"),
        "--output-dir",
        args.output_dir,
        "--input-dir",
        args.input_dir,
        "--provider",
        provider,
    ]
    if args.model:
        cmd.extend(["--model", args.model])
    if args.run_uid:
        cmd.extend(["--run-uid", args.run_uid])
    if args.run_name:
        cmd.extend(["--run-name", args.run_name])
    if args.dry_run:
        cmd.append("--dry-run")
    if args.check:
        cmd.append("--check")
    if args.no_create_tables:
        cmd.append("--no-create-tables")
    return run_command(cmd)


def main() -> int:
    args = parse_args()
    handlers = {
        "prepare": command_prepare,
        "smoke-test": command_smoke_test,
        "extract": command_extract,
        "audit": command_audit,
        "load-db": command_load_db,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
