import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # cli-agent/

os.environ["MEDULLA_RETRY_DELAY_S"] = "0"   # retry backoff off in tests
