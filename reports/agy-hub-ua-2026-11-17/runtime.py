import http.server,threading,subprocess,pathlib,json,time,urllib.request,os
root=pathlib.Path('/sandbox');home=root/'home';(home/'.gemini').mkdir(parents=True,exist_ok=True)
(home/'.gemini/jetski-standalone-oauth-token').write_text(json.dumps({'access_token':'SYNTHETIC-OFFLINE-UA-TEST','token_type':'Bearer','expiry':'2099-01-01T00:00:00Z'}))
rows=[]
class Handler(http.server.BaseHTTPRequestHandler):
 def log_message(self,*args):pass
 def do_POST(self):self.respond()
 def do_GET(self):self.respond()
 def respond(self):
  rows.append({'path':self.path,'ua':self.headers.get('User-Agent'),'method':self.command})
  self.rfile.read(int(self.headers.get('Content-Length','0')))
  data=b'{"error":{"code":401,"message":"offline synthetic test","status":"UNAUTHENTICATED"}}'
  self.send_response(401);self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
server=http.server.ThreadingHTTPServer(('127.0.0.1',18941),Handler);threading.Thread(target=server.serve_forever,daemon=True).start()
env=os.environ.copy();env['HOME']=str(home);env['XDG_CONFIG_HOME']=str(home/'.config');env['NO_PROXY']='*'
args=['/sandbox/hub-2.32.2','--standalone','--override_ide_name','antigravity','--subclient_type','hub','--override_ide_version','2.32.2','--override_user_agent_name','antigravity','--http_server_port','18942','--csrf_token','offline-ua-test','--app_data_dir','hub-test','--api_server_url','http://127.0.0.1:18941','--cloud_code_endpoint','http://127.0.0.1:18941']
log=(root/'runtime.log').open('w');p=subprocess.Popen(args,cwd=root,env=env,stdout=log,stderr=subprocess.STDOUT)
try:
 for i in range(30):
  if p.poll() is not None:break
  try:
   req=urllib.request.Request('http://127.0.0.1:18942/exa.language_server_pb.LanguageServerService/GetUserStatus',data=b'{}',headers={'Content-Type':'application/json','x-codeium-csrf-token':'offline-ua-test','Connect-Protocol-Version':'1'})
   urllib.request.urlopen(req,timeout=1).read()
  except Exception:pass
  if rows:break
  time.sleep(.5)
 time.sleep(2)
finally:
 p.terminate()
 try:p.wait(timeout=5)
 except subprocess.TimeoutExpired:p.kill();p.wait()
 server.shutdown();log.close()
(root/'runtime-ua.json').write_text(json.dumps({'isolation':'docker --network none; synthetic token; local stub returns HTTP 401; no Google requests','launch_args':args,'requests':rows,'hub_exit_code':p.returncode},indent=2))
print(json.dumps({'captured':len(rows),'requests':rows},indent=2))
