import concurrent.futures,subprocess,pathlib,hashlib
root=pathlib.Path('/tmp/agy-hub-ua-20261117')
url='https://storage.googleapis.com/antigravity-public/antigravity-hub/2.32.2-4665802025205760/unsigned/language_server-linux-x64'
size=239952296
chunk=8*1024*1024
parts=list(range(0,size,chunk))
def fetch(start):
 end=min(start+chunk,size)-1
 p=root/f'part-{start}'
 subprocess.run(['curl','-fLsS','--retry','2','--max-time','120','-H',f'Range: bytes={start}-{end}',url+'?ua-part='+str(start),'-o',str(p)],check=True)
 assert p.stat().st_size==end-start+1,(start,p.stat().st_size)
 return p
with concurrent.futures.ThreadPoolExecutor(max_workers=12) as pool:
 paths=list(pool.map(fetch,parts))
h=hashlib.sha256()
with (root/'hub-2.32.2').open('wb') as out:
 for p in paths:
  d=p.read_bytes();out.write(d);h.update(d)
print(size,h.hexdigest())
