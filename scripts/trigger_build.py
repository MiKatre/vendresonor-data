"""Trigger Netlify for the published data commit. Never print the secret hook URL."""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request


def main():
    hook = os.environ.get("NETLIFY_BUILD_HOOK")
    if not hook:
        print("Missing NETLIFY_BUILD_HOOK repository secret", file=sys.stderr)
        return 1
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    payload = json.dumps({"buyer_data_commit": commit}).encode()
    for attempt in range(3):
        try:
            request = urllib.request.Request(hook, data=payload, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                if not 200 <= response.status < 300:
                    raise ValueError("Unexpected build hook response")
            print(f"Netlify rebuild requested for data commit {commit}")
            return 0
        except (urllib.error.URLError, TimeoutError, ValueError):
            if attempt == 2:
                print("Netlify build hook failed after three attempts", file=sys.stderr)
                return 1
            time.sleep(2 ** attempt)
    return 1


if __name__ == "__main__":
    sys.exit(main())
