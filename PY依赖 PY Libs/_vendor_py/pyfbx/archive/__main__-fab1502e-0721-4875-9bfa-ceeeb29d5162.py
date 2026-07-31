"""
Console script
"""
import os
import logging
import argparse
import pprint
from pyfbx import Fbx

log_level = (logging.WARNING, logging.INFO, logging.DEBUG)


def console(log, level):
    log.setLevel(level)
    formatter = logging.Formatter('%(asctime)s - %(name)14s - %(levelname)s - %(message)s')
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    log.addHandler(stream_handler)


def main():
    log = logging.getLogger("pyfbx")
    parser = argparse.ArgumentParser()
    parser.add_argument("app_id", type=str, help="application identifier")
    parser.add_argument("-c", "--command", type=str,
                        help="command, defaults to System.Get_the_current_system_info()",
                        default="System.Get_the_current_system_info()")
    parser.add_argument("-t", "--token", type=str, help="token (or f:<filename>)")
    parser.add_argument("-v", "--verbose", action="count", default=0,
                        help="increase verbosity to INFO, use twice for DEBUG")
    parser.add_argument("-n", "--http", action="store_true",
                        help="disable MDNS and use http known address")
    parser.add_argument("-u", "--url", type=str,
                        help="specific url to query",
                        default=None)
    args = parser.parse_args()
    console(log, log_level[min(2, args.verbose)])
    try:
        myfb = Fbx(nomdns=args.http, url=args.url)
        token = args.token
        if token:
            if token.startswith('f:'):
                with open(args.token[2:]) as tok_file:
                    token = tok_file.read().strip()
        else:
            log.warning(f"Registering app {__name__}, id {args.app_id}, Press button")
            token = myfb.register(app_id=args.app_id, app_name=__name__,
                                  device=os.uname().nodename)
            log.warning(f"Save your application token: {token}")
        myfb.mksession(app_id=args.app_id, token=token)
        pp = pprint.PrettyPrinter(indent=4)
        pp.pprint(eval(f"myfb.{args.command}"))
        return 0
    except BaseException as err:
        print(err)
        return 2


if __name__ == "__main__":
    exit(main())
