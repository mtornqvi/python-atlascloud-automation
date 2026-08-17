"""Generate multiple AtlasCloud images from reference-image instructions."""

import argparse
import base64
import csv
import importlib.util
import json
import mimetypes
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
DEFAULT_MODEL = "bytedance/seedream-v5.0-pro/generate"
RESULTS_DIR = Path("results")
INSTRUCTIONS_DIR = Path("instructions")
API_KEY_ENV = "ATLASCLOUD_API_KEY"
API_URL_ENV = "ATLASCLOUD_API_URL"
MODEL_ENV = "ATLASCLOUD_REF_MODEL"
OUTPUT_DIR_ENV = "ATLASCLOUD_OUTPUT_DIR"
INSTRUCTIONS_DIR_ENV = "ATLASCLOUD_INSTRUCTIONS_DIR"
TIMEOUT_ENV = "ATLASCLOUD_REQUEST_TIMEOUT"


def load_environment() -> None:
    load_dotenv()


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
        "No instruction file found. Create a JSON/YAML/TXT/CSV file under the instructions folder "
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
                    record = {k: v for k, v in normalized.items() if k != "prompt" and v is not None and str(v).strip() != ""}
                    record["prompt"] = prompt
                    rows.append(record)
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
    except json.JSONDecodeError as exc:
        raise ValueError(f"Instruction file is not valid JSON: {path}") from exc

    jobs: list[dict[str, Any]]
    config: dict[str, Any] = {}

    if isinstance(data, list):
        jobs = data
    elif isinstance(data, dict):
        if "jobs" in data:
            config = {k: v for k, v in data.items() if k != "jobs"}
            jobs = data["jobs"]
        elif "instructions" in data:
            config = {k: v for k, v in data.items() if k != "instructions"}
            jobs = data["instructions"]
        elif "prompt" in data or "reference_images" in data or "reference_image" in data:
            config = {}
            jobs = [data]
        else:
            raise ValueError(
                "Instruction file must be a JSON array or an object with a 'jobs' or 'instructions' list."
            )
    else:
        raise ValueError("Instruction file must contain a JSON array or object.")

    if not isinstance(jobs, list) or not jobs:
        raise ValueError(f"No jobs found in instruction file: {path}")

    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            raise ValueError(f"Instruction job #{index + 1} must be an object.")

    return config, jobs


def resolve_reference_paths(job: dict[str, Any], base_dir: Path) -> list[str]:
    reference_images = job.get("reference_images") or job.get("reference_image") or job.get("images")
    if reference_images is None:
        raise ValueError(f"Job '{job.get('name', 'unnamed')}' is missing reference_images/reference_image.")

    if isinstance(reference_images, str):
        references = [reference_images]
    elif isinstance(reference_images, list):
        references = reference_images
    else:
        raise ValueError(f"Job '{job.get('name', 'unnamed')}' has an invalid reference_images value.")

    resolved: list[str] = []
    for item in references:
        if not isinstance(item, str) or not item.strip():
            continue
        value = item.strip()
        if is_url(value):
            resolved.append(value)
            continue

        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = (base_dir / candidate).resolve()
        if not candidate.exists():
            raise FileNotFoundError(f"Reference image not found: {candidate}")
        resolved.append(str(candidate))

    if not resolved:
        raise ValueError(f"Job '{job.get('name', 'unnamed')}' does not contain any usable reference images.")

    return resolved


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


def normalize_jobs(raw_jobs: list[dict[str, Any]], config: dict[str, Any], base_dir: Path) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    default_model = config.get("model") or get_env_value(MODEL_ENV, DEFAULT_MODEL)
    default_output_dir = get_output_directory(config)

    for index, raw_job in enumerate(raw_jobs):
        job = dict(raw_job)
        prompt = str(job.get("prompt") or "").strip()
        if not prompt:
            raise ValueError(f"Instruction job #{index + 1} is missing a prompt.")

        job["prompt"] = prompt
        job["reference_images"] = resolve_reference_paths(job, base_dir)
        job["model"] = job.get("model") or config.get("model") or default_model
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


def image_to_data_uri(image_path: str) -> str:
    if is_url(image_path):
        response = requests.get(image_path, timeout=30)
        response.raise_for_status()
        content_type = response.headers.get("Content-Type", "application/octet-stream")
        encoded = base64.b64encode(response.content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"

    path = Path(image_path)
    mime_type, _ = mimetypes.guess_type(path.name)
    if mime_type is None:
        mime_type = "image/png"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


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
        for key in ("url", "image", "images", "output", "data", "result", "uri", "path"):
            if key in payload:
                value = payload[key]
                if value is not None:
                    return value
        for nested in payload.values():
            value = extract_first_image_value(nested)
            if value is not None:
                return value
    return None


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


def build_payload_candidates(job: dict[str, Any], model: str) -> list[dict[str, Any]]:
    image_uris = [image_to_data_uri(image_path) for image_path in job["reference_images"]]
    width = int(job.get("width") or job.get("size_width") or 1024)
    height = int(job.get("height") or job.get("size_height") or 1024)
    negative_prompt = job.get("negative_prompt") or ""

    payloads: list[dict[str, Any]] = []
    if job.get("payload"):
        payloads.append(dict(job["payload"]))

    base_fields = {
        "model": model,
        "prompt": job["prompt"],
        "negative_prompt": negative_prompt,
        "width": width,
        "height": height,
    }

    payloads.append({**base_fields, "reference_images": [{"image": uri} for uri in image_uris]})
    payloads.append({**base_fields, "images": image_uris})
    payloads.append({
        "model": model,
        "prompt": job["prompt"],
        "negative_prompt": negative_prompt,
        "reference_image": image_uris[0] if len(image_uris) == 1 else image_uris,
        "size": f"{width}x{height}",
    })
    payloads.append({
        "prompt": job["prompt"],
        "negative_prompt": negative_prompt,
        "model": model,
        "reference_images": image_uris,
    })

    unique_payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for payload in payloads:
        key = json.dumps(payload, sort_keys=True)
        if key not in seen:
            unique_payloads.append(payload)
            seen.add(key)
    return unique_payloads


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
        raise RuntimeError(f"Request timed out for payload: {payload.get('prompt', 'unknown')}") from exc
    except ConnectionError as exc:
        raise RuntimeError("Unable to connect to AtlasCloud. Check network or DNS settings.") from exc
    except HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        raise RuntimeError(
            f"AtlasCloud API returned HTTP {status_code} for prompt '{payload.get('prompt', 'unknown')}'."
        ) from exc
    except RequestException as exc:
        raise RuntimeError("An error occurred while calling AtlasCloud.") from exc


def extract_generated_output(response_payload: dict[str, Any]) -> Any:
    value = extract_first_image_value(response_payload)
    if value is None:
        raise ValueError("AtlasCloud response did not include an image URL or payload.")
    return value


def write_result_metadata(result_path: Path, response_payload: Any, job: dict[str, Any], api_url: str) -> None:
    metadata = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "api_url": api_url,
        "job": {
            "prompt": job.get("prompt"),
            "reference_images": job.get("reference_images", []),
            "save_name": job.get("save_name"),
            "model": job.get("model"),
        },
        "response": response_payload,
    }
    with result_path.open("w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def process_job(job: dict[str, Any], api_url: str, api_key: str, timeout: int) -> Path:
    output_dir = Path(job["output_folder"]).expanduser()
    if not output_dir.is_absolute():
        output_dir = Path.cwd() / output_dir
    output_dir.mkdir(parents=True, exist_ok=True)

    payload_candidates = build_payload_candidates(job, job["model"])
    last_error: Exception | None = None

    for payload in payload_candidates:
        try:
            response_payload = submit_generation_request(payload, api_url, api_key, timeout)
            image_value = extract_generated_output(response_payload)
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
            last_error = exc
            print(f"Payload failed: {exc}", file=sys.stderr)

    if last_error is not None:
        raise RuntimeError(f"All AtlasCloud payload variants failed for prompt '{job['prompt']}'. Last error: {last_error}")
    raise RuntimeError(f"No AtlasCloud payload could be formulated for prompt '{job['prompt']}'.")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate images from one or more reference input images using AtlasCloud."
    )
    parser.add_argument(
        "-i",
        "--instructions",
        dest="instructions",
        help="Path to your JSON instruction file. Defaults to the first file in the instructions folder.",
    )
    parser.add_argument(
        "--api-url",
        help="Override the AtlasCloud API URL. The default is the standard image generation endpoint.",
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
        jobs = normalize_jobs(raw_jobs, config, instruction_path.parent)
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
