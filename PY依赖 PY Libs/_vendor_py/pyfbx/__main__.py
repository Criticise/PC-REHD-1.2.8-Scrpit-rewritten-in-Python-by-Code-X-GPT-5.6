"""
Console script
"""
import os
import time
import logging
import json
import argparse
import pprint
import requests
from pyfbx import Fbx
from pyfbx.client import (ResponseError, RequestError)

log_level = (logging.WARNING, logging.INFO, logging.DEBUG)


def console(log, level):
    log.setLevel(level)
    if level == logging.DEBUG:
        formatter = logging.Formatter('%(asctime)s - %(name)14s - %(levelname)7s - %(funcName)10s:%(lineno)3d - %(message)s')
    else:
        formatter = logging.Formatter('%(asctime)s - %(name)14s - %(levelname)7s - %(message)s')
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    log.addHandler(stream_handler)


def output(fbx, command, json_output, send_url):
    try:
        res = eval("fbx.{}".format(command))
    except RequestError as exc:
        if "403 Client Error" in str(exc):
            fbx.log.info("Got 403, refreshing token")
            fbx.mksession()
            res = eval("fbx.{}".format(command))
        else:
            raise(exc)
    if send_url:
        try:
            r = requests.post(send_url, json=res)
        except BaseException as exc:
            log.error("While sending to {}, got exception {}".format(send_url, exc))
    elif json_output:
        print(json.dumps(res))
    else:
        pp = pprint.PrettyPrinter(indent=4)
        pp.pprint(res)


def main():
    log = logging.getLogger("pyfbx")
    parser = argparse.ArgumentParser()
    parser.add_argument("-a", "--app_id", type=str,
                        help="application identifier")
    parser.add_argument("-t", "--token", type=str,
                        help="token (or f:<filename>)")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="increase verbosity to INFO, use twice for DEBUG")
    parser.add_argument("-n", "--http", action="store_true",
                        help="disable MDNS and use http known address")
    parser.add_argument("-j", "--json", action="store_true", default=False,
                        help="json output")
    parser.add_argument("-d", "--delay", type=int,
                        help="cylically send command (number of seconds)")
    parser.add_argument("-u", "--url", type=str,
                        help="specific url to query")
    parser.add_argument("-s", "--send", type=str,
                        help="url to send json to")
    parser.add_argument("-c", "--command", action='append',
                        help="command, defaults to System.Get_the_current_system_info()")
    args = parser.parse_args()
    console(log, log_level[min(2, args.verbose)])
    token = args.token
    app_id = args.app_id
    if not args.command:
        args.command = ["System.Get_the_current_system_info()"]

    myfb = Fbx(nomdns=args.http, url=args.url)
    try:
        if token:
            if token.startswith('f:'):
                with open(args.token[2:]) as tok_file:
                    token, app_id = tok_file.read().splitlines()
        else:
            log.warning("Registering app {}, id {}, Press button".format(__name__, app_id))
            token = myfb.register(app_id=app_id, app_name=__name__,
                                  device=os.uname().nodename)
            log.warning("Save your application token: {}".format(token))
        myfb.mksession(app_id=app_id, token=token)

        while True:
            log.debug("-" * 8)
            for command in args.command:
                output(myfb, command, args.json, args.send)
            if not args.delay:
                break
            time.sleep(args.delay)
    except RequestError as exc:
        log.error("Got Http Error {}".format(exc))
        return 2
    except ResponseError as exc:
        log.error("Got Response Error {}".format(exc))
        return 1
    return 0


if __name__ == "__main__":
    exit(main())
