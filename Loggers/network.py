# coding=utf-8
import pickle
import socket
import sys
import hmac
import struct
import logging

import util

from threading import Thread

from .logger import Logger

if sys.version_info[0] >= 3:
    from json import JSONDecodeError
else:
    JSONDecodeError = ValueError

# From the docs:
#  Threads interact strangely with interrupts: the KeyboardInterrupt exception
#  will be received by an arbitrary thread. (When the signal module is
#  available, interrupts always go to the main thread.)


class NetworkLogger(Logger):
    """Send our results over the network to another instance."""

    supports_batch = True

    def __init__(self, config_options):
        Logger.__init__(self, config_options)

        self.host = Logger.get_config_option(
            config_options,
            'host',
            required=True,
            allow_empty=False
        )
        self.port = Logger.get_config_option(
            config_options,
            'port',
            required_type='int',
            required=True
        )
        self.hostname = socket.gethostname()
        self.key = bytearray(
            Logger.get_config_option(
                config_options,
                'key',
                required=True,
                allow_empty=False),
            'utf-8'
        )

    def describe(self):
        return "Sending monitor results to {0}:{1}".format(self.host, self.port)

    def save_result2(self, name, monitor):
        if not self.doing_batch:  # pragma: no cover
            self.logger_logger.error("NetworkLogger.save_result2() called while not doing batch.")
            return
        self.logger_logger.debug("network logger: %s %s", name, monitor)
        try:
            self.batch_data[monitor.name] = {
                'cls': monitor.__class__.__name__,
                'data': monitor.to_python_dict(),
            }
        except Exception:
            self.logger_logger.exception('Failed to serialize monitor %s', name)

    def process_batch(self):
        try:
            p = util.json_dumps(self.batch_data)
            mac = hmac.new(self.key, p)
            send_bytes = struct.pack('B', mac.digest_size) + mac.digest() + p
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.connect((self.host, self.port))
                s.send(send_bytes)
            finally:
                s.close()
        except Exception as e:
            self.logger_logger.error("Failed to send network data: %s", e)


class Listener(Thread):
    """This class isn't actually a Logger, but is the receiving-end implementation for network logging.

    Here seemed a reasonable place to put it."""

    def __init__(self, simplemonitor, port, key=None, allow_pickle=True):
        """Set up the thread.

        simplemonitor is a SimpleMonitor object which we will put our results into.
        """
        if key is None or key == "":
            raise util.LoggerConfigurationError("Network logger key is missing")
        Thread.__init__(self)
        self.allow_pickle = allow_pickle
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.bind(('', port))
        self.simplemonitor = simplemonitor
        self.key = bytearray(key, 'utf-8')
        self.logger = logging.getLogger('simplemonitor.logger.networklistener')
        self.running = False

    def run(self):
        """The main body of our thread.

        The loop here keeps going until we're killed by the main app.
        When the main app kills us (with join()), socket.listen throws socket.error.
        """
        self.running = True
        while self.running:
            try:
                self.sock.listen(5)
                conn, addr = self.sock.accept()
                self.logger.debug("Got connection from %s", addr[0])
                serialized = bytearray()
                while 1:
                    data = conn.recv(1024)
                    if not data:
                        break
                    serialized += data
                conn.close()
                self.logger.debug("Finished receiving from %s", addr[0])
                try:
                    # first byte is the size of the MAC
                    mac_size = serialized[0]
                    # then the MAC
                    their_digest = serialized[1:mac_size + 1]
                    # then the rest is the serialized data
                    serialized = serialized[mac_size + 1:]
                    mac = hmac.new(self.key, serialized)
                    my_digest = mac.digest()
                except IndexError:  # pragma: no cover
                    raise ValueError('Did not receive any or enough data from %s', addr[0])
                if type(my_digest) is str:
                    self.logger.debug("Computed my digest to be %s; remote is %s", my_digest, their_digest)
                else:
                    self.logger.debug("Computed my digest to be %s; remote is %s", my_digest.hex(), their_digest.hex())
                if not hmac.compare_digest(their_digest, my_digest):
                    raise Exception("Mismatched MAC for network logging data from %s\nMismatched key? Old version of SimpleMonitor?\n" % addr[0])
                try:
                    result = util.json_loads(serialized)
                except JSONDecodeError:
                    result = pickle.loads(serialized)
                try:
                    self.simplemonitor.update_remote_monitor(result, addr[0])
                except Exception:
                    self.logger.exception('Error adding remote monitor')
            except socket.error:
                fail_info = sys.exc_info()
                try:
                    if fail_info[1][0] == 4:
                        # Interrupted system call
                        self.logger.warning("Interrupted system call in thread, I think that's a ^C")
                        self.running = False
                        self.sock.close()
                except IndexError:
                    pass
                if self.running:
                    self.logger.exception("Socket error caught in thread: %s")
            except Exception:
                self.logger.exception("Listener thread caught exception %s")
