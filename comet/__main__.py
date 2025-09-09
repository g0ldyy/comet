import sys
import asyncio

from pathlib import Path

from comet.db_cli import main

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

if __name__ == "__main__":
    asyncio.run(main())
