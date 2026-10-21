"""A browser for `vkm login` in CI (set BROWSER="python browser.py %s").

Like a real browser it returns at once: Python's webbrowser waits for this process, and the CLI only
answers its loopback callback after that. A detached child opens the authorization URL; the fake
gateway redirects straight to the callback, which urllib follows, like a user approving consent.
"""

import subprocess
import sys
from urllib.request import urlopen

if len(sys.argv) == 3 and sys.argv[1] == "--fetch":
    with urlopen(sys.argv[2], timeout=60) as response:
        response.read()
else:
    subprocess.Popen([sys.executable, __file__, "--fetch", sys.argv[1]], start_new_session=True)
