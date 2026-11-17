import struct,json,pathlib,sys,subprocess
p=pathlib.Path(sys.argv[1]);d=p.read_bytes();root=p.parent
# Validate the ELF64 metadata and record dynamic dependencies without executing it.
assert d[:6]==b'\x7fELF\x02\x01'
shoff=struct.unpack_from('<Q',d,40)[0];shentsz,shnum,shstrndx=struct.unpack_from('<HHH',d,58)
sections=[struct.unpack_from('<IIQQQQIIQQ',d,shoff+i*shentsz) for i in range(shnum)]
ss=sections[shstrndx];names=d[ss[4]:ss[4]+ss[5]]
sec={names[s[0]:].split(b'\0')[0].decode():s for s in sections}
(root/'imports.txt').write_text(subprocess.check_output(['readelf','-d',str(p)],text=True))
candidates=[];at=0
while True:
 at=d.find(b'\xf1\xff\xff\xff\x00\x00\x01\x08',at)
 if at<0:break
 h=struct.unpack_from('<8Q',d,at+8)
 if 100<h[0]<1000000 and all(v<len(d) for v in h[3:]):candidates.append((at,h))
 at+=8
rows=[]
for base,h in candidates:
 try:
  n,nfiles,text,fn,cu,file,pc,tab=h
  text=text or (sec['.text'][3]+0x80)
  tmp=[]
  for i in range(n):
   entry,off=struct.unpack_from('<II',d,base+tab+i*8);end=struct.unpack_from('<I',d,base+tab+(i+1)*8)[0]
   no=struct.unpack_from('<i',d,base+tab+off+4)[0];np=base+fn+no
   name=d[np:d.index(b'\0',np)].decode()
   tmp.append({'entry':text+entry,'end':text+end,'name':name})
  if any(r['name']=='runtime.main' for r in tmp):rows=tmp;break
 except (ValueError,struct.error,UnicodeDecodeError):continue
assert rows,'No valid Go pclntab'
(root/'symbols.json').write_text(json.dumps(rows))
print('pclntab',hex(base),'textbase',hex(text),'functions',len(rows))
for r in rows:
 if any(s in r['name'].lower() for s in ('useragent','user_agent','stamp')) and not 'proto' in r['name'].lower(): print(hex(r['entry']),hex(r['end']),r['name'])
