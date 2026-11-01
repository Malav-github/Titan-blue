"""Project Titan - Multiagent Competitive Intelligence System"""

import logging
import httpx

from config import (
    validate_config, DATABASE_PATH, LOG_LEVEL, LLM_MODE, LOCAL_LLM_MODEL, OLLAMA_BASE_URL,
)
from database import TitanDatabase

# Configure logging
logging.basicConfig(
    level=LOG_LEVEL,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

class TitanApp:
    """Main application for Project Titan"""

    def __init__(self):
        """Initialize the Titan application"""
        self.logger = logging.getLogger(__name__)

        # Validate configuration
        if not validate_config():
            self.logger.warning("Configuration incomplete. Some features may not work.")

        # Initialize database
        self.db = TitanDatabase(DATABASE_PATH)
        self.logger.info(f"Database initialized: {DATABASE_PATH}")

    def research_company(self, company_name: str, focus_area: str = None) -> dict:
        """
        Research a company using the multi-agent system

        Args:
            company_name: Name of company to research
            focus_area: Specific area to focus on (pricing, features, team, etc.)

        Returns:
            Dictionary containing research findings
        """
        self.logger.info(f"Starting research for {company_name}")

        # Add company to database
        company_id = self.db.add_company(company_name)
        self.logger.info(f"Company ID: {company_id}")

        # Placeholder: no research workflow exists yet (skills, search and the runner are unbuilt)
        return {
            "status": "not_implemented",
            "company": company_name,
            "focus": focus_area,
        }

    def close(self):
        """Clean up resources"""
        self.db.close()
        self.logger.info("Application shutdown")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

def ollama_models() -> list[str] | None:
    """Names of locally installed models, or None if Ollama is unreachable."""
    try:
        r = httpx.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=3)
        r.raise_for_status()
        return [m["name"] for m in r.json()["models"]]
    except (httpx.HTTPError, KeyError, ValueError):
        return None


def main():
    """Print the real state of the setup."""
    print("=" * 60)
    print("PROJECT TITAN - STATUS")
    print("=" * 60)
    print()

    try:
        with TitanApp() as app:
            models = ollama_models()
            if models is None:
                print(f"FAIL Ollama: not reachable at {OLLAMA_BASE_URL}")
            else:
                note = "" if LOCAL_LLM_MODEL in models else f"  (WARNING: {LOCAL_LLM_MODEL} not installed)"
                print(f"OK   Ollama: {', '.join(models)}{note}")
            print(f"OK   Mode: {LLM_MODE}, default model {LOCAL_LLM_MODEL}")
            print(f"OK   Database: {DATABASE_PATH}")
            print()
            print("Not built yet: skills, search, workflow runner, evidence store")
            print("Tools (fetch, browser, sandbox): run `uv run pytest` to verify")
    except Exception as e:
        print(f"FAIL Error: {e}")
        return 1

    return 0

if __name__ == "__main__":
    exit(main())
