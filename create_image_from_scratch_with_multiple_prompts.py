"""Generate multiple AtlasCloud images from scratch using instruction batches."""

import argparse
import base64
import csv
import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

missing = []
for mod, pkg in (("requests", "requests"), ("dotenv", "python-dotenv")):
    if importlib.util.find_spec(mod) is None:
        missing.append((mod, pkg))

if missing:
    names = ", ".join(pkg for _, pkg in missing)
    print("Missing required Python packages:", names)
    print()
    print("Install them in your virtual environment or system Python. Example:")
    print("  .\\.venv\\Scripts\\Activate.ps1")
    print("  python -m pip install -r requirements.txt")
    print()
    sys.exit(1)

import requests
from dotenv import load_dotenv
from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout

DEFAULT_API_URL = "https://api.atlascloud.ai/api/v1/model/generateImage"
DEFAULT_MODEL = "bytedance/seedream-v5.0-pro/text-to-image"
RESULTS_DIR = Path("results")
INSTRUCTIONS_DIR = Path("instructions")
API_KEY_ENV = "ATLASCLOUD_API_KEY"
API_URL_ENV = "ATLASCLOUD_API_URL"
MODEL_ENV = "ATLASCLOUD_SCRATCH_MODEL"
OUTPUT_DIR_ENV = "ATLASCLOUD_SCRATCH_OUTPUT_DIR"
INSTRUCTIONS_DIR_ENV = "ATLASCLOUD_INSTRUCTIONS_DIR"
TIMEOUT_ENV = "ATLASCLOUD_REQUEST_TIMEOUT"


def load_environment() -> None:
    # Load default .env and then overlay .env.scratch if present. This allows
    # developers to keep scratch/test settings in .env.scratch while preserving
    # a standard .env for production or CI.
    load_dotenv()
    # If a .env.scratch file exists in the current working directory, load it
    # and let its values override those already loaded from .env.
    scratch_path = Path('.env.scratch')
    if scratch_path.exists():
        load_dotenv(dotenv_path=scratch_path)


def get_env_value(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def resolve_instruction_file(path_value: str | None) -> Path:
    if path_value:
        candidate = Path(path_value).expanduser()
        if not candidate.exists():
            raise FileNotFoundError(f"Instruction file not found: {candidate}")
        return candidate

    configured_dir = Path(get_env_value(INSTRUCTIONS_DIR_ENV, str(INSTRUCTIONS_DIR)))
    if configured_dir.exists() and configured_dir.is_dir():
        suffix_priority = {".json": 0, ".yaml": 1, ".yml": 2, ".txt": 3, ".csv": 4}
        candidates = []
        for candidate in configured_dir.rglob("*"):
            if candidate.is_file() and candidate.suffix.lower() in suffix_priority:
                rel = candidate.relative_to(configured_dir)
                depth = len(rel.parts)
                candidates.append(((-depth), suffix_priority.get(candidate.suffix.lower(), 99), candidate.name.lower(), candidate))

        if candidates:
            _, _, _, chosen = sorted(candidates)[0]
            return chosen

    raise FileNotFoundError(
        "No instruction file found. Create a JSON/YAML/TXT/CSV file in the instructions folder "
        "or pass --instructions <path>."
    )


def parse_instruction_file(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if path.suffix.lower() == ".csv":
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                rows = []
                for row in reader:
                    if row is None:
                        continue
                    normalized = {key: (value.strip() if isinstance(value, str) else value) for key, value in row.items() if key}
                    prompt = str(normalized.get("prompt") or "").strip()
                    if not prompt:
                        continue
                    rows.append({k: v for k, v in normalized.items() if k != "prompt" and v is not None and str(v).strip() != ""})
                    rows[-1]["prompt"] = prompt
        except OSError as exc:
            raise RuntimeError(f"Unable to read instruction file: {path}") from exc

        if not rows:
            raise ValueError(f"Instruction file does not contain any prompts: {path}")
        return {}, rows

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Unable to read instruction file: {path}") from exc

    if not raw_text.strip():
        raise ValueError(f"Instruction file is empty: {path}")

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError:
        data = None

    if data is None:
        prompts = [
            line.strip()
            for line in raw_text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        if not prompts:
            raise ValueError(f"Instruction file does not contain any prompts: {path}")
        return {}, [{"prompt": prompt} for prompt in prompts]

    config: dict[str, Any] = {}
    jobs: list[dict[str, Any]]

    if isinstance(data, list):
        jobs = data
    elif isinstance(data, dict):
        if "jobs" in data:
            config = {k: v for k, v in data.items() if k != "jobs"}
            jobs = data["jobs"]
        elif "instructions" in data:
            config = {k: v for k, v in data.items() if k != "instructions"}
            jobs = data["instructions"]
        elif "prompt" in data:
            config = {k: v for k, v in data.items() if k != "prompt"}
            jobs = [data]
        else:
            raise ValueError(
                "Instruction file must be a JSON array or an object with a 'jobs' or 'instructions' list."
            )
    else:
        raise ValueError("Instruction file must be JSON or plain text prompt lines.")

    if not isinstance(jobs, list) or not jobs:
        raise ValueError(f"No jobs found in instruction file: {path}")

    normalized_jobs: list[dict[str, Any]] = []
    for index, job in enumerate(jobs):
        if isinstance(job, str):
            normalized_jobs.append({"prompt": job})
        elif isinstance(job, dict):
            normalized_jobs.append(job)
        else:
            raise ValueError(f"Instruction job #{index + 1} must be a string or object.")

    return config, normalized_jobs


def get_output_directory(config: dict[str, Any]) -> Path:
    configured = config.get("output_folder") or get_env_value(OUTPUT_DIR_ENV, str(RESULTS_DIR))
    output_dir = Path(configured).expanduser()
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)
    return output_dir


def build_default_filename(job: dict[str, Any], index: int) -> str:
    prompt = str(job.get("prompt") or "image")
    name = re.sub(r"[^a-zA-Z0-9._-]+", "_", prompt).strip("._")
    if not name:
        name = f"job_{index + 1}"
    save_name = job.get("save_name") or job.get("output_name")
    if save_name:
        return str(save_name)
    return f"{name}_{index + 1}.png"


def normalize_jobs(raw_jobs: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    default_model = config.get("model") or get_env_value(MODEL_ENV, DEFAULT_MODEL)
    default_output_dir = get_output_directory(config)

    for index, raw_job in enumerate(raw_jobs):
        job = dict(raw_job)
        prompt = str(job.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"Instruction job #{index + 1} is missing a prompt.")

        job["prompt"] = prompt
        job["model"] = str(job.get("model") or config.get("model") or default_model)
        job["output_folder"] = str(job.get("output_folder") or config.get("output_folder") or default_output_dir)
        job["save_name"] = build_default_filename(job, index)
        normalized.append(job)

    return normalized


def get_api_key() -> str:
    api_key = get_env_value(API_KEY_ENV)
    if not api_key:
        raise RuntimeError(
            f"Environment variable {API_KEY_ENV} is required. Create a .env file from a .env*.example file and add your key."
        )
    return api_key


def get_api_url(explicit_url: str | None) -> str:
    return explicit_url or get_env_value(API_URL_ENV, DEFAULT_API_URL)


def get_timeout() -> int:
    timeout = get_env_value(TIMEOUT_ENV, "60")
    try:
        return max(10, int(timeout))
    except ValueError:
        return 60


def get_base_api_url(api_url: str) -> str:
    parsed = urlparse(api_url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"Invalid AtlasCloud API URL: {api_url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def extract_prediction_id(payload: Any) -> str | None:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            prediction_id = data.get("id")
            if prediction_id:
                return str(prediction_id)
        for key in ("prediction_id", "id"):
            value = payload.get(key)
            if value:
                return str(value)
        for nested in payload.values():
            value = extract_prediction_id(nested)
            if value:
                return value
    elif isinstance(payload, list):
        for item in payload:
            value = extract_prediction_id(item)
            if value:
                return value
    return None


def extract_first_image_value(payload: Any) -> Any:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, list):
        for item in payload:
            value = extract_first_image_value(item)
            if value is not None:
                return value
        return None
    if isinstance(payload, dict):
        for key in ("url", "image", "images", "output", "outputs", "data", "result", "uri", "path"):
            if key in payload:
                value = payload[key]
                if isinstance(value, list):
                    for item in value:
                        if item is not None:
                            item_value = extract_first_image_value(item)
                            if item_value is not None:
                                return item_value
                elif value is not None:
                    if isinstance(value, (str, bytes, bytearray)):
                        return value
                    nested = extract_first_image_value(value)
                    if nested is not None:
                        return nested
        for nested in payload.values():
            value = extract_first_image_value(nested)
            if value is not None:
                return value
    return None


def poll_prediction(poll_url: str, api_key: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {api_key}"}

    while True:
        response = requests.get(poll_url, headers=headers, timeout=30)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            raise RuntimeError(f"{response.status_code} {response.reason}: {response.text}") from exc

        result = response.json()
        status = str((result.get("data") or {}).get("status") or "").lower()

        if status in {"completed", "succeeded", "success"}:
            return result
        if status in {"failed", "error"}:
            raise RuntimeError((result.get("data") or {}).get("error") or "Generation failed")

        print(f"Waiting for prediction status: {status or 'unknown'}")
        import time
        time.sleep(2)


def save_output(data: Any, save_path: Path) -> None:
    save_path.parent.mkdir(parents=True, exist_ok=True)

    if isinstance(data, str):
        if data.startswith("data:"):
            _, encoded = data.split(",", 1)
            save_path.write_bytes(base64.b64decode(encoded))
            return

        if data.startswith("http://") or data.startswith("https://"):
            response = requests.get(data, timeout=30)
            response.raise_for_status()
            save_path.write_bytes(response.content)
            return

        raise ValueError(f"Unsupported image output string encountered: {data[:80]}")

    if isinstance(data, (bytes, bytearray)):
        save_path.write_bytes(bytes(data))
        return

    if isinstance(data, dict):
        if "data" in data:
            save_output(data["data"], save_path)
            return
        if "url" in data:
            save_output(data["url"], save_path)
            return

    raise ValueError("Unable to persist the generated image response from the API.")


def submit_generation_request(payload: dict[str, Any], api_url: str, api_key: str, timeout: int) -> dict[str, Any]:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=timeout)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError:
            return {"raw_response": response.text}
    except Timeout as exc:
        raise RuntimeError(f"Request timed out for prompt '{payload.get('prompt', 'unknown')}'.") from exc
    except ConnectionError as exc:
        raise RuntimeError("Unable to connect to AtlasCloud. Check network or DNS settings.") from exc
    except HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        response_text = ""
        if exc.response is not None:
            try:
                response_text = exc.response.text
            except Exception:
                response_text = "(unable to read response body)"
        debug_text = response_text.strip()
        if debug_text:
            print(f"AtlasCloud API error body for prompt '{payload.get('prompt', 'unknown')}': {debug_text}", file=sys.stderr)
        raise RuntimeError(
            f"AtlasCloud API returned HTTP {status_code} for prompt '{payload.get('prompt', 'unknown')}'."
        ) from exc
    except RequestException as exc:
        raise RuntimeError("An error occurred while calling AtlasCloud.") from exc


def build_payload(job: dict[str, Any]) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": job["model"],
        "prompt": job["prompt"],
    }

    if job.get("size"):
        payload["size"] = str(job["size"])
    else:
        width = int(job.get("width") or job.get("size_width") or 2048)
        height = int(job.get("height") or job.get("size_height") or 2048)
        payload["size"] = f"{width}*{height}"

    if job.get("negative_prompt") is not None:
        payload["negative_prompt"] = job["negative_prompt"]
    if job.get("output_format"):
        payload["output_format"] = job["output_format"]
    if job.get("thinking"):
        payload["thinking"] = job["thinking"]
    if job.get("prompt_optimization_mode"):
        payload["prompt_optimization_mode"] = job["prompt_optimization_mode"]
    if job.get("enable_base64_output") is not None:
        payload["enable_base64_output"] = bool(job["enable_base64_output"])
    if job.get("cfg_scale") is not None:
        payload["cfg_scale"] = float(job["cfg_scale"])
    if job.get("steps") is not None:
        payload["steps"] = int(job["steps"])
    if job.get("sampler"):
        payload["sampler"] = job["sampler"]
    if job.get("seed") is not None:
        payload["seed"] = int(job["seed"])
    if job.get("aspect_ratio"):
        payload["aspect_ratio"] = job["aspect_ratio"]
    if job.get("payload"):
        payload = {**payload, **dict(job["payload"])}

    return payload


def write_result_metadata(result_path: Path, response_payload: Any, job: dict[str, Any], api_url: str) -> None:
    metadata = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "api_url": api_url,
        "job": {
            "prompt": job.get("prompt"),
            "save_name": job.get("save_name"),
            "model": job.get("model"),
        },
        "response": response_payload,
    }
    result_path.parent.mkdir(parents=True, exist_ok=True)
    with result_path.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def process_job(job: dict[str, Any], api_url: str, api_key: str, timeout: int) -> Path:
    output_dir = Path(job["output_folder"]).expanduser()
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    payload = build_payload(job)
    try:
        response_payload = submit_generation_request(payload, api_url, api_key, timeout)
        prediction_id = extract_prediction_id(response_payload)
        if prediction_id:
            poll_url = f"{get_base_api_url(api_url)}/api/v1/model/prediction/{prediction_id}"
            response_payload = poll_prediction(poll_url, api_key)

        image_value = extract_first_image_value(response_payload)
        if image_value is None:
            raise ValueError("AtlasCloud response did not include an image URL or payload after polling.")

        save_name = str(job["save_name"]).strip() or f"{datetime.now(timezone.utc):%Y%m%d%H%M%S}.png"
        save_path = output_dir / save_name
        save_output(image_value, save_path)

        debug_dir = Path.cwd() / "debugging"
        metadata_path = debug_dir / f"{Path(save_name).stem}_metadata.json"
        write_result_metadata(metadata_path, response_payload, job, api_url)
        print(f"Saved image: {save_path}")
        print(f"Saved metadata: {metadata_path}")
        return save_path
    except (RuntimeError, ValueError) as exc:
        raise RuntimeError(
            f"AtlasCloud generation failed for prompt '{job['prompt']}': {exc}"
        ) from exc


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate multiple images from scratch using AtlasCloud and an instruction file."
    )
    parser.add_argument(
        "-i",
        "--instructions",
        dest="instructions",
        help="Path to a JSON or text instruction file. Defaults to the first file in the instructions folder.",
    )
    parser.add_argument(
        "--api-url",
        help="Override the AtlasCloud API URL.",
    )
    parser.add_argument(
        "--model",
        help="Override the model configured in the instruction file or environment.",
    )
    return parser.parse_args()


def main() -> int:
    load_environment()
    args = parse_args()

    try:
        instruction_path = resolve_instruction_file(args.instructions)
        config, raw_jobs = parse_instruction_file(instruction_path)
        jobs = normalize_jobs(raw_jobs, config)
        if args.model:
            for job in jobs:
                job["model"] = args.model

        api_key = get_api_key()
        api_url = get_api_url(args.api_url)
        timeout = get_timeout()

        print(f"Using instructions file: {instruction_path}")
        print(f"Using AtlasCloud endpoint: {api_url}")
        for index, job in enumerate(jobs, start=1):
            print(f"Processing job {index}/{len(jobs)}: {job['prompt']}")
            process_job(job, api_url, api_key, timeout)

        print(f"Completed {len(jobs)} generation job(s).")
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
