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

1. Create a `.env` file from `.env.example` and add your API key:
   ```powershell
   copy .env.example .env
   notepad .env
   ```
1. Run the balance test script:
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
