# coding:utf-8

import os
import sys
import errno
import re
import html
import socket
import random
import socks
import logging
import urllib.parse as urlparse
from select import select
from time import time, sleep
from functools import partial
from threading import _start_new_thread as start_new_thread
from http.server import BaseHTTPRequestHandler, SimpleHTTPRequestHandler
from .compat.openssl import res_ciphers, SSL, SSLConnection, CertificateError
from .common import cert
from .common.decompress import decompress_readers
from .common.decorator import make_lock_decorator
from .common.dns import reset_dns, set_dns, dns_resolve, dns
from .common.net import (
    NetWorkIOError, reset_errno, closed_errno, bypass_errno,
    isip, isipv4, isipv6, forward_socket )
from .common.path import web_dir
from .common.proxy import parse_proxy, proxy_no_rdns
from .common.region import isdirect
from .common.util import LRUCache, LimiterFull, message_html
from .GlobalConfig import GC
from .HTTPUtil import http_gws, http_nor
from .RangeFetch import RangeFetchs
from .CFWFetch import cfw_fetch
from .GAEFetch import (
    check_appid_exists, mark_badappid, make_errinfo, gae_urlfetch )
from .FilterUtil import (
    set_temp_action, set_temp_connect_action,
    get_action, get_connect_action )
from .FilterConfig import action_filters

normattachment = partial(re.compile(r'(?<=filename=)([^"\']+)').sub, r'"\1"')
getbytes = re.compile(r'^bytes=(\d*)-(\d*)(,..)?').search
getrange = re.compile(r'^bytes (\d+)-(\d+)/(\d+|\*)').search
_lock_context = make_lock_decorator()

class AutoProxyHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    CAPath = '/ca', '/cadownload'
    valid_cmds = {'CONNECT', 'GET', 'POST', 'HEAD', 'PUT', 'DELETE', 'OPTIONS', 'TRACE', 'PATCH'}
    valid_leadbytes = set(cmd[0].encode() for cmd in valid_cmds)
    gae_fetcmds = {'GET', 'POST', 'HEAD', 'PUT', 'DELETE', 'OPTIONS', 'PATCH'}
    skip_request_headers = (
        'Vary',
        'Via',
        'X-Forwarded-For',
        'Proxy-Authorization',
        'Proxy-Connection',
        'Upgrade',
        'X-Chrome-Variations',
        #'Cache-Control'
        )
    skip_response_headers = (
        'Content-Length',
        'Transfer-Encoding',
        'Connection',
        'Content-Md5',
        'Set-Cookie',
        'Upgrade',
        'Alt-Svc',
        'Alternate-Protocol',
        'Expect-Ct'
        )

    fwd_timeout = GC.LINK_FWDTIMEOUT
    fwd_keeptime = GC.LINK_FWDKEEPTIME
    listen_port = {GC.LISTEN_AUTOPORT, str(GC.LISTEN_AUTOPORT),
                   GC.LISTEN_ACTPORT, str(GC.LISTEN_ACTPORT)}
    request_compress = GC.LINK_REQUESTCOMPRESS

    #?????????
    timeout = 60 * 6
    context_cache = LRUCache(256)
    proxy_connection_time = LRUCache(32)
    badhost = LRUCache(16, 120)
    rangesize = min(GC.GAE_MAXSIZE, GC.AUTORANGE_FAST_MAXSIZE * 4, 1024 * 1024 * 3)

    #?????????
    ssl_servername = GC.LISTEN_IPHOST or '127.0.0.1'
    ssl_request = False
    tunnel = False
    ssl = False
    fakecert = False
    host = None
    url = None
    url_parts = None
    conaborted = False
    action = ''
    target = None

    def __init__(self, request, client_address, server):
        self.client_address = client_address
        self.server = server
        #?????? https ??????????????????
        leadbyte = request.recv(1, socket.MSG_PEEK)
        #??????????????????????????????????????? SSL ????????????????????????
        if leadbyte == b'\x16':
            context = self.get_context(self.ssl_servername)
            context.set_tlsext_servername_callback(self.pick_certificate)
            try:
                request = SSLConnection(context, request)
                request.do_handshake_server_side()
                self.ssl_request = True
                rd, _, ed = select([request], [], [request], 4)
                if ed:
                    raise socket.error(ed)
                byte = request.recv(1, socket.MSG_PEEK) if rd else None
                if not byte:
                    #??????????????????????????????????????????????????????
                    raise CertificateError(-1, '???????????????????????????????????????????????????????????????????????????')
            except Exception as e:
                #if e.args[0] not in bypass_errno:
                servername = request.get_servername() or self.ssl_servername
                logging.warning('%s https ???????????????sni=%r???%r',
                                self.address_string(), servername, e)
                return
        elif leadbyte not in self.valid_leadbytes:
            return
        self.request = request
        self.setup()
        try:
            self.handle()
        finally:
            self.finish()

    def pick_certificate(self, connection):
        servername = connection.get_servername()
        if servername is None:
            if GC.LISTEN_IPHOST is self.ssl_servername:
                return
            servername = GC.LISTEN_IPHOST
        else:
            servername = str(servername, 'iso-8859-1')
        if not servername:
            logging.warning('%s https ??????????????????????????? IP ?????????GotoX ????????? IP-Host ??????',
                            self.address_string())
            return
        new_context = self.get_context(servername)
        connection.set_context(new_context)

    def setup(self):
        #???????????????????????? nagle's algorithm ?????????????????????
        if not self.disable_nagle_algorithm:
            client_ip = self.client_address[0]
            if client_ip.endswith('127.0.0.1') or client_ip == '::1':
                self.disable_nagle_algorithm = True
                if sys.platform != 'darwin':
                    self.request.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 0)
        BaseHTTPRequestHandler.setup(self)

    def write(self, d, logerror=None):
        if not isinstance(d, bytes):
            d = d.encode()
        try:
            return self.wfile.write(d)
        except Exception as e:
            self.conaborted = True
            if logerror:
                logging.debug('%s ????????????????????????%r, %r',
                              self.address_string(), self.url, e)
            raise e

    def handle_one_request(self):
        try:
            self.raw_requestline = self.rfile.readline(65537)
            if len(self.raw_requestline) > 65536:
                self.requestline = ''
                self.request_version = ''
                self.command = ''
                self.send_error(414)
                return
            if not self.raw_requestline or \
                    self.raw_requestline[:1] not in self.valid_leadbytes or \
                    self.server.is_offline:
                self.close_connection = True
                return
            if not self.parse_request():
                # An error code has been sent, just exit
                return
            if self.command == 'CONNECT':
                self.do_CONNECT()
            elif self.command in self.valid_cmds:
                self.do_METHOD()
            else:
                self.send_error(501, 'Unsupported method (%r)' % self.command)
                return
            self.wfile.flush() #actually send the response if not already done.
        except socket.timeout as e:
            #a read or a write timed out.  Discard this connection
            logging.debug('%s Request timed out: %r', self.address_string(), e)
            self.close_connection = True
        except SSL.Error as e:
            if isinstance(e.args[0], list) and any('certificate unknown' in arg for arg in e.args[0][0]):
                logging.warning('%s host=%s???????????? https ?????????????????????????????? GotoX CA ?????????',
                                self.address_string(), self.host)
            self.close_connection = True

    def do_action(self):
        #?????? gws ??????????????????
        #?????? hostname ??????
        self.close_connection = True
        self.ws = self.headers.get('Upgrade') == 'websocket'
        if self.ws:
            self.url = 'ws' + self.url[4:]
            if self.action == 'do_GAE':
                self.action = 'do_FORWARD'
                self.target = None
                logging.warning('%s %s ????????? %r????????? FORWARD???',
                                self.address_string(), self.action[3:], self.url)
        if self.action in ('do_DIRECT', 'do_FORWARD'):
            if self.target:
                iporname, profile = self.target
            else:
                iporname, profile = None, None
            self.hostname = hostname = set_dns(self.host, iporname)
            if hostname is None:
                if self.ssl and not self.fakecert:
                    self.do_FAKECERT()
                else:
                    logging.error('%s ?????????????????????%r????????????%r?????????????????????????????????',
                                  self.address_string(), self.host, self.path)
                    c = message_html('504 ????????????',
                                     '????????????',
                                     '????????? %s ?????????????????????????????????????????????' % self.host).encode()
                    self.write(b'HTTP/1.1 504 Resolve Failed\r\n'
                               b'Content-Type: text/html\r\n'
                               b'Content-Length: %d\r\n\r\n' % len(c))
                    self.write(c)
                return
            if profile == '@v4':
                dns[self.hostname] = [ip for ip in dns[self.hostname] if isipv4(ip)]
            elif profile == '@v6':
                dns[self.hostname] = [ip for ip in dns[self.hostname] if isipv6(ip)]
        getattr(self, self.action)()

    def parse_host(self, host, chost, mhost=True):
        port = None
        #??????????????????????????????
        chost, cport = urlparse.splitport(chost)
        #????????????????????? Host ??????
        if host:
            #??????????????????????????????
            host, port = urlparse.splitport(host)
            #??????????????????????????????????????????
            if chost and port in self.listen_port and host in self.localhosts:
                self.host = host = chost
                port = cport
            else:
                self.host = host
        else:
            self.host = host = chost
        if host[0] == '[':
            self.host = host[1:-1]
        #????????????
        self.port = port = int(port or cport or self.ssl and 443 or 80)
        #?????? Host ??????
        if mhost:
            if (bool(self.ssl), port) not in ((False, 80), (True, 443)):
                if isipv6(host) and host[0] != '[':
                    host = '[%s]:%d' % (host, port)
                else:
                    host = '%s:%d' % (host, port)
            else:
                host = self.host
            if 'Host' in self.headers:
                self.headers.replace_header('Host', host)
            else:
                self.headers['Host'] = host

    def _do_CONNECT(self):
        self.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
        self.tunnel = True
        leadbyte = self.connection.recv(1, socket.MSG_PEEK)
        self.ssl = leadbyte in (b'\x16', b'\x80') # 0x80: ssl20
        if not self.ssl:
            return True
        self.parse_host(self.headers.get('Host'), self.path)
        #????????????
        if self.host in self.localhosts and (
                self.port in (80, 443) or
                self.port in self.listen_port):
            self.do_FAKECERT()
            return True

    def do_CONNECT(self):
        #?????? CONNECT ?????????????????????????????????????????????
        if self._do_CONNECT():
            return
        self.action, self.target = get_connect_action(self.ssl, self.host)
        self.do_action()

    def _do_METHOD(self):
        self.reread_req = False
        self.url_parts = url_parts = urlparse.urlsplit(self.path)
        self.parse_host(self.headers.get('Host'), url_parts.netloc)
        #????????????
        scheme = 'https' if self.ssl else 'http'
        #??????????????????????????????????????????
        self.url_parts = url_parts = urlparse.SplitResult(scheme, self.headers.get('Host'), url_parts.path, url_parts.query, '')
        self.url = url = url_parts.geturl()
        #????????????
        if self.path[0] != '/':
            self.path = url[url.find('/', 12):]
        #????????????
        if self.host in self.localhosts and (
                self.port in (80, 443) or
                self.port in self.listen_port):
            self.do_LOCAL()
            return True

    def do_METHOD(self):
        #?????????????????????????????????????????????????????????
        if self._do_METHOD():
            return
        self.action, self.target = get_action(self.url_parts.scheme, self.host, self.path[1:], self.url)
        self.do_action()

    def write_response_content(self, data, response, need_chunked):
        length = self.response_length
        #???????????????
        if not need_chunked and not length:
            return 0, None
        #??????????????????
        ndata = len(data) if data else 0
        wrote = 0
        err = None
        buf = memoryview(bytearray(self.bufsize))
        try:
            if ndata:
                buf[:ndata] = data
            else:
                ndata = response.readinto(buf)
            while ndata:
                if need_chunked:
                    self.write(b'%x\r\n' % ndata, True)
                    assert ndata == self.write(buf[:ndata].tobytes(), True), '?????????????????????'
                    self.write(b'\r\n', True)
                    wrote += ndata
                else:
                    assert ndata == self.write(buf[:ndata].tobytes(), True), '?????????????????????'
                    wrote += ndata
                    if wrote >= length:
                        break
                ndata = response.readinto(buf)
        except Exception as e:
            err = e
        finally:
            if need_chunked:
                self.write(b'0\r\n\r\n', True)
            return wrote, err

    def handle_request_headers(self):
        #????????????????????????????????????????????????
        if self.reread_req:
            self.close_connection = self.cc
            return self.request_headers.copy(), self.payload
        #????????????
        request_headers = {k.title(): v for k, v in self.headers.items()
                               if k.title() not in self.skip_request_headers}
        if self.ws:
            request_headers['Upgrade'] = 'websocket'
        pconnection = self.headers.get('Proxy-Connection')
        if pconnection and \
                self.request_version < 'HTTP/1.1' and \
                pconnection.lower() != 'keep-alive':
            self.close_connection = True
        else:
            self.close_connection = False
        payload = b''
        length = int(request_headers.get('Content-Length', 0))
        if self.action == 'do_GAE':
            try:
                #??????????????? 32MB??????????????????????????????
                if 0 < length < 33554433:
                    payload = self.rfile.read(length)
                elif 'Transfer-Encoding' in request_headers:
                    value = []
                    length = 0
                    while True:
                        chunk_size_str = self.rfile.readline(65537)
                        if len(chunk_size_str) > 65536:
                            raise Exception('??????????????????')
                        chunk_size = int(chunk_size_str.split(b';')[0], 16)
                        if chunk_size == 0:
                            while True:
                                chunk = self.rfile.readline(65537)
                                if chunk in (b'\r\n', b'\n', b''): # b'' ???????????????????????????
                                    break
                                else:
                                    #?????????????????????????????????????????????????????????????????????
                                    logging.debug('%s "%s %s %s"???????????????%r',
                                                  self.address_string(), self.action[3:], self.command, self.url, chunk)
                            break
                        chunk = self.rfile.read(chunk_size)
                        value.append(chunk)
                        length += len(chunk)
                        if length > 33554432:
                            break
                        if self.rfile.read(2) != b'\r\n':
                            raise Exception('????????????????????? CRLF')
                    payload = b''.join(value)
            except Exception as e:
                logging.error('%s "%s %s %s" ???????????????????????????%r',
                              self.address_string(), self.action[3:], self.command, self.url, e)
                raise
            if length > 33554432:
                logging.error('%s "%s %s %s" ???????????????????????????%d??????????????? GAE ??????',
                              self.address_string(), self.action[3:], self.command, self.url, length)
                raise
        elif self.action not in ('do_DIRECT', 'do_CFW') or \
                length > 65536 or \
                'Transfer-Encoding' in request_headers:
            #???????????????????????? rfile ???????????????????????????
            payload = self.rfile
            self.rfile.readed = 0
        elif length:
            #?????? 64KB ????????????????????????
            try:
                payload = self.rfile.read(length)
            except NetWorkIOError as e:
                logging.error('%s "%s %s %s" ???????????????????????????%r',
                              self.address_string(), self.action[3:], self.command, self.url, e)
                raise
        #???????????????????????????????????????????????????????????????
        if self.request_compress:
            r = request_headers.get('Range')
            if not (r and r.startswith('bytes=')):
                ae = request_headers.get('Accept-Encoding', '')
                aes = []
                if ae:
                    aes.append(ae)
                if 'gzip' not in ae:
                    aes.append('gzip')
                if 'br' not in ae and 'br' in decompress_readers:
                    aes.append('br')
                request_headers['Accept-Encoding'] = ', '.join(aes)
        self.request_headers = request_headers
        self.payload = payload
        self.reread_req = True
        self.cc = self.close_connection
        return request_headers.copy(), payload

    def handle_response_headers(self, response):
        #????????????
        ws_ok = self.ws and response.status == 101
        if ws_ok:
            response_headers = {k.title(): v for k, v in response.headers.items()}
            response_headers.pop('Expect-Ct', None)
            response_headers.pop('Set-Cookie', None)
        else:
            response_headers = {k.title(): v for k, v in response.headers.items()
                                if k.title() not in self.skip_response_headers}
        log =  logging.info
        if self.action == 'do_CFW':
            response_headers = {k: v for k, v in response_headers.items()
                                if not (k.startswith('Cf-') or
                                        k in ('Nel', 'Report-To', 'Server'))}
            if response_headers.pop('X-Fetch-Status', None) != 'ok':
                log = logging.warning
            else:
                sheaders = tuple((k[7:], v) for k, v in response_headers.items()
                                 if k.startswith('Source-'))
                for k, v in sheaders:
                    response_headers.setdefault(k, v)
        cookies = response.headers.get_all('Set-Cookie')
        if cookies and self.action == 'do_CFW':
            cookies = [cookie for cookie in cookies if '.workers.dev' not in cookie]
        if cookies:
            response_headers['Set-Cookie'] = '\r\nSet-Cookie: '.join(cookies)
        if ws_ok:
            data = need_chunked = None
            length = 0
        else:
            if response.status == 206 and not response.length:
                content_range = response.headers.get('Content-Range')
                content_range = getrange(content_range)
                if content_range:
                    start, end = content_range.group(1, 2)
                    self.response_length = int(end) + 1 - int(start)
            else:
                self.response_length = response.length or 0
            #???????????? Accept-Ranges
            if response_headers.get('Accept-Ranges') != 'bytes':
                if response.status == 206:
                    response_headers['Accept-Ranges'] = 'bytes'
                else:
                    response_headers['Accept-Ranges'] = 'none'
            #?????????????????????????????????
            ce = response_headers.get('Content-Encoding')
            if ce:
                if ce.startswith('none'):
                    #????????????????????????????????????????????? 'none'
                    ce = ce[4:].lstrip(', ')
                    if ce:
                        response_headers['Content-Encoding'] = ce
                    else:
                        del response_headers['Content-Encoding']
                if ce and ce not in self.headers.get('Accept-Encoding', '') and \
                        ce in decompress_readers:
                    response = decompress_readers[ce](response)
                    del response_headers['Content-Encoding']
                    response_headers.pop('Content-Length', None)
                    response_headers.pop('Accept-Ranges', None)
                    self.response_length = 0
                    logging.debug('????????? %r ??????????????? %s', ce, self.url)
            length = self.response_length
            data = response.read(self.bufsize)
            need_chunked = data and not length # response ??????????????????????????????
            if need_chunked:
                length = '-'
                if self.request_version == 'HTTP/1.1':
                    response_headers['Transfer-Encoding'] = 'chunked'
                else:
                    # HTTP/1.1 ??????????????? chunked???????????????
                    need_chunked = False
                    self.close_connection = True
            else:
                response_headers['Content-Length'] = length
            if 'Content-Disposition' in response_headers:
                response_headers['Content-Disposition'] = normattachment(response_headers['Content-Disposition'])
            response_headers['Connection' if self.tunnel else 'Proxy-Connection'] = 'close' if self.close_connection else 'keep-alive'
        headers_data = 'HTTP/1.1 %s %s\r\n%s\r\n' % (response.status, response.reason, ''.join('%s: %s\r\n' % x for x in response_headers.items()))
        self.write(headers_data)
        logging.debug('headers_data=%s', headers_data)
        if 300 <= response.status < 400 and \
                response.status != 304 and \
                'Location' in response_headers:
            logging.info('%r ????????????????????? %r',
                         self.url, response_headers['Location'])
        log('%s "%s %s %s HTTP/1.1" %s %s',
            self.address_string(response), self.action[3:], self.command, self.url, response.status, length, color=response.status == 304 and 'green')
        return response, data, need_chunked, ws_ok

    def do_DIRECT(self):
        #????????????????????????
        hostname = self.hostname
        http_util = http_gws if hostname.startswith('google') else http_nor
        request_headers, payload = self.handle_request_headers()
        headers_sent = False
        for retry in range(2):
            if retry > 0 and payload and isinstance(payload, bytes) or hasattr(payload, 'readed') and payload.readed:
                logging.warning('%s do_DIRECT ????????????????????? "%s %s" ????????????', self.address_string(), self.command, self.url)
                self.close_connection = True
                if not headers_sent:
                    c = message_html('504 ????????????',
                                     '????????????',
                                     '?????? %s ???????????????????????????' % self.url).encode()
                    self.write(b'HTTP/1.1 504 Gateway Timeout\r\n'
                               b'Content-Type: text/html\r\n'
                               b'Content-Length: %d\r\n\r\n' % len(c))
                    self.write(c)
                return
            noerror = True
            response = None
            self.close_connection = self.cc
            try:
                connection_cache_key = '%s:%d' % (hostname, self.port)
                response = http_util.request(self, payload, request_headers, self.bufsize, connection_cache_key)
                if not response:
                    #?????????????????????
                    if retry or self.url_parts.path.endswith('favicon.ico'):
                        logging.warning('%s do_DIRECT "%s %s" ??????????????? 404',
                                        self.address_string(), self.command, self.url)
                        c = '404 ???????????????????????????'.encode()
                        self.write(b'HTTP/1.1 404 Not Found\r\n'
                                   b'Content-Type: text/plain; charset=utf-8\r\n'
                                   b'Content-Length: %d\r\n\r\n' % len(c))
                        self.write(c)
                        return
                    #???????????????????????? IP
                    elif self.target or isdirect(self.host):
                        logging.warning('%s do_DIRECT "%s %s" ??????????????????????????????',
                                        self.address_string(), self.command, self.url)
                        continue
                    else:
                        logging.warning('%s do_DIRECT "%s %s" ????????????????????? "%s" ?????????',
                                        self.address_string(), self.command, self.url, GC.LISTEN_ACT)
                        return self.go_TEMPACT()
                #???????????????????????????
                if response.status >= 400:
                    noerror = False
                #???????????????????????? IP
                if response.status == 403 and not isdirect(self.host):
                    logging.warning('%s do_DIRECT "%s %s" ?????????????????????????????? "%s" ?????????',
                                    self.address_string(response), self.command, self.url, GC.LISTEN_ACT)
                    return self.go_TEMPACT()
                response, data, need_chunked, ws_ok = self.handle_response_headers(response)
                headers_sent = True
                if ws_ok:
                    self.forward_websocket(response.sock)
                else:
                    _, err = self.write_response_content(data, response, need_chunked)
                    if err:
                        raise err
                return
            except CertificateError as e:
                noerror = False
                logging.warning('%s do_DIRECT "%s %s" ??????????????????????????? 522',
                                self.address_string(e), self.command, self.url)
                c = message_html('522 ????????????',
                                 '???????????? %s ????????????' % self.host,
                                 e.args[1]).encode()
                self.write(b'HTTP/1.1 522 Certificate Error\r\n'
                           b'Content-Type: text/html\r\n'
                           b'Content-Length: %d\r\n\r\n' % len(c))
                self.write(c)
                return
            except Exception as e:
                noerror = False
                if self.ws or self.conaborted:
                    raise e
                #????????????
                if e.args[0] in reset_errno:
                    if isdirect(self.host):
                        logging.warning('%s do_DIRECT "%s %s" ???????????????????????????',
                                        self.address_string(e), self.command, self.url)
                        continue
                    else:
                        logging.warning('%s do_DIRECT "%s %s" ?????????????????????????????? "%s" ?????????',
                                        self.address_string(e), self.command, self.url, GC.LISTEN_ACT)
                        return self.go_TEMPACT()
                elif e.args[0] not in bypass_errno:
                    logging.warning('%s do_DIRECT "%s %s" ?????????%r',
                                    self.address_string(response or e), self.command, self.url, e)
                    raise e
            finally:
                if self.ws:
                    return
                if not noerror:
                    self.close_connection = True
                if response:
                    response.close()
                    if noerror:
                        #?????????????????????
                        if self.ssl:
                            if GC.GAE_KEEPALIVE or http_util is not http_gws:
                                http_util.ssl_connection_cache[connection_cache_key].append((time(), response.sock))
                            else:
                                #?????????????????????????????? google ??????
                                response.sock.close()
                        else:
                            response.sock.used = None
                            http_util.tcp_connection_cache[connection_cache_key].append((time(), response.sock))
                    else:
                        response.sock.close()

    def fake_OPTIONS(self, request_headers):
        response = [
            'HTTP/1.1 200 OK',
            'Access-Control-Allow-Credentials: true',
            'Access-Control-Allow-Methods: GET, POST, HEAD, PUT, DELETE, OPTIONS, PATCH',
            'Access-Control-Expose-Headers: Content-Encoding, Content-Length, Date, Server, Vary, X-Google-GFE-Backend-Request-Cost, X-FB-Debug, X-Loader-Length',
            'Access-Control-Max-Age: 1728000',
            'Vary: Origin, X-Origin',
            'Content-Length: 0'
        ]
        headers = request_headers.get('Access-Control-Request-Headers', 'Authorization, If-Modified-Since')
        response.append('Access-Control-Allow-Headers: ' + headers)
        origin = request_headers.get('Origin', '*')
        response.append('Access-Control-Allow-Origin: ' + origin)
        response.append('\r\n')
        self.write('\r\n'.join(response))
        logging.info('%s "%s FAKEOPTIONS %s HTTP/1.1" 200 0',
                     self.address_string(), self.action[3:], self.url)

    def do_CFW(self):
        request_headers, payload = self.handle_request_headers()
        headers_sent = False
        if self.target and '@follow' in self.target:
            options = {'redirect': 'true'}
        else:
            options = None
        for retry in range(GC.CFW_FETCHMAX):
            if retry > 0 and payload and isinstance(payload, bytes) or hasattr(payload, 'readed') and payload.readed:
                logging.warning('%s do_CFW ????????????????????? "%s %s" ????????????',
                                self.address_string(), self.command, self.url)
                self.close_connection = True
                if not headers_sent:
                    c = message_html('504 ????????????',
                                     '????????????',
                                     '?????? %s ???????????????????????????' % self.url).encode()
                    self.write(b'HTTP/1.1 504 Gateway Timeout\r\n'
                               b'Content-Type: text/html\r\n'
                               b'Content-Length: %d\r\n\r\n' % len(c))
                    self.write(c)
                return
            noerror = True
            response = None
            self.close_connection = self.cc
            try:
                response = cfw_fetch(self.command, self.host, self.url, request_headers, payload, options)
                if not response:
                    continue
                response, data, need_chunked, ws_ok = self.handle_response_headers(response)
                headers_sent = True
                if ws_ok:
                    self.forward_websocket(response.sock)
                else:
                    _, err = self.write_response_content(data, response, need_chunked)
                    if err:
                        raise err
                return
            except Exception as e:
                noerror = False
                if self.ws or self.conaborted:
                    raise e
                if e.args[0] not in bypass_errno:
                    logging.warning('%s do_CFW "%s %s" ?????????%r',
                                    self.address_string(response or e), self.command, self.url, e)
                    raise e
            finally:
                if self.ws:
                    return
                if not noerror:
                    self.close_connection = True
                if response:
                    response.close()
                    if noerror and GC.CFW_KEEPALIVE:
                        response.http_util.ssl_connection_cache[response.connection_cache_key].append((time(), response.sock))
                    else:
                        response.sock.close()

    def do_GAE(self):
        #??????????????? GAE ??????
        if self.command not in self.gae_fetcmds:
            logging.warning('%s GAE ????????? "%s %s"????????? DIRECT???',
                            self.address_string(), self.command, self.url)
            self.action = 'do_DIRECT'
            self.target = None
            return self.do_action()
        url_parts = self.url_parts
        request_headers, payload = self.handle_request_headers()
        if self.command == 'OPTIONS':
            return self.fake_OPTIONS(request_headers)
        #??????????????? range ?????????
        need_autorange = self.command != 'HEAD' and \
                         'range=' not in url_parts.query and \
                         'range/' not in self.path and \
                         'live=1' not in url_parts.query
        self.range_end = range_end = range_start = 0
        if need_autorange:
            #??????????????????
            need_autorange = 1 if url_parts.path.endswith(GC.AUTORANGE_FAST_ENDSWITH) else 0
            request_range = request_headers.get('Range')
            if request_range is not None:
                request_range = getbytes(request_range)
                if request_range:
                    range_start, range_end, range_other = request_range.group(1, 2, 3)
                    if not range_start or range_other:
                        # autorange ???????????????????????????????????????????????????
                        range_start = 0
                        need_autorange = 0
                    else:
                        range_start = int(range_start)
                        if range_end:
                            self.range_end = range_end = int(range_end)
                            range_length = range_end + 1 - range_start
                            #???????????????????????????????????????
                            if need_autorange is 1:
                                if range_length < self.rangesize:
                                    need_autorange = -1
                            else:
                                need_autorange = 2 if range_length > GC.AUTORANGE_BIG_ONSIZE else -1
                        else:
                            self.range_end = range_end = 0
                            #if need_autorange is 0:
                            #    #??? autorange/fast ??????
                            #    need_autorange = 2
            if need_autorange is 1:
                logging.info('??????[autorange/fast]?????????%r', self.url)
                range_end = range_start + GC.AUTORANGE_FAST_FIRSTSIZE - 1
            elif need_autorange is 2:
                logging.info('??????[autorange/big]?????????%r', self.url)
                range_end = range_start + GC.AUTORANGE_BIG_MAXSIZE - 1
            if need_autorange > 0:
                request_headers['Range'] = 'bytes=%d-%d' % (range_start, range_end)
        else:
            need_autorange = -1
        errors = []
        headers_sent = False
        need_chunked = False
        start = range_start
        end = ''
        accept_ranges = None
        last_response = None
        for retry in range(GC.GAE_FETCHMAX):
            if retry > 0 and payload:
                logging.warning('%s do_GAE ????????????????????? "%s %s" ????????????',
                                self.address_string(last_response), self.command, self.url)
                self.close_connection = True
                return
            noerror = True
            data = None
            response = None
            self.close_connection = self.cc
            try:
                response = gae_urlfetch(self.command, self.url, request_headers, payload)
                last_response = response or last_response
                if response is None:
                    if retry < GC.GAE_FETCHMAX - 1:
                        logging.warning('%s do_GAE ?????????url=%r?????????',
                                        self.address_string(), self.url)
                        sleep(0.5)
                    continue
                appid = response.appid
                #?????? GoProxy ????????????
                if response.reason == 'debug error':
                    app_msg = response.app_msg
                    #????????????
                    if response.app_status == 403:
                        logging.warning('GAE???%r ??????????????????????????????????????? %r',
                                        appid, GC.GAE_PASSWORD)
                        app_msg = ('<h1>******   GAE???%r ????????????????????????????????????******</h1>'
                                   % appid).encode()
                    # GoProxy ?????????????????????
                    elif response.app_status == 502:
                        if b'DEADLINE_EXCEEDED' in app_msg:
                            logging.warning('GAE???%r urlfetch %r ?????? DEADLINE_EXCEEDED?????????',
                                            appid, self.url)
                            continue
                        elif b'ver quota' in app_msg:
                            logging.warning('GAE???%r urlfetch %r ?????? over quota?????????',
                                            appid, self.url)
                            mark_badappid(appid, 60)
                            continue
                        elif b'urlfetch: CLOSED' in app_msg:
                            logging.warning('GAE???%r urlfetch %r ?????? urlfetch: CLOSED?????????',
                                            appid, self.url)
                            sleep(0.5)
                            continue
                        elif b'RESPONSE_TOO_LARGE' in app_msg:
                            logging.warning('GAE???%r urlfetch %r ?????? urlfetch: RESPONSE_TOO_LARGE????????????????????? Range???',
                                            appid, self.url)
                    # GoProxy ??????????????????????????????
                    elif response.app_status == 400:
                        logging.error('%r ?????????????????? GotoX ???????????? GoProxy ??????????????????????????????????????????????????????????????????????????????', appid)
                        app_msg = ('<h2>AppID???%r ?????????????????? GotoX ???????????? GAE ??????????????????????????????????????????????????????????????????????????????<h2>\n'
                                   '???????????????\n' % appid).encode() + app_msg
                    make_errinfo(response, app_msg)
                #???????????????Bad Gateway???Gateway Timeout???
                elif response.app_status in (502, 504):
                    logging.warning('%s do_GAE ???????????????appid=%r???url=%r?????????',
                                    self.address_string(response), appid, self.url)
                    noerror = False
                    sleep(0.5)
                    continue
                #???????????? GAE ?????????Moved Permanently???Found???Forbidden???Method Not Allowed???
                elif response.app_status in (301, 302, 403, 405):
                    noerror = False
                    continue
                #?????? appid ????????????(Service Unavailable)
                elif response.app_status == 503:
                    mark_badappid(appid)
                    self.do_GAE()
                    return
                #??????????????????Internal Server Error???
                elif response.app_status == 500:
                    logging.warning('"%s %s" GAE_APP ?????????????????????',
                                    self.command, self.url)
                    noerror = False
                    continue
                #?????????????????????Bad Request???Unsupported Media Type???
                elif response.app_status in (400, 415):
                    logging.error('%r ?????????????????? GotoX ????????????????????????????????????????????????????????????????????????????????????', appid)
                # appid ????????????Not Found???
                elif response.app_status == 404:
                    if check_appid_exists(appid):
                        continue
                    elif len(GC.GAE_APPIDS) > 1:
                        mark_badappid(appid, remove=True)
                        logging.error('APPID %r ????????????????????????', appid)
                        self.do_GAE()
                    else:
                        logging.error('APPID %r ???????????????????????? APPID ?????? Config.ini ???', appid)
                        if headers_sent:
                            self.close_connection = True
                        else:
                            c = message_html('404 AppID ?????????',
                                             'AppID %r ?????????' % appid,
                                             '????????? %r ?????????????????? AppID ????????????????????? GotoX???' % GC.CONFIG_FILENAME).encode()
                            self.write(b'HTTP/1.1 502 Service Unavailable\r\n'
                                       b'Content-Type: text/html\r\n'
                                       b'Content-Length: %d\r\n\r\n' % len(c))
                            self.write(c)
                    headers_sent = True
                    noerror = False
                    return
                content_length = response.length or 0
                #????????????????????????????????????
                if response.app_status != 200:
                    if not headers_sent:
                        response, data, need_chunked, _ = self.handle_response_headers(response)
                        self.write_response_content(data, response, need_chunked)
                    return
                #??????????????????????????????????????? read ????????????
                content_range = response.headers.get('Content-Range')
                accept_ranges = response.headers.get('Accept-Ranges')
                if content_range:
                    #???????????????????????????Requested Range Not Satisfiable???
                    if response.status != 416:
                        content_range = getrange(content_range)
                        if content_range:
                            start, end, length = content_range.group(1, 2, 3)
                            start = int(start)
                            end = int(end)
                            #??????????????????????????? autorange
                            if length == '*':
                                need_autorange = 0
                            elif need_autorange is 0:
                                if (    #???????????????????????????????????????????????????????????????????????????
                                        (end != range_end and end - start == GC.GAE_MAXSIZE)
                                        #????????????????????????????????? autorange
                                        or (content_length > GC.AUTORANGE_BIG_ONSIZE)):
                                    logging.info('??????[autorange/big]?????????%r', self.url)
                                    need_autorange = 2
                elif (  #??????????????????????????????????????????
                        (headers_sent and start > 0) 
                        #?????????????????? Range ?????????????????????????????????????????????????????????
                        or (range_start > 0 and response.status < 300)):
                    self.close_connection = True
                    return
                elif need_autorange is 0 and \
                        accept_ranges == 'bytes' and \
                        content_length > GC.AUTORANGE_BIG_ONSIZE:
                    #????????????????????????????????? autorange
                    logging.info('??????[autorange/big]?????????%r', self.url)
                    response.status = 206
                    need_autorange = 2
                #??????????????????????????????????????????
                if not headers_sent:
                    #????????????????????????Partial Content???
                    if response.status == 206 and need_autorange > 0:
                        rangefetch = RangeFetchs[need_autorange](self, request_headers, payload, response)
                        response = None
                        return rangefetch.fetch()
                    response, data, need_chunked, _ = self.handle_response_headers(response)
                    headers_sent = True
                wrote, err = self.write_response_content(data, response, need_chunked)
                start += wrote
                if err:
                    raise err
                return
            except Exception as e:
                noerror = False
                if self.conaborted:
                    raise e
                errors.append(e)
                if not isinstance(e, LimiterFull) and (
                        e.args[0] in closed_errno or
                        (isinstance(e, NetWorkIOError) and len(e.args) > 1 and 'bad write' in e.args[1]) or
                        (isinstance(e.args[0], list) and any('bad write' in arg for arg in e.args[0][0]))):
                    #??????????????????
                    logging.debug('%s do_GAE %r ?????? %r?????????',
                                  self.address_string(response or e), self.url, e)
                    self.close_connection = True
                    return
                elif retry < GC.GAE_FETCHMAX - 1:
                    if accept_ranges == 'bytes':
                        #???????????? Range ???????????????
                        if start > 0:
                            request_headers['Range'] = 'bytes=%d-%s' % (start, end)
                    elif start > 0:
                        #??????????????? Range ???????????????????????????
                        logging.error('%s do_GAE "%s %s" ?????????%r',
                                      self.address_string(response or e), self.command, self.url, e)
                        self.close_connection = True
                        return
                    logging.warning('%s do_GAE "%s %s" ?????????%r?????????',
                                    self.address_string(response or e), self.command, self.url, e)
                else:
                    #????????????
                    logging.exception('%s do_GAE "%s %s" ?????????%r',
                                      self.address_string(response or e), self.command, self.url, e)
                    self.close_connection = True
            finally:
                if retry == GC.GAE_FETCHMAX - 1 and not headers_sent:
                    if last_response:
                        errors.append(last_response.read().decode())
                        c = message_html('502 ??????????????????',
                                         '????????? GAE ?????? %s ??????' % self.url,
                                         str(errors)).encode()
                        self.write(b'HTTP/1.1 502 Service Unavailable\r\n'
                                   b'Content-Type: text/html\r\n'
                                   b'Content-Length: %d\r\n\r\n' % len(c))
                    else:
                        if retry > 0 and payload:
                            b = '?????????????????? GAE-%r ???????????????????????????'
                        else:
                            b = 'GAE-%r ?????????????????????????????????'
                        c = message_html('504 GAE ????????????',
                                         b % self.url,
                                         str(errors)).encode()
                        self.write(b'HTTP/1.1 504 Gateway Timeout\r\n'
                                   b'Content-Type: text/html\r\n'
                                   b'Content-Length: %d\r\n\r\n' % len(c))
                    self.write(c)
                if response:
                    response.close()
                    if noerror and GC.GAE_KEEPALIVE:
                        #?????????????????????
                        response.http_util.ssl_connection_cache[response.connection_cache_key].append((time(), response.sock))
                    else:
                        #??????????????????????????????
                        response.sock.close()

    #????????? CFWorker
    if not GC.CFW_WORKER:
        def do_GFW(self):
            noworker = '????????? %r ???????????????????????? CFWorker ????????? [cfw] ?????????????????? GotoX???' % GC.CONFIG_FILENAME
            logging.critical(noworker)
            c = message_html('502 CFWorker ????????????',
                             'CFWorker ????????????????????????????????? CFW ??????',
                             noworker).encode()
            self.write(b'HTTP/1.1 502 Service Unavailable\r\n'
                       b'Content-Type: text/html\r\n'
                       b'Content-Length: %d\r\n\r\n' % len(c))
            self.write(c)
            return

    #????????? AppID
    if not GC.GAE_APPIDS:
        def do_GAE(self):
            noappid = '????????? %r ???????????????????????? AppID ??? [gae] ?????????????????? GotoX???' % GC.CONFIG_FILENAME
            logging.critical(noappid)
            c = message_html('502 AppID ??????',
                             'AppID ??????????????????????????? GAE ??????',
                             noappid).encode()
            self.write(b'HTTP/1.1 502 Service Unavailable\r\n'
                       b'Content-Type: text/html\r\n'
                       b'Content-Length: %d\r\n\r\n' % len(c))
            self.write(c)
            return

    def do_FORWARD(self):
        #?????????????????????
        hostname = self.hostname
        http_util = http_gws if hostname.startswith('google') else http_nor
        host, port = self.host, self.port
        hostip = None
        remote = None
        connection_cache_key = '%s:%d' % (hostname, port)
        if self.fakecert:
            create_connection = http_util.create_ssl_connection
        else:
            create_connection = http_util.create_connection
        for _ in range(2):
            limited = None
            try:
                if not GC.PROXY_ENABLE:
                    remote = create_connection((host, port), hostname, connection_cache_key, ssl=self.ssl, forward=self.fwd_timeout)
                else:
                    hostip = random.choice(dns_resolve(host))
                    remote = create_connection((hostip, port), self.ssl, self.fwd_timeout)
                break
            except LimiterFull as e:
                limited = True
                logging.warning('%s ????????? %r ?????????%r',
                                self.address_string(), self.url or host, e)
            except NetWorkIOError as e:
                logging.warning('%s ????????? %r ?????????%r',
                                self.address_string(e), self.url or host, e)
        if remote is None:
            if not limited and not isdirect(host):
                if self.command == 'CONNECT':
                    logging.warning('%s%s do_FORWARD ?????????????????? (%r, %r) ????????????????????? "FAKECERT & %s" ?????????',
                                    self.address_string(), hostip or '', host, port, GC.LISTEN_ACT)
                    self.go_FAKECERT_TEMPACT()
                elif self.headers.get('Upgrade') == 'websocket':
                    logging.warning('%s%s do_FORWARD websocket ?????????????????? (%r, %r) ?????????',
                                    self.address_string(), hostip or '', host, port)
                else:
                    logging.warning('%s%s do_FORWARD ?????????????????? (%r, %r) ????????????????????? %r ?????????',
                                    self.address_string(), hostip or '', host, port, GC.LISTEN_ACT)
                    self.go_TEMPACT()
            return
        remote.settimeout(self.fwd_timeout)
        if self.command == 'CONNECT':
            logging.info('%s "FWD %s %s:%d HTTP/1.1" - -',
                         self.address_string(remote), self.command, host, port)
        else:
            logging.info('%s "FWD %s %s HTTP/1.1" - -',
                         self.address_string(remote), self.command, self.url)
        self.forward_connect(remote)

    def do_PROXY(self):
        #?????????????????????
        proxytype, proxyuser, proxypass, proxyaddress = parse_proxy(self.target)
        proxyhost, _, proxyport = proxyaddress.rpartition(':')
        ips = dns_resolve(proxyhost).copy()
        if ips:
            ipcnt = len(ips) 
        else:
            logging.error('%s ???????????????????????????%s',
                          self.address_string(), self.target)
            return
        if ipcnt > 1:
            #????????????????????? IP??????????????????????????????
            ips.sort(key=lambda ip: self.proxy_connection_time.get(ip, 0))
        proxyport = int(proxyport)
        while ips:
            proxyip = ips.pop(0)
            rdns = self.target not in proxy_no_rdns
            host = self.host if rdns else dns_resolve(self.host)[0]
            if proxytype:
                proxytype = proxytype.upper()
            if proxytype not in socks.PROXY_TYPES:
                proxytype = 'HTTP'
            proxy_sock = http_nor.get_proxy_socket(proxyip, 8)
            proxy_sock.set_proxy(socks.PROXY_TYPES[proxytype], proxyip, proxyport, rdns, proxyuser, proxypass)
            if ipcnt > 1:
                start_time = time()
            try:
                if self.fakecert:
                    proxy_sock = http_nor.get_ssl_socket(proxy_sock, None if isip(self.host) else self.host.encode())
                proxy_sock.connect((host, self.port))
                if self.fakecert:
                    proxy_sock.do_handshake()
            except Exception as e:
                if rdns and '0x5b' in str(e) and not isip(host):
                    proxy_no_rdns.add(self.target)
                    ips.insert(0, proxyip)
                else:
                    if ipcnt > 1:
                        self.proxy_connection_time[proxyip] = self.fwd_timeout + 1 + random.random()
                    logging.error('%s%s:%d ?????? "%s %s" ??? [%s] ???????????????%s',
                                  self.address_string(), proxyip, proxyport, self.command, self.url or self.path, proxytype, self.target)
                continue
            else:
                if ipcnt > 1:
                    self.proxy_connection_time[proxyip] = time() - start_time
            logging.info('%s%s:%d ?????? "%s %s" ??? [%s] ?????????%s',
                         self.address_string(), proxyip, proxyport, self.command, self.url or self.path, proxytype, self.target)
            proxy_sock.xip = proxyip, proxyport
            self.forward_connect(proxy_sock)

    def do_REDIRECT(self):
        #????????????????????????
        self.close_connection = False
        target, _ = self.target
        logging.info('%s ????????? %r ??? %r',
                     self.address_string(), self.url, target)
        self.write('HTTP/1.1 301 Moved Permanently\r\n'
                   'Location: %s\r\n'
                   'Content-Length: 0\r\n\r\n' % target)

    def do_IREDIRECT(self):
        #????????????????????????????????????
        target, (mhost, raction) = self.target
        if target.startswith('file://'):
            filename = target.lstrip('file:').lstrip('/')
            logging.info('%s %r ?????????????????? %r',
                         self.address_string(), self.url, filename)
            self.do_LOCAL(filename)
        else:
            logging.info('%s ??????????????? %r ??? %r',
                         self.address_string(), self.url, target)
            #????????????
            origurl = self.url
            self.url = url = target
            #????????????
            origssl = self.ssl
            self.url_parts = url_parts = urlparse.urlsplit(target)
            self.ssl = url_parts.scheme == 'https'
            #?????????????????????
            origport = self.port
            self.parse_host(None, url_parts.netloc, mhost)
            #????????????????????????????????????????????????????????????
            if origport not in (80, 443) and self.port in (80, 443):
                self.ssl = origssl
                self.port = origport
                scheme = 'https' if origssl else 'http'
                netloc = '%s:%d' % (self.host, origport)
                self.url_parts = url_parts = urlparse.SplitResult(scheme, netloc, url_parts.path, url_parts.query, '')
                self.url = url = url_parts.geturl()
                logging.warning('%s ?????? %r ?????????????????????????????????????????????????????????????????????????????????????????? %r',
                                self.address_string(), origurl, url)
            #????????????
            self.path = target[target.find('/', target.find('//')+3):]
            #?????? action
            if raction:
                if isinstance(raction, str):
                    self.action, self.target = raction, None
                else:
                    self.action, self.target = raction
            else:
                self.action, self.target = get_action(url_parts.scheme, self.host, self.path[1:], url)
            self.do_action()

    def do_FAKECERT(self):
        #?????????????????????????????????????????????
        if not self.ssl:
            self.close_connection = False
            return
        if self.ssl_request:
            #???????????? MSG_PEEK ???????????????<<????????????>>??????????????????
            #?????????????????????????????????????????????????????????????????????
            p1, p2 = socket.socketpair()
            payload = self.connection.recv(65536)
            start_new_thread(forward_socket, (self.connection, p1, payload))
            self.connection = p2
            self.disable_nagle_algorithm = False
            logging.warning('%s ???????????? https ?????????????????? https ?????????host=%r???????????? https ???????????????????????????????????? http',
                            self.address_string(), self.host)
        context = self.get_context()
        try:
            ssl_sock = SSLConnection(context, self.connection)
            ssl_sock.do_handshake_server_side()
            self.fakecert = True
        except Exception as e:
            if not e.args or e.args[0] not in bypass_errno:
                logging.exception('%s ???????????????????????????host=%r???%r',
                                  self.address_string(), self.host, e)
            return
        #?????????????????????
        self.finish()
        #?????????????????????
        self.request = ssl_sock
        self.setup()
        try:
            #????????????????????????
            self.handle()
        finally:
            #?????????????????????????????????????????????????????? 2 ??? makefile
            ssl_sock.close()

    def list_dir(self, path, displaypath):
        #?????????????????????????????? html
        #?????? http.server.SimpleHTTPRequestHandler.list_directory
        #???????????? UTF-8 ??????
        try:
            namelist = os.listdir(path)
        except OSError as e:
            return e
        namelist.sort(key=lambda a: a.lower())
        r = []
        displaypath = html.escape(displaypath)
        title = 'GotoX web ???????????? - %s' % displaypath
        r.append('<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01//EN" '
                 '"http://www.w3.org/TR/html4/strict.dtd">\n'
                 '<html>\n<head>\n'
                 '<meta http-equiv="Content-Type" '
                 'content="text/html; charset=utf-8">\n'
                 '<title>%s</title>\n'
                 '</head>\n<body>' % title)
        if displaypath == '/':
            r.append('<h2>\n'
                     '&diams;<a href="%s">???????????? GotoX CA ??????????????????</a>\n'
                     '&diams;<a href="%s">???????????? GotoX CA ??????</a>\n'
                     '</h2>\n<hr>' % self.CAPath)
        r.append('<h1>%s</h1>\n<hr>\n<ul>' % title)
        if displaypath != '/':
            r.append('<li><a href="%s/">??????????????????</a><big>&crarr;</big></li>'
                     % displaypath[:-1].rpartition('/')[0])
        for name in namelist:
            fullname = os.path.join(path, name)
            displayname = linkname = name
            # Append / for directories or @ for symbolic links
            if os.path.isdir(fullname):
                displayname = name + "/"
                linkname = name + "/"
            if os.path.islink(fullname):
                displayname = name + "@"
                # Note: a link to a directory displays with @ and links with /
            r.append('<li><a href="%s">%s</a></li>'
                     % (urlparse.quote(linkname, errors='surrogatepass'),
                        html.escape(displayname)))
        r.append('</ul>\n<hr>\n</body>\n</html>\n')
        content = '\n'.join(r).encode(errors='surrogateescape')
        l = len(content)
        self.write('HTTP/1.1 200 Ok\r\n'
                   'Content-Length: %d\r\n'
                   'Content-Type: text/html; charset=utf-8\r\n\r\n' % l)
        self.write(content)
        return l

    guess_type = SimpleHTTPRequestHandler.guess_type
    extensions_map = SimpleHTTPRequestHandler.extensions_map
    extensions_map.update({
        '.ass' : 'text/plain',
        '.flac': 'audio/flac',
        '.mkv' : 'video/mkv',
        '.pac' : 'text/plain',
        })

    def do_LOCAL(self, filename=None):
        #????????????
        if self.path.lower() in self.CAPath:
            return self.send_CA()
        #?????? GotoX ??????
        elif self.url_parts.path == '/docmd':
            return self.do_CMD()
        #?????????????????????????????????
        self.close_connection = False
        path = urlparse.unquote(self.path)
        if filename:
            filename = urlparse.unquote(filename)
        else:
            filename = os.path.join(web_dir, path[1:])
        #????????? web_dir ?????????
        if filename.startswith(web_dir) and os.path.isdir(filename):
            r = self.list_dir(filename, path)
            if isinstance(r, int):
                logging.info('%s "%s %s HTTP/1.1" 200 %s',
                             self.address_string(), self.command, self.url, r)
            else:
                logging.info('%s "%s %s HTTP/1.1" 403 -??????????????????????????????%r',
                             self.address_string(), self.command, self.url, r)
            return
        #??????????????????
        if os.path.isfile(filename):
            content_type = self.guess_type(filename)
            try:
                filesize = os.path.getsize(filename)
                with open(filename, 'rb') as fp:
                    data = fp.read(1048576) # 1M
                    logging.info('%s "%s %s HTTP/1.1" 200 %d',
                                 self.address_string(), self.command, self.url, filesize)
                    self.write('HTTP/1.1 200 Ok\r\n'
                               'Content-Length: %d\r\n'
                               'Content-Type: %s\r\n\r\n'
                               % (filesize, content_type))
                    while data:
                        self.write(data, True)
                        data = fp.read(1048576)
            except Exception as e:
                logging.warning('%s "%s %s HTTP/1.1" 403 -??????????????????????????????%r',
                                self.address_string(), self.command, self.url, filename)
                c = ('<title>403 ??????</title>\n'
                     '<h1>403 ???????????????????????????</h1><hr>\n'
                     '<h2><li>%s</li></h2>\n'
                     '<h2><li>%s</li></h2>\n'
                     % (filename, e)).encode()
                self.write('HTTP/1.1 403 Forbidden\r\n'
                           'Content-Type: text/html; charset=utf-8\r\n'
                           'Content-Length: %d\r\n\r\n' % len(c))
                self.write(c)
        else:
            logging.warning('%s "%s %s HTTP/1.1" 404 -??????????????????????????????%r',
                            self.address_string(), self.command, self.url, filename)
            c = ('<title>404 ????????????</title>\n'
                 '<h1>404 ???????????????????????????</h1><hr>\n'
                 '<h2><li>%s</li></h2>\n' % filename).encode()
            self.write('HTTP/1.1 404 Not Found\r\n'
                       'Content-Type: text/html; charset=utf-8\r\n'
                       'Content-Length: %d\r\n\r\n' % len(c))
            self.write(c)

    def do_BLOCK(self):
        #??????????????????
        self.close_connection = False
        self.write(b'HTTP/1.1 200 Ok\r\n'
                   b'Cache-Control: max-age=86400\r\n'
                   b'Expires:Oct, 01 Aug 2100 00:00:00 GMT\r\n')
        if self.url_parts and \
                self.url_parts.path.endswith(('.jpg', '.gif', '.jpeg', '.png', '.bmp')):
            content = (b'GIF89a\x01\x00\x01\x00\x80\xff\x00\xc0\xc0\xc0'
                       b'\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,\x00'
                       b'\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;')
            self.write(b'Content-Type: image/gif\r\n'
                       b'Content-Length: %d\r\n\r\n' % len(content))
            self.write(content)
        else:
            self.write(b'Content-Length: 0\r\n\r\n')
        logging.warning('%s "%s %s" ???????????????',
                        self.address_string(), self.command, self.url or self.host)

    def _set_temp_ACT(self):
        host = 'http%s://%s' % ('s' if self.ssl else '', self.host)
        #???????????????????????????????????????????????????
        try:
            f = self.badhost[host] & 12
            if f == 0:
                self.badhost[host] |= 4
            elif f == 4:
                if set_temp_action(host):
                    logging.warning('??? %r ?????? %r ??????%s???',
                                    host, GC.LISTEN_ACT, GC.LINK_TEMPTIME_S)
                self.badhost[host] |= 8
        except KeyError:
            self.badhost[host] = 4

    def _set_temp_FAKECERT(self):
        host = 'http%s://%s' % ('s' if self.ssl else '', self.host)
        #???????????????????????????????????????????????????
        try:
            f = self.badhost[host] & 3
            if f == 0:
                self.badhost[host] |= 1
            elif f == 1:
                if set_temp_connect_action(host):
                    logging.warning('??? %r ?????? "FAKECERT" ??????%s???',
                                    host, GC.LINK_TEMPTIME_S)
                self.badhost[host] |= 2
        except KeyError:
            self.badhost[host] = 1

    def go_TEMPACT(self):
        if GC.LISTEN_ACT == 'GAE' and self.command not in self.gae_fetcmds:
            return self.go_BAD()
        self._set_temp_ACT()
        self.action = GC.LISTEN_ACTNAME
        self.do_action()

    def go_FAKECERT(self):
        self._set_temp_FAKECERT()
        self.action = 'do_FAKECERT'
        self.do_action()

    def go_FAKECERT_TEMPACT(self):
        self.path = '/'
        self._set_temp_ACT()
        self._set_temp_ACT()
        self.go_FAKECERT()

    def go_BAD(self):
        self.close_connection = False
        logging.warning('%s request "%s %s" ??????, ?????? 404',
                        self.address_string(), self.command, self.url)
        c = message_html('404 ????????????',
                         '????????????',
                         '?????? "%s %s"<p>??????????????? %s ?????? DIRECT ?????????????????????'
                         % (self.command, GC.LISTEN_ACT, self.url)).encode()
        self.write(b'HTTP/1.0 404\r\n'
                   b'Content-Type: text/html\r\n'
                   b'Content-Length: %d\r\n\r\n' % len(c))
        self.write(c)

    def forward_websocket(self, remote, timeout=108):
        #??????  ping-pong 54
        logging.info('%s ?????? "%s %s %s"',
                     self.address_string(remote), self.action[3:], self.command, self.url)
        try:
            forward_socket(self.connection, remote, timeout=timeout, bufsize=32768)
        except NetWorkIOError as e:
            if e.args[0] not in bypass_errno:
                logging.warning('%s ?????? "%s" ?????????%r',
                                self.address_string(remote), self.url, e)
                raise
        finally:
            logging.debug('%s ???????????????"%s"',
                          self.address_string(remote), self.url)
            self.close_connection = True

    def forward_connect(self, remote, timeout=0, tick=4, bufsize=32768, maxping=None, maxpong=None):
        #?????????????????????????????????????????????
        payload = None
        if self.command != 'CONNECT':
            request_data = []
            #???????????????????????????????????????????????????
            #request_data.append(self.requestline)
            request_data.append('%s %s %s'
                                % (self.command, self.path, self.protocol_version))
            for k, v in self.headers.items():
                if not k.title().startswith('Proxy-'):
                    request_data.append('%s: %s' % (k.title(), v))
            request_data.append('\r\n')
            rebuilt_request = '\r\n'.join(request_data).encode()
            _, payload = self.handle_request_headers()
            if isinstance(payload, bytes) and payload:
                payload = rebuilt_request + payload
            else:
                payload = rebuilt_request
        elif self.ssl_request:
            #???????????? MSG_PEEK ???????????????<<????????????>>
            # select ??????????????????????????????????????????????????????
            payload = self.connection.recv(65536)
        try:
            forward_socket(self.connection, remote, payload, timeout or self.fwd_keeptime, tick, bufsize, maxping, maxpong)
        except NetWorkIOError as e:
            if e.args[0] not in bypass_errno:
                logging.warning('%s ?????? "%s" ?????????%r',
                                self.address_string(remote), self.url or self.host, e)
                raise
        finally:
            logging.debug('%s ???????????????"%s"',
                          self.address_string(remote), self.url or self.host)
            #???????????????????????????????????????????????????????????????????????????????????????????????????
            self.close_connection = True

    @_lock_context
    def get_context(self, servername=None, callback=lambda *x: 1):
        #???????????? ssl context ??????
        host = servername or self.host
        ip = isip(host)
        if not ip:
            hostsp = host.split('.')
            #??????????????????????????????????????????????????????
            #??????com.cn ???????????????????????????????????????????????????????????????
            if len(hostsp) > 2:
                host = '.'.join(hostsp[1:])
        try:
            return self.context_cache[host]
        except KeyError:
            certfile = cert.get_cert(host, ip)
            self.context_cache[host] = context = SSL.Context(GC.LINK_LOCALSSL)
            #???????????? TLS ?????? SSLv3 ???????????????
            if GC.LINK_LOCALSSL == SSL.SSLv23_METHOD:
                context.set_options(SSL.OP_NO_SSLv2)
                context.set_options(SSL.OP_NO_SSLv3)
            #???????????????
            context.set_options(SSL.OP_NO_COMPRESSION)
            #??????????????????
            context.set_options(SSL.OP_ALL)
            #?????????
            context.use_privatekey_file(cert.sub_keyfile)
            context.use_certificate_file(certfile)
            #??????????????????
            context.set_verify(SSL.VERIFY_NONE, callback)
            #????????????
            context.set_cipher_list(res_ciphers)
            context.set_options(SSL.OP_CIPHER_SERVER_PREFERENCE)
            #????????????
            context.set_session_id(os.urandom(16))
            context.set_session_cache_mode(SSL.SESS_CACHE_SERVER)
            return context

    def send_CA(self):
        #?????? CA ??????
        with open(cert.ca_certfile, 'rb') as fp:
            data = fp.read()
        logging.info('"%s HTTP/1.1 200"????????? CA ????????? %r',
                     self.url, self.address_string())
        self.close_connection = False
        self.write(b'HTTP/1.1 200 Ok\r\n'
                   b'Content-Type: application/x-x509-ca-cert\r\n')
        if self.path.lower() == self.CAPath[1]:
            self.write(b'Content-Disposition: attachment; filename="GotoXCA.crt"\r\n')
        self.write('Content-Length: %d\r\n\r\n' % len(data))
        self.write(data)

    def do_CMD(self):
        exit = None
        reqs = urlparse.parse_qs(self.url_parts.query)
        cmd = reqs['cmd'][0] #????????????????????????
        if cmd == 'reset_dns':
            #?????? DNS
            reset_dns()
        elif cmd == 'reset_autorule':
            #??????????????????
            action_filters.reset = True
        elif cmd in ('quit', 'exit', 'off', 'close', 'shutdown'):
            #????????????
            exit = True
        self.close_connection = False
        self.write('HTTP/1.1 204 No Content\r\n'
                   'Content-Length: 0\r\n\r\n')
        logging.warning('%s "%s %s HTTP/1.1" 204 0???GotoX ?????? [%s] ???????????????',
                        self.address_string(), self.command, self.url, cmd)
        if exit:
            sys.exit(0)

    def log_error(self, format, *args):
        self.close_connection = True
        logging.error('%s "%s %s %s" ?????????' + format,
                      self.address_string(), self.action[3:], self.command, self.url or self.host, *args)

    def address_string(self, response=None):
        #??????????????????????????????
        if not hasattr(self, 'address_str'):
            client_ip, client_port = self.client_address[0:2]
            if client_ip.endswith('127.0.0.1'):
                client_ip = 'L4'
            elif client_ip == '::1':
                client_ip = 'L6'
            self.address_str = '%s:%s->' % (client_ip, client_port)
        if not hasattr(response, 'xip'):
            return self.address_str
        xip0, xip1 = response.xip
        if isipv6(xip0):
            xip0 = '[%s]' % xip0
        if xip1 in (80, 443):
            return '%s%s' % (self.address_str, xip0)
        else:
            return '%s%s:%s' % (self.address_str, xip0, xip1)

class ACTProxyHandler(AutoProxyHandler):

    def do_CONNECT(self):
        #?????? CONNECT ???????????????????????????????????????
        if self._do_CONNECT():
            return
        self.action = 'do_FAKECERT'
        self.do_action()

    def do_METHOD(self):
        #??????????????????????????????????????????
        if self._do_METHOD():
            return
        self.action = GC.LISTEN_ACTNAME
        action, target = get_action(self.url_parts.scheme, self.host, self.path[1:], self.url)
        if target and action == self.action:
            self.target = target
        self.do_action()

    def go_TEMPACT(self):
        self.go_BAD()
