# utils.py
import os
from dotenv import load_dotenv

def load_api_key():
    """Loads the Google API key from .env file."""
    load_dotenv()
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        raise ValueError("GOOGLE_API_KEY not found in .env file or environment variables.")
    return api_key

# Add any other common utility functions here if needed