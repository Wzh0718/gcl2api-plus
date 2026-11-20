import json,os,time,uuid,hmac,hashlib
from urllib.parse import parse_qs
from mitmproxy import http
# Run with mitmdump -q; do not enable flow/body recording (-w).
OUT=os.environ['AGY_OAUTH_CAPTURE_OUTPUT']
KEY=os.urandom(32)
def request(flow):
    if flow.request.host not in {'oauth2.googleapis.com','www.googleapis.com'}:return
    if flow.request.path.split('?')[0] not in {'/token','/oauth2/v4/token','/oauth2/v3/token'}:return
    data=parse_qs(flow.request.get_text(strict=False))
    grant=data.get('grant_type',[''])[0]
    if grant not in {'refresh_token','authorization_code'}:grant='other'
    rt=data.get('refresh_token',[''])[0]
    flow.metadata['oauth_observe']={'id':uuid.uuid4().hex,'request_at':flow.request.timestamp_start,'grant_type':grant,'credential_ref':hmac.new(KEY,rt.encode(),hashlib.sha256).hexdigest()[:16] if rt else None}
def emit(flow,failed=False):
    event=flow.metadata.get('oauth_observe')
    if not event:return
    event=dict(event,completed_at=time.time(),status=flow.response.status_code if flow.response else None,network_error=failed)
    if flow.response:
        try:
            body=json.loads(flow.response.get_text(strict=False))
            expiry=body.get('expires_in')
            event['expires_in']=expiry if isinstance(expiry,(int,float)) else None
            event['returns_refresh_token']=bool(body.get('refresh_token'))
            code=body.get('error')
            event['error']=code if code in {'invalid_grant','invalid_client','invalid_request','temporarily_unavailable'} else ('other' if code else None)
        except Exception:event['non_json']=True
    fd=os.open(OUT,os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(fd,'a') as f:f.write(json.dumps(event)+'\n')
def response(flow):emit(flow)
def error(flow):emit(flow,True)
