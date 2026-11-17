
/tmp/agy-hub-ua-20261117/hub-2.32.2：     文件格式 elf64-x86-64


Disassembly of section .text:

0000000007b6f2c0 <crosscall2@@Base+0x2385960>:
 7b6f2c0:	4c 8d a4 24 90 fe ff 	lea    r12,[rsp-0x170]
 7b6f2c7:	ff
 7b6f2c8:	4d 3b 66 10          	cmp    r12,QWORD PTR [r14+0x10]
 7b6f2cc:	0f 86 2c 0b 00 00    	jbe    7b6fdfe <crosscall2@@Base+0x238649e>
 7b6f2d2:	55                   	push   rbp
 7b6f2d3:	48 89 e5             	mov    rbp,rsp
 7b6f2d6:	48 81 ec e8 01 00 00 	sub    rsp,0x1e8
 7b6f2dd:	48 89 8c 24 08 02 00 	mov    QWORD PTR [rsp+0x208],rcx
 7b6f2e4:	00
 7b6f2e5:	44 88 8c 24 28 02 00 	mov    BYTE PTR [rsp+0x228],r9b
 7b6f2ec:	00
 7b6f2ed:	4c 89 84 24 20 02 00 	mov    QWORD PTR [rsp+0x220],r8
 7b6f2f4:	00
 7b6f2f5:	48 89 b4 24 18 02 00 	mov    QWORD PTR [rsp+0x218],rsi
 7b6f2fc:	00
 7b6f2fd:	48 89 9c 24 00 02 00 	mov    QWORD PTR [rsp+0x200],rbx
 7b6f304:	00
 7b6f305:	48 89 84 24 f8 01 00 	mov    QWORD PTR [rsp+0x1f8],rax
 7b6f30c:	00
 7b6f30d:	48 85 ff             	test   rdi,rdi
 7b6f310:	75 24                	jne    7b6f336 <crosscall2@@Base+0x23859d6>
 7b6f312:	48 8b 0d 4f 16 15 03 	mov    rcx,QWORD PTR [rip+0x315164f]        # acc0968 <_ZTISt10bad_typeid@@Base+0x5cc428>
 7b6f319:	90                   	nop
 7b6f31a:	48 85 c9             	test   rcx,rcx
 7b6f31d:	74 09                	je     7b6f328 <crosscall2@@Base+0x23859c8>
 7b6f31f:	48 8b 15 3a 16 15 03 	mov    rdx,QWORD PTR [rip+0x315163a]        # acc0960 <_ZTISt10bad_typeid@@Base+0x5cc420>
 7b6f326:	eb 14                	jmp    7b6f33c <crosscall2@@Base+0x23859dc>
 7b6f328:	b9 0b 00 00 00       	mov    ecx,0xb
 7b6f32d:	48 8d 15 12 06 00 fd 	lea    rdx,[rip+0xfffffffffd000612]        # 4b6f946 <_cgo_topofstack@@Base-0xb188da>
 7b6f334:	eb 06                	jmp    7b6f33c <crosscall2@@Base+0x23859dc>
 7b6f336:	48 89 ca             	mov    rdx,rcx
 7b6f339:	48 89 f9             	mov    rcx,rdi
 7b6f33c:	48 89 8c 24 c0 00 00 	mov    QWORD PTR [rsp+0xc0],rcx
 7b6f343:	00
 7b6f344:	48 89 94 24 e0 01 00 	mov    QWORD PTR [rsp+0x1e0],rdx
 7b6f34b:	00
 7b6f34c:	e8 cf 75 63 fe       	call   61a6920 <crosscall2@@Base+0x9bcfc0>
 7b6f351:	48 85 c0             	test   rax,rax
 7b6f354:	74 0c                	je     7b6f362 <crosscall2@@Base+0x2385a02>
 7b6f356:	48 8b 50 18          	mov    rdx,QWORD PTR [rax+0x18]
 7b6f35a:	4c 8b 40 20          	mov    r8,QWORD PTR [rax+0x20]
 7b6f35e:	66 90                	xchg   ax,ax
 7b6f360:	eb 05                	jmp    7b6f367 <crosscall2@@Base+0x2385a07>
 7b6f362:	45 31 c0             	xor    r8d,r8d
 7b6f365:	31 d2                	xor    edx,edx
 7b6f367:	4c 89 44 24 50       	mov    QWORD PTR [rsp+0x50],r8
 7b6f36c:	48 89 94 24 d8 00 00 	mov    QWORD PTR [rsp+0xd8],rdx
 7b6f373:	00
 7b6f374:	48 8b 15 fd 15 15 03 	mov    rdx,QWORD PTR [rip+0x31515fd]        # acc0978 <_ZTISt10bad_typeid@@Base+0x5cc438>
 7b6f37b:	48 89 54 24 48       	mov    QWORD PTR [rsp+0x48],rdx
 7b6f380:	48 8b 15 e9 15 15 03 	mov    rdx,QWORD PTR [rip+0x31515e9]        # acc0970 <_ZTISt10bad_typeid@@Base+0x5cc430>
 7b6f387:	48 89 94 24 d0 00 00 	mov    QWORD PTR [rsp+0xd0],rdx
 7b6f38e:	00
 7b6f38f:	48 8d 15 b2 0a 0c 02 	lea    rdx,[rip+0x20c0ab2]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6f396:	48 89 94 24 a8 01 00 	mov    QWORD PTR [rsp+0x1a8],rdx
 7b6f39d:	00
 7b6f39e:	48 8d 15 ab 07 43 01 	lea    rdx,[rip+0x14307ab]        # 8f9fb50 <__lxstat@plt+0x202880>
 7b6f3a5:	48 89 94 24 b0 01 00 	mov    QWORD PTR [rsp+0x1b0],rdx
 7b6f3ac:	00
 7b6f3ad:	48 8d 05 eb 72 fe fc 	lea    rax,[rip+0xfffffffffcfe72eb]        # 4b5669f <_cgo_topofstack@@Base-0xb31b81>
 7b6f3b4:	bb 0a 00 00 00       	mov    ebx,0xa
 7b6f3b9:	48 8d 8c 24 a8 01 00 	lea    rcx,[rsp+0x1a8]
 7b6f3c0:	00
 7b6f3c1:	bf 01 00 00 00       	mov    edi,0x1
 7b6f3c6:	89 fe                	mov    esi,edi
 7b6f3c8:	e8 53 a3 ba fd       	call   5719720 <_cgo_topofstack@@Base+0x91500>
 7b6f3cd:	48 89 84 24 d8 01 00 	mov    QWORD PTR [rsp+0x1d8],rax
 7b6f3d4:	00
 7b6f3d5:	48 89 9c 24 b8 00 00 	mov    QWORD PTR [rsp+0xb8],rbx
 7b6f3dc:	00
 7b6f3dd:	48 8d 15 64 0a 0c 02 	lea    rdx,[rip+0x20c0a64]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6f3e4:	48 89 94 24 98 01 00 	mov    QWORD PTR [rsp+0x198],rdx
 7b6f3eb:	00
 7b6f3ec:	48 8d 15 6d 07 43 01 	lea    rdx,[rip+0x143076d]        # 8f9fb60 <__lxstat@plt+0x202890>
 7b6f3f3:	48 89 94 24 a0 01 00 	mov    QWORD PTR [rsp+0x1a0],rdx
 7b6f3fa:	00
 7b6f3fb:	48 8d 05 80 1e fa fc 	lea    rax,[rip+0xfffffffffcfa1e80]        # 4b11282 <_cgo_topofstack@@Base-0xb76f9e>
 7b6f402:	bb 07 00 00 00       	mov    ebx,0x7
 7b6f407:	48 8d 8c 24 98 01 00 	lea    rcx,[rsp+0x198]
 7b6f40e:	00
 7b6f40f:	bf 01 00 00 00       	mov    edi,0x1
 7b6f414:	89 fe                	mov    esi,edi
 7b6f416:	e8 05 a3 ba fd       	call   5719720 <_cgo_topofstack@@Base+0x91500>
 7b6f41b:	48 89 84 24 d0 01 00 	mov    QWORD PTR [rsp+0x1d0],rax
 7b6f422:	00
 7b6f423:	48 89 9c 24 b0 00 00 	mov    QWORD PTR [rsp+0xb0],rbx
 7b6f42a:	00
 7b6f42b:	31 c0                	xor    eax,eax
 7b6f42d:	bb 03 00 00 00       	mov    ebx,0x3
 7b6f432:	31 c9                	xor    ecx,ecx
 7b6f434:	89 df                	mov    edi,ebx
 7b6f436:	48 8d 35 0b 0a 0c 02 	lea    rsi,[rip+0x20c0a0b]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6f43d:	0f 1f 00             	nop    DWORD PTR [rax]
 7b6f440:	e8 bb 36 b1 fd       	call   5682b00 <_cgo_topofstack@@Base-0x5720>
 7b6f445:	48 c7 40 08 0c 00 00 	mov    QWORD PTR [rax+0x8],0xc
 7b6f44c:	00
 7b6f44d:	83 3d 9c 3c 0d 07 00 	cmp    DWORD PTR [rip+0x70d3c9c],0x0        # ec430f0 <_ZTISt10bad_typeid@@Base+0x454ebb0>
 7b6f454:	75 12                	jne    7b6f468 <crosscall2@@Base+0x2385b08>
 7b6f456:	48 8b 94 24 d8 01 00 	mov    rdx,QWORD PTR [rsp+0x1d8]
 7b6f45d:	00
 7b6f45e:	4c 8b 84 24 d0 01 00 	mov    r8,QWORD PTR [rsp+0x1d0]
 7b6f465:	00
 7b6f466:	eb 33                	jmp    7b6f49b <crosscall2@@Base+0x2385b3b>
 7b6f468:	48 8b 10             	mov    rdx,QWORD PTR [rax]
 7b6f46b:	4c 8b 40 10          	mov    r8,QWORD PTR [rax+0x10]
 7b6f46f:	4c 8b 48 20          	mov    r9,QWORD PTR [rax+0x20]
 7b6f473:	e8 a8 8e b1 fd       	call   5688320 <_cgo_topofstack@@Base+0x100>
 7b6f478:	49 89 13             	mov    QWORD PTR [r11],rdx
 7b6f47b:	48 8b 94 24 d8 01 00 	mov    rdx,QWORD PTR [rsp+0x1d8]
 7b6f482:	00
 7b6f483:	49 89 53 08          	mov    QWORD PTR [r11+0x8],rdx
 7b6f487:	4d 89 43 10          	mov    QWORD PTR [r11+0x10],r8
 7b6f48b:	4c 8b 84 24 d0 01 00 	mov    r8,QWORD PTR [rsp+0x1d0]
 7b6f492:	00
 7b6f493:	4d 89 43 18          	mov    QWORD PTR [r11+0x18],r8
 7b6f497:	4d 89 4b 20          	mov    QWORD PTR [r11+0x20],r9
 7b6f49b:	48 89 4c 24 78       	mov    QWORD PTR [rsp+0x78],rcx
 7b6f4a0:	48 89 84 24 f8 00 00 	mov    QWORD PTR [rsp+0xf8],rax
 7b6f4a7:	00
 7b6f4a8:	4c 8d 0d 34 97 01 fd 	lea    r9,[rip+0xfffffffffd019734]        # 4b88be3 <_cgo_topofstack@@Base-0xaff63d>
 7b6f4af:	4c 89 08             	mov    QWORD PTR [rax],r9
 7b6f4b2:	4c 8b 8c 24 b8 00 00 	mov    r9,QWORD PTR [rsp+0xb8]
 7b6f4b9:	00
 7b6f4ba:	4c 89 48 18          	mov    QWORD PTR [rax+0x18],r9
 7b6f4be:	48 89 50 10          	mov    QWORD PTR [rax+0x10],rdx
 7b6f4c2:	48 8b 94 24 b0 00 00 	mov    rdx,QWORD PTR [rsp+0xb0]
 7b6f4c9:	00
 7b6f4ca:	48 89 50 28          	mov    QWORD PTR [rax+0x28],rdx
 7b6f4ce:	4c 89 40 20          	mov    QWORD PTR [rax+0x20],r8
 7b6f4d2:	48 8b 15 a7 27 0d 07 	mov    rdx,QWORD PTR [rip+0x70d27a7]        # ec41c80 <_ZTISt10bad_typeid@@Base+0x454d740>
 7b6f4d9:	48 85 d2             	test   rdx,rdx
 7b6f4dc:	75 24                	jne    7b6f502 <crosscall2@@Base+0x2385ba2>
 7b6f4de:	48 8b 05 4b b3 05 03 	mov    rax,QWORD PTR [rip+0x305b34b]        # abca830 <_ZTISt10bad_typeid@@Base+0x4d62f0>
 7b6f4e5:	48 8b 1d 4c b3 05 03 	mov    rbx,QWORD PTR [rip+0x305b34c]        # abca838 <_ZTISt10bad_typeid@@Base+0x4d62f8>
 7b6f4ec:	90                   	nop
 7b6f4ed:	e8 ee c5 b4 fd       	call   56bbae0 <_cgo_topofstack@@Base+0x338c0>
 7b6f4f2:	48 8b 4c 24 78       	mov    rcx,QWORD PTR [rsp+0x78]
 7b6f4f7:	48 89 c2             	mov    rdx,rax
 7b6f4fa:	48 8b 84 24 f8 00 00 	mov    rax,QWORD PTR [rsp+0xf8]
 7b6f501:	00
 7b6f502:	48 85 d2             	test   rdx,rdx
 7b6f505:	7f 0a                	jg     7b6f511 <crosscall2@@Base+0x2385bb1>
 7b6f507:	ba 03 00 00 00       	mov    edx,0x3
 7b6f50c:	e9 ca 00 00 00       	jmp    7b6f5db <crosscall2@@Base+0x2385c7b>
 7b6f511:	44 0f 11 bc 24 98 01 	movups XMMWORD PTR [rsp+0x198],xmm15
 7b6f518:	00 00
 7b6f51a:	48 89 d0             	mov    rax,rdx
 7b6f51d:	0f 1f 00             	nop    DWORD PTR [rax]
 7b6f520:	e8 db de b0 fd       	call   567d400 <_cgo_topofstack@@Base-0xae20>
 7b6f525:	48 8d 0d 5c 0b 0c 02 	lea    rcx,[rip+0x20c0b5c]        # 9c30088 <__lxstat@plt+0xe92db8>
 7b6f52c:	48 89 8c 24 98 01 00 	mov    QWORD PTR [rsp+0x198],rcx
 7b6f533:	00
 7b6f534:	48 89 84 24 a0 01 00 	mov    QWORD PTR [rsp+0x1a0],rax
 7b6f53b:	00
 7b6f53c:	48 8d 05 22 35 f8 fc 	lea    rax,[rip+0xfffffffffcf83522]        # 4af2a65 <_cgo_topofstack@@Base-0xb957bb>
 7b6f543:	bb 05 00 00 00       	mov    ebx,0x5
 7b6f548:	48 8d 8c 24 98 01 00 	lea    rcx,[rsp+0x198]
 7b6f54f:	00
 7b6f550:	bf 01 00 00 00       	mov    edi,0x1
 7b6f555:	89 fe                	mov    esi,edi
 7b6f557:	e8 c4 a1 ba fd       	call   5719720 <_cgo_topofstack@@Base+0x91500>
 7b6f55c:	48 8b 4c 24 78       	mov    rcx,QWORD PTR [rsp+0x78]
 7b6f561:	48 83 f9 04          	cmp    rcx,0x4
 7b6f565:	72 0a                	jb     7b6f571 <crosscall2@@Base+0x2385c11>
 7b6f567:	48 8b 94 24 f8 00 00 	mov    rdx,QWORD PTR [rsp+0xf8]
 7b6f56e:	00
 7b6f56f:	eb 41                	jmp    7b6f5b2 <crosscall2@@Base+0x2385c52>
 7b6f571:	48 89 9c 24 b8 00 00 	mov    QWORD PTR [rsp+0xb8],rbx
 7b6f578:	00
 7b6f579:	48 89 84 24 d8 01 00 	mov    QWORD PTR [rsp+0x1d8],rax
 7b6f580:	00
 7b6f581:	48 8b 84 24 f8 00 00 	mov    rax,QWORD PTR [rsp+0xf8]
 7b6f588:	00
 7b6f589:	bb 04 00 00 00       	mov    ebx,0x4
 7b6f58e:	bf 01 00 00 00       	mov    edi,0x1
 7b6f593:	48 8d 35 ae 08 0c 02 	lea    rsi,[rip+0x20c08ae]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6f59a:	e8 61 35 b1 fd       	call   5682b00 <_cgo_topofstack@@Base-0x5720>
 7b6f59f:	48 8b 9c 24 b8 00 00 	mov    rbx,QWORD PTR [rsp+0xb8]
 7b6f5a6:	00
 7b6f5a7:	48 89 c2             	mov    rdx,rax
 7b6f5aa:	48 8b 84 24 d8 01 00 	mov    rax,QWORD PTR [rsp+0x1d8]
 7b6f5b1:	00
 7b6f5b2:	48 89 5a 38          	mov    QWORD PTR [rdx+0x38],rbx
 7b6f5b6:	83 3d 33 3b 0d 07 00 	cmp    DWORD PTR [rip+0x70d3b33],0x0        # ec430f0 <_ZTISt10bad_typeid@@Base+0x454ebb0>
 7b6f5bd:	74 10                	je     7b6f5cf <crosscall2@@Base+0x2385c6f>
 7b6f5bf:	4c 8b 42 30          	mov    r8,QWORD PTR [rdx+0x30]
 7b6f5c3:	e8 f8 8c b1 fd       	call   56882c0 <_cgo_topofstack@@Base+0xa0>
 7b6f5c8:	49 89 03             	mov    QWORD PTR [r11],rax
 7b6f5cb:	4d 89 43 08          	mov    QWORD PTR [r11+0x8],r8
 7b6f5cf:	48 89 42 30          	mov    QWORD PTR [rdx+0x30],rax
 7b6f5d3:	48 89 d0             	mov    rax,rdx
 7b6f5d6:	ba 04 00 00 00       	mov    edx,0x4
 7b6f5db:	48 8b 9c 24 20 02 00 	mov    rbx,QWORD PTR [rsp+0x220]
 7b6f5e2:	00
 7b6f5e3:	48 85 db             	test   rbx,rbx
 7b6f5e6:	0f 84 01 01 00 00    	je     7b6f6ed <crosscall2@@Base+0x2385d8d>
 7b6f5ec:	48 89 8c 24 a8 00 00 	mov    QWORD PTR [rsp+0xa8],rcx
 7b6f5f3:	00
 7b6f5f4:	48 89 84 24 c8 01 00 	mov    QWORD PTR [rsp+0x1c8],rax
 7b6f5fb:	00
 7b6f5fc:	48 89 54 24 70       	mov    QWORD PTR [rsp+0x70],rdx
 7b6f601:	44 0f 11 bc 24 98 01 	movups XMMWORD PTR [rsp+0x198],xmm15
 7b6f608:	00 00
 7b6f60a:	48 8b 84 24 18 02 00 	mov    rax,QWORD PTR [rsp+0x218]
 7b6f611:	00
 7b6f612:	e8 49 de b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6f617:	48 8d 0d 2a 08 0c 02 	lea    rcx,[rip+0x20c082a]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6f61e:	48 89 8c 24 98 01 00 	mov    QWORD PTR [rsp+0x198],rcx
 7b6f625:	00
 7b6f626:	48 89 84 24 a0 01 00 	mov    QWORD PTR [rsp+0x1a0],rax
 7b6f62d:	00
 7b6f62e:	48 8d 05 d7 72 04 fd 	lea    rax,[rip+0xfffffffffd0472d7]        # 4bb690c <_cgo_topofstack@@Base-0xad1914>
 7b6f635:	bb 0e 00 00 00       	mov    ebx,0xe
 7b6f63a:	48 8d 8c 24 98 01 00 	lea    rcx,[rsp+0x198]
 7b6f641:	00
 7b6f642:	bf 01 00 00 00       	mov    edi,0x1
 7b6f647:	89 fe                	mov    esi,edi
 7b6f649:	e8 d2 a0 ba fd       	call   5719720 <_cgo_topofstack@@Base+0x91500>
 7b6f64e:	48 8b 4c 24 70       	mov    rcx,QWORD PTR [rsp+0x70]
 7b6f653:	48 ff c1             	inc    rcx
 7b6f656:	48 8b 94 24 a8 00 00 	mov    rdx,QWORD PTR [rsp+0xa8]
 7b6f65d:	00
 7b6f65e:	66 90                	xchg   ax,ax
 7b6f660:	48 39 ca             	cmp    rdx,rcx
 7b6f663:	72 0a                	jb     7b6f66f <crosscall2@@Base+0x2385d0f>
 7b6f665:	48 8b b4 24 c8 01 00 	mov    rsi,QWORD PTR [rsp+0x1c8]
 7b6f66c:	00
 7b6f66d:	eb 48                	jmp    7b6f6b7 <crosscall2@@Base+0x2385d57>
 7b6f66f:	48 89 9c 24 b8 00 00 	mov    QWORD PTR [rsp+0xb8],rbx
 7b6f676:	00
 7b6f677:	48 89 84 24 d8 01 00 	mov    QWORD PTR [rsp+0x1d8],rax
 7b6f67e:	00
 7b6f67f:	48 8b 84 24 c8 01 00 	mov    rax,QWORD PTR [rsp+0x1c8]
 7b6f686:	00
 7b6f687:	48 89 cb             	mov    rbx,rcx
 7b6f68a:	48 89 d1             	mov    rcx,rdx
 7b6f68d:	bf 01 00 00 00       	mov    edi,0x1
 7b6f692:	48 8d 35 af 07 0c 02 	lea    rsi,[rip+0x20c07af]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6f699:	e8 62 34 b1 fd       	call   5682b00 <_cgo_topofstack@@Base-0x5720>
 7b6f69e:	48 89 c6             	mov    rsi,rax
 7b6f6a1:	48 89 ca             	mov    rdx,rcx
 7b6f6a4:	48 8b 84 24 d8 01 00 	mov    rax,QWORD PTR [rsp+0x1d8]
 7b6f6ab:	00
 7b6f6ac:	48 89 d9             	mov    rcx,rbx
 7b6f6af:	48 8b 9c 24 b8 00 00 	mov    rbx,QWORD PTR [rsp+0xb8]
 7b6f6b6:	00
 7b6f6b7:	4c 8d 41 ff          	lea    r8,[rcx-0x1]
 7b6f6bb:	49 c1 e0 04          	shl    r8,0x4
 7b6f6bf:	4a 89 5c 06 08       	mov    QWORD PTR [rsi+r8*1+0x8],rbx
 7b6f6c4:	83 3d 25 3a 0d 07 00 	cmp    DWORD PTR [rip+0x70d3a25],0x0        # ec430f0 <_ZTISt10bad_typeid@@Base+0x454ebb0>
 7b6f6cb:	74 10                	je     7b6f6dd <crosscall2@@Base+0x2385d7d>
 7b6f6cd:	4e 8b 0c 06          	mov    r9,QWORD PTR [rsi+r8*1]
 7b6f6d1:	e8 ea 8b b1 fd       	call   56882c0 <_cgo_topofstack@@Base+0xa0>
 7b6f6d6:	49 89 03             	mov    QWORD PTR [r11],rax
 7b6f6d9:	4d 89 4b 08          	mov    QWORD PTR [r11+0x8],r9
 7b6f6dd:	4a 89 04 06          	mov    QWORD PTR [rsi+r8*1],rax
 7b6f6e1:	48 89 f0             	mov    rax,rsi
 7b6f6e4:	48 89 cb             	mov    rbx,rcx
 7b6f6e7:	48 89 d1             	mov    rcx,rdx
 7b6f6ea:	48 89 da             	mov    rdx,rbx
 7b6f6ed:	44 0f b6 84 24 28 02 	movzx  r8d,BYTE PTR [rsp+0x228]
 7b6f6f4:	00 00
 7b6f6f6:	45 84 c0             	test   r8b,r8b
 7b6f6f9:	74 4e                	je     7b6f749 <crosscall2@@Base+0x2385de9>
 7b6f6fb:	48 8d 5a 01          	lea    rbx,[rdx+0x1]
 7b6f6ff:	90                   	nop
 7b6f700:	48 39 d9             	cmp    rcx,rbx
 7b6f703:	73 11                	jae    7b6f716 <crosscall2@@Base+0x2385db6>
 7b6f705:	bf 01 00 00 00       	mov    edi,0x1
 7b6f70a:	48 8d 35 37 07 0c 02 	lea    rsi,[rip+0x20c0737]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6f711:	e8 ea 33 b1 fd       	call   5682b00 <_cgo_topofstack@@Base-0x5720>
 7b6f716:	48 8d 53 ff          	lea    rdx,[rbx-0x1]
 7b6f71a:	48 c1 e2 04          	shl    rdx,0x4
 7b6f71e:	48 c7 44 10 08 1b 00 	mov    QWORD PTR [rax+rdx*1+0x8],0x1b
 7b6f725:	00 00
 7b6f727:	83 3d c2 39 0d 07 00 	cmp    DWORD PTR [rip+0x70d39c2],0x0        # ec430f0 <_ZTISt10bad_typeid@@Base+0x454ebb0>
 7b6f72e:	74 0c                	je     7b6f73c <crosscall2@@Base+0x2385ddc>
 7b6f730:	4c 8b 04 10          	mov    r8,QWORD PTR [rax+rdx*1]
 7b6f734:	e8 67 8b b1 fd       	call   56882a0 <_cgo_topofstack@@Base+0x80>
 7b6f739:	4d 89 03             	mov    QWORD PTR [r11],r8
 7b6f73c:	4c 8d 05 5b ce 11 fd 	lea    r8,[rip+0xfffffffffd11ce5b]        # 4c8c59e <_cgo_topofstack@@Base-0x9fbc82>
 7b6f743:	4c 89 04 10          	mov    QWORD PTR [rax+rdx*1],r8
 7b6f747:	eb 03                	jmp    7b6f74c <crosscall2@@Base+0x2385dec>
 7b6f749:	48 89 d3             	mov    rbx,rdx
 7b6f74c:	48 89 84 24 f8 00 00 	mov    QWORD PTR [rsp+0xf8],rax
 7b6f753:	00
 7b6f754:	48 89 4c 24 78       	mov    QWORD PTR [rsp+0x78],rcx
 7b6f759:	48 89 5c 24 70       	mov    QWORD PTR [rsp+0x70],rbx
 7b6f75e:	48 8b 15 1b 12 15 03 	mov    rdx,QWORD PTR [rip+0x315121b]        # acc0980 <_ZTISt10bad_typeid@@Base+0x5cc440>
 7b6f765:	4c 8b 05 1c 12 15 03 	mov    r8,QWORD PTR [rip+0x315121c]        # acc0988 <_ZTISt10bad_typeid@@Base+0x5cc448>
 7b6f76c:	4c 8b 8c 24 f8 01 00 	mov    r9,QWORD PTR [rsp+0x1f8]
 7b6f773:	00
 7b6f774:	4d 85 c9             	test   r9,r9
 7b6f777:	75 0c                	jne    7b6f785 <crosscall2@@Base+0x2385e25>
 7b6f779:	31 f6                	xor    esi,esi
 7b6f77b:	31 ff                	xor    edi,edi
 7b6f77d:	0f 1f 00             	nop    DWORD PTR [rax]
 7b6f780:	e9 66 01 00 00       	jmp    7b6f8eb <crosscall2@@Base+0x2385f8b>
 7b6f785:	4c 89 44 24 40       	mov    QWORD PTR [rsp+0x40],r8
 7b6f78a:	48 89 94 24 c8 00 00 	mov    QWORD PTR [rsp+0xc8],rdx
 7b6f791:	00
 7b6f792:	49 8b 51 30          	mov    rdx,QWORD PTR [r9+0x30]
 7b6f796:	48 8b 84 24 00 02 00 	mov    rax,QWORD PTR [rsp+0x200]
 7b6f79d:	00
 7b6f79e:	48 8d 1d eb f9 1b 02 	lea    rbx,[rip+0x21bf9eb]        # 9d2f190 <__lxstat@plt+0xf91ec0>
 7b6f7a5:	48 8d 0d 34 0b 0d 07 	lea    rcx,[rip+0x70d0b34]        # ec402e0 <_ZTISt10bad_typeid@@Base+0x454bda0>
 7b6f7ac:	ff d2                	call   rdx
 7b6f7ae:	48 8d 15 7b 58 4e 02 	lea    rdx,[rip+0x24e587b]        # a055030 <__lxstat@plt+0x12b7d60>
 7b6f7b5:	48 39 d0             	cmp    rax,rdx
 7b6f7b8:	75 11                	jne    7b6f7cb <crosscall2@@Base+0x2385e6b>
 7b6f7ba:	48 8b 03             	mov    rax,QWORD PTR [rbx]
 7b6f7bd:	48 8b 53 08          	mov    rdx,QWORD PTR [rbx+0x8]
 7b6f7c1:	48 8b 73 10          	mov    rsi,QWORD PTR [rbx+0x10]
 7b6f7c5:	48 8b 7b 18          	mov    rdi,QWORD PTR [rbx+0x18]
 7b6f7c9:	eb 15                	jmp    7b6f7e0 <crosscall2@@Base+0x2385e80>
 7b6f7cb:	bf 00 00 00 00       	mov    edi,0x0
 7b6f7d0:	ba 00 00 00 00       	mov    edx,0x0
 7b6f7d5:	be 00 00 00 00       	mov    esi,0x0
 7b6f7da:	b8 00 00 00 00       	mov    eax,0x0
 7b6f7df:	90                   	nop
 7b6f7e0:	74 12                	je     7b6f7f4 <crosscall2@@Base+0x2385e94>
 7b6f7e2:	48 8b 5c 24 40       	mov    rbx,QWORD PTR [rsp+0x40]
 7b6f7e7:	48 8b 84 24 c8 00 00 	mov    rax,QWORD PTR [rsp+0xc8]
 7b6f7ee:	00
 7b6f7ef:	e9 88 00 00 00       	jmp    7b6f87c <crosscall2@@Base+0x2385f1c>
 7b6f7f4:	48 89 7c 24 60       	mov    QWORD PTR [rsp+0x60],rdi
 7b6f7f9:	48 89 b4 24 e8 00 00 	mov    QWORD PTR [rsp+0xe8],rsi
 7b6f800:	00
 7b6f801:	90                   	nop
 7b6f802:	48 8d 8c 24 78 01 00 	lea    rcx,[rsp+0x178]
 7b6f809:	00
 7b6f80a:	44 0f 11 39          	movups XMMWORD PTR [rcx],xmm15
 7b6f80e:	44 0f 11 79 10       	movups XMMWORD PTR [rcx+0x10],xmm15
 7b6f813:	48 89 d3             	mov    rbx,rdx
 7b6f816:	e8 45 dc b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6f81b:	48 8d 0d 26 06 0c 02 	lea    rcx,[rip+0x20c0626]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6f822:	48 89 8c 24 78 01 00 	mov    QWORD PTR [rsp+0x178],rcx
 7b6f829:	00
 7b6f82a:	48 89 84 24 80 01 00 	mov    QWORD PTR [rsp+0x180],rax
 7b6f831:	00
 7b6f832:	48 8b 84 24 e8 00 00 	mov    rax,QWORD PTR [rsp+0xe8]
 7b6f839:	00
 7b6f83a:	48 8b 5c 24 60       	mov    rbx,QWORD PTR [rsp+0x60]
 7b6f83f:	90                   	nop
 7b6f840:	e8 1b dc b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6f845:	48 8d 0d fc 05 0c 02 	lea    rcx,[rip+0x20c05fc]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6f84c:	48 89 8c 24 88 01 00 	mov    QWORD PTR [rsp+0x188],rcx
 7b6f853:	00
 7b6f854:	48 89 84 24 90 01 00 	mov    QWORD PTR [rsp+0x190],rax
 7b6f85b:	00
 7b6f85c:	48 8d 05 77 30 f8 fc 	lea    rax,[rip+0xfffffffffcf83077]        # 4af28da <_cgo_topofstack@@Base-0xb95946>
 7b6f863:	bb 05 00 00 00       	mov    ebx,0x5
 7b6f868:	48 8d 8c 24 78 01 00 	lea    rcx,[rsp+0x178]
 7b6f86f:	00
 7b6f870:	bf 02 00 00 00       	mov    edi,0x2
 7b6f875:	89 fe                	mov    esi,edi
 7b6f877:	e8 a4 9e ba fd       	call   5719720 <_cgo_topofstack@@Base+0x91500>
 7b6f87c:	48 89 84 24 f0 00 00 	mov    QWORD PTR [rsp+0xf0],rax
 7b6f883:	00
 7b6f884:	48 89 5c 24 68       	mov    QWORD PTR [rsp+0x68],rbx
 7b6f889:	48 8b 94 24 f8 01 00 	mov    rdx,QWORD PTR [rsp+0x1f8]
 7b6f890:	00
 7b6f891:	48 8b 52 30          	mov    rdx,QWORD PTR [rdx+0x30]
 7b6f895:	48 8b 84 24 00 02 00 	mov    rax,QWORD PTR [rsp+0x200]
 7b6f89c:	00
 7b6f89d:	48 8d 1d ec b6 1b 02 	lea    rbx,[rip+0x21bb6ec]        # 9d2af90 <__lxstat@plt+0xf8dcc0>
 7b6f8a4:	48 8d 0d 35 0a 0d 07 	lea    rcx,[rip+0x70d0a35]        # ec402e0 <_ZTISt10bad_typeid@@Base+0x454bda0>
 7b6f8ab:	ff d2                	call   rdx
 7b6f8ad:	48 8d 15 94 05 0c 02 	lea    rdx,[rip+0x20c0594]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6f8b4:	48 39 d0             	cmp    rax,rdx
 7b6f8b7:	75 09                	jne    7b6f8c2 <crosscall2@@Base+0x2385f62>
 7b6f8b9:	48 8b 03             	mov    rax,QWORD PTR [rbx]
 7b6f8bc:	48 8b 5b 08          	mov    rbx,QWORD PTR [rbx+0x8]
 7b6f8c0:	eb 04                	jmp    7b6f8c6 <crosscall2@@Base+0x2385f66>
 7b6f8c2:	31 db                	xor    ebx,ebx
 7b6f8c4:	31 c0                	xor    eax,eax
 7b6f8c6:	48 8b 4c 24 78       	mov    rcx,QWORD PTR [rsp+0x78]
 7b6f8cb:	4c 8b 44 24 68       	mov    r8,QWORD PTR [rsp+0x68]
 7b6f8d0:	48 89 de             	mov    rsi,rbx
 7b6f8d3:	48 8b 94 24 f0 00 00 	mov    rdx,QWORD PTR [rsp+0xf0]
 7b6f8da:	00
 7b6f8db:	48 89 c7             	mov    rdi,rax
 7b6f8de:	48 8b 84 24 f8 00 00 	mov    rax,QWORD PTR [rsp+0xf8]
 7b6f8e5:	00
 7b6f8e6:	48 8b 5c 24 70       	mov    rbx,QWORD PTR [rsp+0x70]
 7b6f8eb:	48 89 bc 24 e0 00 00 	mov    QWORD PTR [rsp+0xe0],rdi
 7b6f8f2:	00
 7b6f8f3:	48 89 74 24 58       	mov    QWORD PTR [rsp+0x58],rsi
 7b6f8f8:	0f 1f 84 00 00 00 00 	nop    DWORD PTR [rax+rax*1+0x0]
 7b6f8ff:	00
 7b6f900:	4d 85 c0             	test   r8,r8
 7b6f903:	0f 84 ef 00 00 00    	je     7b6f9f8 <crosscall2@@Base+0x2386098>
 7b6f909:	44 0f 11 bc 24 98 01 	movups XMMWORD PTR [rsp+0x198],xmm15
 7b6f910:	00 00
 7b6f912:	48 89 d0             	mov    rax,rdx
 7b6f915:	4c 89 c3             	mov    rbx,r8
 7b6f918:	e8 43 db b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6f91d:	48 8d 0d 24 05 0c 02 	lea    rcx,[rip+0x20c0524]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6f924:	48 89 8c 24 98 01 00 	mov    QWORD PTR [rsp+0x198],rcx
 7b6f92b:	00
 7b6f92c:	48 89 84 24 a0 01 00 	mov    QWORD PTR [rsp+0x1a0],rax
 7b6f933:	00
 7b6f934:	48 8d 05 b4 92 01 fd 	lea    rax,[rip+0xfffffffffd0192b4]        # 4b88bef <_cgo_topofstack@@Base-0xaff631>
 7b6f93b:	bb 0c 00 00 00       	mov    ebx,0xc
 7b6f940:	48 8d 8c 24 98 01 00 	lea    rcx,[rsp+0x198]
 7b6f947:	00
 7b6f948:	bf 01 00 00 00       	mov    edi,0x1
 7b6f94d:	89 fe                	mov    esi,edi
 7b6f94f:	e8 cc 9d ba fd       	call   5719720 <_cgo_topofstack@@Base+0x91500>
 7b6f954:	48 8b 4c 24 70       	mov    rcx,QWORD PTR [rsp+0x70]
 7b6f959:	48 ff c1             	inc    rcx
 7b6f95c:	48 8b 54 24 78       	mov    rdx,QWORD PTR [rsp+0x78]
 7b6f961:	48 39 ca             	cmp    rdx,rcx
 7b6f964:	72 0a                	jb     7b6f970 <crosscall2@@Base+0x2386010>
 7b6f966:	48 8b b4 24 f8 00 00 	mov    rsi,QWORD PTR [rsp+0xf8]
 7b6f96d:	00
 7b6f96e:	eb 48                	jmp    7b6f9b8 <crosscall2@@Base+0x2386058>
 7b6f970:	48 89 9c 24 b8 00 00 	mov    QWORD PTR [rsp+0xb8],rbx
 7b6f977:	00
 7b6f978:	48 89 84 24 d8 01 00 	mov    QWORD PTR [rsp+0x1d8],rax
 7b6f97f:	00
 7b6f980:	48 8b 84 24 f8 00 00 	mov    rax,QWORD PTR [rsp+0xf8]
 7b6f987:	00
 7b6f988:	48 89 cb             	mov    rbx,rcx
 7b6f98b:	48 89 d1             	mov    rcx,rdx
 7b6f98e:	bf 01 00 00 00       	mov    edi,0x1
 7b6f993:	48 8d 35 ae 04 0c 02 	lea    rsi,[rip+0x20c04ae]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6f99a:	e8 61 31 b1 fd       	call   5682b00 <_cgo_topofstack@@Base-0x5720>
 7b6f99f:	48 89 c6             	mov    rsi,rax
 7b6f9a2:	48 89 ca             	mov    rdx,rcx
 7b6f9a5:	48 8b 84 24 d8 01 00 	mov    rax,QWORD PTR [rsp+0x1d8]
 7b6f9ac:	00
 7b6f9ad:	48 89 d9             	mov    rcx,rbx
 7b6f9b0:	48 8b 9c 24 b8 00 00 	mov    rbx,QWORD PTR [rsp+0xb8]
 7b6f9b7:	00
 7b6f9b8:	4c 8d 41 ff          	lea    r8,[rcx-0x1]
 7b6f9bc:	49 c1 e0 04          	shl    r8,0x4
 7b6f9c0:	4a 89 5c 06 08       	mov    QWORD PTR [rsi+r8*1+0x8],rbx
 7b6f9c5:	83 3d 24 37 0d 07 00 	cmp    DWORD PTR [rip+0x70d3724],0x0        # ec430f0 <_ZTISt10bad_typeid@@Base+0x454ebb0>
 7b6f9cc:	74 10                	je     7b6f9de <crosscall2@@Base+0x238607e>
 7b6f9ce:	4e 8b 0c 06          	mov    r9,QWORD PTR [rsi+r8*1]
 7b6f9d2:	e8 e9 88 b1 fd       	call   56882c0 <_cgo_topofstack@@Base+0xa0>
 7b6f9d7:	49 89 03             	mov    QWORD PTR [r11],rax
 7b6f9da:	4d 89 4b 08          	mov    QWORD PTR [r11+0x8],r9
 7b6f9de:	4a 89 04 06          	mov    QWORD PTR [rsi+r8*1],rax
 7b6f9e2:	48 8b bc 24 e0 00 00 	mov    rdi,QWORD PTR [rsp+0xe0]
 7b6f9e9:	00
 7b6f9ea:	48 89 f0             	mov    rax,rsi
 7b6f9ed:	48 89 cb             	mov    rbx,rcx
 7b6f9f0:	48 8b 74 24 58       	mov    rsi,QWORD PTR [rsp+0x58]
 7b6f9f5:	48 89 d1             	mov    rcx,rdx
 7b6f9f8:	48 8b 15 99 0f 15 03 	mov    rdx,QWORD PTR [rip+0x3150f99]        # acc0998 <_ZTISt10bad_typeid@@Base+0x5cc458>
 7b6f9ff:	90                   	nop
 7b6fa00:	48 85 d2             	test   rdx,rdx
 7b6fa03:	0f 84 17 01 00 00    	je     7b6fb20 <crosscall2@@Base+0x23861c0>
 7b6fa09:	48 89 8c 24 a0 00 00 	mov    QWORD PTR [rsp+0xa0],rcx
 7b6fa10:	00
 7b6fa11:	48 89 9c 24 98 00 00 	mov    QWORD PTR [rsp+0x98],rbx
 7b6fa18:	00
 7b6fa19:	48 89 84 24 c0 01 00 	mov    QWORD PTR [rsp+0x1c0],rax
 7b6fa20:	00
 7b6fa21:	48 8b 05 68 0f 15 03 	mov    rax,QWORD PTR [rip+0x3150f68]        # acc0990 <_ZTISt10bad_typeid@@Base+0x5cc450>
 7b6fa28:	44 0f 11 bc 24 98 01 	movups XMMWORD PTR [rsp+0x198],xmm15
 7b6fa2f:	00 00
 7b6fa31:	48 89 d3             	mov    rbx,rdx
 7b6fa34:	e8 27 da b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6fa39:	48 8d 0d 08 04 0c 02 	lea    rcx,[rip+0x20c0408]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6fa40:	48 89 8c 24 98 01 00 	mov    QWORD PTR [rsp+0x198],rcx
 7b6fa47:	00
 7b6fa48:	48 89 84 24 a0 01 00 	mov    QWORD PTR [rsp+0x1a0],rax
 7b6fa4f:	00
 7b6fa50:	48 8d 05 7d b2 05 fd 	lea    rax,[rip+0xfffffffffd05b27d]        # 4bcacd4 <_cgo_topofstack@@Base-0xabd54c>
 7b6fa57:	bb 0f 00 00 00       	mov    ebx,0xf
 7b6fa5c:	48 8d 8c 24 98 01 00 	lea    rcx,[rsp+0x198]
 7b6fa63:	00
 7b6fa64:	bf 01 00 00 00       	mov    edi,0x1
 7b6fa69:	89 fe                	mov    esi,edi
 7b6fa6b:	e8 b0 9c ba fd       	call   5719720 <_cgo_topofstack@@Base+0x91500>
 7b6fa70:	48 8b 8c 24 98 00 00 	mov    rcx,QWORD PTR [rsp+0x98]
 7b6fa77:	00
 7b6fa78:	48 ff c1             	inc    rcx
 7b6fa7b:	48 8b 94 24 a0 00 00 	mov    rdx,QWORD PTR [rsp+0xa0]
 7b6fa82:	00
 7b6fa83:	48 39 ca             	cmp    rdx,rcx
 7b6fa86:	72 0a                	jb     7b6fa92 <crosscall2@@Base+0x2386132>
 7b6fa88:	48 8b b4 24 c0 01 00 	mov    rsi,QWORD PTR [rsp+0x1c0]
 7b6fa8f:	00
 7b6fa90:	eb 4c                	jmp    7b6fade <crosscall2@@Base+0x238617e>
 7b6fa92:	48 89 9c 24 b8 00 00 	mov    QWORD PTR [rsp+0xb8],rbx
 7b6fa99:	00
 7b6fa9a:	48 89 84 24 d8 01 00 	mov    QWORD PTR [rsp+0x1d8],rax
 7b6faa1:	00
 7b6faa2:	48 8b 84 24 c0 01 00 	mov    rax,QWORD PTR [rsp+0x1c0]
 7b6faa9:	00
 7b6faaa:	48 89 cb             	mov    rbx,rcx
 7b6faad:	48 89 d1             	mov    rcx,rdx
 7b6fab0:	bf 01 00 00 00       	mov    edi,0x1
 7b6fab5:	48 8d 35 8c 03 0c 02 	lea    rsi,[rip+0x20c038c]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6fabc:	0f 1f 40 00          	nop    DWORD PTR [rax+0x0]
 7b6fac0:	e8 3b 30 b1 fd       	call   5682b00 <_cgo_topofstack@@Base-0x5720>
 7b6fac5:	48 89 c6             	mov    rsi,rax
 7b6fac8:	48 89 ca             	mov    rdx,rcx
 7b6facb:	48 8b 84 24 d8 01 00 	mov    rax,QWORD PTR [rsp+0x1d8]
 7b6fad2:	00
 7b6fad3:	48 89 d9             	mov    rcx,rbx
 7b6fad6:	48 8b 9c 24 b8 00 00 	mov    rbx,QWORD PTR [rsp+0xb8]
 7b6fadd:	00
 7b6fade:	4c 8d 41 ff          	lea    r8,[rcx-0x1]
 7b6fae2:	49 c1 e0 04          	shl    r8,0x4
 7b6fae6:	4a 89 5c 06 08       	mov    QWORD PTR [rsi+r8*1+0x8],rbx
 7b6faeb:	83 3d fe 35 0d 07 00 	cmp    DWORD PTR [rip+0x70d35fe],0x0        # ec430f0 <_ZTISt10bad_typeid@@Base+0x454ebb0>
 7b6faf2:	74 10                	je     7b6fb04 <crosscall2@@Base+0x23861a4>
 7b6faf4:	4e 8b 0c 06          	mov    r9,QWORD PTR [rsi+r8*1]
 7b6faf8:	e8 c3 87 b1 fd       	call   56882c0 <_cgo_topofstack@@Base+0xa0>
 7b6fafd:	49 89 03             	mov    QWORD PTR [r11],rax
 7b6fb00:	4d 89 4b 08          	mov    QWORD PTR [r11+0x8],r9
 7b6fb04:	4a 89 04 06          	mov    QWORD PTR [rsi+r8*1],rax
 7b6fb08:	48 8b bc 24 e0 00 00 	mov    rdi,QWORD PTR [rsp+0xe0]
 7b6fb0f:	00
 7b6fb10:	48 89 f0             	mov    rax,rsi
 7b6fb13:	48 89 cb             	mov    rbx,rcx
 7b6fb16:	48 8b 74 24 58       	mov    rsi,QWORD PTR [rsp+0x58]
 7b6fb1b:	48 89 d1             	mov    rcx,rdx
 7b6fb1e:	66 90                	xchg   ax,ax
 7b6fb20:	48 85 f6             	test   rsi,rsi
 7b6fb23:	0f 84 01 01 00 00    	je     7b6fc2a <crosscall2@@Base+0x23862ca>
 7b6fb29:	48 89 8c 24 90 00 00 	mov    QWORD PTR [rsp+0x90],rcx
 7b6fb30:	00
 7b6fb31:	48 89 9c 24 88 00 00 	mov    QWORD PTR [rsp+0x88],rbx
 7b6fb38:	00
 7b6fb39:	48 89 84 24 b8 01 00 	mov    QWORD PTR [rsp+0x1b8],rax
 7b6fb40:	00
 7b6fb41:	44 0f 11 bc 24 98 01 	movups XMMWORD PTR [rsp+0x198],xmm15
 7b6fb48:	00 00
 7b6fb4a:	48 89 f8             	mov    rax,rdi
 7b6fb4d:	48 89 f3             	mov    rbx,rsi
 7b6fb50:	e8 0b d9 b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6fb55:	48 8d 0d ec 02 0c 02 	lea    rcx,[rip+0x20c02ec]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6fb5c:	48 89 8c 24 98 01 00 	mov    QWORD PTR [rsp+0x198],rcx
 7b6fb63:	00
 7b6fb64:	48 89 84 24 a0 01 00 	mov    QWORD PTR [rsp+0x1a0],rax
 7b6fb6b:	00
 7b6fb6c:	48 8d 05 36 6b fe fc 	lea    rax,[rip+0xfffffffffcfe6b36]        # 4b566a9 <_cgo_topofstack@@Base-0xb31b77>
 7b6fb73:	bb 0a 00 00 00       	mov    ebx,0xa
 7b6fb78:	48 8d 8c 24 98 01 00 	lea    rcx,[rsp+0x198]
 7b6fb7f:	00
 7b6fb80:	bf 01 00 00 00       	mov    edi,0x1
 7b6fb85:	89 fe                	mov    esi,edi
 7b6fb87:	e8 94 9b ba fd       	call   5719720 <_cgo_topofstack@@Base+0x91500>
 7b6fb8c:	48 8b 8c 24 88 00 00 	mov    rcx,QWORD PTR [rsp+0x88]
 7b6fb93:	00
 7b6fb94:	48 ff c1             	inc    rcx
 7b6fb97:	48 8b 94 24 90 00 00 	mov    rdx,QWORD PTR [rsp+0x90]
 7b6fb9e:	00
 7b6fb9f:	90                   	nop
 7b6fba0:	48 39 ca             	cmp    rdx,rcx
 7b6fba3:	72 0a                	jb     7b6fbaf <crosscall2@@Base+0x238624f>
 7b6fba5:	48 8b b4 24 b8 01 00 	mov    rsi,QWORD PTR [rsp+0x1b8]
 7b6fbac:	00
 7b6fbad:	eb 48                	jmp    7b6fbf7 <crosscall2@@Base+0x2386297>
 7b6fbaf:	48 89 9c 24 b8 00 00 	mov    QWORD PTR [rsp+0xb8],rbx
 7b6fbb6:	00
 7b6fbb7:	48 89 84 24 d8 01 00 	mov    QWORD PTR [rsp+0x1d8],rax
 7b6fbbe:	00
 7b6fbbf:	48 8b 84 24 b8 01 00 	mov    rax,QWORD PTR [rsp+0x1b8]
 7b6fbc6:	00
 7b6fbc7:	48 89 cb             	mov    rbx,rcx
 7b6fbca:	48 89 d1             	mov    rcx,rdx
 7b6fbcd:	bf 01 00 00 00       	mov    edi,0x1
 7b6fbd2:	48 8d 35 6f 02 0c 02 	lea    rsi,[rip+0x20c026f]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6fbd9:	e8 22 2f b1 fd       	call   5682b00 <_cgo_topofstack@@Base-0x5720>
 7b6fbde:	48 89 c6             	mov    rsi,rax
 7b6fbe1:	48 89 ca             	mov    rdx,rcx
 7b6fbe4:	48 8b 84 24 d8 01 00 	mov    rax,QWORD PTR [rsp+0x1d8]
 7b6fbeb:	00
 7b6fbec:	48 89 d9             	mov    rcx,rbx
 7b6fbef:	48 8b 9c 24 b8 00 00 	mov    rbx,QWORD PTR [rsp+0xb8]
 7b6fbf6:	00
 7b6fbf7:	4c 8d 41 ff          	lea    r8,[rcx-0x1]
 7b6fbfb:	49 c1 e0 04          	shl    r8,0x4
 7b6fbff:	4a 89 5c 06 08       	mov    QWORD PTR [rsi+r8*1+0x8],rbx
 7b6fc04:	83 3d e5 34 0d 07 00 	cmp    DWORD PTR [rip+0x70d34e5],0x0        # ec430f0 <_ZTISt10bad_typeid@@Base+0x454ebb0>
 7b6fc0b:	74 10                	je     7b6fc1d <crosscall2@@Base+0x23862bd>
 7b6fc0d:	4e 8b 0c 06          	mov    r9,QWORD PTR [rsi+r8*1]
 7b6fc11:	e8 aa 86 b1 fd       	call   56882c0 <_cgo_topofstack@@Base+0xa0>
 7b6fc16:	49 89 03             	mov    QWORD PTR [r11],rax
 7b6fc19:	4d 89 4b 08          	mov    QWORD PTR [r11+0x8],r9
 7b6fc1d:	4a 89 04 06          	mov    QWORD PTR [rsi+r8*1],rax
 7b6fc21:	48 89 cb             	mov    rbx,rcx
 7b6fc24:	48 89 f0             	mov    rax,rsi
 7b6fc27:	48 89 d1             	mov    rcx,rdx
 7b6fc2a:	48 8d 3d b3 05 f7 fc 	lea    rdi,[rip+0xfffffffffcf705b3]        # 4ae01e4 <_cgo_topofstack@@Base-0xba803c>
 7b6fc31:	be 02 00 00 00       	mov    esi,0x2
 7b6fc36:	e8 a5 b6 bc fd       	call   573b2e0 <_cgo_topofstack@@Base+0xb30c0>
 7b6fc3b:	48 89 84 24 00 01 00 	mov    QWORD PTR [rsp+0x100],rax
 7b6fc42:	00
 7b6fc43:	48 89 9c 24 80 00 00 	mov    QWORD PTR [rsp+0x80],rbx
 7b6fc4a:	00
 7b6fc4b:	48 8b 54 24 48       	mov    rdx,QWORD PTR [rsp+0x48]
 7b6fc50:	48 85 d2             	test   rdx,rdx
 7b6fc53:	0f 84 e8 00 00 00    	je     7b6fd41 <crosscall2@@Base+0x23863e1>
 7b6fc59:	48 8d 8c 24 38 01 00 	lea    rcx,[rsp+0x138]
 7b6fc60:	00
 7b6fc61:	44 0f 11 39          	movups XMMWORD PTR [rcx],xmm15
 7b6fc65:	44 0f 11 79 10       	movups XMMWORD PTR [rcx+0x10],xmm15
 7b6fc6a:	44 0f 11 79 20       	movups XMMWORD PTR [rcx+0x20],xmm15
 7b6fc6f:	44 0f 11 79 30       	movups XMMWORD PTR [rcx+0x30],xmm15
 7b6fc74:	48 8b 84 24 e0 01 00 	mov    rax,QWORD PTR [rsp+0x1e0]
 7b6fc7b:	00
 7b6fc7c:	48 8b 9c 24 c0 00 00 	mov    rbx,QWORD PTR [rsp+0xc0]
 7b6fc83:	00
 7b6fc84:	e8 d7 d7 b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6fc89:	48 8d 0d b8 01 0c 02 	lea    rcx,[rip+0x20c01b8]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6fc90:	48 89 8c 24 38 01 00 	mov    QWORD PTR [rsp+0x138],rcx
 7b6fc97:	00
 7b6fc98:	48 89 84 24 40 01 00 	mov    QWORD PTR [rsp+0x140],rax
 7b6fc9f:	00
 7b6fca0:	48 8b 84 24 d0 00 00 	mov    rax,QWORD PTR [rsp+0xd0]
 7b6fca7:	00
 7b6fca8:	48 8b 5c 24 48       	mov    rbx,QWORD PTR [rsp+0x48]
 7b6fcad:	e8 ae d7 b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6fcb2:	48 8d 0d 8f 01 0c 02 	lea    rcx,[rip+0x20c018f]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6fcb9:	48 89 8c 24 48 01 00 	mov    QWORD PTR [rsp+0x148],rcx
 7b6fcc0:	00
 7b6fcc1:	48 89 84 24 50 01 00 	mov    QWORD PTR [rsp+0x150],rax
 7b6fcc8:	00
 7b6fcc9:	48 8b 84 24 d8 00 00 	mov    rax,QWORD PTR [rsp+0xd8]
 7b6fcd0:	00
 7b6fcd1:	48 8b 5c 24 50       	mov    rbx,QWORD PTR [rsp+0x50]
 7b6fcd6:	e8 85 d7 b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6fcdb:	48 8d 0d 66 01 0c 02 	lea    rcx,[rip+0x20c0166]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6fce2:	48 89 8c 24 58 01 00 	mov    QWORD PTR [rsp+0x158],rcx
 7b6fce9:	00
 7b6fcea:	48 89 84 24 60 01 00 	mov    QWORD PTR [rsp+0x160],rax
 7b6fcf1:	00
 7b6fcf2:	48 8b 84 24 00 01 00 	mov    rax,QWORD PTR [rsp+0x100]
 7b6fcf9:	00
 7b6fcfa:	48 8b 9c 24 80 00 00 	mov    rbx,QWORD PTR [rsp+0x80]
 7b6fd01:	00
 7b6fd02:	e8 59 d7 b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6fd07:	48 8d 0d 3a 01 0c 02 	lea    rcx,[rip+0x20c013a]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6fd0e:	48 89 8c 24 68 01 00 	mov    QWORD PTR [rsp+0x168],rcx
 7b6fd15:	00
 7b6fd16:	48 89 84 24 70 01 00 	mov    QWORD PTR [rsp+0x170],rax
 7b6fd1d:	00
 7b6fd1e:	48 8d 05 26 07 03 fd 	lea    rax,[rip+0xfffffffffd030726]        # 4ba044b <_cgo_topofstack@@Base-0xae7dd5>
 7b6fd25:	bb 0d 00 00 00       	mov    ebx,0xd
 7b6fd2a:	48 8d 8c 24 38 01 00 	lea    rcx,[rsp+0x138]
 7b6fd31:	00
 7b6fd32:	bf 04 00 00 00       	mov    edi,0x4
 7b6fd37:	89 fe                	mov    esi,edi
 7b6fd39:	e8 e2 99 ba fd       	call   5719720 <_cgo_topofstack@@Base+0x91500>
 7b6fd3e:	c9                   	leave
 7b6fd3f:	90                   	nop
 7b6fd40:	c3                   	ret
 7b6fd41:	48 8d 8c 24 08 01 00 	lea    rcx,[rsp+0x108]
 7b6fd48:	00
 7b6fd49:	44 0f 11 39          	movups XMMWORD PTR [rcx],xmm15
 7b6fd4d:	44 0f 11 79 10       	movups XMMWORD PTR [rcx+0x10],xmm15
 7b6fd52:	44 0f 11 79 20       	movups XMMWORD PTR [rcx+0x20],xmm15
 7b6fd57:	48 8b 84 24 e0 01 00 	mov    rax,QWORD PTR [rsp+0x1e0]
 7b6fd5e:	00
 7b6fd5f:	48 8b 9c 24 c0 00 00 	mov    rbx,QWORD PTR [rsp+0xc0]
 7b6fd66:	00
 7b6fd67:	e8 f4 d6 b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6fd6c:	48 8d 0d d5 00 0c 02 	lea    rcx,[rip+0x20c00d5]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6fd73:	48 89 8c 24 08 01 00 	mov    QWORD PTR [rsp+0x108],rcx
 7b6fd7a:	00
 7b6fd7b:	48 89 84 24 10 01 00 	mov    QWORD PTR [rsp+0x110],rax
 7b6fd82:	00
 7b6fd83:	48 8b 84 24 d8 00 00 	mov    rax,QWORD PTR [rsp+0xd8]
 7b6fd8a:	00
 7b6fd8b:	48 8b 5c 24 50       	mov    rbx,QWORD PTR [rsp+0x50]
 7b6fd90:	e8 cb d6 b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6fd95:	48 8d 0d ac 00 0c 02 	lea    rcx,[rip+0x20c00ac]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6fd9c:	48 89 8c 24 18 01 00 	mov    QWORD PTR [rsp+0x118],rcx
 7b6fda3:	00
 7b6fda4:	48 89 84 24 20 01 00 	mov    QWORD PTR [rsp+0x120],rax
 7b6fdab:	00
 7b6fdac:	48 8b 84 24 00 01 00 	mov    rax,QWORD PTR [rsp+0x100]
 7b6fdb3:	00
 7b6fdb4:	48 8b 9c 24 80 00 00 	mov    rbx,QWORD PTR [rsp+0x80]
 7b6fdbb:	00
 7b6fdbc:	0f 1f 40 00          	nop    DWORD PTR [rax+0x0]
 7b6fdc0:	e8 9b d6 b0 fd       	call   567d460 <_cgo_topofstack@@Base-0xadc0>
 7b6fdc5:	48 8d 0d 7c 00 0c 02 	lea    rcx,[rip+0x20c007c]        # 9c2fe48 <__lxstat@plt+0xe92b78>
 7b6fdcc:	48 89 8c 24 28 01 00 	mov    QWORD PTR [rsp+0x128],rcx
 7b6fdd3:	00
 7b6fdd4:	48 89 84 24 30 01 00 	mov    QWORD PTR [rsp+0x130],rax
 7b6fddb:	00
 7b6fddc:	48 8d 05 d0 68 fe fc 	lea    rax,[rip+0xfffffffffcfe68d0]        # 4b566b3 <_cgo_topofstack@@Base-0xb31b6d>
 7b6fde3:	bb 0a 00 00 00       	mov    ebx,0xa
 7b6fde8:	48 8d 8c 24 08 01 00 	lea    rcx,[rsp+0x108]
 7b6fdef:	00
 7b6fdf0:	bf 03 00 00 00       	mov    edi,0x3
 7b6fdf5:	89 fe                	mov    esi,edi
 7b6fdf7:	e8 24 99 ba fd       	call   5719720 <_cgo_topofstack@@Base+0x91500>
 7b6fdfc:	c9                   	leave
 7b6fdfd:	c3                   	ret
 7b6fdfe:	48 89 44 24 08       	mov    QWORD PTR [rsp+0x8],rax
 7b6fe03:	48 89 5c 24 10       	mov    QWORD PTR [rsp+0x10],rbx
 7b6fe08:	48 89 4c 24 18       	mov    QWORD PTR [rsp+0x18],rcx
 7b6fe0d:	48 89 7c 24 20       	mov    QWORD PTR [rsp+0x20],rdi
 7b6fe12:	48 89 74 24 28       	mov    QWORD PTR [rsp+0x28],rsi
 7b6fe17:	4c 89 44 24 30       	mov    QWORD PTR [rsp+0x30],r8
 7b6fe1c:	44 88 4c 24 38       	mov    BYTE PTR [rsp+0x38],r9b
 7b6fe21:	e8 ba 69 b1 fd       	call   56867e0 <_cgo_topofstack@@Base-0x1a40>
 7b6fe26:	48 8b 44 24 08       	mov    rax,QWORD PTR [rsp+0x8]
 7b6fe2b:	48 8b 5c 24 10       	mov    rbx,QWORD PTR [rsp+0x10]
 7b6fe30:	48 8b 4c 24 18       	mov    rcx,QWORD PTR [rsp+0x18]
 7b6fe35:	48 8b 7c 24 20       	mov    rdi,QWORD PTR [rsp+0x20]
 7b6fe3a:	48 8b 74 24 28       	mov    rsi,QWORD PTR [rsp+0x28]
 7b6fe3f:	4c 8b 44 24 30       	mov    r8,QWORD PTR [rsp+0x30]
 7b6fe44:	44 0f b6 4c 24 38    	movzx  r9d,BYTE PTR [rsp+0x38]
 7b6fe4a:	e9 71 f4 ff ff       	jmp    7b6f2c0 <crosscall2@@Base+0x2385960>
 7b6fe4f:	cc                   	int3
 7b6fe50:	cc                   	int3
 7b6fe51:	cc                   	int3
 7b6fe52:	cc                   	int3
 7b6fe53:	cc                   	int3
 7b6fe54:	cc                   	int3
 7b6fe55:	cc                   	int3
 7b6fe56:	cc                   	int3
 7b6fe57:	cc                   	int3
 7b6fe58:	cc                   	int3
 7b6fe59:	cc                   	int3
 7b6fe5a:	cc                   	int3
 7b6fe5b:	cc                   	int3
 7b6fe5c:	cc                   	int3
 7b6fe5d:	cc                   	int3
 7b6fe5e:	cc                   	int3
 7b6fe5f:	cc                   	int3
