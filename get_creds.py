from dropbox import session

appKey = 'iqbyp81yfd7idcc'
appSecret = 'r7ipcthsogmfj54'

sess = session.DropboxSession(appKey, appSecret, 'dropbox')
request_token = sess.obtain_request_token()
url = sess.build_authorize_url(request_token)

print "hit url " + url  + " in a browser and then hit enter"
raw_input() 
 
token = sess.obtain_access_token(request_token) 

print "Your FTP User: \n"
print  token.key + ":" + token.secret


