"""Test configuration: no LLM, inline transport, isolated temp database."""
import os
import sys
import tempfile

os.environ["AGENT_TRANSPORT"] = "inline"
os.environ["LLM_PROVIDER_ORDER"] = ""
os.environ["CAMPUS_DB"] = os.path.join(tempfile.gettempdir(), "campus_test.db")
os.environ["BACKGROUND_TASKS"] = "0"
os.environ["SEED_ON_START"] = "0"
os.environ["AUTO_RESOLVE"] = "1"
os.environ["AUTO_APPLY_THRESHOLD"] = "0.75"

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

if os.path.exists(os.environ["CAMPUS_DB"]):
    os.remove(os.environ["CAMPUS_DB"])
