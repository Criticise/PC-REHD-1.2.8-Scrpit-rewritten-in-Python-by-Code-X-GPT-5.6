import os
import requests

try:
    r = requests.get("http://mafreebox.freebox.fr/api_version")
except BaseException:
    os.environ['TEST_SKIP_LOCAL'] = '1'
