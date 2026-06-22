import os
import json
from pathlib import Path
from backend.models import AppConfig

CONFIG_PATH = Path("config.json")

def load_config() -> AppConfig:
    """Load config.json from disk, falling back to defaults if not found or invalid."""
    if not CONFIG_PATH.exists():
        config = AppConfig()
        save_config(config)
        return config
    
    try:
        with open(CONFIG_PATH, "r") as f:
            data = json.load(f)
        # Parse through Pydantic to validate and apply defaults for missing keys
        return AppConfig(**data)
    except Exception as e:
        print(f"Error loading config: {e}. Reverting to defaults.")
        config = AppConfig()
        save_config(config)
        return config

def save_config(config: AppConfig) -> None:
    """Save config to config.json on disk."""
    try:
        # Create output directory if it doesn't exist
        out_dir = Path(config.output_dir)
        if not out_dir.is_absolute():
            out_dir = Path(os.getcwd()) / out_dir
        out_dir.mkdir(parents=True, exist_ok=True)
        
        with open(CONFIG_PATH, "w") as f:
            f.write(config.model_dump_json(indent=2))
    except Exception as e:
        print(f"Failed to save config: {e}")

def update_config(new_data: dict) -> AppConfig:
    """Validate and update configuration, returning the new validated config."""
    # We can load existing config and update it to merge fields
    current = load_config()
    updated_dict = current.model_dump()
    updated_dict.update(new_data)
    
    # Validates updated fields via Pydantic
    validated = AppConfig(**updated_dict)
    save_config(validated)
    return validated
