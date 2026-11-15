#!/usr/bin/env python3
"""NtS-Radio - server de streaming radio (SHOUTcast/Icecast compatibil) cu AutoDJ și statistici."""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from nts.app import Radio  # noqa: E402


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    data = os.environ.get("NTS_DATA") or os.path.join(here, "data")
    port = int(os.environ.get("NTS_PORT", "8000"))
    host = os.environ.get("NTS_HOST", "0.0.0.0")
    radio = Radio(data, os.path.join(here, "web"), host, port)
    try:
        asyncio.run(radio.start())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
