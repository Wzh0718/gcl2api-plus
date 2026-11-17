
/tmp/antigravity-hub-2.31.2-linux-x64：     文件格式 elf64-x86-64


Disassembly of section .text:

00000000074ca500 <crosscall2@@Base+0x1f272e0>:
 74ca500:	4c 8d a4 24 90 fe ff 	lea    r12,[rsp-0x170]
 74ca507:	ff
 74ca508:	4d 3b 66 10          	cmp    r12,QWORD PTR [r14+0x10]
 74ca50c:	0f 86 2c 0b 00 00    	jbe    74cb03e <crosscall2@@Base+0x1f27e1e>
 74ca512:	55                   	push   rbp
 74ca513:	48 89 e5             	mov    rbp,rsp
 74ca516:	48 81 ec e8 01 00 00 	sub    rsp,0x1e8
 74ca51d:	48 89 8c 24 08 02 00 	mov    QWORD PTR [rsp+0x208],rcx
 74ca524:	00
 74ca525:	44 88 8c 24 28 02 00 	mov    BYTE PTR [rsp+0x228],r9b
 74ca52c:	00
 74ca52d:	4c 89 84 24 20 02 00 	mov    QWORD PTR [rsp+0x220],r8
 74ca534:	00
 74ca535:	48 89 b4 24 18 02 00 	mov    QWORD PTR [rsp+0x218],rsi
 74ca53c:	00
 74ca53d:	48 89 9c 24 00 02 00 	mov    QWORD PTR [rsp+0x200],rbx
 74ca544:	00
 74ca545:	48 89 84 24 f8 01 00 	mov    QWORD PTR [rsp+0x1f8],rax
 74ca54c:	00
 74ca54d:	48 85 ff             	test   rdi,rdi
 74ca550:	75 24                	jne    74ca576 <crosscall2@@Base+0x1f27356>
 74ca552:	48 8b 0d 7f 0b 4e 03 	mov    rcx,QWORD PTR [rip+0x34e0b7f]        # a9ab0d8 <_ZTISt10bad_typeid@@Base+0x5c89d8>
 74ca559:	90                   	nop
 74ca55a:	48 85 c9             	test   rcx,rcx
 74ca55d:	74 09                	je     74ca568 <crosscall2@@Base+0x1f27348>
 74ca55f:	48 8b 15 6a 0b 4e 03 	mov    rdx,QWORD PTR [rip+0x34e0b6a]        # a9ab0d0 <_ZTISt10bad_typeid@@Base+0x5c89d0>
 74ca566:	eb 14                	jmp    74ca57c <crosscall2@@Base+0x1f2735c>
 74ca568:	b9 0b 00 00 00       	mov    ecx,0xb
 74ca56d:	48 8d 15 4b 65 55 fd 	lea    rdx,[rip+0xfffffffffd55654b]        # 4a20abf <_cgo_topofstack@@Base-0xadc721>
 74ca574:	eb 06                	jmp    74ca57c <crosscall2@@Base+0x1f2735c>
 74ca576:	48 89 ca             	mov    rdx,rcx
 74ca579:	48 89 f9             	mov    rcx,rdi
 74ca57c:	48 89 8c 24 c0 00 00 	mov    QWORD PTR [rsp+0xc0],rcx
 74ca583:	00
 74ca584:	48 89 94 24 e0 01 00 	mov    QWORD PTR [rsp+0x1e0],rdx
 74ca58b:	00
 74ca58c:	e8 4f b8 88 fe       	call   5d55de0 <crosscall2@@Base+0x7b2bc0>
 74ca591:	48 85 c0             	test   rax,rax
 74ca594:	74 0c                	je     74ca5a2 <crosscall2@@Base+0x1f27382>
 74ca596:	48 8b 50 18          	mov    rdx,QWORD PTR [rax+0x18]
 74ca59a:	4c 8b 40 20          	mov    r8,QWORD PTR [rax+0x20]
 74ca59e:	66 90                	xchg   ax,ax
 74ca5a0:	eb 05                	jmp    74ca5a7 <crosscall2@@Base+0x1f27387>
 74ca5a2:	45 31 c0             	xor    r8d,r8d
 74ca5a5:	31 d2                	xor    edx,edx
 74ca5a7:	4c 89 44 24 50       	mov    QWORD PTR [rsp+0x50],r8
 74ca5ac:	48 89 94 24 d8 00 00 	mov    QWORD PTR [rsp+0xd8],rdx
 74ca5b3:	00
 74ca5b4:	48 8b 15 2d 0b 4e 03 	mov    rdx,QWORD PTR [rip+0x34e0b2d]        # a9ab0e8 <_ZTISt10bad_typeid@@Base+0x5c89e8>
 74ca5bb:	48 89 54 24 48       	mov    QWORD PTR [rsp+0x48],rdx
 74ca5c0:	48 8b 15 19 0b 4e 03 	mov    rdx,QWORD PTR [rip+0x34e0b19]        # a9ab0e0 <_ZTISt10bad_typeid@@Base+0x5c89e0>
 74ca5c7:	48 89 94 24 d0 00 00 	mov    QWORD PTR [rsp+0xd0],rdx
 74ca5ce:	00
 74ca5cf:	48 8d 15 62 67 48 02 	lea    rdx,[rip+0x2486762]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74ca5d6:	48 89 94 24 a8 01 00 	mov    QWORD PTR [rsp+0x1a8],rdx
 74ca5dd:	00
 74ca5de:	48 8d 15 eb e6 81 01 	lea    rdx,[rip+0x181e6eb]        # 8ce8cd0 <__lxstat@plt+0x202850>
 74ca5e5:	48 89 94 24 b0 01 00 	mov    QWORD PTR [rsp+0x1b0],rdx
 74ca5ec:	00
 74ca5ed:	48 8d 05 29 d4 53 fd 	lea    rax,[rip+0xfffffffffd53d429]        # 4a07a1d <_cgo_topofstack@@Base-0xaf57c3>
 74ca5f4:	bb 0a 00 00 00       	mov    ebx,0xa
 74ca5f9:	48 8d 8c 24 a8 01 00 	lea    rcx,[rsp+0x1a8]
 74ca600:	00
 74ca601:	bf 01 00 00 00       	mov    edi,0x1
 74ca606:	89 fe                	mov    esi,edi
 74ca608:	e8 33 09 0c fe       	call   558af40 <_cgo_topofstack@@Base+0x8dd60>
 74ca60d:	48 89 84 24 d8 01 00 	mov    QWORD PTR [rsp+0x1d8],rax
 74ca614:	00
 74ca615:	48 89 9c 24 b8 00 00 	mov    QWORD PTR [rsp+0xb8],rbx
 74ca61c:	00
 74ca61d:	48 8d 15 14 67 48 02 	lea    rdx,[rip+0x2486714]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74ca624:	48 89 94 24 98 01 00 	mov    QWORD PTR [rsp+0x198],rdx
 74ca62b:	00
 74ca62c:	48 8d 15 ad e6 81 01 	lea    rdx,[rip+0x181e6ad]        # 8ce8ce0 <__lxstat@plt+0x202860>
 74ca633:	48 89 94 24 a0 01 00 	mov    QWORD PTR [rsp+0x1a0],rdx
 74ca63a:	00
 74ca63b:	48 8d 05 bb 85 4f fd 	lea    rax,[rip+0xfffffffffd4f85bb]        # 49c2bfd <_cgo_topofstack@@Base-0xb3a5e3>
 74ca642:	bb 07 00 00 00       	mov    ebx,0x7
 74ca647:	48 8d 8c 24 98 01 00 	lea    rcx,[rsp+0x198]
 74ca64e:	00
 74ca64f:	bf 01 00 00 00       	mov    edi,0x1
 74ca654:	89 fe                	mov    esi,edi
 74ca656:	e8 e5 08 0c fe       	call   558af40 <_cgo_topofstack@@Base+0x8dd60>
 74ca65b:	48 89 84 24 d0 01 00 	mov    QWORD PTR [rsp+0x1d0],rax
 74ca662:	00
 74ca663:	48 89 9c 24 b0 00 00 	mov    QWORD PTR [rsp+0xb0],rbx
 74ca66a:	00
 74ca66b:	31 c0                	xor    eax,eax
 74ca66d:	bb 03 00 00 00       	mov    ebx,0x3
 74ca672:	31 c9                	xor    ecx,ecx
 74ca674:	89 df                	mov    edi,ebx
 74ca676:	48 8d 35 bb 66 48 02 	lea    rsi,[rip+0x24866bb]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74ca67d:	0f 1f 00             	nop    DWORD PTR [rax]
 74ca680:	e8 3b d4 02 fe       	call   54f7ac0 <_cgo_topofstack@@Base-0x5720>
 74ca685:	48 c7 40 08 0c 00 00 	mov    QWORD PTR [rax+0x8],0xc
 74ca68c:	00
 74ca68d:	83 3d dc 1f 7e 05 00 	cmp    DWORD PTR [rip+0x57e1fdc],0x0        # ccac670 <_ZTISt10bad_typeid@@Base+0x28c9f70>
 74ca694:	75 12                	jne    74ca6a8 <crosscall2@@Base+0x1f27488>
 74ca696:	48 8b 94 24 d8 01 00 	mov    rdx,QWORD PTR [rsp+0x1d8]
 74ca69d:	00
 74ca69e:	4c 8b 84 24 d0 01 00 	mov    r8,QWORD PTR [rsp+0x1d0]
 74ca6a5:	00
 74ca6a6:	eb 33                	jmp    74ca6db <crosscall2@@Base+0x1f274bb>
 74ca6a8:	48 8b 10             	mov    rdx,QWORD PTR [rax]
 74ca6ab:	4c 8b 40 10          	mov    r8,QWORD PTR [rax+0x10]
 74ca6af:	4c 8b 48 20          	mov    r9,QWORD PTR [rax+0x20]
 74ca6b3:	e8 28 2c 03 fe       	call   54fd2e0 <_cgo_topofstack@@Base+0x100>
 74ca6b8:	49 89 13             	mov    QWORD PTR [r11],rdx
 74ca6bb:	48 8b 94 24 d8 01 00 	mov    rdx,QWORD PTR [rsp+0x1d8]
 74ca6c2:	00
 74ca6c3:	49 89 53 08          	mov    QWORD PTR [r11+0x8],rdx
 74ca6c7:	4d 89 43 10          	mov    QWORD PTR [r11+0x10],r8
 74ca6cb:	4c 8b 84 24 d0 01 00 	mov    r8,QWORD PTR [rsp+0x1d0]
 74ca6d2:	00
 74ca6d3:	4d 89 43 18          	mov    QWORD PTR [r11+0x18],r8
 74ca6d7:	4d 89 4b 20          	mov    QWORD PTR [r11+0x20],r9
 74ca6db:	48 89 4c 24 78       	mov    QWORD PTR [rsp+0x78],rcx
 74ca6e0:	48 89 84 24 f8 00 00 	mov    QWORD PTR [rsp+0xf8],rax
 74ca6e7:	00
 74ca6e8:	4c 8d 0d 1b f4 56 fd 	lea    r9,[rip+0xfffffffffd56f41b]        # 4a39b0a <_cgo_topofstack@@Base-0xac36d6>
 74ca6ef:	4c 89 08             	mov    QWORD PTR [rax],r9
 74ca6f2:	4c 8b 8c 24 b8 00 00 	mov    r9,QWORD PTR [rsp+0xb8]
 74ca6f9:	00
 74ca6fa:	4c 89 48 18          	mov    QWORD PTR [rax+0x18],r9
 74ca6fe:	48 89 50 10          	mov    QWORD PTR [rax+0x10],rdx
 74ca702:	48 8b 94 24 b0 00 00 	mov    rdx,QWORD PTR [rsp+0xb0]
 74ca709:	00
 74ca70a:	48 89 50 28          	mov    QWORD PTR [rax+0x28],rdx
 74ca70e:	4c 89 40 20          	mov    QWORD PTR [rax+0x20],r8
 74ca712:	48 8b 15 e7 0a 7e 05 	mov    rdx,QWORD PTR [rip+0x57e0ae7]        # ccab200 <_ZTISt10bad_typeid@@Base+0x28c8b00>
 74ca719:	48 85 d2             	test   rdx,rdx
 74ca71c:	75 24                	jne    74ca742 <crosscall2@@Base+0x1f27522>
 74ca71e:	48 8b 05 3b e1 3e 03 	mov    rax,QWORD PTR [rip+0x33ee13b]        # a8b8860 <_ZTISt10bad_typeid@@Base+0x4d6160>
 74ca725:	48 8b 1d 3c e1 3e 03 	mov    rbx,QWORD PTR [rip+0x33ee13c]        # a8b8868 <_ZTISt10bad_typeid@@Base+0x4d6168>
 74ca72c:	90                   	nop
 74ca72d:	e8 6e c2 03 fe       	call   55069a0 <_cgo_topofstack@@Base+0x97c0>
 74ca732:	48 8b 4c 24 78       	mov    rcx,QWORD PTR [rsp+0x78]
 74ca737:	48 89 c2             	mov    rdx,rax
 74ca73a:	48 8b 84 24 f8 00 00 	mov    rax,QWORD PTR [rsp+0xf8]
 74ca741:	00
 74ca742:	48 85 d2             	test   rdx,rdx
 74ca745:	7f 0a                	jg     74ca751 <crosscall2@@Base+0x1f27531>
 74ca747:	ba 03 00 00 00       	mov    edx,0x3
 74ca74c:	e9 ca 00 00 00       	jmp    74ca81b <crosscall2@@Base+0x1f275fb>
 74ca751:	44 0f 11 bc 24 98 01 	movups XMMWORD PTR [rsp+0x198],xmm15
 74ca758:	00 00
 74ca75a:	48 89 d0             	mov    rax,rdx
 74ca75d:	0f 1f 00             	nop    DWORD PTR [rax]
 74ca760:	e8 5b 7c 02 fe       	call   54f23c0 <_cgo_topofstack@@Base-0xae20>
 74ca765:	48 8d 0d 0c 68 48 02 	lea    rcx,[rip+0x248680c]        # 9950f78 <__lxstat@plt+0xe6aaf8>
 74ca76c:	48 89 8c 24 98 01 00 	mov    QWORD PTR [rsp+0x198],rcx
 74ca773:	00
 74ca774:	48 89 84 24 a0 01 00 	mov    QWORD PTR [rsp+0x1a0],rax
 74ca77b:	00
 74ca77c:	48 8d 05 22 a3 4d fd 	lea    rax,[rip+0xfffffffffd4da322]        # 49a4aa5 <_cgo_topofstack@@Base-0xb5873b>
 74ca783:	bb 05 00 00 00       	mov    ebx,0x5
 74ca788:	48 8d 8c 24 98 01 00 	lea    rcx,[rsp+0x198]
 74ca78f:	00
 74ca790:	bf 01 00 00 00       	mov    edi,0x1
 74ca795:	89 fe                	mov    esi,edi
 74ca797:	e8 a4 07 0c fe       	call   558af40 <_cgo_topofstack@@Base+0x8dd60>
 74ca79c:	48 8b 4c 24 78       	mov    rcx,QWORD PTR [rsp+0x78]
 74ca7a1:	48 83 f9 04          	cmp    rcx,0x4
 74ca7a5:	72 0a                	jb     74ca7b1 <crosscall2@@Base+0x1f27591>
 74ca7a7:	48 8b 94 24 f8 00 00 	mov    rdx,QWORD PTR [rsp+0xf8]
 74ca7ae:	00
 74ca7af:	eb 41                	jmp    74ca7f2 <crosscall2@@Base+0x1f275d2>
 74ca7b1:	48 89 9c 24 b8 00 00 	mov    QWORD PTR [rsp+0xb8],rbx
 74ca7b8:	00
 74ca7b9:	48 89 84 24 d8 01 00 	mov    QWORD PTR [rsp+0x1d8],rax
 74ca7c0:	00
 74ca7c1:	48 8b 84 24 f8 00 00 	mov    rax,QWORD PTR [rsp+0xf8]
 74ca7c8:	00
 74ca7c9:	bb 04 00 00 00       	mov    ebx,0x4
 74ca7ce:	bf 01 00 00 00       	mov    edi,0x1
 74ca7d3:	48 8d 35 5e 65 48 02 	lea    rsi,[rip+0x248655e]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74ca7da:	e8 e1 d2 02 fe       	call   54f7ac0 <_cgo_topofstack@@Base-0x5720>
 74ca7df:	48 8b 9c 24 b8 00 00 	mov    rbx,QWORD PTR [rsp+0xb8]
 74ca7e6:	00
 74ca7e7:	48 89 c2             	mov    rdx,rax
 74ca7ea:	48 8b 84 24 d8 01 00 	mov    rax,QWORD PTR [rsp+0x1d8]
 74ca7f1:	00
 74ca7f2:	48 89 5a 38          	mov    QWORD PTR [rdx+0x38],rbx
 74ca7f6:	83 3d 73 1e 7e 05 00 	cmp    DWORD PTR [rip+0x57e1e73],0x0        # ccac670 <_ZTISt10bad_typeid@@Base+0x28c9f70>
 74ca7fd:	74 10                	je     74ca80f <crosscall2@@Base+0x1f275ef>
 74ca7ff:	4c 8b 42 30          	mov    r8,QWORD PTR [rdx+0x30]
 74ca803:	e8 78 2a 03 fe       	call   54fd280 <_cgo_topofstack@@Base+0xa0>
 74ca808:	49 89 03             	mov    QWORD PTR [r11],rax
 74ca80b:	4d 89 43 08          	mov    QWORD PTR [r11+0x8],r8
 74ca80f:	48 89 42 30          	mov    QWORD PTR [rdx+0x30],rax
 74ca813:	48 89 d0             	mov    rax,rdx
 74ca816:	ba 04 00 00 00       	mov    edx,0x4
 74ca81b:	48 8b 9c 24 20 02 00 	mov    rbx,QWORD PTR [rsp+0x220]
 74ca822:	00
 74ca823:	48 85 db             	test   rbx,rbx
 74ca826:	0f 84 01 01 00 00    	je     74ca92d <crosscall2@@Base+0x1f2770d>
 74ca82c:	48 89 8c 24 a8 00 00 	mov    QWORD PTR [rsp+0xa8],rcx
 74ca833:	00
 74ca834:	48 89 84 24 c8 01 00 	mov    QWORD PTR [rsp+0x1c8],rax
 74ca83b:	00
 74ca83c:	48 89 54 24 70       	mov    QWORD PTR [rsp+0x70],rdx
 74ca841:	44 0f 11 bc 24 98 01 	movups XMMWORD PTR [rsp+0x198],xmm15
 74ca848:	00 00
 74ca84a:	48 8b 84 24 18 02 00 	mov    rax,QWORD PTR [rsp+0x218]
 74ca851:	00
 74ca852:	e8 c9 7b 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74ca857:	48 8d 0d da 64 48 02 	lea    rcx,[rip+0x24864da]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74ca85e:	48 89 8c 24 98 01 00 	mov    QWORD PTR [rsp+0x198],rcx
 74ca865:	00
 74ca866:	48 89 84 24 a0 01 00 	mov    QWORD PTR [rsp+0x1a0],rax
 74ca86d:	00
 74ca86e:	48 8d 05 04 ca 59 fd 	lea    rax,[rip+0xfffffffffd59ca04]        # 4a67279 <_cgo_topofstack@@Base-0xa95f67>
 74ca875:	bb 0e 00 00 00       	mov    ebx,0xe
 74ca87a:	48 8d 8c 24 98 01 00 	lea    rcx,[rsp+0x198]
 74ca881:	00
 74ca882:	bf 01 00 00 00       	mov    edi,0x1
 74ca887:	89 fe                	mov    esi,edi
 74ca889:	e8 b2 06 0c fe       	call   558af40 <_cgo_topofstack@@Base+0x8dd60>
 74ca88e:	48 8b 4c 24 70       	mov    rcx,QWORD PTR [rsp+0x70]
 74ca893:	48 ff c1             	inc    rcx
 74ca896:	48 8b 94 24 a8 00 00 	mov    rdx,QWORD PTR [rsp+0xa8]
 74ca89d:	00
 74ca89e:	66 90                	xchg   ax,ax
 74ca8a0:	48 39 ca             	cmp    rdx,rcx
 74ca8a3:	72 0a                	jb     74ca8af <crosscall2@@Base+0x1f2768f>
 74ca8a5:	48 8b b4 24 c8 01 00 	mov    rsi,QWORD PTR [rsp+0x1c8]
 74ca8ac:	00
 74ca8ad:	eb 48                	jmp    74ca8f7 <crosscall2@@Base+0x1f276d7>
 74ca8af:	48 89 9c 24 b8 00 00 	mov    QWORD PTR [rsp+0xb8],rbx
 74ca8b6:	00
 74ca8b7:	48 89 84 24 d8 01 00 	mov    QWORD PTR [rsp+0x1d8],rax
 74ca8be:	00
 74ca8bf:	48 8b 84 24 c8 01 00 	mov    rax,QWORD PTR [rsp+0x1c8]
 74ca8c6:	00
 74ca8c7:	48 89 cb             	mov    rbx,rcx
 74ca8ca:	48 89 d1             	mov    rcx,rdx
 74ca8cd:	bf 01 00 00 00       	mov    edi,0x1
 74ca8d2:	48 8d 35 5f 64 48 02 	lea    rsi,[rip+0x248645f]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74ca8d9:	e8 e2 d1 02 fe       	call   54f7ac0 <_cgo_topofstack@@Base-0x5720>
 74ca8de:	48 89 c6             	mov    rsi,rax
 74ca8e1:	48 89 ca             	mov    rdx,rcx
 74ca8e4:	48 8b 84 24 d8 01 00 	mov    rax,QWORD PTR [rsp+0x1d8]
 74ca8eb:	00
 74ca8ec:	48 89 d9             	mov    rcx,rbx
 74ca8ef:	48 8b 9c 24 b8 00 00 	mov    rbx,QWORD PTR [rsp+0xb8]
 74ca8f6:	00
 74ca8f7:	4c 8d 41 ff          	lea    r8,[rcx-0x1]
 74ca8fb:	49 c1 e0 04          	shl    r8,0x4
 74ca8ff:	4a 89 5c 06 08       	mov    QWORD PTR [rsi+r8*1+0x8],rbx
 74ca904:	83 3d 65 1d 7e 05 00 	cmp    DWORD PTR [rip+0x57e1d65],0x0        # ccac670 <_ZTISt10bad_typeid@@Base+0x28c9f70>
 74ca90b:	74 10                	je     74ca91d <crosscall2@@Base+0x1f276fd>
 74ca90d:	4e 8b 0c 06          	mov    r9,QWORD PTR [rsi+r8*1]
 74ca911:	e8 6a 29 03 fe       	call   54fd280 <_cgo_topofstack@@Base+0xa0>
 74ca916:	49 89 03             	mov    QWORD PTR [r11],rax
 74ca919:	4d 89 4b 08          	mov    QWORD PTR [r11+0x8],r9
 74ca91d:	4a 89 04 06          	mov    QWORD PTR [rsi+r8*1],rax
 74ca921:	48 89 f0             	mov    rax,rsi
 74ca924:	48 89 cb             	mov    rbx,rcx
 74ca927:	48 89 d1             	mov    rcx,rdx
 74ca92a:	48 89 da             	mov    rdx,rbx
 74ca92d:	44 0f b6 84 24 28 02 	movzx  r8d,BYTE PTR [rsp+0x228]
 74ca934:	00 00
 74ca936:	45 84 c0             	test   r8b,r8b
 74ca939:	74 4e                	je     74ca989 <crosscall2@@Base+0x1f27769>
 74ca93b:	48 8d 5a 01          	lea    rbx,[rdx+0x1]
 74ca93f:	90                   	nop
 74ca940:	48 39 d9             	cmp    rcx,rbx
 74ca943:	73 11                	jae    74ca956 <crosscall2@@Base+0x1f27736>
 74ca945:	bf 01 00 00 00       	mov    edi,0x1
 74ca94a:	48 8d 35 e7 63 48 02 	lea    rsi,[rip+0x24863e7]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74ca951:	e8 6a d1 02 fe       	call   54f7ac0 <_cgo_topofstack@@Base-0x5720>
 74ca956:	48 8d 53 ff          	lea    rdx,[rbx-0x1]
 74ca95a:	48 c1 e2 04          	shl    rdx,0x4
 74ca95e:	48 c7 44 10 08 1b 00 	mov    QWORD PTR [rax+rdx*1+0x8],0x1b
 74ca965:	00 00
 74ca967:	83 3d 02 1d 7e 05 00 	cmp    DWORD PTR [rip+0x57e1d02],0x0        # ccac670 <_ZTISt10bad_typeid@@Base+0x28c9f70>
 74ca96e:	74 0c                	je     74ca97c <crosscall2@@Base+0x1f2775c>
 74ca970:	4c 8b 04 10          	mov    r8,QWORD PTR [rax+rdx*1]
 74ca974:	e8 e7 28 03 fe       	call   54fd260 <_cgo_topofstack@@Base+0x80>
 74ca979:	4d 89 03             	mov    QWORD PTR [r11],r8
 74ca97c:	4c 8d 05 83 02 67 fd 	lea    r8,[rip+0xfffffffffd670283]        # 4b3ac06 <_cgo_topofstack@@Base-0x9c25da>
 74ca983:	4c 89 04 10          	mov    QWORD PTR [rax+rdx*1],r8
 74ca987:	eb 03                	jmp    74ca98c <crosscall2@@Base+0x1f2776c>
 74ca989:	48 89 d3             	mov    rbx,rdx
 74ca98c:	48 89 84 24 f8 00 00 	mov    QWORD PTR [rsp+0xf8],rax
 74ca993:	00
 74ca994:	48 89 4c 24 78       	mov    QWORD PTR [rsp+0x78],rcx
 74ca999:	48 89 5c 24 70       	mov    QWORD PTR [rsp+0x70],rbx
 74ca99e:	48 8b 15 4b 07 4e 03 	mov    rdx,QWORD PTR [rip+0x34e074b]        # a9ab0f0 <_ZTISt10bad_typeid@@Base+0x5c89f0>
 74ca9a5:	4c 8b 05 4c 07 4e 03 	mov    r8,QWORD PTR [rip+0x34e074c]        # a9ab0f8 <_ZTISt10bad_typeid@@Base+0x5c89f8>
 74ca9ac:	4c 8b 8c 24 f8 01 00 	mov    r9,QWORD PTR [rsp+0x1f8]
 74ca9b3:	00
 74ca9b4:	4d 85 c9             	test   r9,r9
 74ca9b7:	75 0c                	jne    74ca9c5 <crosscall2@@Base+0x1f277a5>
 74ca9b9:	31 f6                	xor    esi,esi
 74ca9bb:	31 ff                	xor    edi,edi
 74ca9bd:	0f 1f 00             	nop    DWORD PTR [rax]
 74ca9c0:	e9 66 01 00 00       	jmp    74cab2b <crosscall2@@Base+0x1f2790b>
 74ca9c5:	4c 89 44 24 40       	mov    QWORD PTR [rsp+0x40],r8
 74ca9ca:	48 89 94 24 c8 00 00 	mov    QWORD PTR [rsp+0xc8],rdx
 74ca9d1:	00
 74ca9d2:	49 8b 51 30          	mov    rdx,QWORD PTR [r9+0x30]
 74ca9d6:	48 8b 84 24 00 02 00 	mov    rax,QWORD PTR [rsp+0x200]
 74ca9dd:	00
 74ca9de:	48 8d 1d 53 ed 57 02 	lea    rbx,[rip+0x257ed53]        # 9a49738 <__lxstat@plt+0xf632b8>
 74ca9e5:	48 8d 0d f4 ee 7d 05 	lea    rcx,[rip+0x57deef4]        # cca98e0 <_ZTISt10bad_typeid@@Base+0x28c71e0>
 74ca9ec:	ff d2                	call   rdx
 74ca9ee:	48 8d 15 83 4f 89 02 	lea    rdx,[rip+0x2894f83]        # 9d5f978 <__lxstat@plt+0x12794f8>
 74ca9f5:	48 39 d0             	cmp    rax,rdx
 74ca9f8:	75 11                	jne    74caa0b <crosscall2@@Base+0x1f277eb>
 74ca9fa:	48 8b 03             	mov    rax,QWORD PTR [rbx]
 74ca9fd:	48 8b 53 08          	mov    rdx,QWORD PTR [rbx+0x8]
 74caa01:	48 8b 73 10          	mov    rsi,QWORD PTR [rbx+0x10]
 74caa05:	48 8b 7b 18          	mov    rdi,QWORD PTR [rbx+0x18]
 74caa09:	eb 15                	jmp    74caa20 <crosscall2@@Base+0x1f27800>
 74caa0b:	bf 00 00 00 00       	mov    edi,0x0
 74caa10:	ba 00 00 00 00       	mov    edx,0x0
 74caa15:	be 00 00 00 00       	mov    esi,0x0
 74caa1a:	b8 00 00 00 00       	mov    eax,0x0
 74caa1f:	90                   	nop
 74caa20:	74 12                	je     74caa34 <crosscall2@@Base+0x1f27814>
 74caa22:	48 8b 5c 24 40       	mov    rbx,QWORD PTR [rsp+0x40]
 74caa27:	48 8b 84 24 c8 00 00 	mov    rax,QWORD PTR [rsp+0xc8]
 74caa2e:	00
 74caa2f:	e9 88 00 00 00       	jmp    74caabc <crosscall2@@Base+0x1f2789c>
 74caa34:	48 89 7c 24 60       	mov    QWORD PTR [rsp+0x60],rdi
 74caa39:	48 89 b4 24 e8 00 00 	mov    QWORD PTR [rsp+0xe8],rsi
 74caa40:	00
 74caa41:	90                   	nop
 74caa42:	48 8d 8c 24 78 01 00 	lea    rcx,[rsp+0x178]
 74caa49:	00
 74caa4a:	44 0f 11 39          	movups XMMWORD PTR [rcx],xmm15
 74caa4e:	44 0f 11 79 10       	movups XMMWORD PTR [rcx+0x10],xmm15
 74caa53:	48 89 d3             	mov    rbx,rdx
 74caa56:	e8 c5 79 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74caa5b:	48 8d 0d d6 62 48 02 	lea    rcx,[rip+0x24862d6]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74caa62:	48 89 8c 24 78 01 00 	mov    QWORD PTR [rsp+0x178],rcx
 74caa69:	00
 74caa6a:	48 89 84 24 80 01 00 	mov    QWORD PTR [rsp+0x180],rax
 74caa71:	00
 74caa72:	48 8b 84 24 e8 00 00 	mov    rax,QWORD PTR [rsp+0xe8]
 74caa79:	00
 74caa7a:	48 8b 5c 24 60       	mov    rbx,QWORD PTR [rsp+0x60]
 74caa7f:	90                   	nop
 74caa80:	e8 9b 79 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74caa85:	48 8d 0d ac 62 48 02 	lea    rcx,[rip+0x24862ac]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74caa8c:	48 89 8c 24 88 01 00 	mov    QWORD PTR [rsp+0x188],rcx
 74caa93:	00
 74caa94:	48 89 84 24 90 01 00 	mov    QWORD PTR [rsp+0x190],rax
 74caa9b:	00
 74caa9c:	48 8d 05 77 9e 4d fd 	lea    rax,[rip+0xfffffffffd4d9e77]        # 49a491a <_cgo_topofstack@@Base-0xb588c6>
 74caaa3:	bb 05 00 00 00       	mov    ebx,0x5
 74caaa8:	48 8d 8c 24 78 01 00 	lea    rcx,[rsp+0x178]
 74caaaf:	00
 74caab0:	bf 02 00 00 00       	mov    edi,0x2
 74caab5:	89 fe                	mov    esi,edi
 74caab7:	e8 84 04 0c fe       	call   558af40 <_cgo_topofstack@@Base+0x8dd60>
 74caabc:	48 89 84 24 f0 00 00 	mov    QWORD PTR [rsp+0xf0],rax
 74caac3:	00
 74caac4:	48 89 5c 24 68       	mov    QWORD PTR [rsp+0x68],rbx
 74caac9:	48 8b 94 24 f8 01 00 	mov    rdx,QWORD PTR [rsp+0x1f8]
 74caad0:	00
 74caad1:	48 8b 52 30          	mov    rdx,QWORD PTR [rdx+0x30]
 74caad5:	48 8b 84 24 00 02 00 	mov    rax,QWORD PTR [rsp+0x200]
 74caadc:	00
 74caadd:	48 8d 1d d4 ab 57 02 	lea    rbx,[rip+0x257abd4]        # 9a456b8 <__lxstat@plt+0xf5f238>
 74caae4:	48 8d 0d f5 ed 7d 05 	lea    rcx,[rip+0x57dedf5]        # cca98e0 <_ZTISt10bad_typeid@@Base+0x28c71e0>
 74caaeb:	ff d2                	call   rdx
 74caaed:	48 8d 15 44 62 48 02 	lea    rdx,[rip+0x2486244]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74caaf4:	48 39 d0             	cmp    rax,rdx
 74caaf7:	75 09                	jne    74cab02 <crosscall2@@Base+0x1f278e2>
 74caaf9:	48 8b 03             	mov    rax,QWORD PTR [rbx]
 74caafc:	48 8b 5b 08          	mov    rbx,QWORD PTR [rbx+0x8]
 74cab00:	eb 04                	jmp    74cab06 <crosscall2@@Base+0x1f278e6>
 74cab02:	31 db                	xor    ebx,ebx
 74cab04:	31 c0                	xor    eax,eax
 74cab06:	48 8b 4c 24 78       	mov    rcx,QWORD PTR [rsp+0x78]
 74cab0b:	4c 8b 44 24 68       	mov    r8,QWORD PTR [rsp+0x68]
 74cab10:	48 89 de             	mov    rsi,rbx
 74cab13:	48 8b 94 24 f0 00 00 	mov    rdx,QWORD PTR [rsp+0xf0]
 74cab1a:	00
 74cab1b:	48 89 c7             	mov    rdi,rax
 74cab1e:	48 8b 84 24 f8 00 00 	mov    rax,QWORD PTR [rsp+0xf8]
 74cab25:	00
 74cab26:	48 8b 5c 24 70       	mov    rbx,QWORD PTR [rsp+0x70]
 74cab2b:	48 89 bc 24 e0 00 00 	mov    QWORD PTR [rsp+0xe0],rdi
 74cab32:	00
 74cab33:	48 89 74 24 58       	mov    QWORD PTR [rsp+0x58],rsi
 74cab38:	0f 1f 84 00 00 00 00 	nop    DWORD PTR [rax+rax*1+0x0]
 74cab3f:	00
 74cab40:	4d 85 c0             	test   r8,r8
 74cab43:	0f 84 ef 00 00 00    	je     74cac38 <crosscall2@@Base+0x1f27a18>
 74cab49:	44 0f 11 bc 24 98 01 	movups XMMWORD PTR [rsp+0x198],xmm15
 74cab50:	00 00
 74cab52:	48 89 d0             	mov    rax,rdx
 74cab55:	4c 89 c3             	mov    rbx,r8
 74cab58:	e8 c3 78 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74cab5d:	48 8d 0d d4 61 48 02 	lea    rcx,[rip+0x24861d4]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74cab64:	48 89 8c 24 98 01 00 	mov    QWORD PTR [rsp+0x198],rcx
 74cab6b:	00
 74cab6c:	48 89 84 24 a0 01 00 	mov    QWORD PTR [rsp+0x1a0],rax
 74cab73:	00
 74cab74:	48 8d 05 9b ef 56 fd 	lea    rax,[rip+0xfffffffffd56ef9b]        # 4a39b16 <_cgo_topofstack@@Base-0xac36ca>
 74cab7b:	bb 0c 00 00 00       	mov    ebx,0xc
 74cab80:	48 8d 8c 24 98 01 00 	lea    rcx,[rsp+0x198]
 74cab87:	00
 74cab88:	bf 01 00 00 00       	mov    edi,0x1
 74cab8d:	89 fe                	mov    esi,edi
 74cab8f:	e8 ac 03 0c fe       	call   558af40 <_cgo_topofstack@@Base+0x8dd60>
 74cab94:	48 8b 4c 24 70       	mov    rcx,QWORD PTR [rsp+0x70]
 74cab99:	48 ff c1             	inc    rcx
 74cab9c:	48 8b 54 24 78       	mov    rdx,QWORD PTR [rsp+0x78]
 74caba1:	48 39 ca             	cmp    rdx,rcx
 74caba4:	72 0a                	jb     74cabb0 <crosscall2@@Base+0x1f27990>
 74caba6:	48 8b b4 24 f8 00 00 	mov    rsi,QWORD PTR [rsp+0xf8]
 74cabad:	00
 74cabae:	eb 48                	jmp    74cabf8 <crosscall2@@Base+0x1f279d8>
 74cabb0:	48 89 9c 24 b8 00 00 	mov    QWORD PTR [rsp+0xb8],rbx
 74cabb7:	00
 74cabb8:	48 89 84 24 d8 01 00 	mov    QWORD PTR [rsp+0x1d8],rax
 74cabbf:	00
 74cabc0:	48 8b 84 24 f8 00 00 	mov    rax,QWORD PTR [rsp+0xf8]
 74cabc7:	00
 74cabc8:	48 89 cb             	mov    rbx,rcx
 74cabcb:	48 89 d1             	mov    rcx,rdx
 74cabce:	bf 01 00 00 00       	mov    edi,0x1
 74cabd3:	48 8d 35 5e 61 48 02 	lea    rsi,[rip+0x248615e]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74cabda:	e8 e1 ce 02 fe       	call   54f7ac0 <_cgo_topofstack@@Base-0x5720>
 74cabdf:	48 89 c6             	mov    rsi,rax
 74cabe2:	48 89 ca             	mov    rdx,rcx
 74cabe5:	48 8b 84 24 d8 01 00 	mov    rax,QWORD PTR [rsp+0x1d8]
 74cabec:	00
 74cabed:	48 89 d9             	mov    rcx,rbx
 74cabf0:	48 8b 9c 24 b8 00 00 	mov    rbx,QWORD PTR [rsp+0xb8]
 74cabf7:	00
 74cabf8:	4c 8d 41 ff          	lea    r8,[rcx-0x1]
 74cabfc:	49 c1 e0 04          	shl    r8,0x4
 74cac00:	4a 89 5c 06 08       	mov    QWORD PTR [rsi+r8*1+0x8],rbx
 74cac05:	83 3d 64 1a 7e 05 00 	cmp    DWORD PTR [rip+0x57e1a64],0x0        # ccac670 <_ZTISt10bad_typeid@@Base+0x28c9f70>
 74cac0c:	74 10                	je     74cac1e <crosscall2@@Base+0x1f279fe>
 74cac0e:	4e 8b 0c 06          	mov    r9,QWORD PTR [rsi+r8*1]
 74cac12:	e8 69 26 03 fe       	call   54fd280 <_cgo_topofstack@@Base+0xa0>
 74cac17:	49 89 03             	mov    QWORD PTR [r11],rax
 74cac1a:	4d 89 4b 08          	mov    QWORD PTR [r11+0x8],r9
 74cac1e:	4a 89 04 06          	mov    QWORD PTR [rsi+r8*1],rax
 74cac22:	48 8b bc 24 e0 00 00 	mov    rdi,QWORD PTR [rsp+0xe0]
 74cac29:	00
 74cac2a:	48 89 f0             	mov    rax,rsi
 74cac2d:	48 89 cb             	mov    rbx,rcx
 74cac30:	48 8b 74 24 58       	mov    rsi,QWORD PTR [rsp+0x58]
 74cac35:	48 89 d1             	mov    rcx,rdx
 74cac38:	48 8b 15 c9 04 4e 03 	mov    rdx,QWORD PTR [rip+0x34e04c9]        # a9ab108 <_ZTISt10bad_typeid@@Base+0x5c8a08>
 74cac3f:	90                   	nop
 74cac40:	48 85 d2             	test   rdx,rdx
 74cac43:	0f 84 17 01 00 00    	je     74cad60 <crosscall2@@Base+0x1f27b40>
 74cac49:	48 89 8c 24 a0 00 00 	mov    QWORD PTR [rsp+0xa0],rcx
 74cac50:	00
 74cac51:	48 89 9c 24 98 00 00 	mov    QWORD PTR [rsp+0x98],rbx
 74cac58:	00
 74cac59:	48 89 84 24 c0 01 00 	mov    QWORD PTR [rsp+0x1c0],rax
 74cac60:	00
 74cac61:	48 8b 05 98 04 4e 03 	mov    rax,QWORD PTR [rip+0x34e0498]        # a9ab100 <_ZTISt10bad_typeid@@Base+0x5c8a00>
 74cac68:	44 0f 11 bc 24 98 01 	movups XMMWORD PTR [rsp+0x198],xmm15
 74cac6f:	00 00
 74cac71:	48 89 d3             	mov    rbx,rdx
 74cac74:	e8 a7 77 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74cac79:	48 8d 0d b8 60 48 02 	lea    rcx,[rip+0x24860b8]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74cac80:	48 89 8c 24 98 01 00 	mov    QWORD PTR [rsp+0x198],rcx
 74cac87:	00
 74cac88:	48 89 84 24 a0 01 00 	mov    QWORD PTR [rsp+0x1a0],rax
 74cac8f:	00
 74cac90:	48 8d 05 fb 06 5b fd 	lea    rax,[rip+0xfffffffffd5b06fb]        # 4a7b392 <_cgo_topofstack@@Base-0xa81e4e>
 74cac97:	bb 0f 00 00 00       	mov    ebx,0xf
 74cac9c:	48 8d 8c 24 98 01 00 	lea    rcx,[rsp+0x198]
 74caca3:	00
 74caca4:	bf 01 00 00 00       	mov    edi,0x1
 74caca9:	89 fe                	mov    esi,edi
 74cacab:	e8 90 02 0c fe       	call   558af40 <_cgo_topofstack@@Base+0x8dd60>
 74cacb0:	48 8b 8c 24 98 00 00 	mov    rcx,QWORD PTR [rsp+0x98]
 74cacb7:	00
 74cacb8:	48 ff c1             	inc    rcx
 74cacbb:	48 8b 94 24 a0 00 00 	mov    rdx,QWORD PTR [rsp+0xa0]
 74cacc2:	00
 74cacc3:	48 39 ca             	cmp    rdx,rcx
 74cacc6:	72 0a                	jb     74cacd2 <crosscall2@@Base+0x1f27ab2>
 74cacc8:	48 8b b4 24 c0 01 00 	mov    rsi,QWORD PTR [rsp+0x1c0]
 74caccf:	00
 74cacd0:	eb 4c                	jmp    74cad1e <crosscall2@@Base+0x1f27afe>
 74cacd2:	48 89 9c 24 b8 00 00 	mov    QWORD PTR [rsp+0xb8],rbx
 74cacd9:	00
 74cacda:	48 89 84 24 d8 01 00 	mov    QWORD PTR [rsp+0x1d8],rax
 74cace1:	00
 74cace2:	48 8b 84 24 c0 01 00 	mov    rax,QWORD PTR [rsp+0x1c0]
 74cace9:	00
 74cacea:	48 89 cb             	mov    rbx,rcx
 74caced:	48 89 d1             	mov    rcx,rdx
 74cacf0:	bf 01 00 00 00       	mov    edi,0x1
 74cacf5:	48 8d 35 3c 60 48 02 	lea    rsi,[rip+0x248603c]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74cacfc:	0f 1f 40 00          	nop    DWORD PTR [rax+0x0]
 74cad00:	e8 bb cd 02 fe       	call   54f7ac0 <_cgo_topofstack@@Base-0x5720>
 74cad05:	48 89 c6             	mov    rsi,rax
 74cad08:	48 89 ca             	mov    rdx,rcx
 74cad0b:	48 8b 84 24 d8 01 00 	mov    rax,QWORD PTR [rsp+0x1d8]
 74cad12:	00
 74cad13:	48 89 d9             	mov    rcx,rbx
 74cad16:	48 8b 9c 24 b8 00 00 	mov    rbx,QWORD PTR [rsp+0xb8]
 74cad1d:	00
 74cad1e:	4c 8d 41 ff          	lea    r8,[rcx-0x1]
 74cad22:	49 c1 e0 04          	shl    r8,0x4
 74cad26:	4a 89 5c 06 08       	mov    QWORD PTR [rsi+r8*1+0x8],rbx
 74cad2b:	83 3d 3e 19 7e 05 00 	cmp    DWORD PTR [rip+0x57e193e],0x0        # ccac670 <_ZTISt10bad_typeid@@Base+0x28c9f70>
 74cad32:	74 10                	je     74cad44 <crosscall2@@Base+0x1f27b24>
 74cad34:	4e 8b 0c 06          	mov    r9,QWORD PTR [rsi+r8*1]
 74cad38:	e8 43 25 03 fe       	call   54fd280 <_cgo_topofstack@@Base+0xa0>
 74cad3d:	49 89 03             	mov    QWORD PTR [r11],rax
 74cad40:	4d 89 4b 08          	mov    QWORD PTR [r11+0x8],r9
 74cad44:	4a 89 04 06          	mov    QWORD PTR [rsi+r8*1],rax
 74cad48:	48 8b bc 24 e0 00 00 	mov    rdi,QWORD PTR [rsp+0xe0]
 74cad4f:	00
 74cad50:	48 89 f0             	mov    rax,rsi
 74cad53:	48 89 cb             	mov    rbx,rcx
 74cad56:	48 8b 74 24 58       	mov    rsi,QWORD PTR [rsp+0x58]
 74cad5b:	48 89 d1             	mov    rcx,rdx
 74cad5e:	66 90                	xchg   ax,ax
 74cad60:	48 85 f6             	test   rsi,rsi
 74cad63:	0f 84 01 01 00 00    	je     74cae6a <crosscall2@@Base+0x1f27c4a>
 74cad69:	48 89 8c 24 90 00 00 	mov    QWORD PTR [rsp+0x90],rcx
 74cad70:	00
 74cad71:	48 89 9c 24 88 00 00 	mov    QWORD PTR [rsp+0x88],rbx
 74cad78:	00
 74cad79:	48 89 84 24 b8 01 00 	mov    QWORD PTR [rsp+0x1b8],rax
 74cad80:	00
 74cad81:	44 0f 11 bc 24 98 01 	movups XMMWORD PTR [rsp+0x198],xmm15
 74cad88:	00 00
 74cad8a:	48 89 f8             	mov    rax,rdi
 74cad8d:	48 89 f3             	mov    rbx,rsi
 74cad90:	e8 8b 76 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74cad95:	48 8d 0d 9c 5f 48 02 	lea    rcx,[rip+0x2485f9c]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74cad9c:	48 89 8c 24 98 01 00 	mov    QWORD PTR [rsp+0x198],rcx
 74cada3:	00
 74cada4:	48 89 84 24 a0 01 00 	mov    QWORD PTR [rsp+0x1a0],rax
 74cadab:	00
 74cadac:	48 8d 05 74 cc 53 fd 	lea    rax,[rip+0xfffffffffd53cc74]        # 4a07a27 <_cgo_topofstack@@Base-0xaf57b9>
 74cadb3:	bb 0a 00 00 00       	mov    ebx,0xa
 74cadb8:	48 8d 8c 24 98 01 00 	lea    rcx,[rsp+0x198]
 74cadbf:	00
 74cadc0:	bf 01 00 00 00       	mov    edi,0x1
 74cadc5:	89 fe                	mov    esi,edi
 74cadc7:	e8 74 01 0c fe       	call   558af40 <_cgo_topofstack@@Base+0x8dd60>
 74cadcc:	48 8b 8c 24 88 00 00 	mov    rcx,QWORD PTR [rsp+0x88]
 74cadd3:	00
 74cadd4:	48 ff c1             	inc    rcx
 74cadd7:	48 8b 94 24 90 00 00 	mov    rdx,QWORD PTR [rsp+0x90]
 74cadde:	00
 74caddf:	90                   	nop
 74cade0:	48 39 ca             	cmp    rdx,rcx
 74cade3:	72 0a                	jb     74cadef <crosscall2@@Base+0x1f27bcf>
 74cade5:	48 8b b4 24 b8 01 00 	mov    rsi,QWORD PTR [rsp+0x1b8]
 74cadec:	00
 74caded:	eb 48                	jmp    74cae37 <crosscall2@@Base+0x1f27c17>
 74cadef:	48 89 9c 24 b8 00 00 	mov    QWORD PTR [rsp+0xb8],rbx
 74cadf6:	00
 74cadf7:	48 89 84 24 d8 01 00 	mov    QWORD PTR [rsp+0x1d8],rax
 74cadfe:	00
 74cadff:	48 8b 84 24 b8 01 00 	mov    rax,QWORD PTR [rsp+0x1b8]
 74cae06:	00
 74cae07:	48 89 cb             	mov    rbx,rcx
 74cae0a:	48 89 d1             	mov    rcx,rdx
 74cae0d:	bf 01 00 00 00       	mov    edi,0x1
 74cae12:	48 8d 35 1f 5f 48 02 	lea    rsi,[rip+0x2485f1f]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74cae19:	e8 a2 cc 02 fe       	call   54f7ac0 <_cgo_topofstack@@Base-0x5720>
 74cae1e:	48 89 c6             	mov    rsi,rax
 74cae21:	48 89 ca             	mov    rdx,rcx
 74cae24:	48 8b 84 24 d8 01 00 	mov    rax,QWORD PTR [rsp+0x1d8]
 74cae2b:	00
 74cae2c:	48 89 d9             	mov    rcx,rbx
 74cae2f:	48 8b 9c 24 b8 00 00 	mov    rbx,QWORD PTR [rsp+0xb8]
 74cae36:	00
 74cae37:	4c 8d 41 ff          	lea    r8,[rcx-0x1]
 74cae3b:	49 c1 e0 04          	shl    r8,0x4
 74cae3f:	4a 89 5c 06 08       	mov    QWORD PTR [rsi+r8*1+0x8],rbx
 74cae44:	83 3d 25 18 7e 05 00 	cmp    DWORD PTR [rip+0x57e1825],0x0        # ccac670 <_ZTISt10bad_typeid@@Base+0x28c9f70>
 74cae4b:	74 10                	je     74cae5d <crosscall2@@Base+0x1f27c3d>
 74cae4d:	4e 8b 0c 06          	mov    r9,QWORD PTR [rsi+r8*1]
 74cae51:	e8 2a 24 03 fe       	call   54fd280 <_cgo_topofstack@@Base+0xa0>
 74cae56:	49 89 03             	mov    QWORD PTR [r11],rax
 74cae59:	4d 89 4b 08          	mov    QWORD PTR [r11+0x8],r9
 74cae5d:	4a 89 04 06          	mov    QWORD PTR [rsi+r8*1],rax
 74cae61:	48 89 cb             	mov    rbx,rcx
 74cae64:	48 89 f0             	mov    rax,rsi
 74cae67:	48 89 d1             	mov    rcx,rdx
 74cae6a:	48 8d 3d 35 74 4c fd 	lea    rdi,[rip+0xfffffffffd4c7435]        # 49922a6 <_cgo_topofstack@@Base-0xb6af3a>
 74cae71:	be 02 00 00 00       	mov    esi,0x2
 74cae76:	e8 05 0b 0e fe       	call   55ab980 <crosscall2@@Base+0x8760>
 74cae7b:	48 89 84 24 00 01 00 	mov    QWORD PTR [rsp+0x100],rax
 74cae82:	00
 74cae83:	48 89 9c 24 80 00 00 	mov    QWORD PTR [rsp+0x80],rbx
 74cae8a:	00
 74cae8b:	48 8b 54 24 48       	mov    rdx,QWORD PTR [rsp+0x48]
 74cae90:	48 85 d2             	test   rdx,rdx
 74cae93:	0f 84 e8 00 00 00    	je     74caf81 <crosscall2@@Base+0x1f27d61>
 74cae99:	48 8d 8c 24 38 01 00 	lea    rcx,[rsp+0x138]
 74caea0:	00
 74caea1:	44 0f 11 39          	movups XMMWORD PTR [rcx],xmm15
 74caea5:	44 0f 11 79 10       	movups XMMWORD PTR [rcx+0x10],xmm15
 74caeaa:	44 0f 11 79 20       	movups XMMWORD PTR [rcx+0x20],xmm15
 74caeaf:	44 0f 11 79 30       	movups XMMWORD PTR [rcx+0x30],xmm15
 74caeb4:	48 8b 84 24 e0 01 00 	mov    rax,QWORD PTR [rsp+0x1e0]
 74caebb:	00
 74caebc:	48 8b 9c 24 c0 00 00 	mov    rbx,QWORD PTR [rsp+0xc0]
 74caec3:	00
 74caec4:	e8 57 75 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74caec9:	48 8d 0d 68 5e 48 02 	lea    rcx,[rip+0x2485e68]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74caed0:	48 89 8c 24 38 01 00 	mov    QWORD PTR [rsp+0x138],rcx
 74caed7:	00
 74caed8:	48 89 84 24 40 01 00 	mov    QWORD PTR [rsp+0x140],rax
 74caedf:	00
 74caee0:	48 8b 84 24 d0 00 00 	mov    rax,QWORD PTR [rsp+0xd0]
 74caee7:	00
 74caee8:	48 8b 5c 24 48       	mov    rbx,QWORD PTR [rsp+0x48]
 74caeed:	e8 2e 75 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74caef2:	48 8d 0d 3f 5e 48 02 	lea    rcx,[rip+0x2485e3f]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74caef9:	48 89 8c 24 48 01 00 	mov    QWORD PTR [rsp+0x148],rcx
 74caf00:	00
 74caf01:	48 89 84 24 50 01 00 	mov    QWORD PTR [rsp+0x150],rax
 74caf08:	00
 74caf09:	48 8b 84 24 d8 00 00 	mov    rax,QWORD PTR [rsp+0xd8]
 74caf10:	00
 74caf11:	48 8b 5c 24 50       	mov    rbx,QWORD PTR [rsp+0x50]
 74caf16:	e8 05 75 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74caf1b:	48 8d 0d 16 5e 48 02 	lea    rcx,[rip+0x2485e16]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74caf22:	48 89 8c 24 58 01 00 	mov    QWORD PTR [rsp+0x158],rcx
 74caf29:	00
 74caf2a:	48 89 84 24 60 01 00 	mov    QWORD PTR [rsp+0x160],rax
 74caf31:	00
 74caf32:	48 8b 84 24 00 01 00 	mov    rax,QWORD PTR [rsp+0x100]
 74caf39:	00
 74caf3a:	48 8b 9c 24 80 00 00 	mov    rbx,QWORD PTR [rsp+0x80]
 74caf41:	00
 74caf42:	e8 d9 74 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74caf47:	48 8d 0d ea 5d 48 02 	lea    rcx,[rip+0x2485dea]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74caf4e:	48 89 8c 24 68 01 00 	mov    QWORD PTR [rsp+0x168],rcx
 74caf55:	00
 74caf56:	48 89 84 24 70 01 00 	mov    QWORD PTR [rsp+0x170],rax
 74caf5d:	00
 74caf5e:	48 8d 05 6d 61 58 fd 	lea    rax,[rip+0xfffffffffd58616d]        # 4a510d2 <_cgo_topofstack@@Base-0xaac10e>
 74caf65:	bb 0d 00 00 00       	mov    ebx,0xd
 74caf6a:	48 8d 8c 24 38 01 00 	lea    rcx,[rsp+0x138]
 74caf71:	00
 74caf72:	bf 04 00 00 00       	mov    edi,0x4
 74caf77:	89 fe                	mov    esi,edi
 74caf79:	e8 c2 ff 0b fe       	call   558af40 <_cgo_topofstack@@Base+0x8dd60>
 74caf7e:	c9                   	leave
 74caf7f:	90                   	nop
 74caf80:	c3                   	ret
 74caf81:	48 8d 8c 24 08 01 00 	lea    rcx,[rsp+0x108]
 74caf88:	00
 74caf89:	44 0f 11 39          	movups XMMWORD PTR [rcx],xmm15
 74caf8d:	44 0f 11 79 10       	movups XMMWORD PTR [rcx+0x10],xmm15
 74caf92:	44 0f 11 79 20       	movups XMMWORD PTR [rcx+0x20],xmm15
 74caf97:	48 8b 84 24 e0 01 00 	mov    rax,QWORD PTR [rsp+0x1e0]
 74caf9e:	00
 74caf9f:	48 8b 9c 24 c0 00 00 	mov    rbx,QWORD PTR [rsp+0xc0]
 74cafa6:	00
 74cafa7:	e8 74 74 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74cafac:	48 8d 0d 85 5d 48 02 	lea    rcx,[rip+0x2485d85]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74cafb3:	48 89 8c 24 08 01 00 	mov    QWORD PTR [rsp+0x108],rcx
 74cafba:	00
 74cafbb:	48 89 84 24 10 01 00 	mov    QWORD PTR [rsp+0x110],rax
 74cafc2:	00
 74cafc3:	48 8b 84 24 d8 00 00 	mov    rax,QWORD PTR [rsp+0xd8]
 74cafca:	00
 74cafcb:	48 8b 5c 24 50       	mov    rbx,QWORD PTR [rsp+0x50]
 74cafd0:	e8 4b 74 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74cafd5:	48 8d 0d 5c 5d 48 02 	lea    rcx,[rip+0x2485d5c]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74cafdc:	48 89 8c 24 18 01 00 	mov    QWORD PTR [rsp+0x118],rcx
 74cafe3:	00
 74cafe4:	48 89 84 24 20 01 00 	mov    QWORD PTR [rsp+0x120],rax
 74cafeb:	00
 74cafec:	48 8b 84 24 00 01 00 	mov    rax,QWORD PTR [rsp+0x100]
 74caff3:	00
 74caff4:	48 8b 9c 24 80 00 00 	mov    rbx,QWORD PTR [rsp+0x80]
 74caffb:	00
 74caffc:	0f 1f 40 00          	nop    DWORD PTR [rax+0x0]
 74cb000:	e8 1b 74 02 fe       	call   54f2420 <_cgo_topofstack@@Base-0xadc0>
 74cb005:	48 8d 0d 2c 5d 48 02 	lea    rcx,[rip+0x2485d2c]        # 9950d38 <__lxstat@plt+0xe6a8b8>
 74cb00c:	48 89 8c 24 28 01 00 	mov    QWORD PTR [rsp+0x128],rcx
 74cb013:	00
 74cb014:	48 89 84 24 30 01 00 	mov    QWORD PTR [rsp+0x130],rax
 74cb01b:	00
 74cb01c:	48 8d 05 0e ca 53 fd 	lea    rax,[rip+0xfffffffffd53ca0e]        # 4a07a31 <_cgo_topofstack@@Base-0xaf57af>
 74cb023:	bb 0a 00 00 00       	mov    ebx,0xa
 74cb028:	48 8d 8c 24 08 01 00 	lea    rcx,[rsp+0x108]
 74cb02f:	00
 74cb030:	bf 03 00 00 00       	mov    edi,0x3
 74cb035:	89 fe                	mov    esi,edi
 74cb037:	e8 04 ff 0b fe       	call   558af40 <_cgo_topofstack@@Base+0x8dd60>
 74cb03c:	c9                   	leave
 74cb03d:	c3                   	ret
 74cb03e:	48 89 44 24 08       	mov    QWORD PTR [rsp+0x8],rax
 74cb043:	48 89 5c 24 10       	mov    QWORD PTR [rsp+0x10],rbx
 74cb048:	48 89 4c 24 18       	mov    QWORD PTR [rsp+0x18],rcx
 74cb04d:	48 89 7c 24 20       	mov    QWORD PTR [rsp+0x20],rdi
 74cb052:	48 89 74 24 28       	mov    QWORD PTR [rsp+0x28],rsi
 74cb057:	4c 89 44 24 30       	mov    QWORD PTR [rsp+0x30],r8
 74cb05c:	44 88 4c 24 38       	mov    BYTE PTR [rsp+0x38],r9b
 74cb061:	e8 3a 07 03 fe       	call   54fb7a0 <_cgo_topofstack@@Base-0x1a40>
 74cb066:	48 8b 44 24 08       	mov    rax,QWORD PTR [rsp+0x8]
 74cb06b:	48 8b 5c 24 10       	mov    rbx,QWORD PTR [rsp+0x10]
 74cb070:	48 8b 4c 24 18       	mov    rcx,QWORD PTR [rsp+0x18]
 74cb075:	48 8b 7c 24 20       	mov    rdi,QWORD PTR [rsp+0x20]
 74cb07a:	48 8b 74 24 28       	mov    rsi,QWORD PTR [rsp+0x28]
 74cb07f:	4c 8b 44 24 30       	mov    r8,QWORD PTR [rsp+0x30]
 74cb084:	44 0f b6 4c 24 38    	movzx  r9d,BYTE PTR [rsp+0x38]
 74cb08a:	e9 71 f4 ff ff       	jmp    74ca500 <crosscall2@@Base+0x1f272e0>
 74cb08f:	cc                   	int3
 74cb090:	cc                   	int3
 74cb091:	cc                   	int3
 74cb092:	cc                   	int3
 74cb093:	cc                   	int3
 74cb094:	cc                   	int3
 74cb095:	cc                   	int3
 74cb096:	cc                   	int3
 74cb097:	cc                   	int3
 74cb098:	cc                   	int3
 74cb099:	cc                   	int3
 74cb09a:	cc                   	int3
 74cb09b:	cc                   	int3
 74cb09c:	cc                   	int3
 74cb09d:	cc                   	int3
 74cb09e:	cc                   	int3
 74cb09f:	cc                   	int3
