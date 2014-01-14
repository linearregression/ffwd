"""A reference tunneling proxy for EVD."""

import time
import base64
import json
import asyncore
import sys
import socket
import logging

log = logging.getLogger(__name__)


class _DispatcherBase(asyncore.dispatcher):
    def __init__(self, protocol):
        asyncore.dispatcher.__init__(self)
        self._create_socket(protocol)

    def _create_socket(self, protocol):
        if protocol == 'tcp':
            self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        else:
            self.create_socket(socket.AF_INET, socket.SOCK_DGRAM)


class _TunnelBindConnection(asyncore.dispatcher_with_send):
    RECV_MAX = 8192

    def __init__(self, tunnel, sock, addr):
        asyncore.dispatcher_with_send.__init__(self, sock)
        self.tunnel = tunnel
        self.addr = addr

    def handle_close(self):
        """implement asyncore.dispatcher_with_send#handle_close."""
        log.info("%s: closed", repr(self.addr), exc_info=sys.exc_info())
        self.stunnel.remove_client(self.addr)
        self.close()

    def handle_error(self):
        """implement asyncore.dispatcher_with_send#handle_error."""
        log.info("%s: error", repr(self.addr), exc_info=sys.exc_info())
        self.tunnel.remove_client(self.addr)
        self.close()

    def handle_read(self):
        """implement asyncore.dispatcher#handle_read."""
        data = self.recv(self.RECV_MAX)
        self.tunnel.receive_client_data(data)


class _TunnelBind(_DispatcherBase):
    RECV_MAX = 8192

    def __init__(self, tunnel, protocol, port):
        _DispatcherBase.__init__(self, protocol)
        self._tunnel = tunnel
        self._protocol = protocol
        self._port = port
        self._clients = dict()

    def handle_read(self):
        """Receive data for UDP sockets."""
        data, _ = self.recvfrom(self.RECV_MAX)
        self._tunnel.receive_client_data(self._protocol, self._port, data)

    def receive_client_data(self, data):
        """Receive data from TCP connections."""
        self._tunnel.receive_client_data(self._protocol, self._port, data)

    def handle_close(self):
        """implement asyncore.dispatcher#handle_close."""
        for client in self._clients.values():
            client.close()

        self.close()

    def handle_accept(self):
        """implement asyncore.dispatcher#handle_accept."""
        pair = self.accept()

        if pair is not None:
            sock, addr = pair
            self._clients[addr] = _TunnelBindConnection(self, sock, addr)

    def remove_client(self, addr):
        """Remove the client connection associated with addr."""
        try:
            del self._clients[addr]
        except KeyError:
            pass


class _LineProtocol(object):
    RECV_MAX = 8192

    delimiter = '\n'

    def __init__(self):
        self._in_buffer = list()

    def _parse_lines(self, data):
        if not data:
            return

        while data:
            i = 0

            for i, c in enumerate(data):
                if c == self.delimiter:
                    yield "".join(self._in_buffer) + data[:i]
                    self._in_buffer = []
                    data = data[i + 1:]
                    break

            if i == len(data):
                self._in_buffer.append(data)
                break

    def handle_read(self):
        """implement asyncore.dispatcher#handle_read."""

        data = self.recv(self.RECV_MAX)

        for line in self._parse_lines(data):
            try:
                self.receive_line(line)
            except:
                log.error("receive_line failed", exc_info=sys.exc_info())

    def send_line(self, line):
        """Send a line of data using the specified delimiter."""
        self.send_data(line + self.delimiter)


class _BufferedWriter(object):
    def __init__(self):
        self._out_buffer = []
        self._out_size = 0
        self._out_connected = False

    def writable(self):
        """implement asyncore.dispatcher#writable."""
        return self._out_size > 0

    def handle_write(self):
        """implement asyncore.dispatcher#handle_write."""
        if self._out_size <= 0:
            return

        buf = "".join(self._out_buffer)
        sent = self.send(buf)
        new_buf = buf[sent:]
        self._out_buffer = [new_buf]
        self._out_size = len(new_buf)

    def send_data(self, data):
        """Send data by adding it to an output buffer."""
        self._out_buffer.append(data)
        self._out_size += len(data)


class _TunnelClient(_BufferedWriter, _LineProtocol, _DispatcherBase):
    def __init__(self, metadata, protocol='tcp'):
        _DispatcherBase.__init__(self, protocol)
        _LineProtocol.__init__(self)
        _BufferedWriter.__init__(self)

        self._metadata = metadata
        self._config = None
        self._servers = list()

        self.send_line(json.dumps(self._metadata))

    def handle_error(self):
        """implement asyncore.dispatcher#handle_error."""
        exc_info = sys.exc_info()
        log.error("error: %s", str(exc_info[1]), exc_info=exc_info)
        self._close()

    def handle_close(self):
        """implement asyncore.dispatcher#handle_close."""
        log.info("closed")
        self._close()

    def _close(self):
        for server in self._servers:
            server.close()

        self._servers = []
        self._config = None

        self.close()

    def receive_client_data(self, protocol, port, data):
        """Handle data received by a connected client."""
        data = base64.b64encode(data)
        self.send_line("%s %s %s" % (protocol, port, data))

    def handle_connect(self):
        """implement asyncore.dispatcher#handle_connect."""
        log.info("connected")

    def receive_line(self, line):
        """implement _LineProtocol#receive_line."""
        if self._config is None:
            self._config = json.loads(line)
            self._bind_all()

    def _bind_all(self):
        """Bind all protocol/port combinations from configuration."""
        log.info("CONFIG: %s", repr(self._config))

        bind = self._config.get('bind', [])

        for b in bind:
            protocol = b['protocol']
            port = b['port']

            try:
                self.servers.append(self._bind_one(protocol, port))
            except:
                log.error("failed to bind: %s", repr(b),
                          exc_info=sys.exc_info())
                continue

        if len(self.servers) != len(bind):
            log.error("unable to bind everything: %s", repr(bind))
            self._close()

    def _bind_one(self, protocol, port):
        """Bind a single protocol/port combination.

        Returns a _TunnelBind instance.

        """
        server = _TunnelBind(self, protocol, port)
        server.bind(('127.0.0.1', port))
        server.set_reuse_addr()

        if protocol == "tcp":
            server.listen(5)

        return server


def _main(args):
    logging.basicConfig(level=logging.INFO)

    if len(args) > 0:
        with open(args[0]) as f:
            metadata = json.load(f)
    else:
        metadata = dict()

    addr = ('127.0.0.1', 9000)

    reconnect_timeout = 10.0

    while True:
        client = _TunnelClient(metadata)
        client.connect(addr)
        asyncore.loop()
        log.info("reconnecting in %ds", reconnect_timeout)
        time.sleep(reconnect_timeout)

if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
