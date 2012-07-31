from dropbox import rest, client
import urlparse
import socket
import os
import asynchat
import StringIO
import httplib
import urllib
import ssl
import asyncore
import json


class FakeSocket(StringIO.StringIO):
    def makefile(self, *args, **kw):
        return self


class AsyncConnect(asyncore.dispatcher):
    """ Represents an asynchronous http connection """

    def __init__(self, host, port, on_connect):
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.connect((host, port))
        self.ca_certs = rest.TRUSTED_CERT_FILE
        self.cert_reqs = ssl.CERT_REQUIRED
        self.on_connect = on_connect
        self.host = host
        self.port = port

    def handle_connect(self):

        # Do ssl handshake. Not good async implementation for this
        self.socket.setblocking(1)
        self.socket = ssl.wrap_socket(self.socket, cert_reqs=self.cert_reqs, ca_certs=self.ca_certs)
        cert = self.socket.getpeercert()
        hostname = self.host.split(':', 0)[0]
        rest.match_hostname(cert, hostname)

        # Now start the handler
        self.socket.setblocking(0)
        self.handler = AsyncHandler(self.socket)
        self.on_connect(self.handler)


class AsyncHandler(asynchat.async_chat):
    """ Handler for handling asynchronous http requests and responses """

    def __init__(self, conn):
        asynchat.async_chat.__init__(self, conn)
        self.set_terminator('\r\n\r\n')
        self.buffer = []
        self.can_receive = True
        self.response = None

        # Large body buffer size to deal with ssl issue mentioned in readme
        self.ac_in_buffer_size = 16384

    def handle_read(self):
        if self.can_receive:
            try:
                # When response is received, read the headers
                # and the status
                if not self.response:
                    # Need to set blocking for the response to be
                    # able to parse headers
                    self.socket.setblocking(1)
                    self.response = httplib.HTTPResponse(self.socket)
                    self.response.begin()
                    self.response.async_handler = self
                    self.header_callback(self)

                # Continue reading the body in case the callback doesnt expect
                # a raw response
                if self.can_receive:
                    self.socket.setblocking(0)
                    asynchat.async_chat.handle_read(self)

            except ssl.SSLError:
                print "Got ssl error "
                pass

    def collect_incoming_data(self, data):
        self.buffer.append(data)

    def found_terminator(self):
        if not self.response:
            r = ''.join(self.buffer)
            self.buffer = []
            fsocket = FakeSocket(r)
            self.response = httplib.HTTPResponse(fsocket)
            self.response.begin()
            self.header_callback(self)
        else:
            body = ''.join(self.buffer)
            fsocket = FakeSocket(body)
            self.response.fp = fsocket.makefile('rb')
            stringbody = self.response.read()
            body = rest.json_loadb(stringbody)
            self.body_callback(body)
            self.close()

    def stop_receiving(self):
        self.can_receive = False

    def close(self):
        self.socket.close()
        asynchat.async_chat.close(self)


class AsyncRESTClientObject(rest.RESTClientObject):
    """ Implementation of a rest client, which sends requests async """

    def processResponseHeader(self, handler, callback=None, raw_response=False):
        """ Gets the response from a connection (conn). If raw_response is true
        returns the raw HTTPResponse object with the connection open"""

        if handler.response.status != 200:
            handler.close()
            if callback:
                callback(handler.response)
        else:
            if raw_response:
                handler.stop_receiving()
                callback(handler.response)
            else:
                pass

    def get_request(self, method, url, headers):
        """ Returns the raw http request """

        req = '%s %s %s\r\n' % (method, url, 'HTTP/1.1')
        headers['Host'] = urlparse.urlparse(url).hostname
        headers['Accept-Encoding'] = 'identity'
        for header, value in headers.items():
            req += '%s: %s\r\n' % (header, value)
        # store a blank line to indicate end of headers
        req += '\r\n'
        return req

    def request(self, method, url, body=None, headers=None, callback=None, raw_response=False, post_params=None):
        """ Sends a http request asynchronously and calls the callback with the response
        callback should take in one argument which is the HTTPResponse """

        headers = headers or {}
        headers['User-Agent'] = 'ModifiedDropboxPythonSDK'

        if post_params:
            if body:
                raise ValueError("body parameter cannot be used with post_params parameter")
            body = urllib.urlencode(post_params)
            headers["Content-type"] = "application/x-www-form-urlencoded"

        host = urlparse.urlparse(url).hostname
        try:
            if body:
                try:
                    clen = len(body)
                except (TypeError, AttributeError):
                    try:
                        clen = body.len
                    except AttributeError:
                        clen = os.fstat(body.fileno()).st_size

                if clen != None:
                    headers["Content-Length"] = clen

            # get the raw http request
            request = self.get_request(method, url, headers)

            # called when the reponse headers have been read
            def header_cb(handler):
                self.processResponseHeader(handler, callback, raw_response=raw_response)

            # Called when the async tcp handshake has been completed
            def on_connect(handler):
                handler.header_callback = header_cb
                handler.body_callback = callback
                handler.push(request)
                if body:
                    handler.push(body)

            AsyncConnect(host, 443, on_connect)

        except socket.error, e:
            raise rest.RESTSocketError(host, e)
        except rest.CertificateError, e:
            raise rest.RESTSocketError(host, "SSL certificate error: " + e)

    def PUT(self, url, body, headers=None, raw_response=False, callback=None):
        assert type(raw_response) == bool
        return self.request("PUT", url, body=body, headers=headers, callback=callback, raw_response=raw_response)

    def POST(self, url, params=None, headers=None, raw_response=False, callback=None):
        assert type(raw_response) == bool
        if params is None:
            params = {}
        return self.request("POST", url,
                            post_params=params, headers=headers, raw_response=raw_response, callback=callback)

    def GET(self, url, headers=None, raw_response=False, callback=None):
        assert type(raw_response) == bool
        return self.request("GET", url, headers=headers, raw_response=raw_response, callback=callback)


class AsyncRESTClient(rest.RESTClient):
    """ Asynchronous rest client used by the async dropbox client """
    IMPL = AsyncRESTClientObject()


class AsyncDBClient(client.DropboxClient):
    """ Asynchronous version of the dropbox client. All methods require a callback.
    Al methods make an asynchronous request and call the callback when they receive
    a response. The functionality for all methods is the same as the DropboxClient"""

    def __init__(self, session):
        client.DropboxClient.__init__(self, session, rest_client=AsyncRESTClient)

    def put_file(self, full_path, file_obj, overwrite=False, parent_rev=None, callback=None):
        path = "/files_put/%s%s" % (self.session.root, client.format_path(full_path))

        params = {
            'overwrite': bool(overwrite),
            }

        if parent_rev is not None:
            params['parent_rev'] = parent_rev

        url, params, headers = self.request(path, params, method='PUT', content_server=True)

        return self.rest_client.PUT(url, file_obj, headers, callback=callback)

    def metadata(self, path, list=True, file_limit=25000, hash=None, rev=None, include_deleted=False, callback=None):
        path = "/metadata/%s%s" % (self.session.root, client.format_path(path))

        params = {'file_limit': file_limit,
                  'list': 'true',
                  'include_deleted': include_deleted,
                  }

        if not list:
            params['list'] = 'false'
        if hash is not None:
            params['hash'] = hash
        if rev:
            params['rev'] = rev

        url, params, headers = self.request(path, params, method='GET')

        return self.rest_client.GET(url, headers, callback=callback)

    def chunked_upload(self, file_obj, upload_id=None, offset=None, callback=None):
        path = "/chunked_upload"

        if not offset:
            offset = 0

        params = {
            'offset': int(offset),
            }

        if upload_id is not None:
            params['upload_id'] = upload_id

        url, params, headers = self.request(path, params, method='PUT', content_server=True)
        return self.rest_client.PUT(url, file_obj, headers, callback=callback)

    def commit_chunk(self, full_path, upload_id, overwrite=False, parent_rev=None, callback=None):
        path = "/commit_chunked_upload/%s%s" % (self.session.root, client.format_path(full_path))

        params = {
            'overwrite': bool(overwrite),
            'upload_id': str(upload_id)
            }

        if parent_rev is not None:
            params['parent_rev'] = parent_rev

        url, params, headers = self.request(path, params=params, method='POST', content_server=True)
        return self.rest_client.POST(url, params=params, headers=headers, callback=callback)

    def get_file(self, from_path, rev=None, callback=None):
        path = "/files/%s%s" % (self.session.root, client.format_path(from_path))

        params = {}
        if rev is not None:
            params['rev'] = rev

        url, params, headers = self.request(path, params, method='GET', content_server=True)
        return self.rest_client.request("GET", url, headers=headers, raw_response=True, callback=callback)

    def get_file_and_metadata(self, from_path, rev=None, callback=None):
        def cb(response):
            metadata = None
            try:
                metadata = AsyncDBClient.parse_metadata_as_dict(response)
            except:
                pass  # The error handling is done when the response is read
            callback(response, metadata)

        return self.get_file(from_path, rev=rev, callback=cb)

    @staticmethod
    def parse_metadata_as_dict(dropbox_raw_response):
        """Parses file metadata from a raw dropbox HTTP response, raising a
        dropbox.rest.ErrorResponse if parsing fails.
        """
        metadata = None
        for header, header_val in dropbox_raw_response.getheaders():
            if header.lower() == 'x-dropbox-metadata':
                try:
                    metadata = json.loads(header_val)
                except ValueError:
                    raise rest.ErrorResponse(dropbox_raw_response)
        if not metadata:
            raise rest.ErrorResponse(dropbox_raw_response)
        return metadata
