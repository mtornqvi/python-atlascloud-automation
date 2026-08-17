import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = ROOT / "create_image_from_scratch_with_multiple_prompts.py"

spec = importlib.util.spec_from_file_location("scratch_script", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class InstructionLoadingTests(unittest.TestCase):
    def test_resolve_instruction_file_uses_csv_when_present(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            instructions_dir = Path(tmp_dir) / "instructions"
            nested_dir = instructions_dir / "create-images-for-journalist-characters"
            nested_dir.mkdir(parents=True)

            (instructions_dir / "sample_reference_prompt_batch.json").write_text(
                '{"jobs": [{"prompt": "wrong prompt"}]}',
                encoding="utf-8",
            )
            csv_path = nested_dir / "prompts_journalists.csv"
            csv_path.write_text(
                'slug,name,role,prompt\n'
                'JOURNALIST_01,Test Journalist,Reporter,"A cinematic portrait of a young reporter in a studio."\n',
                encoding="utf-8",
            )

            original = os.environ.get(module.INSTRUCTIONS_DIR_ENV)
            try:
                os.environ[module.INSTRUCTIONS_DIR_ENV] = str(instructions_dir)
                resolved = module.resolve_instruction_file(None)
                self.assertEqual(resolved, csv_path)
                _, jobs = module.parse_instruction_file(resolved)
                self.assertEqual(jobs[0]["prompt"], "A cinematic portrait of a young reporter in a studio.")
            finally:
                if original is None:
                    os.environ.pop(module.INSTRUCTIONS_DIR_ENV, None)
                else:
                    os.environ[module.INSTRUCTIONS_DIR_ENV] = original

    def test_build_payload_matches_atlascloud_api_example(self):
        job = {
            "model": "bytedance/seedream-v5.0-pro/text-to-image",
            "prompt": "test prompt",
            "size": "2048*1152",
            "output_format": "jpeg",
            "thinking": "enabled",
            "prompt_optimization_mode": "standard",
            "enable_base64_output": False,
        }

        payload = module.build_payload(job)
        self.assertEqual(payload["model"], "bytedance/seedream-v5.0-pro/text-to-image")
        self.assertEqual(payload["size"], "2048*1152")
        self.assertEqual(payload["output_format"], "jpeg")
        self.assertEqual(payload["thinking"], "enabled")
        self.assertEqual(payload["enable_base64_output"], False)
        self.assertNotIn("width", payload)
        self.assertNotIn("height", payload)

    def test_extract_prediction_id_handles_async_response(self):
        payload = {
            "code": 200,
            "data": {
                "id": "prediction_123",
                "status": "processing",
                "outputs": [],
            },
        }
        self.assertEqual(module.extract_prediction_id(payload), "prediction_123")

    def test_extract_first_image_value_handles_completed_outputs_list(self):
        payload = {
            "code": 200,
            "data": {
                "id": "prediction_456",
                "status": "completed",
                "outputs": ["https://example.com/generated.png"],
            },
        }
        self.assertEqual(
            module.extract_first_image_value(payload),
            "https://example.com/generated.png",
        )


if __name__ == "__main__":
    unittest.main()
