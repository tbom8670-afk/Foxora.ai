#!/usr/bin/env python3
"""Measure how far this device is from Soroush's server.

    python3 tools/soroush_ping.py

The bot already connects straight to Soroush's main server
(im-server.splus.ir:443); there is no nearer address to switch to.  What does
change the delay is the internet line of the phone.  Run this once on Wi-Fi
and once on each SIM, then keep the bot on the line with the lowest number.
Needs no login and sends nothing to your account.
"""
import socket
import ssl
import statistics
import sys
import time

HOST = "im-server.splus.ir"
PORT = 443
ROUNDS = 8


def main():
    try:
        addresses = sorted({
            info[4][0]
            for info in socket.getaddrinfo(HOST, PORT, type=socket.SOCK_STREAM)
        })
    except OSError as error:
        print(f"DNS failed: {error}")
        return 1
    print(f"{HOST} -> {', '.join(addresses)}")
    context = ssl.create_default_context()
    for address in addresses:
        tcp, tls = [], []
        for _ in range(ROUNDS):
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sock = socket.socket(family, socket.SOCK_STREAM)
            sock.settimeout(5)
            try:
                started = time.perf_counter()
                sock.connect((address, PORT))
                tcp.append((time.perf_counter() - started) * 1000)
                started = time.perf_counter()
                with context.wrap_socket(sock, server_hostname=HOST):
                    tls.append((time.perf_counter() - started) * 1000)
            except OSError as error:
                print(f"  {address}: failed ({error})")
            finally:
                try:
                    sock.close()
                except OSError:
                    pass
            time.sleep(0.2)
        if tcp:
            print(
                f"  {address}: one round trip ~ "
                f"{statistics.median(tcp):.0f} ms "
                f"(best {min(tcp):.0f}, worst {max(tcp):.0f}, "
                f"{len(tcp)}/{ROUNDS} ok)"
            )
        if tls:
            print(f"  {address}: secure handshake ~ {statistics.median(tls):.0f} ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())
