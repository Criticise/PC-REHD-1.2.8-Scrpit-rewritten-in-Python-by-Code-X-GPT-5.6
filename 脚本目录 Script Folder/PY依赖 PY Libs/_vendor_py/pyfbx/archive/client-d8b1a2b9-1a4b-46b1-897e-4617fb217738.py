#!/usr/bin/python3
"""
Fbx Client
"""
import os
import logging
import re
import hmac
import hashlib
import time
import requests
from . import api
from . import utils
from . import mdns

__all__ = [
    "FbxTransport",
    "Fbx",
    "FbxClass", ]


class FbxClass():
    """
    Base class for Fbx subsystems
    """

    def __init__(self, transport):
        self._trn = transport


class FbxTransport():
    """
    Transport abstraction and context handling for all methods
    """

    def __init__(self, url=None, nomdns=False, session=None):
        self.log = logging.getLogger("pyfbx.trans")
        self._session = session or requests.session()
        self._session.verify = os.path.join(os.path.dirname(__file__), 'fb.pem')
        self.set_url(url, nomdns)

    def set_url(self, url, nomdns):
        if url is None:
            if nomdns:
                self._url = self.get_local_base()
            else:
                self._url = mdns.FbxMDNS().search() or self.get_local_base()
        else:
            if re.search("https?://", url) is None:
                url = "http://" + url
            if "/api/" not in url:
                self._url = self.get_local_base(url)
            else:
                self._url = url

    def set_session_header(self, session_token):
        self._session.headers.update({'X-Fbx-App-Auth': session_token})

    def api_exec(self, http_method, endpoint, post_data=None, **kwargs):
        req_response = self._session.request(
            http_method,
            self._url + "/" + endpoint.format(**kwargs), json=post_data)
        self.log.debug(">> Sent %s %s/%s Post: %s", http_method, self._url,
                       endpoint.format(**kwargs), post_data)
        req_response.raise_for_status()
        response = req_response.json()
        if response['success']:
            if 'result' in response:
                self.log.debug(f"<< Got  {response['result']}")
                return response['result']
        else:
            raise FbxErrorResponse(response['error_code'], response['msg'])

    def get_local_base(self, url=api._DISC_HTTP_URL):
        response = self._session.get(f"{url}/api_version").json()
        self.log.debug(f"<< Detected api {response['api_version']}")
        return "%s%sv%s" % (url, response['api_base_url'],
                            response['api_version'][0])


class Fbx():
    """
    Freebox object
    """

    def __init__(self, url=None, nomdns=False, session=None):
        self.log = logging.getLogger("pyfbx.fbx")
        self._trn = FbxTransport(url, nomdns=nomdns, session=session)

        # Create on the fly attributes to classes
        _globals = globals()
        for m_class in api.SYSTEMS:
            setattr(self, m_class, _globals[m_class](self._trn))
            for name, meth in api.SYSTEMS[m_class].items():
                utils.add_class_func(getattr(self, m_class).__class__, name, meth)

    def register(self, app_id, app_name, device):
        """
        Register app
        """
        self.app_id = app_id
        data = {"app_id": self.app_id, "app_name": app_name, "device_name": device}
        res = self._trn.api_exec("POST", "login/authorize/", data)
        trackid, self.token = res["track_id"], res["app_token"]
        s = "pending"
        self.log.info("Press Ok on the freebox to register application")
        while s == "pending":
            s = self._trn.api_exec("GET", f"login/authorize/{trackid}")["status"]
            if s == "pending":
                time.sleep(1)
        self.log.debug(f"Registration returned: {s}")
        return s == "granted" and self.token

    def mksession(self, app_id=None, token=None):
        if token:       # Don't overwrite previous token (used for refresh)
            self.token = token
        if app_id:
            self.app_id = app_id
        login = self._trn.api_exec("GET", "login/")
        if not login['logged_in']:
            data = {
                "app_id": self.app_id,
                "password": hmac.new(bytes(self.token, "ascii"),
                                     bytes(login['challenge'], "ascii"),
                                     hashlib.sha1).hexdigest()
            }
            resp = self._trn.api_exec("POST", "login/session/", data)
            session_token = resp["session_token"]
            self.app_id = app_id
            self._trn.set_session_header(session_token)
            self.log.info(f"Authenticated")
            return resp["permissions"]


class FbxErrorResponse(Exception):
    def __init__(self, error_code, msg):
        self.error_code = error_code
        self.msg = msg

    def __str__(self):
        return f'{self.msg} [{self.error_code}]'


log = logging.getLogger("pyfbx")

# All FB subsystems are classes deriving from FbxClass
for _classname in api.SYSTEMS:
    log.debug(f"Adding class {_classname} to locals")
    locals()[_classname] = type(_classname, (FbxClass, ), {})
    __all__.append(_classname)
