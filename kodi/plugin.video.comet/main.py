import os
import sys

sys.path.append(os.path.join(os.path.dirname(__file__), "lib"))

if __name__ == "__main__":
    from lib.router import addon_router

    addon_router()
