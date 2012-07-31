Authentication: 
    Please use script get_creds.py to get a dropbox username compatible
    with the FTP server. This will be in the form of <accesstoken>:<accesskey>

Usage: 
    sudo python server.py

Commands implemented: 
    MLSD
    LIST
    CWD
    PWD
    USER
    PASS
    STOR
    RETR
    PORT
    PASV

Commands from the unix ftp client: 
    ls
    cd
    put
    get

Known problems: 
    Progress bar for file upload:
        On a STOR reques, Even after the ftp client says that it has transmitted everything
        the server still takes some time to upload all the chunks to dropbox, hence the client has to 
        wait a bit beofre the server sends a confirmation. This results in the progress bar 
        being at 100% and a bad user experience.
        This can be fixed by throlttling the incoming speed even more or having a callback from 
        the upload method to register progress after every small chunk sent. 

    SSL related issues: 
        I ecountered a bunch of problems using ssl sockets with asyncore in python. Not having used 
        asyncore or ssl sockets before, this proved to by pretty time consuming. By the time I realised
        the issues caused by ssl, it was too late to change my implementation to use the twisted
        module or switching over to nodejs. The problems are mentioned below: 
        
        - There is no implementation to make SSL handshakes asynchronous:
            After receiving an async TCP handshake, I had to do a blocking SSL handshake. 
            This affects the throughput of the system when it is making several requests to dropbox

        - Large get requests (List specifically) are slow:
            Another issue I hit with SSL sockets was that the select.select() method never called
            handle_read() even though there was data waiting in the socket. The first read happens, 
            but asyncore.loop() never calls handle_read() until the dropbox api closes the connection.
            In our case, we are using this call only for List requests. In case the list of files in a 
            directory is too big, the ftp client will have to wait a bit for the response to come back. 
            The server will not get blocked on it though.


    FTP response codes might not be correct:
        Even though mostly the first 2 digits will be correct, the complete response codes
        might not be. I put in whatever I could find.  
