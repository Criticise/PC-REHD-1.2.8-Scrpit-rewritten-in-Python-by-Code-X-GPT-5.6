"""
Search Freebox through MDNS
"""

import time
import logging
from zeroconf import ServiceBrowser, Zeroconf
from . import api


class FbxMDNS():
    """
    Search for a freebox using MDNS
    """
    class MyListener:
        """
        Callback class to get information of the available MDNS service
        """

        def __init__(self):
            self._info = None

        def add_service(self, zeroconf, stype, name):
            info = zeroconf.get_service_info(stype, name)
            self._info = info

        @property
        def svc_info(self):
            return self._info

    def __init__(self, timeout=1):
        self.timeout = timeout
        self.log = logging.getLogger("pyfbx.mdns")

    def search(self, svc_name=api._DISC_MDNS_NAME, timeout=1):
        self.log.debug("Searching with MDNS")
        zeroconf = Zeroconf()
        self._listener = FbxMDNS.MyListener()
        browser = ServiceBrowser(zeroconf, svc_name, self._listener)
        time.sleep(timeout)
        self.log.debug("Closing MDNS")
        browser.cancel()    # slow but required to
        zeroconf.close()    # stop listener thread
        if self.svc_info:
            prop = self.svc_prop
            base = "%sv%s" % (prop['api_base_url'], prop['api_version'][0])
            if int(prop['https_available']):
                return "https://{}:{}{}".format(prop['api_domain'], prop['https_port'], base)
            else:
                return "http://{}{}".format(prop['api_domain'], base)

    @property
    def svc_info(self):
        return self._listener.svc_info

    @property
    def svc_prop(self):
        return {k.decode(): v.decode()
                for k, v in self._listener.svc_info.properties.items()}
