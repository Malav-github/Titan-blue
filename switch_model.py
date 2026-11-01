#!/usr/bin/env python
"""Model selector utility for switching between local LLMs"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from config import AVAILABLE_MODELS, LOCAL_LLM_MODEL
import re


def read_env():
    """Read .env file"""
    env_path = Path(__file__).parent / ".env"
    with open(env_path, "r") as f:
        return f.read()


def write_env(content):
    """Write .env file"""
    env_path = Path(__file__).parent / ".env"
    with open(env_path, "w") as f:
        f.write(content)


def list_models():
    """Display available models"""
    print("\n" + "=" * 70)
    print("AVAILABLE MODELS FOR PROJECT TITAN")
    print("=" * 70 + "\n")

    current = LOCAL_LLM_MODEL

    for i, (model_name, info) in enumerate(AVAILABLE_MODELS.items(), 1):
        marker = "→" if model_name == current else " "
        print(f"{marker} [{i}] {model_name}")
        print(f"     License: {info['license']} | Tools: {info['tools']}")
        print(f"     {info['description']}")
        if model_name == current:
            print(f"     ← CURRENT MODEL")
        print()


def switch_model(model_name):
    """Switch to specified model"""
    if model_name not in AVAILABLE_MODELS:
        print(f"❌ Unknown model: {model_name}")
        print(f"   Available: {', '.join(AVAILABLE_MODELS.keys())}")
        return False

    # Read current .env
    env_content = read_env()

    # Replace model setting
    pattern = r'LOCAL_LLM_MODEL=\S*'
    new_content = re.sub(pattern, f'LOCAL_LLM_MODEL={model_name}', env_content)

    # Write back
    write_env(new_content)

    info = AVAILABLE_MODELS[model_name]
    print(f"\n✓ Switched to {model_name}")
    print(f"  {info['description']}")
    print(f"  License: {info['license']}")

    return True


def main():
    """Interactive model selector"""
    print("\n" + "=" * 70)
    print("PROJECT TITAN - LLM MODEL SWITCHER")
    print("=" * 70)

    if len(sys.argv) > 1:
        model = sys.argv[1]
        switch_model(model)
        return 0

    # Interactive mode
    list_models()

    names = list(AVAILABLE_MODELS)
    choice = input(f"Enter choice (1-{len(names)}, model name, or q): ").strip().lower()

    if choice == "q":
        return 0

    model = names[int(choice) - 1] if choice.isdigit() and 1 <= int(choice) <= len(names) else choice

    if switch_model(model):
        print("\nRestart your agents to use the new model.")
        return 0
    else:
        return 1


if __name__ == "__main__":
    exit(main())
