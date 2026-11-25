#!/usr/bin/env python3
"""ShopDesk entry point.

  python3 server.py                       run the web server
  python3 server.py create-admin EMAIL    create a platform administrator (prompts for password)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from shopdesk.app import main  # noqa: E402

if __name__ == "__main__":
    main(sys.argv[1:])
