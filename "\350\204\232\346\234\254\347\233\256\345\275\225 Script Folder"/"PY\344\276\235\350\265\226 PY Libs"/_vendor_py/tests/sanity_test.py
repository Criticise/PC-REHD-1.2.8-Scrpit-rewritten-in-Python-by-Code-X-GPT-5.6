#!/usr/bin/env python3

import os
import time
import sys
import requests
import pytest
from mock import patch, MagicMock
from pyfbx import Fbx


def test_fbx_hardcoded_url():
    f = Fbx(url="http://12.34.56.78/api/v4")
    f = Fbx(url="12.34.56.78/api/v4")


@pytest.mark.skipif('TEST_SKIP_LOCAL' in os.environ, reason="No freebox")
def test_fbx_mdns():
    f = Fbx()
    assert isinstance(f, Fbx)
    with patch('pyfbx.mdns.FbxMDNS.search', return_value=None):
        f = Fbx()
    f = Fbx(url="http://192.168.1.254")
    f = Fbx(nomdns=True)
    f = Fbx(session=requests.Session())
