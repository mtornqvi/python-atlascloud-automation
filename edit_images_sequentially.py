"""Edit images sequentially from a folder using AtlasCloud and save output metadata to dated results files."""

import base64
import json
import math
import mimetypes
import os
import sys
import time
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Tuple
from urllib.parse import urlparse
import traceback

import requests
from dotenv import load_dotenv
from PIL import Image
from requests.exceptions import ConnectionError, HTTPError, RequestException, Timeout

DEFAULT_API_URL = "https://api.atlascloud.ai/api/v1/model/generateImage"
RESULTS_DIR = Path("results")
API_KEY_ENV = "ATLASCLOUD_API_KEY"
API_URL_ENV = "ATLASCLOUD_API_URL"
MODEL_ENV = "ATLASCLOUD_EDIT_MODEL"
PROMPT_ENV = "ATLASCLOUD_EDIT_PROMPT"
IMAGE_FOLDER_ENV = "ATLASCLOUD_EDIT_IMAGE_FOLDER"
SAVE_FOLDER_ENV = "ATLASCLOUD_EDIT_SAVE_FOLDER"


def load_environment() -> None:
    load_dotenv()


def get_env_value(name: str, required: bool = False) -> str:
    value = os.getenv(name, "")
    if required and not value:
        raise RuntimeError(f"Environment variable {name} is required.")
    return value.strip()


IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tiff", ".webp"}


def get_images_from_folder(folder: str) -> list[Path]:
    if not folder:
        raise RuntimeError(f"Environment variable {IMAGE_FOLDER_ENV} is required.")
    folder_path = Path(folder)
    if not folder_path.exists():
        raise RuntimeError(f"Image folder not found: {folder}")
    if not folder_path.is_dir():
        raise RuntimeError(f"Image folder is not a directory: {folder}")

    images = [path for path in sorted(folder_path.iterdir()) if path.suffix.lower() in IMAGE_EXTENSIONS and path.is_file()]
    if not images:
        raise RuntimeError(f"No supported image files found in folder: {folder}")
    return images


def is_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and parsed.netloc != ""


def open_image(image_location: str) -> Image.Image:
    if is_url(image_location):
        response = requests.get(image_location, timeout=30)
        response.raise_for_status()
        return Image.open(BytesIO(response.content))

    path = Path(image_location)
    if not path.exists():
        raise FileNotFoundError(f"Image path not found: {image_location}")
    return Image.open(path)


MIN_IMAGE_AREA = 921600


def detect_image_size(image_location: str) -> str:
    with open_image(image_location) as image:
        width, height = image.size

    area = width * height
    if area >= MIN_IMAGE_AREA:
        return f"{width}*{height}"

    scale_factor = math.ceil(math.sqrt(MIN_IMAGE_AREA / area))
    width = int(width * scale_factor)
    height = int(height * scale_factor)
    # keep ratio consistent by rounding to even values if needed
    width += width % 2
    height += height % 2
    return f"{width}*{height}"


def get_base_api_url(api_url: str) -> str:
    parsed = urlparse(api_url)
    if not parsed.scheme or not parsed.netloc:
        raise RuntimeError(f"Invalid AtlasCloud API URL: {api_url}")
    return f"{parsed.scheme}://{parsed.netloc}"


def get_save_folder() -> Path | None:
    save_folder = get_env_value(SAVE_FOLDER_ENV)
    if not save_folder:
        return None
    folder = Path(save_folder)
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def extract_output_location(output: Any) -> Tuple[str, str]:
    if isinstance(output, str):
        return output, "jpeg"
    if isinstance(output, dict):
        for key in ("url", "output", "image", "uri", "path"):
            value = output.get(key)
            if isinstance(value, str) and value:
                return value, str(output.get("format", "jpeg"))
        if isinstance(output.get("data"), str):
            return output["data"], str(output.get("format", "jpeg"))
    raise ValueError("Unable to extract a saveable image location from the AtlasCloud output.")


def image_to_data_uri(image_path: Path) -> str:
    mime_type, _ = mimetypes.guess_type(image_path)
    if mime_type is None:
        mime_type = "image/jpeg"
    raw_bytes = image_path.read_bytes()
    encoded = base64.b64encode(raw_bytes).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def save_output_image(output: Any, save_dir: Path, prediction_id: str, original_image: str | None = None) -> Path:
    def get_original_name() -> str | None:
        if not original_image:
            return None
        if is_url(original_image):
            name = Path(urlparse(original_image).path).name
        else:
            name = Path(original_image).name
        if name:
            return name
        return None

    def filename_from_original(ext: str) -> Path:
        original_name = get_original_name()
        if original_name:
            suffix = Path(original_name).suffix
            if not suffix:
                return save_dir / f"{original_name}.{ext.lstrip('.') or 'jpeg'}"
            return save_dir / original_name
        return save_dir / f"{prediction_id}.{ext.lstrip('.') or 'jpeg'}"

    output_value, output_format = extract_output_location(output)
    if output_value.startswith("data:"):
        _, encoded = output_value.split(",", 1)
        raw_bytes = base64.b64decode(encoded)
        ext = output_format.split("/")[-1] if "/" in output_format else output_format
        save_path = filename_from_original(ext)
        save_path.write_bytes(raw_bytes)
        return save_path

    if output_value.startswith("http"):
        response = requests.get(output_value, timeout=30)
        response.raise_for_status()
        suffix = Path(urlparse(output_value).path).suffix
        if suffix:
            save_path = save_dir / (Path(get_original_name() or f"{prediction_id}{suffix}").name)
        else:
            save_path = filename_from_original(output_format)
        save_path.write_bytes(response.content)
        return save_path

    raise ValueError("Output data is not a recognized URL or base64 payload.")


def post_edit(api_url: str, api_key: str, payload: dict) -> dict:
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
    }

    try:
        response = requests.post(api_url, headers=headers, json=payload, timeout=30)
        response.raise_for_status()
        return response.json()
    except Timeout as exc:
        raise RuntimeError("Request timed out while sending the edit request.") from exc
    except ConnectionError as exc:
        raise RuntimeError(
            "Unable to connect to AtlasCloud. Check network or DNS settings."
        ) from exc
    except HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "unknown"
        raise RuntimeError(
            f"AtlasCloud API returned HTTP {status_code}. Verify the API key, URL, and payload."
        ) from exc
    except RequestException as exc:
        raise RuntimeError("An error occurred while requesting AtlasCloud.") from exc


def poll_prediction(poll_url: str, api_key: str) -> dict:
    headers = {"Authorization": f"Bearer {api_key}"}

    while True:
        response = requests.get(poll_url, headers=headers, timeout=30)
        try:
            response.raise_for_status()
        except HTTPError as exc:
            raise RuntimeError(f"{response.status_code} {response.reason}: {response.text}") from exc

        result = response.json()
        status = result.get("data", {}).get("status")

        if status == "completed":
            return result
        if status == "failed":
            raise RuntimeError(result.get("data", {}).get("error") or "Generation failed")

        print(f"Waiting for prediction status: {status}")
        time.sleep(2)


def write_results(result: dict, output_file: Path) -> None:
    output_file.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    with output_file.open("w", encoding="utf-8") as handle:
        handle.write(f"AtlasCloud image edit fetched on {timestamp}\n")
        handle.write(json.dumps(result, ensure_ascii=False, indent=2))
        handle.write("\n")


def should_skip_error(message: str) -> bool:
    lower = message.lower()
    return (
        "copyright" in lower
        or "copyright restrictions" in lower
        or "output image may be related to copyright" in lower
        or "500 server error" in lower
        or "internal server error" in lower
    )


def write_skipped_list(skipped: list[tuple[str, str]], output_file: Path) -> None:
    if not skipped:
        return
    output_file.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    with output_file.open("w", encoding="utf-8") as handle:
        handle.write(f"Skipped images written on {timestamp}\n")
        for name, reason in skipped:
            handle.write(f"{name}: {reason}\n")


def log_error(save_folder: Path | None, image_name: str, exc: Exception) -> None:
    """Append a detailed error entry to a log file under the save folder (or results)."""
    folder = (save_folder or RESULTS_DIR) / "logs"
    folder.mkdir(parents=True, exist_ok=True)
    log_file = folder / "errors.log"
    timestamp = datetime.now(timezone.utc).isoformat()
    with log_file.open("a", encoding="utf-8") as fh:
        fh.write(f"[{timestamp}] {image_name}: {exc}\n")
        fh.write(traceback.format_exc())
        fh.write("\n")


def main() -> int:
    load_environment()
    api_key = get_env_value(API_KEY_ENV, required=True)
    api_url = get_env_value(API_URL_ENV) or DEFAULT_API_URL
    model = get_env_value(MODEL_ENV) or "bytedance/seedream-v5.0-pro/edit"
    prompt = get_env_value(PROMPT_ENV, required=True)
    image_folder = get_env_value(IMAGE_FOLDER_ENV, required=True)
    images = get_images_from_folder(image_folder)
    processed = 0
    skipped = 0
    skipped_entries: list[tuple[str, str]] = []
    for image_path in images:
        output_name = image_path.name
        if save_folder is not None and (save_folder / output_name).exists():
            print(f"Skipping existing image: {output_name}")
            skipped += 1
            skipped_entries.append((output_name, "Already exists in save folder"))
            continue

        try:
            size = detect_image_size(str(image_path))
            payload = {
                "model": model,
                "enable_base64_output": False,
                "prompt": prompt,
                "size": size,
                "output_format": "jpeg",
                "images": [image_to_data_uri(image_path)],
                "thinking": "disabled",
            }

            print(f"Sending edit request for {output_name} to {api_url}")
            response = post_edit(api_url, api_key, payload)
            prediction_id = response.get("data", {}).get("id")
            if not prediction_id:
                raise RuntimeError("Missing prediction ID in AtlasCloud response.")

            base_url = get_base_api_url(api_url)
            poll_url = f"{base_url}/api/v1/model/prediction/{prediction_id}"
            print(f"Polling prediction status at {poll_url}")
            result = poll_prediction(poll_url, api_key)

            output_file = RESULTS_DIR / f"edit_{image_path.stem}_{datetime.now(timezone.utc):%Y.%m.%d}.json"
            write_results(result, output_file)
            print(f"Wrote response to {output_file}")

            if isinstance(result.get("data", {}).get("outputs"), list):
                output = result["data"]["outputs"][0]
                print("Generated image output:", output)
                if save_folder is not None:
                    try:
                        saved_path = save_output_image(output, save_folder, prediction_id, str(image_path))
                        print(f"Saved edited image to {saved_path}")
                    except Exception as exc:
                        # Log save failures but continue
                        print(f"Warning: failed to save edited image: {exc}", file=sys.stderr)
                        log_error(save_folder, output_name, exc)

            processed += 1

        except RuntimeError as exc:
            message = str(exc)
            if should_skip_error(message):
                print(f"Skipping {output_name}: {message}")
                skipped += 1
                skipped_entries.append((output_name, message))
                log_error(save_folder, output_name, exc)
                continue
            # other runtime errors should be logged and the script continues
            print(f"Error processing {output_name}: {message}", file=sys.stderr)
            log_error(save_folder, output_name, exc)
            skipped += 1
            skipped_entries.append((output_name, message))
            continue
        except Exception as exc:
            # unexpected errors: log and continue
            print(f"Unexpected error processing {output_name}: {exc}", file=sys.stderr)
            log_error(save_folder, output_name, exc)
            skipped += 1
            skipped_entries.append((output_name, str(exc)))
            continue

    skipped_file = save_folder / "skipped.txt" if save_folder is not None else RESULTS_DIR / "skipped.txt"
    write_skipped_list(skipped_entries, skipped_file)
    print(f"Done. Processed: {processed}, skipped: {skipped}, total: {len(images)}")
    return 0
