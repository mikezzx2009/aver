#!/usr/bin/env python3
"""Make systemd's web startup finish only after API and sandbox proxy are ready."""
import http.client
import os
import socket
import time


def main():
    host = os.environ.get('AGENT_CLOUD_HOST', '127.0.0.1')
    port = int(os.environ.get('AGENT_CLOUD_PORT', '8080'))
    proxy_host = os.environ.get('AGENT_CLOUD_PROXY_HOST', '172.29.0.1')
    proxy_port = int(os.environ.get('AGENT_CLOUD_PROXY_PORT', '8090'))
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        connection = http.client.HTTPConnection(host, port, timeout=1)
        try:
            connection.request('GET', '/health')
            response = connection.getresponse()
            if response.status == 200:
                with socket.create_connection((proxy_host, proxy_port), timeout=1):
                    return
        except (OSError, http.client.HTTPException):
            pass
        finally:
            connection.close()
        time.sleep(0.25)
    raise SystemExit('API/model proxy did not become ready within 45 seconds.')


if __name__ == '__main__':
    main()
