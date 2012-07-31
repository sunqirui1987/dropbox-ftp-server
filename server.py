import asyncore
import asynchat
import socket
import pprint
from dropbox import session
import os
import async_rest
from optparse import OptionParser

debugMode = False


def log(msg):
    if debugMode:
        print msg


class ServerError(Exception):
    """ Error caused because of an invalid command or server unable
    to process command """


class FsError(Exception):
    """ Error caused during execution of a file system command
    In this case dropbox """


class ReadProducer():
    """ Producer for an objects which implement the read(size) method
    and give the next <size> bytes. eg. HttpResponse """

    def __init__(self, reader, bufferSize):
        self.reader = reader
        self.size = bufferSize

    def more(self):
        buffer = self.reader.read(self.size)
        if len(buffer) == 0:
            if hasattr(self.reader, 'close'):
                self.reader.close()
        return buffer

    def close(self):
        try:
            # Try to close the socket (if any) which
            # created this read buffer
            self.reader.close()
            if hasattr(self.reader, 'async_handler'):
                self.reader.async_handler.close()
        except:
            pass


def resp_dec(ftp_handler):
    """ Decorator for all the response callbacks. Handles response failures
    First argument for all functions this decorotes should be the response """
    def decorate(f):
        def wrapper(*args):
            response = args[0]
            if hasattr(response, 'status'):
                if response.status != 200:
                    if response.status == 401:
                        ftp_handler.prompt_user_for_login()
                        return
                    else:
                        ftp_handler.respond("550 Command to file system failed")
                        return
            return f(*args)
        return wrapper
    return decorate


class DbUploadBuffer():
    """ Represents the buffer to upload a file to dropbox.
    Handles the buffering, breaking of the file into smaller chunks
    and also sending it over to dropbox """

    def __init__(self, path, ftp_handler):
        self.ftp_handler = ftp_handler
        self.buffer = []
        self.buffer_len = 0
        self.done_receiving = False

        # Required for dropbox chunked_upload
        self.path = path
        self.chunks_to_send = []
        self.sending_chunk = False
        self.upload_id = None
        self.offset = 0

    # Slows the process of receiving data from the client in case an upload
    # is in place and there is already another one queued
    def can_receive(self):
        return len(self.chunks_to_send) == 0

    # Queue a chunk to be sent to dropbox
    def q_chunk(self, chunk):
        if len(chunk) > 0:
            self.chunks_to_send.append(chunk)
        self.send_chunk()

    def send_chunk(self):

        @resp_dec(self.ftp_handler)
        def chunk_callback(resp):
            self.upload_id = resp['upload_id']
            self.offset = resp['offset']
            self.sending_chunk = False
            self.send_chunk()

        #Already sending a chunk. Hold till it returns
        if (self.sending_chunk):
            return
        #Send more chunks if there are more to send
        if (len(self.chunks_to_send)) > 0:
            chunk = self.chunks_to_send.pop(0)
            self.sending_chunk = True
            self.ftp_handler.db_client.chunked_upload(chunk, upload_id=self.upload_id,
                offset=self.offset, callback=chunk_callback)
        #Send commit only if done receiving from the server
        elif self.done_receiving:
            self.send_commit()

    # Send the commit_chunk message to dropbox to finalize the upload
    def send_commit(self):

        @resp_dec(self.ftp_handler)
        def commit_callback(resp):
            self.ftp_handler.respond("250 Transfer successful")

        self.ftp_handler.db_client.commit_chunk(self.path, self.upload_id, callback=commit_callback)

    def write(self, data):
        """ Write to the buffer """
        self.buffer_len += len(data)
        self.buffer.append(data)
        # if it has reached the size of a dropbox upload chunk, send it accross
        if (self.buffer_len >= self.ftp_handler.db_chunk_size):
            chunk = ''.join(self.buffer)
            self.buffer = []
            self.buffer_len = 0
            self.q_chunk(chunk)

    def close(self):
        """ Called when the socket is done receiving from the ftp client """
        chunk = ''.join(self.buffer)
        self.done_receiving = True
        self.q_chunk(chunk)


class DTPHandler(asynchat.async_chat):
    """ Represents the data channel which is used to send and receive data
    from the ftp client """

    def __init__(self, conn, ftp_handler):
        self.ftp_handler = ftp_handler
        asynchat.async_chat.__init__(self, conn)
        self.done = False
        self.can_receive = False
        self.bytes_received = 0
        self.response = None
        # Dropbox upload buffers the received data is written to
        self.buffer = None

    def handle_read(self):
        if self.can_receive and self.buffer and self.buffer.can_receive():
            try:
                chunk = self.recv(self.ac_in_buffer_size)
                self.buffer.write(chunk)
            except:
                self.handle_error()

            if not chunk:
                if not self.done:
                    self.end_receiving()

    # This is called when data has finished being sent to the client
    # or data has been finished receiving
    def handle_close(self):
        if self.can_receive:
            if not self.done:
                self.end_receiving()
        else:
            self.ftp_handler.respond("226 Transfer complete")

        self.ftp_handler.on_data_channel_close()
        asynchat.async_chat.close(self)

    def start_receiving(self, buffer):
        self.can_receive = True
        self.buffer = buffer

    def end_receiving(self):
        self.buffer.close()
        self.done = True

    def close(self):
        if hasattr(self, 'producer'):
            try:
                # Close the producer in case it hasn't been closed
                self.producer.close()
            except:
                pass
        self.socket.close()
        asyncore.dispatcher.close(self)


class PassiveDTP(asyncore.dispatcher):
    """ Represents the passive data channel acceptor for the FTP server """

    def __init__(self, ftp_handler):
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ftp_handler = ftp_handler

        addr = ftp_handler.socket.getsockname()[0]
        self.bind((addr, 0))
        self.listen(5)
        # Create socket, send address and wait for the accept of the ftp client
        port = self.socket.getsockname()[1]
        ftp_handler.respond('227 Entering passive mode (%s,%d,%d).' % (
                            addr.replace('.', ','), port // 256, port % 256))

    def handle_accept(self):
        try:
            sock, addr = self.accept()
        except socket.error:
            self.close()
            log("socket error on accept")
        else:
            self.close()
            self.ftp_handler.data_channel = DTPHandler(sock, self.ftp_handler)
            self.ftp_handler.on_data_channel_open()

    def close(self):
        asyncore.dispatcher.close(self)


class ActiveDTP(asyncore.dispatcher):
    """ Represents the active data acceptor for the FTP server """

    def __init__(self, ip, port, ftp_handler):
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        self.ftp_handler = ftp_handler
        # use the same address which the client has bound to on this server
        # and bind to any free port
        addr = ftp_handler.socket.getsockname()[0]
        self.bind((addr, 0))

        # try to make an active connection to the client
        try:
            self.connect((ip, port))
        except:
            ftp_handler.respond("520 Failed to connect to client")

    def handle_connect(self):
        self.ftp_handler.respond("200 connection established")
        # Create the data channel now
        self.ftp_handler.data_channel = DTPHandler(self.socket, self.ftp_handler)
        self.ftp_handler.on_data_channel_open()

    def close(self):
        asyncore.dispatcher.close(self)


def cmd(requireAuth=True):
    """ Decorator for all the FTP commands. Handles auth, responses and
    failures """
    def decorate(f):
        def wrapper(self, *args, **kwargs):
            if requireAuth and (not self.authenticated or not self.db_client):
                self.respond("520 Not authenticated. ")
                return

            try:
                return f(self, *args, **kwargs)
            except FsError as e:
                self.respond("550 " + e.args[0])
            except ServerError as e:
                self.respond("500 " + e.args[0])

        wrapper.__doc__ = f.__doc__
        return wrapper
    return decorate


class FTPHandler(asynchat.async_chat):
    """ The FTP handler which processes the FTP commands received from the
    client. It is the command channel between the client and the server
    and remains connected throughout the session """

    def __init__(self, conn, server):
        asynchat.async_chat.__init__(self, conn)
        self.buffer = []
        self.set_terminator("\r\n")
        self.authenticated = False
        self.data_channel = None
        self.server = server
        self.out_data_q = None
        self.receiver_callback = None
        # Dropbox client
        self.db_client = None
        self.db_chunk_size = 1024000  # 1mb chunk size for db uploads
        self.session = session.DropboxSession(self.server.dbKey, self.server.dbSecret, 'dropbox')

    def handle(self):
        self.respond("220 ftp-dropbox")

    def collect_incoming_data(self, data):
        self.buffer.append(data)

    def found_terminator(self):
        line = ''.join(self.buffer)
        self.buffer = []
        cmd = line.split(' ')[0].upper()
        if (not cmd) or (len(cmd) == 0):
            self.respond("500 invalid command")
        log("<= : " + line)
        try:
            func = getattr(self, "ftp_" + cmd)
        except AttributeError:
            self.respond("500 Command not implemented")
        else:
            arg = line[len(cmd) + 1:]
            if len(arg) == 0:
                func()
            else:
                func(arg)

    def close(self):
        if self.data_channel:
            self.data_channel.close()
            self.data_channel = None
        asynchat.async_chat.close(self)
        self.socket.close()

    def close_dtp(self):
        self.dtp.close()

    def respond(self, msg):
        log("=> : " + msg)
        self.push(msg + '\r\n')

    # Push the data to the channel or queue it if we are
    # still waiting for a connection
    def push_data(self, data, isproducer=False):
        if self.data_channel:
            self.respond("125 Connection already established Transferring data")
            if isproducer:
                self.data_channel.push_with_producer(data)
                self.data_channel.producer = data
            else:
                self.data_channel.push(data)
            self.data_channel.close_when_done()
        else:
            self.out_data_q = (data, isproducer)

    def on_data_channel_open(self):
        if self.out_data_q:
            # Flush the queue once the channel opens
            q = self.out_data_q
            self.out_data_q = None
            self.push_data(*q)
        elif self.receiver_callback:
            self.receiver_callback()

    def on_data_channel_close(self):
        self.data_channel = None

    def create_db_client(self):
        # Create the dropbox client
        self.db_client = async_rest.AsyncDBClient(self.session)
        self.db_client.pwd = "/"
        self.respond("230 User successfully authenticated")
        self.authenticated = True

    def prompt_user_for_login(self):
        self.push("530-User " + " has not authenticated with dropbox. " +
        "\r\nUse the script: get_creds.py to get your username\r\nThen try to connect again\r\n")
        self.respond("530 ")

    # Different FTP commands
    @cmd(requireAuth=False)
    def ftp_USER(self, name):
        if self.authenticated:
            self.respond("530 User already authenticated")
        elif len(name) == 0 or name == "anonymous":
            self.prompt_user_for_login()
        else:
            tokens = name.split(':')
            if len(tokens) != 2:
                self.access_token = name
                self.respond("331 send password")
            else:
                self.session.set_token(tokens[0], tokens[1])
                self.create_db_client()

    @cmd(requireAuth=False)
    def ftp_PASS(self, pwd=None):
        if self.authenticated:
            self.respond("231 authenticated fine")
        else:
            if not pwd or len(pwd) == 0:
                self.prompt_user_for_login()
            else:
                self.session.set_token(self.access_token, pwd)
                self.create_db_client()

    @cmd(requireAuth=False)
    def ftp_SYST(self):
        self.respond("215 UNIX Type: L8")

    @cmd(requireAuth=False)
    def ftp_FEAT(self):
        self.push('211-Features:\r\nUTF8\r\n')
        self.respond('211 End')

    @cmd()
    def ftp_PWD(self):
        """ Get current directory """
        if self.db_client.pwd:
                self.respond('257 "%s" is the current directory' % self.db_client.pwd)
        else:
            raise FsError('Unable to get pwd')

    @cmd()
    def ftp_MLSD(self, path=None):
        """ List information about a file/directory """
        self.ftp_LIST(path)

    @cmd()
    def ftp_PASV(self, line=None):
        """ Create a passive data connection """
        if self.data_channel:
            self.data_channel.close()
            self.data_channel = None
        self.dtp = PassiveDTP(self)

    @cmd()
    def ftp_PORT(self, line):
        # Parse PORT request for getting IP and PORT.
        # Request comes in as:
        # > h1,h2,h3,h4,p1,p2
        # IP address is h1.h2.h3.h4
        # TCP port number is (p1 * 256) + p2.
        try:
            addr = map(int, line.split(','))
            if len(addr) != 6:
                raise ValueError
            for x in addr[:4]:
                if not 0 <= x <= 255:
                    raise ValueError
            ip = '%d.%d.%d.%d' % tuple(addr[:4])
            port = (addr[4] * 256) + addr[5]
            if not 0 <= port <= 65535:
                raise ValueError
        except (ValueError, OverflowError):
            self.respond("501 Invalid PORT format.")
            return

        #close any existing data channels
        if self.data_channel:
            self.data_channel.close()
            self.data_channel = None

        #Create the DTP which also creates the data channel
        self.dtp = ActiveDTP(ip, port, self)

    @cmd(requireAuth=False)
    def ftp_TYPE(self, type):
        self.respond("200 Type set to binary")

    @cmd()
    def ftp_NLST(self, path=None):
        self.ftp_LIST(path=path, onlyNames=True)

    @cmd()
    def ftp_LIST(self, path=None, listall=True, onlyNames=False):

        def get_data_from_md(md, onlyName=False):
                """ Returns ftp data from db metadata """
                name = os.path.basename(md['path'])
                if onlyName:
                    return str(name) + '\r\n'
                else:
                    params = {}
                    params['type'] = 'dir' if (md['is_dir']) else 'file'
                    # TODO: parse the other params as well.
                    params['size'] = md['bytes']
                    #params['modify'] = md['modified']
                    paramstring = ''.join(["%s=%s;" % (x, params[x]) \
                                      for x in params.keys()])
                    data = '%s %s\r\n' % (paramstring, name)
                    return data

        @resp_dec(self)
        def list_callback(resp):
            data = ''
            if 'contents' in resp:
                for f in resp['contents']:
                    data = data + get_data_from_md(f, onlyName=onlyNames)
            else:
                data = get_data_from_md(resp)
            self.push_data(data.encode('utf-8'))

        if not path:
            path = self.db_client.pwd
        self.db_client.metadata(path, list=listall, callback=list_callback)

    @cmd()
    def ftp_CWD(self, path):
        """ Change directory to path. path is relative to the current dir """
        if not path:
            self.respond('500 Invalid path specified')
            return
        newpath = self.db_client.pwd
        if path == "..":
            newpath = "/".join(self.db_client.pwd.split("/")[0:-1])
        elif path != "/":
            newpath += "/" + path

        if newpath == path:
            self.respond('250 "%s" is the current directory' % self.db_client.pwd)
        else:
            @resp_dec(self)
            def cwd_callback(resp):
                if not resp['is_dir']:
                    self.respond("550 Invalid path")
                else:
                    self.db_client.pwd = newpath
                    self.respond('250 "%s" is the current directory' % self.db_client.pwd)

            self.db_client.metadata(newpath, False, callback=cwd_callback)

    @cmd()
    def ftp_STOR(self, path):
        """ FTP command to signal an incoming upload of a file """
        path = self.db_client.pwd + "/" + path
        log('Sending file: ' + path)

        write_buffer = DbUploadBuffer(path, self)
        if self.data_channel:
            self.respond("125 Connection established. Ready to receive file")
            self.data_channel.start_receiving(write_buffer)
        else:
            def rcb():
                self.data_channel.start_receiving(write_buffer)
            self.receiver_callback = rcb

    @cmd()
    def ftp_RETR(self, path):
        """ FTP command to signal a download for a file from
        server to local """

        path = self.db_client.pwd + "/" + path
        log("Retreiving file " + path)

        @resp_dec(self)
        def retr_callback(response, metadata):
            if metadata and 'is_dir' in metadata and not metadata['is_dir']:
                # Creates a producer from the raw httpResponse
                producer = ReadProducer(response, asynchat.async_chat.ac_out_buffer_size)
                self.push_data(producer, True)
            else:
                self.respond("520 Path is not a file")

        self.db_client.get_file_and_metadata(path, callback=retr_callback)


class FTPServer(asyncore.dispatcher):
    """ Server which creates an FTP socket and listens on the address provided
    When it receives requests, it dispatches them to the FTPHandler class which
    processes the FTP request"""

    def __init__(self, host, port):
        asyncore.dispatcher.__init__(self)
        self.create_socket(socket.AF_INET, socket.SOCK_STREAM)
        asyncore.dispatcher.set_reuse_addr(self)
        self.bind((host, port))
        self.listen(5)
        self.users = {}
        self.dbKey = "iqbyp81yfd7idcc"
        self.dbSecret = "r7ipcthsogmfj54"

    def serve_forever(self):
        asyncore.loop()

    def handle_accept(self):
        try:
            conn, addr = self.accept()
        except socket.error:
            log("socket error on accept")

        handler = None
        log("Trying to handle accept for " + pprint.pformat(addr))
        try:
            handler = FTPHandler(conn, self)
            handler.handle()
            log(pprint.pformat(addr) + " connected")
        except:
            if handler:
                handler.close()
            log("failed to connect handler for" + pprint.pformat(addr))


def main():
    print "Starting server"
    server = FTPServer('localhost', 21)
    server.serve_forever()

if __name__ == "__main__":
    parser = OptionParser()
    (options, args) = parser.parse_args()
    if 'debug' in args:
        debugMode = True
    main()
