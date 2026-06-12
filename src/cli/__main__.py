"""Allow running as `python -m src.cli`."""
import sys
from src.cli.main import main

sys.exit(main())
