# python-atlascloud-automation
Python script to automate AtlasCloud image generation

## Setup

1. Create and activate a local virtual environment:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   ```
2. Install dependencies:
   ```powershell
   python -m pip install --upgrade pip
   python -m pip install -r requirements.txt
   ```

## Notes

- The `.venv` directory is created locally and is ignored by Git.
- The `.env` file should contain your AtlasCloud API key and must not be committed.
- The `results/` folder is ignored by Git and is used for dated output files.
- Add any required packages to `requirements.txt`.

## AtlasCloud Model Listing

1. Create a `.env` file from an example template and add your API key:
   ```powershell
   copy .env.example .env
   notepad .env
   ```
2. Run the balance test script:
   ```powershell
   python .\atlascloud_balance.py
   ```
3. The output will be saved to `results/balance_YYYY.MM.DD.txt`.

## AtlasCloud Sequential Image Edit

1. Set up `.env` with your API key, prompt, and source image folder:
   ```powershell
   ATLASCLOUD_EDIT_IMAGE_FOLDER=C:/Users/mikko/OneDrive/Reference/AtlasCloud/Aqua__Barbie/trimmed
   ```
2. Optionally add a save folder for edited images:
   ```powershell
   ATLASCLOUD_EDIT_SAVE_FOLDER=C:/Users/mikko/OneDrive/Reference/AtlasCloud/Aqua__Barbie/nude
   ```
3. Run the edit script:
   ```powershell
   python .\edit_images_sequentially.py
   ```
4. Each source image is edited sequentially.
5. The full response for each image is saved to `results/edit_<original-name>_YYYY.MM.DD.json`.
6. If configured, edited images are saved into the folder from `ATLASCLOUD_EDIT_SAVE_FOLDER`.
7. Images already present in `ATLASCLOUD_EDIT_SAVE_FOLDER` are skipped.

## AtlasCloud Multi-Prompt Reference Image Generation

Use the script `create_images_from_ref_images_with_multiple_prompts.py` when you want to run a batch of prompts against one or more reference images.

1. Copy an example environment file to `.env`:
   ```powershell
   copy .env.ref-images.example .env
   notepad .env
   ```
2. Add your AtlasCloud API key and adjust the prompt model/output folder if needed. The current default reference model is `bytedance/seedream-v5.0-pro/generate`.
3. Add a job file under `instructions/` using the JSON format below:
   ```json
   {
     "name": "sample_reference_prompt_batch",
     "model": "bytedance/seedream-v5.0-pro/generate",
     "output_folder": "results/reference_prompts",
     "jobs": [
       {
         "name": "portrait_variant_01",
         "prompt": "A cinematic portrait of a young woman with soft studio lighting.",
         "reference_images": [
           "./reference_images/pose_01.png",
           "./reference_images/color_01.png"
         ],
         "save_name": "portrait_variant_01.png"
       }
     ]
   }
   ```
4. Run the generation script:
   ```powershell
   python .\create_images_from_ref_images_with_multiple_prompts.py --instructions .\instructions\sample_reference_prompt_batch.json
   ```
5. The script resolves each image path relative to the instruction file, submits the job to AtlasCloud, and saves the generated images into the configured output folder.

## AtlasCloud Multi-Prompt Scratch Image Generation

Use the script `create_image_from_scratch_with_multiple_prompts.py` when you want to generate images directly from prompts without reference images.

1. Copy an example environment file to `.env`:
    ```powershell
    copy .env.scratch.example .env
    notepad .env
    ```
2. Add your AtlasCloud API key and adjust the model/output folder if needed. The current default scratch model is `bytedance/seedream-v5.0-pro/text-to-image`.
3. Add a prompt batch under `instructions/`:
    ```json
    {
      "name": "sample_scratch_prompt_batch",
      "model": "bytedance/seedream-v5.0-pro/generate",
      "output_folder": "results/scratch_prompts",
      "jobs": [
        {
          "prompt": "A futuristic neon city skyline at dusk, cinematic composition, ultra-detailed.",
          "width": 1024,
          "height": 1024,
          "save_name": "dreamscape_01.png"
        }
      ]
    }
    ```
4. Run the generator:
    ```powershell
    python .\create_image_from_scratch_with_multiple_prompts.py --instructions .\instructions\sample_scratch_prompt_batch.json
    ```
5. The script submits each prompt to AtlasCloud and saves the generated image plus JSON metadata in the output folder.

The repo intentionally keeps only `.env*.example` files in version control. Real `.env` files are ignored by Git and should stay local-only.
